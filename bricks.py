"""
Bricks — lecture/écriture générique de briques PreCogn (JSON {id, type, title, contenu, ...},
même format que les Rules lues par config_resolver.py) pour représenter des objets d'identité
(Organisation, User) — pas seulement des Rules de configuration.

Décisions de Stéphane (2026-07-20), actées ici :
- Chaque organisation est un dossier sous ORGS_ROOT_FOLDER_ID (config_resolver.py). `org_id`
  est choisi par l'organisation elle-même (nom lisible, slugifié) — jamais dérivé d'un
  identifiant technique du backend de stockage (Drive aujourd'hui, potentiellement autre chose
  plus tard, voir connector_ownstorage.py). Si le nom choisi est déjà pris, l'appelant doit en
  proposer un autre — pas de suffixe auto muet.
- Une brique `User` appartient à son organisation (pas d'identité globale) : la même personne
  membre de plusieurs orgs a une brique par org, reliées seulement par email. La vue
  transversale ("cette personne est dans ces org1/org2/org3") est un futur composant niveau
  Structory, pas construit ici — seul l'index par email (ci-dessous) existe, et il vit dans
  Analyzor, pas ailleurs.
- Chaque brique porte son propre `uid`, indépendant du backend de stockage.
"""
import json
import re
import time
import unicodedata
import uuid

import config_resolver
import docling_registry
import embed_bricks as _embed
from connector_ownstorage import create_folder, find_files_by_fulltext, list_files, read_file, read_files_parallel, trash_file, write_file

BRICK_SCHEMA_VERSION = '1'

_org_folder_cache = {}
_email_index = {'built_at': 0, 'index': {}}
_EMAIL_INDEX_TTL_SECONDS = 6 * 60 * 60  # même durée que le cache de config_resolver.py


def new_uid(prefix):
    return f'{prefix}_{uuid.uuid4().hex[:20]}'


def _now():
    return time.strftime('%Y%m%dT%H%M%S', time.gmtime())  # aligné sur le format des bricks Rule existantes


def _slugify(name):
    base = unicodedata.normalize('NFD', (name or '').strip().lower())
    base = ''.join(c for c in base if unicodedata.category(c) != 'Mn')
    base = re.sub(r'[^a-z0-9]+', '_', base).strip('_')
    return base[:60]


def _new_brick(brick_type, title, contenu, owner):
    uid = new_uid(brick_type.lower()[:3])
    ts = _now()
    return {
        'id': f'{brick_type.upper()}-{uid}',
        'uid': uid,
        'type': brick_type,
        'title': title,
        'creator': 'subscriptions_onboarding',
        'created': ts,
        'modified': ts,
        'owner': owner,
        'language': 'fr',
        'version': BRICK_SCHEMA_VERSION,
        'status': 'Active',
        'tags': [],
        'rights': 'internal',  # pas de gestion de droits fine aujourd'hui — signalé comme
                                # limite connue (Stéphane 2026-07-19/20), pas réglé ici.
        'source': 'AccountPanel',
        'relations': [],
        'contenu': contenu,
    }


def write_brick(folder_id, brick):
    """Écrit une brique sur Drive, avec embedding automatique injecté avant sérialisation.
    L'embedding est généré via Ollama (nomic-embed-text, 768d) et stocké directement dans
    le JSON — toute brique écrite par ce module porte son embedding, sans étape séparée."""
    try:
        _embed.embed_brick(brick)
    except Exception:
        pass  # embedding non-bloquant : si Ollama est down, la brique est écrite sans
    filename = f"{brick['type'].lower()}_{brick['uid']}.json"
    return write_file(folder_id, filename, json.dumps(brick, ensure_ascii=False, indent=2))


_list_bricks_cache = {}
_LIST_BRICKS_CACHE_TTL_SECONDS = 6 * 60 * 60  # même TTL que config_resolver._cached_bricks


