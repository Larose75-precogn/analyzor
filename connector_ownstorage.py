"""
Connector OwnStorage — isole tout le reste du programme du backend de
stockage réel de l'organisation (BYOS : Google Drive aujourd'hui, potentiellement
OneDrive/S3/local plus tard). Aucun autre fichier ne doit importer directement
une bibliothèque Google Drive.

Outil PreCogn (au même niveau que Docling) : utilisable par filiation depuis
Structory, compta_copro, etc. — pas un outil propre à analyzor.

Interface stable :
- list_files(folder_id) -> liste de {id, name, mime_type}
- read_file(file_id) -> str (contenu texte)
- write_file(folder_id, name, content, mime_type='application/json') -> id du fichier créé
- update_file(file_id, content, mime_type='application/json') -> remplace un fichier classique (HTML, JSON...)
- create_folder(parent_folder_id, name) -> id du dossier créé
- read_doc_text(doc_id) -> str (texte brut d'un Google Doc natif)
- replace_doc_text(doc_id, text) -> remplace tout le corps d'un Google Doc natif, édité en place

Un dossier Drive n'est PAS soumis au même mur de quota qu'un Google Doc natif (vérifié
empiriquement le 2026-07-20 : le compte de service crée et supprime un dossier sans erreur,
contrairement à un Doc/Sheet natif qui échoue en storageQuotaExceeded) — create_folder()
fonctionne directement, pas besoin de passer par un compte utilisateur réel comme pour les Docs.

Un Google Doc natif n'est PAS un fichier classique (pas de media upload possible,
storageQuotaExceeded si on essaie de le créer via write_file) : il passe par l'API
Google Docs (documents.batchUpdate), un service distinct de Drive. L'API Docs doit
être activée sur le projet GCP du compte de service (2026-07-18 : activée via
`gcloud services enable docs.googleapis.com`) - sans quoi elle répond 403 même avec
le bon scope. Le compte de service ne peut pas non plus CRÉER un nouveau Doc (même
souci de quota que write_file) : la création passe par un compte utilisateur réel,
seule l'édition ensuite passe par ce connector.
"""

import os
import threading

SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), 'gdrive-service-account.json')
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/spreadsheets',
]

_service = None
_docs_service = None
_sheets_service = None
_thread_local = threading.local()


def _get_service():
    global _service
    if _service is not None:
        return _service

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    _service = build('drive', 'v3', credentials=credentials)
    return _service


def _get_docs_service():
    global _docs_service
    if _docs_service is not None:
        return _docs_service

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    _docs_service = build('docs', 'v1', credentials=credentials)
    return _docs_service


def list_files(folder_id):
    service = _get_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, mimeType)"
    ).execute()
    return [
        {"id": f["id"], "name": f["name"], "mime_type": f["mimeType"]}
        for f in results.get("files", [])
    ]


def read_file(file_id):
    service = _get_service()
    content = service.files().get_media(fileId=file_id).execute()
    return content.decode('utf-8') if isinstance(content, bytes) else content


def _get_thread_service():
    """Une instance de service Drive PAR THREAD (jamais le `_service` global partagé) — les
    transports httplib2 sous-jacents ne sont pas thread-safe, contrairement aux credentials
    eux-mêmes (juste un rafraîchissement de token, thread-safe côté google-auth). Utilisé
    uniquement par `read_files_parallel`, jamais par le reste du connector (lecture séquentielle
    normale, `read_file`, continue d'utiliser `_get_service()`)."""
    if not hasattr(_thread_local, 'service'):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _thread_local.service = build('drive', 'v3', credentials=credentials)
    return _thread_local.service


def read_files_parallel(file_ids, max_workers=10):
    """Lit plusieurs fichiers EN PARALLÈLE (I/O-bound — l'appel réseau Drive domine largement le
    coût, les threads Python contournent le GIL sans souci ici). Root cause d'une lenteur réelle
    et mesurée plusieurs fois en conditions réelles (2026-07-26 : ~8s à froid pour 18 briques
    Compte ; 2026-07-31, retour de Stéphane "le chargement est vraiment trop long" : jusqu'à 16s
    pour 19 briques smcdemo) : `list_bricks`/`_read_bricks` faisaient un `read_file()` PAR
    FICHIER, séquentiellement — chaque appel Drive individuel coûte ~500-800ms de latence réseau/
    auth, ce qui s'additionne linéairement avec le nombre de briques. Avec ~10 threads,
    ~20 fichiers se lisent en ~2 vagues au lieu de 20 allers-retours séquentiels.
    Retourne {file_id: contenu} — un fichier en échec (illisible, supprimé entre-temps) est
    absent du résultat, jamais bloquant pour les autres."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _read_one(file_id):
        service = _get_thread_service()
        content = service.files().get_media(fileId=file_id).execute()
        return content.decode('utf-8') if isinstance(content, bytes) else content

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_read_one, fid): fid for fid in file_ids}
        for future in as_completed(futures):
            fid = futures[future]
            try:
                results[fid] = future.result()
            except Exception:
                continue
    return results


def write_file(folder_id, name, content, mime_type='application/json'):
    from googleapiclient.http import MediaInMemoryUpload

    service = _get_service()
    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype=mime_type)
    file = service.files().create(
        body={"name": name, "parents": [folder_id], "mimeType": mime_type},
        media_body=media,
        fields="id"
    ).execute()
    return file["id"]


def find_files_by_fulltext(query_text):
    """Cherche parmi tous les fichiers visibles par ce compte de service (y compris ceux
    seulement partagés en lecture, pas seulement ceux sous un dossier connu) un texte donné.
    Utilisé pour retrouver le dossier Drive d'une organisation BYOS créée en libre-service
    (ConnectorIdentity.js) ailleurs que sous ORGS_ROOT_FOLDER_ID — le seul lien vers elle est
    le partage en lecture fait à la création, pas une position fixe dans l'arborescence."""
    service = _get_service()
    escaped = query_text.replace("\\", "\\\\").replace("'", "\\'")
    results = service.files().list(
        q=f"fullText contains '{escaped}' and trashed = false",
        fields="files(id, name, mimeType, parents)"
    ).execute()
    return [
        {"id": f["id"], "name": f["name"], "mime_type": f["mimeType"], "parents": f.get("parents", [])}
        for f in results.get("files", [])
    ]


