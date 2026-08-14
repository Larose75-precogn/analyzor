"""
Résolution de la table de configuration (entonnoir) d'analyzor, en cascade :

    organisation -> module -> Structory -> PreCogn

Chaque niveau est une ou plusieurs briques Rule (JSON) dans un dossier Drive.
On fusionne du plus général (PreCogn) au plus spécifique (organisation) — un
niveau plus spécifique surcharge les clés qu'il définit (héritage, pas
remplacement total).

BYOS réel : lecture via connector_ownstorage (Google Drive), pas de fichiers
locaux. Mêmes identifiants de dossier que Bibliotheque/ledger_api pour rester
cohérent entre les outils Apps Script et Python.
"""

import time
from connector_ownstorage import list_files, read_file, read_files_parallel
import json

PRECOGN_FOLDER_ID = '135SXvs9tRRsycS3GaF1svBFiLfjmZZj5'  # PreCogn/ (racine — niveau universel, parent de Structory)
STRUCTORY_FOLDER_ID = '1vYWtlIxTzZBB4e29J8ymZSdZQxyVkzqz'  # Structory/
COMPTA_COPRO_FOLDER_ID = '1ll52W0IaTt9ZBbKd6VQ0334oj7-toxVA'  # Structory/compta copro/ (module - Rules génériques, réutilisables par toute organisation qui utilise ce module)
ORGS_ROOT_FOLDER_ID = '1HKVOGreRhSF2VNynJBb_uQX9y_ar-DGR'  # Structory/orgs/ (une organisation = un sous-dossier, imbriqué sous Structory = accès parent naturel)
SUIVRE_MES_COMPTES_FOLDER_ID = '1gEBmUJbT_UxMX3H2U-4IdPcim2Ze91Si'  # Structory/Noad/ (module - fille de Structory, sœur de compta copro, créé le 2026-07-18, pas encore de règles métier propres ; renommé "noad" -> "suivre_mes_comptes" le 2026-07-20, même dossier Drive, id inchangé)

MODULE_FOLDER_ID = {
    'compta_copro': COMPTA_COPRO_FOLDER_ID,
    'suivre_mes_comptes': SUIVRE_MES_COMPTES_FOLDER_ID,
}

# Un dossier dédié par organisation, distinct du dossier du module (2026-07-18 :
# avant cette date, l'organisation partageait le dossier du module - conflation
# entre "règles génériques réutilisables" et "données propres à cette organisation
# précise" (ex. son journal technique), corrigée sur demande de Stéphane.
ORG_FOLDER_ID = {
    'copro_1crE1G2RerFeXQfHNh0yERfvfAjVKGUz53LE9szCqMMs': '1WR6LsZvz7Da8wHnYD6x091iTmEDWQzVb',  # Structory/orgs/copro_1crE1G2Rer.../
    'smcspl': '1o0RBjT4MyCDdusgLqdGbvNF7RGglURn2',  # Structory/SuivreMesComptes/smcspl/ — org fille de suivre_mes_comptes (EURL SPL), créée 2026-07-21, nichée dans le dossier Drive de sa mère plutôt que sous Structory/orgs/ comme les autres (demande explicite de Stéphane : "smcspl est une fille de smc dans l'ownstorage du user")
    'jdb': '1k3Ox78IrGL2vJBhDioDSFZx5WSYWAJu4',  # Precogn/Precogn/Structory/JournaldeBanque/ — dossier créé manuellement par Larose75 (chemin Drive Windows fourni), retrouvé le 2026-08-14 via le repli plein-texte (bricks.py::_folder_id_for_org, needle "JournaldeBanque") puisqu'il n'était nichée ni sous ORGS_ROOT_FOLDER_ID ni enregistré dans docling_registry. Contient seulement 2 fichiers legacy ("0 JournaldeBanque"/".pdf") — PAS de journal.ledger : le placeholder doit encore être créé par Apps Script/identité réelle (storageQuotaExceeded sinon), voir ~/projects/jdb/CLAUDE.md pour ce point bloquant.
}

_cache = {}
_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h : la config change rarement


def _read_bricks(folder_id):
    """Lit toutes les briques JSON d'un dossier Drive. Renvoie [] si le
    dossier n'existe pas ou est vide - jamais d'erreur bloquante."""
    if not folder_id:
        return []
    # 'application/json' : briques écrites par le compte de service (write_file).
    # 'text/plain' : briques écrites via DriveApp (Apps Script, ConnectorIdentity.js ou
    # créées manuellement) — DriveApp.createFile n'a pas d'équivalent 'application/json'
    # natif. Même bug/fix déjà appliqué à bricks.py::list_bricks (2026-07-22) : sans ce
    # deuxième type, toute brique Rule créée hors du compte de service (ex. connector
    # Mercury, 2026-07-25) était invisible de la cascade organisation->module->Structory.
    candidates = [f for f in list_files(folder_id) if f['mime_type'] in ('application/json', 'text/plain')]
    # Lecture PARALLÈLE (2026-07-31) — même correctif de latence que bricks.py::list_bricks,
    # voir connector_ownstorage.read_files_parallel.
    contents = read_files_parallel([f['id'] for f in candidates])
    bricks = []
    for f in candidates:
        raw = contents.get(f['id'])
        if raw is None:
            continue
        try:
            bricks.append(json.loads(raw))
        except (json.JSONDecodeError, Exception):
            continue  # une brique corrompue ne doit pas bloquer les autres
    return bricks


