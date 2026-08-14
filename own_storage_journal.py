"""
OwnStorage — journal ledger-cli dans le Drive de l'organisation, pas sur le VPS
(2026-08-10, retour de Stéphane : "le journal doit être dans le storage de l'orga, pas sur
le VPS" — le VPS n'est qu'un lieu d'exécution temporaire, jamais où les données vivent).

Même protocole en 2 temps que org_secrets.py : le compte de service (Python) ne peut jamais
CRÉER de nouveau fichier Drive (storageQuotaExceeded), seulement METTRE À JOUR un fichier
existant (update_file, jamais bloqué). Le placeholder vide est créé côté Apps Script
(DriveApp, identité réelle) — voir ConnectorIdentity.js::identityEnsureJournalPlaceholder.
"""

import connector_ownstorage as _drive
import bricks as _bricks

JOURNAL_FILENAME = 'journal.ledger'


def _find_journal_file(folder_id):
    for f in _drive.list_files(folder_id):
        if f['name'] == JOURNAL_FILENAME:
            return f['id']
    return None


def get_journal(org_id):
    """Contenu actuel du journal depuis le Drive de l'org.
    Retourne {'success': True, 'content': str, 'fileId': str}
          ou {'success': False, 'errorCode': 'unknown_org'}
          ou {'success': False, 'errorCode': 'needs_bootstrap', 'folderId': str}
    """
    folder_id = _bricks._folder_id_for_org(org_id)
    if not folder_id:
        return {'success': False, 'errorCode': 'unknown_org'}

    file_id = _find_journal_file(folder_id)
    if not file_id:
        return {'success': False, 'errorCode': 'needs_bootstrap', 'folderId': folder_id}

    content = _drive.read_file(file_id)
    return {'success': True, 'content': content, 'fileId': file_id}


def set_journal(org_id, content):
    """Remplace le contenu du journal dans le Drive de l'org (update_file — jamais bloqué
    par le quota, contrairement à une création)."""
    folder_id = _bricks._folder_id_for_org(org_id)
    if not folder_id:
        return {'success': False, 'errorCode': 'unknown_org'}

    file_id = _find_journal_file(folder_id)
    if not file_id:
        return {'success': False, 'errorCode': 'needs_bootstrap', 'folderId': folder_id}

    _drive.update_file(file_id, content, mime_type='text/plain')
    return {'success': True, 'fileId': file_id}
