"""
Génère et stocke les embeddings dans chaque brique JSON (champ _embedding).

Modèle : nomic-embed-text via Ollama (768 dimensions), via connector_ollama.
Hash MD5 du texte représentatif → skip si brique inchangée.

Usage CLI :
    python3 embed_bricks.py                     # toutes les briques
    python3 embed_bricks.py compta_copro        # un module
    python3 embed_bricks.py --dry-run           # liste sans écrire

Usage Python :
    from embed_bricks import embed_brick, brick_text, embed_all_bricks
"""

import json
import hashlib
import sys
from pathlib import Path

import connector_ollama

BRICKS_BASE = Path('/home/ubuntu/ledger_api/modules')

_EMBEDDING_FIELDS = {'_embedding', '_embedding_hash'}


def brick_text(b: dict) -> str:
    """Texte canonique d'une brique — tout ce qui compte sémantiquement."""
    parts = []
    for key in ('type', 'nom', 'title', 'id', 'description'):
        v = b.get(key)
        if v and isinstance(v, str):
            parts.append(v)
    for key in ('budget_annuel_eur', 'montant_trimestriel_eur', 'copropriétaires',
                'ecritures_types', 'comptes', 'contenu', 'schedule_2026',
                'contreparties', 'classes', 'keywords'):
        v = b.get(key)
        if v is not None:
            snippet = json.dumps(v, ensure_ascii=False)[:400]
            parts.append(f'{key}: {snippet}')
    return ' | '.join(parts)


def embed_text(text: str) -> list:
    """Appel Ollama /api/embed → vecteur 768d — via connector_ollama."""
    result = connector_ollama.embed({"input": text})
    if result.get("success") and result.get("embedding"):
        return result["embedding"]
    raise RuntimeError(result.get("error", "embedding failed"))


def embed_brick(brick: dict) -> bool:
    """Génère et injecte _embedding + _embedding_hash dans un dict brique.
    Skip si le hash est inchangé. Retourne True si l'embedding a été maj."""
    text = brick_text(brick)
    h = hashlib.md5(text.encode()).hexdigest()
    if brick.get('_embedding_hash') == h:
        return False
    brick['_embedding'] = embed_text(text)
    brick['_embedding_hash'] = h
    return True


def brick_without_embedding(b: dict) -> dict:
    """Retourne une copie du dict sans les champs _embedding (pour Drive/export)."""
    return {k: v for k, v in b.items() if k not in _EMBEDDING_FIELDS}


def embed_all_bricks(module: str = None, dry_run: bool = False) -> dict:
    """
    Parcourt toutes les briques JSON et génère/met à jour _embedding.
    Retourne un rapport {chemin: statut}.
    """
    if module:
        files = sorted(BRICKS_BASE.glob(f'{module}/bricks/*.json')) + \
                sorted(BRICKS_BASE.glob(f'{module}/*.json'))
    else:
        files = sorted(BRICKS_BASE.glob('**/bricks/*.json')) + \
                [f for f in sorted(BRICKS_BASE.glob('**/*.json'))
                 if '/bricks/' not in str(f)]

    if not files:
        return {'error': 'Aucun fichier trouvé'}

    results = {}
    for f in files:
        try:
            b = json.loads(f.read_text())
            if dry_run:
                results[f.name] = f'à embarquer ({len(brick_text(b))} chars)'
                continue
            updated = embed_brick(b)
            if not updated:
                results[f.name] = 'cached'
                continue
            f.write_text(json.dumps(b, ensure_ascii=False, indent=2))
            results[f.name] = f'OK'
        except Exception as e:
            results[f.name] = f'ERREUR: {e}'

    return results


if __name__ == '__main__':
    args    = sys.argv[1:]
    dry_run = '--dry-run' in args
    args    = [a for a in args if not a.startswith('--')]
    module  = args[0] if args else None

    print(f"Embedding {'(dry-run) ' if dry_run else ''}— module: {module or 'tous'}")
    results = embed_all_bricks(module=module, dry_run=dry_run)
    for name, status in results.items():
        print(f'  {name}: {status}')