def list_bricks(folder_id, brick_type=None):
    """Scan Drive d'un dossier, avec cache (2026-07-26) : mesuré à ~8s à froid pour 18 briques
    Compte (`/api/org/{org_id}/bricks?type=Compte`, appelée par la vue patrimoine du Navigator
    ET par le daily-report), assez proche du timeout de 10s de l'Executor pour provoquer des 502
    intermittents en usage réel. Même classe de bug déjà corrigée pour `resolve_connectors`
    (config_resolver.py, 2026-07-25) — mais un cache DIFFÉRENT ici (pas partagé avec
    _cached_bricks) car les métadonnées de compte (nom, établissement, nature, titulaire, uid de
    connector) changent rarement, alors que le SOLDE lui-même n'est jamais stocké sur la brique
    (source unique : le journal ledger-cli), donc aucun risque d'afficher un solde périmé."""
    cache_key = (folder_id, brick_type)
    cached = _list_bricks_cache.get(cache_key)
    if cached and (time.time() - cached['t']) < _LIST_BRICKS_CACHE_TTL_SECONDS:
        return cached['bricks']

    # 'application/json' : briques écrites par ce module (create_org/create_user_in_org).
    # 'text/plain' : briques écrites par ConnectorIdentity.js (Apps Script, MimeType.PLAIN_TEXT
    # — DriveApp.createFile n'a pas d'équivalent 'application/json' natif), même format JSON
    # dedans. Sans ce deuxième type, toute organisation créée en libre-service (org-onboarding)
    # était invisible d'Analyzor (bug trouvé le 2026-07-22, jamais exercé avant).
    candidates = [f for f in list_files(folder_id) if f['mime_type'] in ('application/json', 'text/plain')]
    # Lecture PARALLÈLE (2026-07-31, retour de Stéphane : "le chargement est vraiment trop
    # long") — un read_file() par fichier, séquentiellement, mesurait jusqu'à 16s à froid pour
    # 19 briques (voir docstring de read_files_parallel).
    contents = read_files_parallel([f['id'] for f in candidates])
    bricks = []
    for f in candidates:
        raw = contents.get(f['id'])
        if raw is None:
            continue
        try:
            brick = json.loads(raw)
        except (json.JSONDecodeError, Exception):
            continue
        if brick_type and brick.get('type') != brick_type:
            continue
        brick['_fileId'] = f['id']
        bricks.append(brick)
    _list_bricks_cache[cache_key] = {'bricks': bricks, 't': time.time()}
    return bricks


def register_org_address(org_id, uid, folder_id, backend='gdrive'):
    """Enregistre/màj l'adresse BYOS d'une organisation dans Docling (docling_registry.py) —
    appelé par ConnectorDocling.js juste après la création (ou l'auto-cicatrisation ci-dessous
    quand une org antérieure au registre est retrouvée par le repli plein-texte)."""
    docling_registry.register_address(org_id, uid, folder_id, backend=backend)
    _org_folder_cache[org_id] = folder_id


def _folder_id_for_org(org_id):
    if org_id in _org_folder_cache:
        return _org_folder_cache[org_id]

    entry = docling_registry.resolve_address(org_id)
    if entry and entry.get('folderId'):
        _org_folder_cache[org_id] = entry['folderId']
        return entry['folderId']

    for f in list_files(config_resolver.ORGS_ROOT_FOLDER_ID):
        if f['mime_type'] == 'application/vnd.google-apps.folder' and f['name'] == org_id:
            _org_folder_cache[org_id] = f['id']
            return f['id']

    # Dernier repli : organisations créées en libre-service avant l'existence de ce registre
    # (ex. "tata", 2026-07-21) — retrouvées par le contenu de leur brique Organisation plutôt
    # que par position dans l'arborescence. Auto-cicatrisant : une fois retrouvée, enregistrée
    # dans le registre pour que ce repli coûteux ne soit plus jamais nécessaire pour cette org.
    needle = f'org:{org_id}'
    for f in find_files_by_fulltext(needle):
        if not f['name'].startswith('organisation_') or not f.get('parents'):
            continue
        try:
            brick = json.loads(read_file(f['id']))
        except (json.JSONDecodeError, Exception):
            continue
        if brick.get('owner') == needle:
            folder_id = f['parents'][0]
            register_org_address(org_id, brick.get('uid'), folder_id)
            return folder_id
    return None