def _cached_bricks(folder_id):
    """`_read_bricks(folder_id)` avec cache (même TTL que le reste du fichier) — un scan Drive
    complet par dossier prend plusieurs secondes ; sans cache, `resolve_connectors` (appelé à
    chaque synchronisation d'un compte, potentiellement plusieurs comptes par org) devenait
    assez lent pour dépasser le timeout de l'Executor (2026-07-25)."""
    if not folder_id:
        return []
    cache_key = f'bricks:{folder_id}'
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached['t']) < _CACHE_TTL_SECONDS:
        return cached['bricks']
    bricks = _read_bricks(folder_id)
    _cache[cache_key] = {'bricks': bricks, 't': time.time()}
    return bricks


def _merge_tables(bricks, config):
    for brick in bricks:
        table = brick.get('contenu', {}).get('table')
        if table:
            config.update(table)


def resolve_table_config(org_id=None, module=None):
    cache_key = f"{module}:{org_id}"
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached['t']) < _CACHE_TTL_SECONDS:
        return cached['config']

    config = {}
    sources = []

    precogn_bricks = _read_bricks(PRECOGN_FOLDER_ID)
    if precogn_bricks:
        _merge_tables(precogn_bricks, config)
        sources.append('precogn')

    structory_bricks = _read_bricks(STRUCTORY_FOLDER_ID)
    if structory_bricks:
        _merge_tables(structory_bricks, config)
        sources.append('structory')

    if module:
        module_bricks = _read_bricks(MODULE_FOLDER_ID.get(module))
        if module_bricks:
            _merge_tables(module_bricks, config)
            sources.append(f'module:{module}')

    if org_id:
        org_bricks = _read_bricks(ORG_FOLDER_ID.get(org_id))
        if org_bricks:
            _merge_tables(org_bricks, config)
            sources.append(f'org:{org_id}')

    config['_sources'] = sources
    _cache[cache_key] = {'config': config, 't': time.time()}
    return config


def invalidate_module_cache(module):
    """Invalide le cache de `_cached_bricks` pour le dossier Drive d'un module — nécessaire
    après la création d'une brique Rule connector par `identityEnsureConnectorRule`
    (ConnectorIdentity.js, écriture directe DriveApp, jamais par ce process), même raison que
    `bricks.invalidate_comptes_cache` pour les Comptes. Sans ça, une Rule fraîchement créée
    reste invisible jusqu'à 6h (TTL)."""
    folder_id = MODULE_FOLDER_ID.get(module)
    if folder_id:
        _cache.pop(f'bricks:{folder_id}', None)


def resolve_connectors(etablissement, nature, org_id=None, module=None):
    """Résout les connectors compatibles (bricks Rule "connector", ARCHITECTURE.md §6 du
    projet Suivre Mes Comptes) pour un établissement + une nature de compte donnés, cascade
    Structory -> module -> org. Chaque brique connector décrit un périmètre explicite
    (etablissement + nature_couverte, moins hors_perimetre) — l'appelant (l'Executor) ne
    choisit jamais un connector directement, seul ce mécanisme le fait.
    Renvoie une liste (généralement 0 ou 1 match, mais jamais fusionnée/dédupliquée — un
    recouvrement entre niveaux de la cascade est un signal à corriger, pas à masquer)."""
    etablissement = (etablissement or '').strip().lower()
    matches = []

    # _read_bricks re-scanne tout le dossier Drive à chaque appel (aucun cache) — mesuré à
    # ~24s pour une résolution complète (2026-07-25), bien au-delà du timeout de l'Executor.
    # _cached_bricks() réutilise le même cache TTL que resolve_table_config (les connectors
    # changent aussi rarement que la config de table).
    levels = [
        ('precogn', _cached_bricks(PRECOGN_FOLDER_ID)),
        ('structory', _cached_bricks(STRUCTORY_FOLDER_ID)),
    ]
    if module:
        levels.append((f'module:{module}', _cached_bricks(MODULE_FOLDER_ID.get(module))))
    if org_id:
        levels.append((f'org:{org_id}', _cached_bricks(ORG_FOLDER_ID.get(org_id))))

    for level, bricks in levels:
        for brick in bricks:
            c = brick.get('contenu', {})
            if 'interface' not in c or 'etablissement' not in c:
                continue
            if c['etablissement'].strip().lower() != etablissement:
                continue
            if nature in (c.get('hors_perimetre') or []):
                continue
            if c.get('nature_couverte') and nature not in c['nature_couverte']:
                continue
            matches.append({
                'interface': c['interface'],
                'brickId': brick.get('id'),
                'title': brick.get('title'),
                'level': level,
            })

    return matches


def resolve_query_keywords():
    """Vocabulaire de reconnaissance des consultations (garde-fou déterministe
    côté Communicator) — lu depuis les briques Rule du niveau Structory."""
    cache_key = 'query_keywords'
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached['t']) < _CACHE_TTL_SECONDS:
        return cached['config']

    keywords = set()
    for brick in _read_bricks(PRECOGN_FOLDER_ID) + _read_bricks(STRUCTORY_FOLDER_ID):
        for group in brick.get('contenu', {}).values():
            if isinstance(group, list):
                # Seules les listes de chaînes sont du vocabulaire (ex: rule_0002) —
                # d'autres bricks (ex: rule_0001) ont des listes de dicts (plan
                # comptable) qui ne doivent pas se retrouver mélangées ici.
                keywords.update(k.lower() for k in group if isinstance(k, str))

    result = sorted(keywords)
    _cache[cache_key] = {'config': result, 't': time.time()}
    return result