def create_folder(parent_folder_id, name):
    service = _get_service()
    folder = service.files().create(
        body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_folder_id]},
        fields='id'
    ).execute()
    return folder['id']


def update_file(file_id, content, mime_type='application/json'):
    """Remplace le contenu d'un fichier existant (même id, même emplacement)."""
    from googleapiclient.http import MediaInMemoryUpload

    service = _get_service()
    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype=mime_type)
    service.files().update(fileId=file_id, media_body=media).execute()


def trash_file(file_id):
    """Met un fichier à la corbeille (récupérable côté Drive, jamais de suppression
    définitive) — contrairement à write_file(), ne consomme aucun quota de stockage côté
    compte de service, fonctionne donc même là où la création est bloquée
    (storageQuotaExceeded)."""
    service = _get_service()
    service.files().update(fileId=file_id, body={'trashed': True}).execute()


def read_doc_text(doc_id):
    """Texte brut (sans mise en forme) du corps d'un Google Doc natif."""
    doc = _get_docs_service().documents().get(documentId=doc_id).execute()
    text = []
    for element in doc.get('body', {}).get('content', []):
        paragraph = element.get('paragraph')
        if not paragraph:
            continue
        for run in paragraph.get('elements', []):
            text_run = run.get('textRun')
            if text_run:
                text.append(text_run.get('content', ''))
    return ''.join(text)


def replace_doc_text(doc_id, text):
    """Remplace tout le corps d'un Google Doc natif par `text`, en place (même
    id, même lien) - édité via l'API Docs (documents.batchUpdate), pas Drive."""
    docs_service = _get_docs_service()
    doc = docs_service.documents().get(documentId=doc_id).execute()

    end_index = doc['body']['content'][-1]['endIndex']
    requests = []
    if end_index - 1 > 1:
        requests.append({'deleteContentRange': {'range': {'startIndex': 1, 'endIndex': end_index - 1}}})
    requests.append({'insertText': {'location': {'index': 1}, 'text': text}})

    docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()


# ── Sheets API ────────────────────────────────────────────────────────────────

def _get_sheets_service():
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    _sheets_service = build('sheets', 'v4', credentials=credentials)
    return _sheets_service


def get_sheet_tabs(spreadsheet_id):
    """Liste des onglets (titre + sheetId) d'un Google Sheets."""
    result = _get_sheets_service().spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields='sheets.properties'
    ).execute()
    return [
        {'title': s['properties']['title'], 'sheetId': s['properties']['sheetId']}
        for s in result.get('sheets', [])
    ]


def read_sheet_range(spreadsheet_id, range_name):
    """Lit une plage (ex: '1 - 451001 AMSELLEM!A:F'). Retourne liste de listes."""
    result = _get_sheets_service().spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_name
    ).execute()
    return result.get('values', [])


def write_sheet_range(spreadsheet_id, range_name, values):
    """Écrit values (liste de listes) dans la plage. Remplace les cellules existantes."""
    _get_sheets_service().spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption='USER_ENTERED',
        body={'values': values},
    ).execute()


def clear_sheet_range(spreadsheet_id, range_name):
    """Vide une plage sans toucher au formatage."""
    _get_sheets_service().spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range_name, body={}
    ).execute()


def set_number_format(spreadsheet_id, sheet_gid, start_row, end_row,
                      start_col, end_col, pattern, ftype='NUMBER'):
    """Applique un format de cellule uniforme à une plage (indices 0-based, fin exclusive).

    Indispensable à la fidélité du round-trip Sheet→journal→Sheet : journaltosheet écrit des
    nombres bruts (ex. 129.6, 526.0) et des dates texte, sans jamais fixer de format. L'affichage
    dépendait donc du format préexistant de la colonne ("General" → "129,6"/"526" incohérents).
    En fixant un format canonique (montants "0.00", dates "yyyy/mm/dd"), toute régénération
    s'affiche à l'identique. `ftype` : 'NUMBER' | 'DATE' | 'TEXT' (voir NumberFormatType de
    l'API Sheets). C'est du FORMAT, jamais une réécriture de valeur — non destructif."""
    _get_sheets_service().spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={'requests': [{
            'repeatCell': {
                'range': {
                    'sheetId': sheet_gid,
                    'startRowIndex': start_row, 'endRowIndex': end_row,
                    'startColumnIndex': start_col, 'endColumnIndex': end_col,
                },
                'cell': {'userEnteredFormat': {'numberFormat': {'type': ftype, 'pattern': pattern}}},
                'fields': 'userEnteredFormat.numberFormat',
            }
        }]},
    ).execute()