def org_exists(org_id):
    return _folder_id_for_org(org_id) is not None


def create_org(name):
    """Crée une organisation : dossier Drive sous ORGS_ROOT_FOLDER_ID + brique Organisation
    dedans. org_id = nom slugifié par l'organisation elle-même ; si déjà pris, retourne une
    erreur pour que l'appelant en propose un autre (jamais de suffixe auto muet, Stéphane
    2026-07-20 : "c'est l'orga qui définit son nom, s'il est pris, elle en choisit un autre")."""
    org_id = _slugify(name)
    if not org_id:
        return {'success': False, 'errorCode': 'invalid_name'}
    if org_exists(org_id):
        return {'success': False, 'errorCode': 'org_id_taken', 'orgId': org_id}

    folder_id = create_folder(config_resolver.ORGS_ROOT_FOLDER_ID, org_id)
    _org_folder_cache[org_id] = folder_id
    brick = _new_brick('Organisation', name, {'name': name, 'joinPolicy': 'restricted'}, owner=f'org:{org_id}')
    write_brick(folder_id, brick)
    return {'success': True, 'orgId': org_id, 'folderId': folder_id, 'org': brick}


def get_org(org_id):
    folder_id = _folder_id_for_org(org_id)
    if not folder_id:
        return None
    orgs = list_bricks(folder_id, 'Organisation')
    return orgs[0] if orgs else None


def create_user_in_org(org_id, email, name=None, role='editor'):
    """Écrit une brique User dans le dossier de cette org. Idempotent par email : si une brique
    User avec cet email existe déjà dans cette org précise, la retourne telle quelle plutôt que
    d'en créer une deuxième."""
    folder_id = _folder_id_for_org(org_id)
    if not folder_id:
        return {'success': False, 'errorCode': 'unknown_org'}
    email = (email or '').strip().lower()
    if not email or '@' not in email:
        return {'success': False, 'errorCode': 'invalid_email'}

    for b in list_bricks(folder_id, 'User'):
        if (b.get('contenu') or {}).get('email') == email:
            return {'success': True, 'user': b, 'created': False}

    brick = _new_brick('User', name or email, {'email': email, 'name': name, 'role': role}, owner=f'org:{org_id}')
    write_brick(folder_id, brick)
    _index_add(email, org_id, brick)
    return {'success': True, 'user': brick, 'created': True}


def list_users(org_id):
    folder_id = _folder_id_for_org(org_id)
    if not folder_id:
        return []
    return list_bricks(folder_id, 'User')


# Suggestions seulement, jamais une liste fermée — une organisation doit pouvoir choisir
# n'importe quel type de compte (retour de Stéphane, 2026-07-26), voir ConnectorIdentity.js
# pour le même principe côté chemin de création réel (Apps Script).
COMPTE_NATURES_SUGGESTIONS = {'courant', 'épargne', 'titres', 'assurance_vie', 'retraite', 'crypto'}
COMPTE_CHAMPS_REQUIS = ('etablissement', 'titulaire', 'nom', 'nature', 'devise_origine')


def create_compte(org_id, contenu):
    """Écrit une brique Compte dans le dossier de cette org (Suivre Mes Comptes
    ARCHITECTURE.md §1.1) — jamais déduite automatiquement, toujours créée explicitement par
    l'utilisateur (à la main ou par API, jamais par un connector). Le lien vers un connector
    n'est jamais stocké ici : il est recalculé à chaque synchronisation par l'Executor à
    partir de `etablissement` + `nature` (§2)."""
    folder_id = _folder_id_for_org(org_id)
    if not folder_id:
        return {'success': False, 'errorCode': 'unknown_org'}

    manquants = [c for c in COMPTE_CHAMPS_REQUIS if not (contenu or {}).get(c)]
    if manquants:
        return {'success': False, 'errorCode': 'champs_manquants', 'champs': manquants}

    nature = contenu['nature'].strip()

    compte_contenu = {c: contenu[c].strip() if isinstance(contenu[c], str) else contenu[c] for c in COMPTE_CHAMPS_REQUIS}
    brick = _new_brick('Compte', compte_contenu['nom'], compte_contenu, owner=f'org:{org_id}')
    write_brick(folder_id, brick)
    _list_bricks_cache.pop((folder_id, 'Compte'), None)
    return {'success': True, 'compte': brick, 'created': True}


