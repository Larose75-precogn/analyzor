"""
Analyzor — module understand
Interprète un message utilisateur dans le contexte brique d'une organisation PreCogn.
Chaîne : bricks → LLMPreCogn → Ollama (fallback) → réponse complète

Si un document est fourni (texte déjà extrait via Docling), il est injecté
dans le contexte avant l'appel LLM.

Connectors utilisés :
- connector_docling      : extraction texte (PDF, XLSX, CSV)
- connector_llmprecogn   : LLM cloud (groq, cerebras, deepseek)
- connector_ollama       : LLM local (fallback) + embedding (retrieval)

Point d'entrée : understand(org_id, message, last_message='', document_text='')
Retourne dict : {intent, response?, libelle?, montant?, sens?, compteNom?, solde?, ...}
"""

import hashlib, json, re, requests
import numpy as np
from pathlib import Path
from datetime import date

import connector_llmprecogn
import connector_ollama
import docling_registry

LEDGER_URL  = 'http://localhost:8080'
BRICKS_BASE = Path('/home/ubuntu/ledger_api/modules')
_ORG_EMBED_CACHE_PATH = Path(__file__).parent / 'data' / '_org_brick_embeddings.json'
_org_embed_cache = None


# ── Chargement du contexte ─────────────────────────────────────────────────────

def _get_module(org_id: str) -> str:
    try:
        r = requests.get(f'{LEDGER_URL}/api/org/{org_id}/module', timeout=5)
        return r.json().get('module', 'compta_copro')
    except Exception:
        return 'compta_copro'


def _get_brick_context(org_id: str) -> str:
    """Contexte textuel brique complet (Structory + module + org) via ledger_api."""
    try:
        r = requests.get(f'{LEDGER_URL}/api/context/structory',
                         params={'orgId': org_id}, timeout=10)
        return r.json().get('context', '')
    except Exception:
        return ''


