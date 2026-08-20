"""Journal technique par organisation — publié en HTML et synchronisé vers un
Google Doc dédié (créé une fois, mis à jour en place ensuite).

N'importe quel service de l'écosystème (ledger_api, subscriptions_api, Communicator
via connector, analyzor lui-même) peut logger une action pour une organisation via
log_action(). Stockage local BYOS v0 (JSONL append-only, même logique que
ledger_api/orgs/<id>/journal.ledger), un fichier par organisation.

La synchro Google Doc utilise directement l'API Drive avec le compte de service
(gdrive-service-account.json, déjà utilisé par connector_ownstorage.py) plutôt que
connector_ownstorage.write_file/update_file : ces deux fonctions utilisent le même
mimeType pour le corps ET le média, ce qui empêche la conversion HTML -> Google Doc
natif (il faut mimeType cible `google-apps.document` sur le corps, `text/html` sur
le média — deux valeurs différentes). Implémenté ici pour ne pas modifier un fichier
partagé avec une autre session en cours sur ce projet.
"""
import json
import os
import time

JOURNALS_DIR = os.path.join(os.path.dirname(__file__), 'journals')
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), 'gdrive-service-account.json')
SCOPES = ['https://www.googleapis.com/auth/drive']

# Dossier "Structory" dans le Drive du propriétaire de la plateforme, déjà partagé avec le
# compte de service (utilisé par connector_ownstorage.py pour les briques org/module) — sans
# ça, un Doc créé par le compte de service serait invisible pour lui (Drive isolé du service
# account). Les journaux techniques y vont par défaut, dans un sous-dossier dédié.
DEFAULT_PARENT_FOLDER_ID = '1vYWtlIxTzZBB4e29J8ymZSdZQxyVkzqz'

_service = None


def _get_drive_service():
    global _service
    if _service is not None:
        return _service
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    _service = build('drive', 'v3', credentials=credentials)
    return _service


def _org_dir(org_id):
    d = os.path.join(JOURNALS_DIR, org_id)
    os.makedirs(d, exist_ok=True)
    return d


def _entries_path(org_id):
    return os.path.join(_org_dir(org_id), 'entries.jsonl')


def _gdoc_id_path(org_id):
    return os.path.join(_org_dir(org_id), 'gdoc_file_id.txt')


def log_action(org_id, actor, summary, details=None):
    """actor : qui a agi, ex. "ledger_api", "Communicator", "session:compta-copro"."""
    entry = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'actor': actor,
        'summary': summary,
        'details': details or [],
    }
    with open(_entries_path(org_id), 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry


def get_entries(org_id):
    path = _entries_path(org_id)
    if not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    entries.reverse()
    return entries


def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def render_html(org_id):
    entries = get_entries(org_id)
    rows = []
    for e in entries:
        details_html = ''.join(f'<li>{_esc(d)}</li>' for d in e.get('details', []))
        rows.append(
            '<article>'
            f'<div class="meta"><span class="date">{_esc(e["timestamp"])}</span> '
            f'<span class="actor">{_esc(e["actor"])}</span></div>'
            f'<h2>{_esc(e["summary"])}</h2>'
            f'<ul>{details_html}</ul>'
            '</article>'
        )
    body = ''.join(rows) if rows else '<p><em>Aucune entrée.</em></p>'
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Journal technique — {_esc(org_id)}</title>'
        '<style>'
        'body{font-family:-apple-system,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:#20242a;}'
        'h1{font-size:22px;}'
        'article{padding:20px 0;border-bottom:1px solid #ddd;}'
        '.meta{font-family:monospace;font-size:12px;color:#6b7178;margin-bottom:6px;}'
        '.actor{background:#e4eeec;color:#2b6e68;padding:2px 6px;border-radius:3px;}'
        'h2{font-size:16px;margin:4px 0 8px;}'
        'ul{margin:0;padding-left:18px;font-size:14px;}'
        '</style></head><body>'
        f'<h1>Journal technique — {_esc(org_id)}</h1>{body}'
        '</body></html>'
    )


def sync_to_gdoc(org_id, parent_folder_id=None):
    """Crée le Google Doc de l'org s'il n'existe pas encore, sinon met à jour son
    contenu EN PLACE (même id, même URL, contrairement au Drive MCP limité — le
    compte de service a le scope drive complet).

    parent_folder_id : le fichier DOIT être créé dans un dossier déjà partagé avec un
    vrai utilisateur (Stéphane) — un compte de service seul n'a aucun quota Drive à lui
    (limitation Google connue), la création échoue sinon avec `storageQuotaExceeded`.
    """
    if parent_folder_id is None:
        parent_folder_id = DEFAULT_PARENT_FOLDER_ID

    from googleapiclient.http import MediaInMemoryUpload

    html = render_html(org_id)
    service = _get_drive_service()
    id_path = _gdoc_id_path(org_id)
    media = MediaInMemoryUpload(html.encode('utf-8'), mimetype='text/html')

    if os.path.exists(id_path):
        file_id = open(id_path).read().strip()
        try:
            service.files().update(fileId=file_id, media_body=media).execute()
            return file_id, False
        except Exception:
            pass  # fichier peut-être supprimé côté Drive entre-temps -> on en recrée un

    body = {'name': f'Journal technique — {org_id}', 'mimeType': 'application/vnd.google-apps.document'}
    if parent_folder_id:
        body['parents'] = [parent_folder_id]
    try:
        file = service.files().create(body=body, media_body=media, fields='id').execute()
    except Exception as e:
        if 'storageQuotaExceeded' in str(e):
            raise RuntimeError(
                f"Le compte de service ne peut pas créer de nouveau Google Doc pour "
                f"orgId={org_id} (aucun quota Drive propre à un compte de service, "
                f"limitation Google). Bootstrap requis une fois : créer le Doc avec un "
                f"vrai compte utilisateur dans un dossier déjà partagé avec le compte de "
                f"service, puis appeler POST /api/journal/register-gdoc "
                f"{{orgId, fileId}}."
            ) from e
        raise
    with open(id_path, 'w') as f:
        f.write(file['id'])
    return file['id'], True