def invalidate_comptes_cache(org_id):
    """Invalide le cache de list_bricks (6h de TTL) pour les Comptes d'une org — nécessaire
    après une écriture qui contourne ce module (ex. identityCreateCompte, qui écrit
    directement via DriveApp/Apps Script, jamais par write_brick() ici) : Analyzor n'a alors
    aucun moyen de savoir qu'un fichier vient d'apparaître dans Drive sans qu'on le lui dise
    explicitement (bug réel trouvé le 2026-07-26 : un compte créé via Navigator restait
    invisible jusqu'à expiration du cache)."""
    folder_id = _folder_id_for_org(org_id)
    if folder_id:
        _list_bricks_cache.pop((folder_id, 'Compte'), None)
        _list_bricks_cache.pop((folder_id, None), None)


def delete_compte(org_id, compte_uid):
    """Met à la corbeille une brique Compte (jamais de suppression définitive) — même principe
    que create_compte : à la main ou par API, jamais déduit automatiquement. Contrairement à
    create_compte(), ne consomme aucun quota de stockage côté compte de service (trash_file,
    pas write_file) : fonctionne même là où la CRÉATION de nouveaux comptes reste bloquée
    (storageQuotaExceeded, voir connector_ownstorage.py)."""
    folder_id = _folder_id_for_org(org_id)
    if not folder_id:
        return {'success': False, 'errorCode': 'unknown_org'}

    for b in list_bricks(folder_id, 'Compte'):
        if b.get('uid') == compte_uid:
            trash_file(b['_fileId'])
            _list_bricks_cache.pop((folder_id, 'Compte'), None)
            return {'success': True, 'deleted': compte_uid}

    return {'success': False, 'errorCode': 'compte_introuvable'}


def list_comptes(org_id):
    folder_id = _folder_id_for_org(org_id)
    if not folder_id:
        return []
    return list_bricks(folder_id, 'Compte')


# ── Index en mémoire {email -> [{orgId, uid, name}]}, propriété d'Analyzor ─────────────────
# Même principe que le cache de config_resolver.py (_cache/_CACHE_TTL_SECONDS) : reconstruit
# depuis les briques, jamais lui-même une source de vérité, jetable à tout moment. Répond à
# "Analyzor peut analyser les users et voir s'il les connaît déjà" (Stéphane 2026-07-20).

def _rebuild_email_index():
    index = {}
    for org_folder in list_files(config_resolver.ORGS_ROOT_FOLDER_ID):
        if org_folder['mime_type'] != 'application/vnd.google-apps.folder':
            continue
        org_id = org_folder['name']
        _org_folder_cache[org_id] = org_folder['id']
        for b in list_bricks(org_folder['id'], 'User'):
            email = (b.get('contenu') or {}).get('email')
            if not email:
                continue
            index.setdefault(email, []).append(
                {'orgId': org_id, 'uid': b.get('uid'), 'name': (b.get('contenu') or {}).get('name'), 'role': (b.get('contenu') or {}).get('role')}
            )
    _email_index['index'] = index
    _email_index['built_at'] = time.time()


def _ensure_email_index():
    if time.time() - _email_index['built_at'] > _EMAIL_INDEX_TTL_SECONDS:
        _rebuild_email_index()


def _index_add(email, org_id, brick):
    _ensure_email_index()
    _email_index['index'].setdefault(email, []).append(
        {'orgId': org_id, 'uid': brick.get('uid'), 'name': (brick.get('contenu') or {}).get('name'), 'role': (brick.get('contenu') or {}).get('role')}
    )


def lookup_by_email(email):
    _ensure_email_index()
    return _email_index['index'].get((email or '').strip().lower(), [])