def _load_org_embed_cache() -> dict:
    global _org_embed_cache
    if _org_embed_cache is not None:
        return _org_embed_cache
    try:
        _org_embed_cache = json.loads(_ORG_EMBED_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _org_embed_cache = {}
    return _org_embed_cache


def _save_org_embed_cache():
    _ORG_EMBED_CACHE_PATH.parent.mkdir(exist_ok=True)
    _ORG_EMBED_CACHE_PATH.write_text(json.dumps(_org_embed_cache, ensure_ascii=False))


def _get_org_bricks(org_id: str) -> list:
    """Briques RÉELLES de l'org (Compte : vrais soldes, vrais établissements) — pas seulement
    le vocabulaire générique du module (2026-08-03, retour de Stéphane : "il est évident que les
    outils Structory et PreCogn doivent s'appliquer aux organisations et leurs briques sinon
    tout ce qu'on fait n'a aucun intérêt"). Sans ça, le Communicator ne pouvait donner AUCUN
    vrai chiffre — juste des définitions abstraites du module.
    Un embedding par compte est calculé et mis en cache disque, jamais recalculé à chaque
    message (retour de Stéphane : "embed doit donner un embedding pour tous les objets") —
    basé sur l'IDENTITÉ structurelle du compte (établissement+nature+titulaire+produit+nom),
    JAMAIS le solde (qui change en continu et n'a pas de sens à ré-embedder à chaque fois)."""
    import bricks as _bricks
    comptes = _bricks.list_comptes(org_id)
    if not comptes:
        return []

    payload = []
    for c in comptes:
        ct = c.get('contenu', {})
        payload.append({
            'etablissement': ct.get('etablissement'), 'nature': ct.get('nature'),
            'titulaire': ct.get('titulaire'), 'produit': ct.get('produit'),
            'devise': ct.get('devise_origine') or 'EUR',
        })
    soldes_by_key = {}
    try:
        r = requests.post(f'{LEDGER_URL}/api/ledger/comptes-solde',
                          json={'orgId': org_id, 'comptes': payload}, timeout=15)
        r.raise_for_status()
        for item in r.json().get('comptes', []):
            key = (item.get('etablissement'), item.get('nature'), item.get('titulaire'), item.get('produit'))
            soldes_by_key[key] = item
    except Exception:
        pass

    cache = _load_org_embed_cache()
    cache_dirty = False
    result = []
    for c in comptes:
        ct = c.get('contenu', {})
        key = (ct.get('etablissement'), ct.get('nature'), ct.get('titulaire'), ct.get('produit'))
        solde_info = soldes_by_key.get(key, {})
        nom = ct.get('nom') or c.get('title')
        identity_text = f"Compte {nom} — {ct.get('etablissement')} {ct.get('nature')} {ct.get('titulaire') or ''} {ct.get('produit') or ''}".strip()
        cache_key = org_id + ':' + str(c.get('uid')) + ':' + hashlib.md5(identity_text.encode()).hexdigest()
        embedding = cache.get(cache_key)
        if embedding is None:
            try:
                embedding = _embed_text(identity_text)
                cache[cache_key] = embedding
                cache_dirty = True
            except Exception:
                embedding = None
        result.append({
            'title': nom,
            '_embedding': embedding,
            'contenu': {
                'etablissement': ct.get('etablissement'), 'nature': ct.get('nature'),
                'titulaire': ct.get('titulaire'), 'produit': ct.get('produit'),
                'solde': solde_info.get('solde', 0.0), 'devise': ct.get('devise_origine') or 'EUR',
                'lastDate': solde_info.get('lastDate'),
            },
        })

    if cache_dirty:
        _save_org_embed_cache()

    return result


def _summarize_org_totals(org_bricks: list) -> str:
    """Vrai total actuel, PAR DEVISE (jamais fusionné) — toujours injecté en entier, jamais
    filtré par la recherche sémantique top-k (une question "combien j'ai au total" a besoin de
    TOUS les comptes, pas des k plus proches sémantiquement du message)."""
    if not org_bricks:
        return ''
    totals = {}
    for b in org_bricks:
        c = b.get('contenu', {})
        devise = c.get('devise') or 'EUR'
        totals[devise] = totals.get(devise, 0.0) + (c.get('solde') or 0.0)
    lignes = '\n'.join(f'  {devise} : {montant:,.2f}'.replace(',', ' ') for devise, montant in totals.items())
    return f"\n# Comptes réels de cette organisation ({len(org_bricks)})\nTotal actuel, par devise (jamais additionné entre devises) :\n{lignes}\n"


def _get_bricks_raw(module: str) -> list:
    path = BRICKS_BASE / module / 'bricks'
    if not path.exists():
        return []
    bricks = []
    for f in sorted(path.glob('*.json')):
        try:
            bricks.append(json.loads(f.read_text()))
        except Exception:
            pass
    return bricks


def _embed_text(text: str) -> list:
    """Embedding via Ollama nomic-embed-text (768d) — délègue au connector."""
    result = connector_ollama.embed({"input": text})
    if result.get("success") and result.get("embedding"):
        return result["embedding"]
    raise RuntimeError(result.get("error", "embedding failed"))


def _cosine_sim(a: list, b: list) -> float:
    va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom  = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom > 1e-9 else 0.0


def _retrieve_relevant_bricks(message: str, bricks: list, k: int = 5) -> list:
    """Retourne les k briques les plus proches du message (cosine similarity)."""
    with_emb    = [b for b in bricks if b.get('_embedding')]
    without_emb = [b for b in bricks if not b.get('_embedding')]

    if not with_emb:
        return bricks[:k]

    try:
        msg_emb = _embed_text(message)
        scored  = [(b, _cosine_sim(msg_emb, b['_embedding'])) for b in with_emb]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = [b for b, _ in scored[:k]]
    except Exception:
        top = with_emb[:k]

    return top + without_emb


def _summarize_bricks(bricks: list) -> str:
    """Résumé lisible d'une sélection de briques pour le prompt LLM."""
    lines    = []
    DATA_KEYS = ('budget_annuel_eur', 'montant_trimestriel_eur',
                 'copropriétaires', 'schedule_2026', 'compte_provision',
                 'ecritures_types')
    SKIP_KEYS = {'_embedding', '_embedding_hash'}
    for b in bricks:
        title = b.get('title') or b.get('nom') or b.get('id', '?')
        data  = {k: v for k, v in b.items() if k in DATA_KEYS}
        if data:
            lines.append(f'[{title}] {json.dumps(data, ensure_ascii=False)[:800]}')
        elif b.get('contenu'):
            c = b['contenu']
            snippet = (c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))[:300]
            lines.append(f'[{title}] {snippet}')
        else:
            desc = b.get('description', '')[:150]
            lines.append(f'[{title}]{": " + desc if desc else ""}')
    return '\n'.join(lines)


