"""Secrets par organisation (clés API de connectors, etc.) — chiffrement au repos, clé de
chiffrement propre à chaque org, stockée UNIQUEMENT dans le Drive de cette même org, dans un
sous-dossier séparé et plus restreint que les bricks Rule normales (`_secrets/`, jamais lu par
la cascade organisation->module->Structory->PreCogn de config_resolver.py — les briques Rule
sont lues directement dans le dossier de l'org, pas récursivement, donc un sous-dossier en est
naturellement exclu).

Décision de Stéphane (2026-07-20, ARCHITECTURE.md Suivre Mes Comptes §12 point 7) : rien d'une
organisation ne doit jamais être stocké en dehors de cette organisation — donc pas de clé
maîtresse partagée sur le VPS ni entre organisations. Chaque org a sa propre clé, générée à la
volée au premier secret stocké.
"""
from googleapiclient.errors import HttpError
from cryptography.fernet import Fernet

import time as _time

import bricks as _bricks
from connector_ownstorage import list_files, read_file, read_files_parallel, write_file, update_file, create_folder

SECRETS_SUBFOLDER_NAME = '_secrets'
_KEY_FILENAME = '_key.bin'

# Cache court (2026-08-01, retour de Stéphane : "délai très très long" sur la recherche de
# banque, qui appelle get_secret jusqu'à 3 fois PAR APPEL sans aucun cache jusqu'ici — TTL
# volontairement court par rapport aux 6h des caches de bricks : un secret vient d'être modifié
# en self-service (OrgPanel) doit être vu vite, contrairement à des métadonnées de compte qui
# changent rarement.
_secret_cache = {}
_SECRET_CACHE_TTL_SECONDS = 2 * 60


def _invalidate_secret_cache(org_id, name):
    _secret_cache.pop((org_id, name), None)


def _is_quota_error(e):
    return isinstance(e, HttpError) and getattr(e, 'status_code', None) == 403 and 'storageQuotaExceeded' in str(e)


def _secrets_folder_id(org_id, create=False):
    org_folder_id = _bricks._folder_id_for_org(org_id)
    if not org_folder_id:
        return None
    for f in list_files(org_folder_id):
        if f['mime_type'] == 'application/vnd.google-apps.folder' and f['name'] == SECRETS_SUBFOLDER_NAME:
            return f['id']
    if not create:
        return None
    return create_folder(org_folder_id, SECRETS_SUBFOLDER_NAME)


def _get_or_create_org_key(secrets_folder_id):
    """Retourne (key_bytes, needs_bootstrap). `needs_bootstrap=True` signifie : le fichier
    `_key.bin` n'existe pas encore et le compte de service ne peut pas le CRÉER (quota Drive,
    voir CLAUDE.md suivre_mes_comptes) — l'appelant doit d'abord faire créer un fichier
    placeholder vide via DriveApp (identityEnsureSecretPlaceholder, ConnectorIdentity.js), puis
    rappeler cette fonction : à ce moment le fichier EXISTE, `update_file` (jamais bloqué)
    prendra le relais tout seul."""
    for f in list_files(secrets_folder_id):
        if f['name'] == _KEY_FILENAME:
            content = read_file(f['id'])
            if content:
                return content.encode(), False
            # Placeholder vide créé par Apps Script : le remplir avec une vraie clé, via
            # update_file (le fichier existe déjà, jamais bloqué par le quota).
            key = Fernet.generate_key()
            update_file(f['id'], key.decode(), mime_type='text/plain')
            return key, False

    key = Fernet.generate_key()
    try:
        write_file(secrets_folder_id, _KEY_FILENAME, key.decode(), mime_type='text/plain')
        return key, False
    except HttpError as e:
        if _is_quota_error(e):
            return None, True
        raise


def set_secret(org_id, name, value):
    """Chiffre `value` et l'écrit (ou le remplace s'il existe déjà) dans le sous-dossier
    `_secrets/` de l'organisation. Retourne {'success': False, 'errorCode': 'unknown_org'} si
    l'org n'existe pas, ou {'success': False, 'errorCode': 'needs_bootstrap', 'missingFiles':
    [...]} si le compte de service ne peut pas créer les fichiers nécessaires (premier secret
    de cette org) — l'appelant doit alors créer les placeholders vides via DriveApp
    (identityEnsureSecretPlaceholder) puis rappeler cette même fonction."""
    folder_id = _secrets_folder_id(org_id, create=True)
    if not folder_id:
        return {'success': False, 'errorCode': 'unknown_org'}

    key, needs_bootstrap_key = _get_or_create_org_key(folder_id)
    missing = ['_key.bin'] if needs_bootstrap_key else []

    existing_file_id = None
    for f in list_files(folder_id):
        if f['name'] == f'{name}.enc':
            existing_file_id = f['id']
            break
    if existing_file_id is None:
        missing.append(f'{name}.enc')

    if missing:
        return {'success': False, 'errorCode': 'needs_bootstrap', 'missingFiles': missing, 'folderId': folder_id}

    token = Fernet(key).encrypt(value.encode()).decode()
    update_file(existing_file_id, token, mime_type='text/plain')
    return {'success': True}


def get_secret(org_id, name):
    """Déchiffre et retourne le secret `name` de l'org, ou None s'il n'existe pas (org
    inconnue, dossier _secrets absent, ou secret absent — jamais d'exception, un secret
    manquant est un cas normal, pas une erreur)."""
    folder_id = _secrets_folder_id(org_id, create=False)
    if not folder_id:
        return None

    key_file_id = None
    token_file_id = None
    for f in list_files(folder_id):
        if f['name'] == _KEY_FILENAME:
            key_file_id = f['id']
        elif f['name'] == f'{name}.enc':
            token_file_id = f['id']
    if not key_file_id or not token_file_id:
        return None

    # Lecture en parallèle (2026-08-01, retour de Stéphane : "aucune info sur une recherche en
    # cours juste un délai très très long" sur la recherche de banque, qui appelle get_secret
    # jusqu'à 3 fois par appel — Powens + 2 modes Enable Banking) — les 2 fichiers (clé +
    # secret) n'ont aucune dépendance entre eux, jamais besoin de les lire l'un après l'autre.
    contents = read_files_parallel([key_file_id, token_file_id])
    key = contents.get(key_file_id)
    token = contents.get(token_file_id)
    if key is not None:
        key = key.encode()

    if not key or not token:
        return None
    return Fernet(key).decrypt(token.encode()).decode()


def list_secret_names(org_id):
    """Liste les noms de secrets stockés pour une org (jamais les valeurs) — pour un écran
    d'admin qui affiche 'clé GMC : configurée' sans jamais afficher la clé elle-même."""
    folder_id = _secrets_folder_id(org_id, create=False)
    if not folder_id:
        return []
    return sorted(
        f['name'][:-4] for f in list_files(folder_id)
        if f['name'].endswith('.enc')
    )
