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
"""

import os

SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), 'gdrive-service-account.json')
SCOPES = ['https://www.googleapis.com/auth/drive']

_service = None


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


def update_file(file_id, content, mime_type='application/json'):
    """Remplace le contenu d'un fichier existant (même id, même emplacement)."""
    from googleapiclient.http import MediaInMemoryUpload

    service = _get_service()
    media = MediaInMemoryUpload(content.encode('utf-8'), mimetype=mime_type)
    service.files().update(fileId=file_id, media_body=media).execute()