# ── Prompt LLM ─────────────────────────────────────────────────────────────────


# Cadrage + exemples PAR MODULE (2026-08-03) — bug réel trouvé en testant le Communicator pour
# Suivre Mes Comptes : le prompt entier était codé en dur pour ComptaCopro ("Tu es l'assistant
# PreCogn d'une copropriété", exemples PCG "601=eau"...), quel que soit le module réel de l'org.
# La brique vocabulaire du module (bricks_summary) était bien injectée plus bas, mais noyée sous
# ce cadrage contradictoire — un visiteur smcdemo demandant "combien j'ai au total" (question
# patrimoniale parfaitement légitime) se faisait répondre "hors de l'objectif de mon assistant",
# pendant qu'une question hors-sujet ("raconte une blague") obtenait une vraie réponse. Cadrage +
# exemples désormais résolus depuis ce dict, un module absent retombe sur un cadrage neutre qui
# s'appuie uniquement sur bricks_summary (jamais un silence total, jamais un mauvais cadrage).
_MODULE_FRAMING = {
    'compta_copro': (
        "Tu es l'assistant PreCogn d'une copropriété (comptabilité ledger-cli, PCG français).",
        'Comptes PCG courants : 601=eau, 602=élec, 611=entretien, 615=travaux, 616=assurance, 622=frais bancaires, 701=appel fonds\n\n'
        'Exemples :\n'
        '- "budget" → answer (budget_annuel_eur dans les données)\n'
        '- "dépenses 2026" → query balance filters:["6"] beginDate:{year}/01/01\n'
        '- "solde aouchiche" → query balance filters:["aouchiche"]\n'
        '- "j\'ai payé 200€ eau" → add_entry libelle:"Eau" montant:200 sens:"depense" compte:"601000"\n'
    ),
    'suivre_mes_comptes': (
        "Tu es l'assistant PreCogn d'un suivi de patrimoine multi-comptes (PAS une comptabilité d'entreprise). "
        "Le total de patrimoine n'est JAMAIS un seul chiffre : cite TOUJOURS chaque devise séparément "
        "(ex. \"243 256,24 € et 38 880,33 $\"), ne les additionne JAMAIS entre elles, et ne réponds "
        "JAMAIS un montant qui ne provient pas exactement des données ci-dessous.",
        'Exemples :\n'
        '- "combien j\'ai au total" / "mon patrimoine" → answer citant CHAQUE devise du total réel ci-dessous, jamais une seule\n'
        '- "solde de mon livret bleu" → answer avec le solde exact de ce compte dans les données\n'
        '- "mon compte Fintra est à 1500€" → add_entry ou balance_point selon le contexte (constat de solde, jamais une dépense/recette)\n'
    ),
}
_MODULE_FRAMING_DEFAULT = (
    "Tu es l'assistant PreCogn de cette organisation.",
    '',
)


def _build_prompt(message: str, bricks_summary: str,
                  last_message: str, document_text: str, module: str = '') -> str:
    year = date.today().year
    hist = f'Message précédent : "{last_message}"\n' if last_message else ''
    intro, module_examples = _MODULE_FRAMING.get(module, _MODULE_FRAMING_DEFAULT)
    module_examples = module_examples.format(year=year) if module_examples else ''

    if document_text:
        doc_section = f'\n[Document joint extrait par Docling]\n{document_text[:4000]}\n'
        batch_section = f"""
{{"intent":"batch_entries","entries":[
  {{"libelle":"...","montant":12.5,"sens":"depense","date":"{year}/01/15","compte":"601000"}},
  {{"libelle":"...","montant":50.0,"sens":"depense","date":"{year}/02/01","compte":"615007"}}
]}}   — relevé bancaire ou facture avec PLUSIEURS lignes (une entry par ligne de transaction)
"""
        doc_example = f'- Relevé CSV/PDF avec plusieurs dépenses → batch_entries (une entry par ligne)\n- Facture unique → add_entry\n'
    else:
        doc_section = ''
        batch_section = ''
        doc_example  = ''

    return f"""{intro}

# Outils disponibles
- Docling : extraction de documents (PDF, XLSX, CSV) → texte structuré
- Analyzor : moteur d'analyse, briques JSON, contexte org
- LLMPrecogn / Ollama : intelligence conversationnelle (c'est toi)
- ledger-cli : journal comptable en partie double

# Données de l'organisation
{bricks_summary}
{doc_section}
{hist}Message : "{message}"

Réponds en JSON uniquement :

{{"intent":"answer","response":"..."}}          — question sur l'org ou les outils
{{"intent":"query","command":"balance","filters":["6"],"beginDate":"{year}/01/01"}}  — consulter le journal
{{"intent":"add_entry","libelle":"...","montant":12.5,"sens":"depense"}}             — une seule écriture
{batch_section}{{"intent":"unclear","response":"..."}}          — hors périmètre : réponds librement en français

filters accepte un numéro de classe ("6") OU un numéro de compte précis ("411", "512") —
si le message donne un numéro de compte (même suivi de ":libellé", ex "411:Clients"), c'est
TOUJOURS une query balance sur ce numéro seul (sans le libellé), jamais unclear :
- "solde 411:Clients" / "solde du compte 411" / "combien sur le 512" → {{"intent":"query","command":"balance","filters":["411"]}}

{module_examples}{doc_example}- "bonjour" / question hors sujet → unclear avec réponse naturelle en français
"""


# ── Appels LLM (via connectors) ────────────────────────────────────────────────

def _call_llmprecogn(prompt: str) -> tuple[str | None, str | None]:
    """LLMPreCogn cloud (groq, cerebras, deepseek...) — via connector.
    Retourne (content, provider)."""
    result = connector_llmprecogn.analyse({
        "task": {"mission": prompt, "language": "fr"},
        "context": "Tu es un assistant comptable PreCogn. Réponds en JSON uniquement.",
    })
    if result.get("success") and result.get("content"):
        return result["content"], result.get("provider")
    return None, None


OLLAMA_TIMEOUT_SECONDS = 3  # "5 secondes max" TOTAL pour l'utilisateur (Stéphane, 2026-08-03) —
# fixé à 3s (pas 5s) pour laisser de la marge au repli LLMPrecogn/Groq (~0.4s) + le reste du
# traitement (soldes réels, retrieval), sans jamais dépasser 5s au total. Ollama reste le LLM n°1,
# mais jamais au-delà de ce budget : au-delà, repli automatique et silencieux vers LLMPrecogn
# (voir understand(), jamais laissé attendre l'utilisateur plus longtemps qu'annoncé).


def _call_ollama(prompt: str) -> tuple[str | None, str | None]:
    """Ollama local (LLM n°1) — via connector, budget de temps strict.
    Retourne (content, provider)."""
    result = connector_ollama.generate({
        "prompt": prompt,
        "options": {"temperature": 0.1},
        "timeout": OLLAMA_TIMEOUT_SECONDS,
    })
    if result.get("success") and result.get("content"):
        return result["content"], "ollama:qwen2.5-coder:3b"
    return None, None


_NON_TRANSACTION = ('solde', 'total des mouvements', 'crediteur', 'débiteur')

def _normalize_batch(entries: list) -> dict:
    """Normalise debit/credit/description → montant/sens/libelle."""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        debit  = e.get('debit') or e.get('Debit') or 0
        credit = e.get('credit') or e.get('Credit') or 0
        montant = float(e.get('montant') or debit or credit or 0)
        if not montant:
            continue
        libelle = e.get('libelle') or e.get('description') or e.get('Libelle', '?')
        lib_low = libelle.lower()
        if any(kw in lib_low for kw in _NON_TRANSACTION):
            continue
        sens = e.get('sens', 'depense')
        if credit and not debit:
            sens = 'recette'
        out.append({
            'libelle': libelle,
            'montant': montant,
            'sens':    sens,
            'date':    e.get('date') or e.get('Date', ''),
            'compte':  e.get('compte', ''),
        })
    return {'intent': 'batch_entries', 'entries': out}


def _parse_json(text: str) -> dict:
    if not text:
        return {'intent': 'unclear', 'response': 'Pas de réponse du moteur.'}

    # Cas 1 : blocs ```json ... ``` — chercher en priorité les tableaux
    for block in re.findall(r'```(?:json)?\s*([\s\S]*?)```', text):
        block = block.strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                return _normalize_batch(parsed)
            if isinstance(parsed, dict):
                if parsed.get('intent') == 'batch_entries':
                    return parsed
        except Exception:
            pass

    # Cas 2 : tableau JSON nu dans le texte
    for m in re.finditer(r'\[\s*\{[\s\S]*?\}\s*\]', text):
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                return _normalize_batch(parsed)
        except Exception:
            pass

    # Cas 3 : objet JSON simple (intent:...)
    for m in re.finditer(r'\{[^{}]*\}', text):
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict) and parsed.get('intent'):
                return parsed
        except Exception:
            pass

    # Fallback
    return {'intent': 'unclear', 'response': text[:400]}


# ── Extraction de transactions depuis un document ─────────────────────────

def _extract_transactions_csv(text: str) -> list:
    """Parse un relevé CSV bancaire → liste d'entrées normalisées."""
    import csv, io
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            r = {k.strip().lower(): v.strip() for k, v in row.items() if v}
            libelle = r.get('libelle') or r.get('label') or r.get('description') or r.get('libellé') or ''
            if not libelle:
                continue
            debit  = float(r.get('debit','').replace(',','.') or 0)
            credit = float(r.get('credit','').replace(',','.') or r.get('crédit','').replace(',','.') or 0)
            montant = debit or credit
            if not montant:
                raw = r.get('montant','').replace(',','.')
                if raw:
                    montant = abs(float(raw))
                    credit = float(raw) > 0
            if not montant:
                continue
            sens = 'recette' if (credit and not debit) else 'depense'
            date = r.get('date','')
            rows.append({'libelle': libelle, 'montant': montant,
                         'sens': sens, 'date': date, 'compte': ''})
    except Exception:
        pass
    return rows


def _clean_layout_text(text: str) -> str:
    """Nettoie le texte pdftotext -layout.

    Détecte les colonnes Débit/Crédit depuis la ligne d'en-tête et annote
    chaque transaction avec [DEBIT:x] ou [CREDIT:x] selon la position du montant.
    """
    lines = text.splitlines()

    debit_col = credit_col = None
    for line in lines:
        if 'Débit' in line and 'Crédit' in line:
            debit_col  = line.index('Débit')
            credit_col = line.index('Crédit')
            break

    date_pat = re.compile(r'^\s*(\d{2}/\d{2}/\d{4})')

    result = []
    for line in lines:
        stripped = line.rstrip()
        if not stripped.strip():
            continue

        if debit_col and date_pat.match(stripped):
            m = re.search(r'\s{8,}([\d.]+,\d{2})\s*$', stripped)
            if m:
                amt_pos = m.start(1)
                amt_str = m.group(1).replace('.', '').replace(',', '.')
                try:
                    amt = float(amt_str)
                except ValueError:
                    amt = None
                label = stripped[:m.start()].strip()
                if amt and debit_col and credit_col:
                    tag = '[CREDIT]' if amt_pos >= credit_col - 5 else '[DEBIT]'
                    result.append(f'{label} {tag} {amt:.2f}')
                    continue

        compressed = re.sub(r' {6,}', ' | ', stripped.lstrip())
        if compressed:
            result.append(compressed)

    return '\n'.join(result)


def _parse_tagged_transactions(cleaned: str) -> list:
    """Parse les lignes taguées [DEBIT]/[CREDIT] produites par _clean_layout_text."""
    entries = []
    for line in cleaned.splitlines():
        m = re.match(
            r'(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s+(.+?)\s+\[(DEBIT|CREDIT)\]\s+([\d.]+)$',
            line.strip()
        )
        if not m:
            continue
        raw_date, libelle, col, raw_amt = m.groups()
        d, mo, y = raw_date.split('/')
        entries.append({
            'libelle': libelle.strip(),
            'montant': float(raw_amt),
            'sens':    'depense' if col == 'DEBIT' else 'recette',
            'date':    f'{y}/{mo}/{d}',
        })
    return entries


def _extract_transactions_llm(doc_text: str) -> list:
    """Extraction depuis un document PDF/DOCX — pré-traitement puis parse direct."""
    cleaned = _clean_layout_text(doc_text)

    tagged = _parse_tagged_transactions(cleaned)
    if tagged:
        return tagged

    prompt = f"""Extrais les transactions de ce document financier.
Réponds avec UN SEUL tableau JSON, sans aucun autre texte :
[{{"libelle":"...","montant":145.07,"sens":"depense","date":"2026/03/09"}}]

Règles : sens="depense" si sortie, "recette" si entrée. Date AAAA/MM/JJ.
Ne jamais écrire de texte hors du tableau JSON.

Document :
{cleaned[:5000]}"""

    # LLMPrecogn (cloud, Groq) en premier, Ollama local en repli (retour de Stéphane,
    # 2026-08-03 : "osef de sa place faut que ça marche" — priorité annulée après mesure réelle,
    # Ollama local prend 100+s dès qu'un contexte réel est envoyé sur ce serveur, contre <1s pour
    # Groq). Bug réel corrigé au passage : `_call_llmprecogn(prompt) or _call_ollama(prompt)` ne
    # tombait JAMAIS en repli — un tuple (même (None, None)) est toujours vrai en Python,
    # donc le "or" ne se déclenchait jamais quel que soit le contenu réel de la réponse.
    raw, _ = _call_llmprecogn(prompt)
    if not raw:
        raw, _ = _call_ollama(prompt)
    if not raw:
        return []

    for block in re.findall(r'```(?:json)?\s*([\s\S]*?)```', raw):
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    m = re.search(r'\[\s*\{[\s\S]*?\}\s*\]', raw)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return []


def _extract_transactions(filename: str, doc_text: str) -> list:
    """Route vers le bon extracteur selon le type de fichier."""
    ext = (filename or '').lower().rsplit('.', 1)[-1]
    if ext == 'csv':
        rows = _extract_transactions_csv(doc_text)
        if rows:
            return rows
    return _extract_transactions_llm(doc_text)


# ── Exécution query ────────────────────────────────────────────────────────────

def _execute_query(org_id: str, parsed: dict) -> str:
    try:
        # Défense en profondeur : si le LLM renvoie quand même "411:Clients" (libellé inclus)
        # malgré la consigne du prompt, on ne garde que le numéro avant ":" — /api/ledger/query
        # rejette tout filtre commençant par "-" mais un libellé après ":" cassait juste le
        # match ledger-cli (aucun compte ne s'appelle littéralement "411:Clients" au sens regex).
        raw_filters = parsed.get('filters', [])
        filters = [f.split(':', 1)[0] if isinstance(f, str) else f for f in raw_filters]
        body = {
            'orgId': org_id,
            'command': parsed.get('command', 'balance'),
            'filters': filters,
        }
        if parsed.get('beginDate'): body['beginDate'] = parsed['beginDate']
        if parsed.get('endDate'):   body['endDate']   = parsed['endDate']
        r = requests.post(f'{LEDGER_URL}/api/ledger/query', json=body, timeout=15)
        result = r.json()
        if result.get('success') and (result.get('output') or '').strip():
            return '\U0001f4ca Résultat :\n' + result['output']
        period = ''
        if parsed.get('beginDate') or parsed.get('endDate'):
            period = ' pour cette période'
        # balance vide != absence de mouvement : un compte peut être intégralement soldé
        # (ex: un client déjà payé) — ledger n'affiche rien quand le solde net est 0,
        # ce n'est pas la même chose qu'aucune écriture n'ayant jamais existé.
        if parsed.get('command', 'balance') in ('balance', 'bal') and filters:
            return f'\U0001f4ca Solde de {", ".join(filters)} : 0,00 €{period} (compte soldé).'
        return f'\U0001f4ed Aucun mouvement{period}.'
    except Exception as e:
        return f'\u274c Erreur ledger : {e}'


# ── Point d'entrée ─────────────────────────────────────────────────────────────

def understand(org_id: str, message: str,
               last_message: str = '', document_text: str = '',
               document_base64: str = '', document_filename: str = '') -> dict:
    """Interprète message dans le contexte brique de l'org.

    Chaîne :
      1. Charge bricks (ledger_api /api/context/structory + JSON bruts)
      2. Inject document_text si fourni (texte Docling pré-extrait)
      3. LLMPreCogn → Ollama (fallback), via connectors
      4. Si intent=query → exécute ledger et enrichit la réponse
    """
    module     = _get_module(org_id)
    bricks     = _get_bricks_raw(module)
    top_bricks = _retrieve_relevant_bricks(message, bricks, k=5)
    bricks_sum = _summarize_bricks(top_bricks)

    # Vraies briques de l'org (2026-08-03) — le total par devise (compact, quelques lignes)
    # n'est JAMAIS filtré par top-k : une question "combien j'ai au total" a besoin de TOUS
    # les comptes, pas d'un échantillon. Le détail par compte, lui, EST filtré par recherche
    # sémantique (retour de Stéphane, même jour : "5 secondes max" — Ollama en LLM n°1 ne peut
    # tenir ce budget qu'avec un prompt raisonnable, pas 19+ comptes en JSON complet à chaque
    # question, y compris pour une question qui n'en concerne qu'un seul).
    org_bricks = _get_org_bricks(org_id)
    if org_bricks:
        bricks_sum += _summarize_org_totals(org_bricks)
        top_org_bricks = _retrieve_relevant_bricks(message, org_bricks, k=5)
        bricks_sum += '\n' + _summarize_bricks(top_org_bricks)

    if document_base64 and not document_text:
        import base64, tempfile, os as _os
        try:
            ext = _os.path.splitext(document_filename or 'doc.bin')[1].lower() or '.bin'
            raw_bytes = base64.b64decode(document_base64)
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name
            try:
                import connector_docling as _doc
                document_text = _doc.extract_text(tmp_path)
            finally:
                _os.unlink(tmp_path)
        except Exception:
            document_text = ''

    if document_text:
        raw_entries = _extract_transactions(document_filename, document_text)
        if raw_entries:
            normalized = _normalize_batch(raw_entries)
            entries = normalized['entries']
            if entries:
                return {'intent': 'batch_entries', 'entries': entries,
                        'response': f'\U0001f4c4 {len(entries)} écriture(s) détectée(s) dans le document.'}

    prompt = _build_prompt(message, bricks_sum, last_message, document_text, module)

    # LLMPrecogn (cloud, Groq) en premier, Ollama local en repli (retour de Stéphane, 2026-08-03
    # — priorité Ollama annulée après mesure réelle : 100+s en local avec le vrai contexte
    # de l'org, contre <1s pour Groq. "osef de sa place faut que ça marche.")
    raw, provider = _call_llmprecogn(prompt)
    if raw is None:
        raw, provider = _call_ollama(prompt)
    parsed = _parse_json(raw)

    intent = parsed.get('intent', 'unclear')

    # Enregistrer l'analyse dans Docling (bus central d'information)
    try:
        docling_registry.record_understand(
            org_id=org_id,
            message=message,
            intent=intent,
            response_preview=parsed.get('response', '')[:200],
            provider=provider,
            has_document=bool(document_text or document_base64),
            embedding_used=bool(top_bricks),
        )
    except Exception:
        pass

    # Suggérer le facilitateur si l'org n'en a pas encore
    fac_info = docling_registry.facilitateur_info(org_id)
    if not fac_info.get("generated"):
        try:
            docling_registry.suggest_facilitateur(org_id)
        except Exception:
            pass
        parsed.setdefault("suggestedAction", "generer_facilitateur")

    if intent == 'query':
        parsed['response'] = _execute_query(org_id, parsed)

    if intent == 'batch_entries':
        entries = parsed.get('entries', [])
        parsed['response'] = f'\U0001f4c4 {len(entries)} écriture(s) à importer depuis le document.'

    if not parsed.get('response') and intent not in ('add_entry', 'balance_point', 'batch_entries'):
        parsed['response'] = (
            "Je n'ai pas compris. Exemples : "
            "\"solde banque\", \"dépenses 2026\", \"j'ai payé 200€ d'eau\"."
        )

    return parsed
