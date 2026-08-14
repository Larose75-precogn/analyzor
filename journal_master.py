"""Journal technique MASTER — un seul Google Doc qui regroupe TOUTES les organisations
et TOUS les outils, chaque écriture indiquant son origine (orgId d'où elle vient).

Séparé de journal.py à dessein (une autre session le modifie en ce moment) : ce fichier
ne fait qu'IMPORTER journal.py, jamais ne le modifie. Les journaux par organisation
(entries.jsonl, Doc par org, contrôle d'accès hiérarchique) restent inchangés et
continuent d'exister tels quels — ce script ajoute une vue agrégée en plus, il ne
remplace rien.

Usage : python3 journal_master.py
"""
import json
import os

import journal as _journal

JOURNALS_DIR = _journal.JOURNALS_DIR
MASTER_ORG_ID = '_master'

# Dossier "Precogn" — parent direct de "Structory" dans le Drive, confirmé accessible
# en lecture/écriture par le compte de service. Le Doc MASTER doit être au sommet de la
# hiérarchie (Precogn), pas dans Structory, sur demande explicite de Stéphane.
PRECOGN_FOLDER_ID = '135SXvs9tRRsycS3GaF1svBFiLfjmZZj5'

# Dossiers qui ne sont pas de vraies orgs/outils (artefacts, à ignorer dans l'agrégat).
IGNORE_DIRS = {MASTER_ORG_ID, 'compta_copro.ORPHELIN_fusionne_20260719'}

# Le journal MASTER est un journal TECHNIQUE : il ne synthétise que le travail technique
# (sessions de dev, commits git, agents claude) — JAMAIS les journaux ledgercli/comptables ni
# les événements métier runtime (écritures 'sheet-communicator', points de solde 'executor',
# provisioning 'ledger_api', recherches bancaires, abonnements, snapshots archi cron...).
# Décision Stéphane 2026-08-14 : « ce ne sont pas les journaux ledgercli qui doivent être
# synthétisés ici mais uniquement les journaux techniques. » Filtrage par ACTEUR à la génération
# uniquement : les entries.jsonl sources restent intactes (chaque org garde son journal complet),
# seul l'agrégat MASTER est filtré — donc réversible, et chaque regénération future reste propre.
TECHNICAL_ACTOR_PREFIXES = ('session:', 'git-commit:', 'claude')


def _is_technical_entry(entry):
    """Vrai si l'entrée relève du travail technique (à inclure dans le MASTER), faux si c'est un
    événement métier/comptable/runtime (à exclure). Distinction par préfixe d'acteur."""
    return (entry.get('actor') or '').startswith(TECHNICAL_ACTOR_PREFIXES)


import re as _re

_EMAIL_RE = _re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def _redact_pii(text):
    """Masque le PII non-ambigu (adresses email) dans le rendu du MASTER. Même dans une entrée
    TECHNIQUE, une transcription /rep peut citer un email réel — il n'a rien à faire dans un
    journal technique agrégé. Masquage au RENDU seulement (sources intactes). Les noms de
    personnes ne sont pas masqués ici (choix subjectif, liste à valider séparément)."""
    return _EMAIL_RE.sub('[email masqué]', text or '')


def _all_org_ids():
    if not os.path.isdir(JOURNALS_DIR):
        return []
    return sorted(
        d for d in os.listdir(JOURNALS_DIR)
        if os.path.isdir(os.path.join(JOURNALS_DIR, d))
        and d not in IGNORE_DIRS
        and os.path.exists(os.path.join(JOURNALS_DIR, d, 'entries.jsonl'))
    )


def _merged_entries():
    """Toutes les entrées de tous les orgs, triées chronologiquement, chacune taguée
    avec son orgId d'origine."""
    merged = []
    for org_id in _all_org_ids():
        path = os.path.join(JOURNALS_DIR, org_id, 'entries.jsonl')
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                if not _is_technical_entry(e):
                    continue  # événement métier/comptable/runtime — pas dans le journal technique
                e['summary'] = _redact_pii(e.get('summary', ''))
                e['details'] = [_redact_pii(d) for d in e.get('details', [])]
                e['_origin_org'] = org_id
                merged.append(e)
    merged.sort(key=lambda e: e.get('timestamp', ''))
    return merged


def render_master_html():
    entries = _merged_entries()
    rows = []
    for e in entries:
        details_html = ''.join(f'<li>{_journal._esc(d)}</li>' for d in e.get('details', []))
        rows.append(
            '<article>'
            f'<div class="meta"><span class="date">{_journal._esc(e["timestamp"])}</span> '
            f'<span class="origin">{_journal._esc(e["_origin_org"])}</span> '
            f'<span class="actor">{_journal._esc(e["actor"])}</span></div>'
            f'<h2>{_journal._esc(e["summary"])}</h2>'
            f'<ul>{details_html}</ul>'
            '</article>'
        )
    body = ''.join(rows) if rows else '<p><em>Aucune entrée.</em></p>'
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Journal technique — MASTER (toutes organisations)</title>'
        '<style>'
        'body{font-family:-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#20242a;}'
        'h1{font-size:22px;}'
        'article{padding:20px 0;border-bottom:1px solid #ddd;}'
        '.meta{font-family:monospace;font-size:12px;color:#6b7178;margin-bottom:6px;}'
        '.origin{background:#eee4d8;color:#8a5a1f;padding:2px 6px;border-radius:3px;font-weight:bold;}'
        '.actor{background:#e4eeec;color:#2b6e68;padding:2px 6px;border-radius:3px;}'
        'h2{font-size:16px;margin:4px 0 8px;}'
        'ul{margin:0;padding-left:18px;font-size:14px;}'
        '</style></head><body>'
        '<h1>Journal technique — MASTER (toutes organisations)</h1>'
        f'<p>Regroupe : {", ".join(_all_org_ids())}</p>'
        f'{body}'
        '</body></html>'
    )


def sync_master_to_gdoc(parent_folder_id=None, preserve_header=True):
    """Même mécanique que journal.sync_to_gdoc, mais pour le Doc unique agrégé —
    stocké sous un orgId dédié '_master' pour réutiliser _org_dir/_gdoc_id_path sans
    dupliquer la logique de création/mise à jour Google Drive.

    preserve_header : si True (par défaut), tout ce qui précède la ligne
    '======...======' dans le Doc existant est préservé tel quel à chaque
    synchronisation — jamais écrasé, même en usage courant répété."""
    if parent_folder_id is None:
        parent_folder_id = PRECOGN_FOLDER_ID

    from googleapiclient.http import MediaInMemoryUpload

    service = _journal._get_drive_service()
    id_path = _journal._gdoc_id_path(MASTER_ORG_ID)

    if os.path.exists(id_path):
        file_id = open(id_path).read().strip()
        header_html = ''
        if preserve_header:
            exported = service.files().export(fileId=file_id, mimeType='text/html').execute()
            exported_html = exported.decode('utf-8')
            marker_pos = exported_html.find(SEPARATOR_MARKER)
            if marker_pos != -1:
                # coupe juste après le </p> qui contient la ligne de séparation —
                # tout ce qui précède (en-tête écrit par Stéphane) est préservé tel quel.
                close_p = exported_html.find('</p>', marker_pos)
                if close_p != -1:
                    body_start = exported_html.find('<body')
                    body_open_end = exported_html.find('>', body_start) + 1
                    header_html = exported_html[body_open_end:close_p + len('</p>')]
        entries_html = _render_entries_fragment(_merged_entries())
        full_html = (
            '<!doctype html><html><head><meta charset="utf-8"></head><body>'
            f'{header_html}{entries_html}'
            '</body></html>'
        )
        media = MediaInMemoryUpload(full_html.encode('utf-8'), mimetype='text/html')
        service.files().update(fileId=file_id, media_body=media).execute()
        return file_id, False

    html = render_master_html()
    media = MediaInMemoryUpload(html.encode('utf-8'), mimetype='text/html')
    body = {'name': 'Journal technique — MASTER (toutes organisations)',
            'mimeType': 'application/vnd.google-apps.document'}
    if parent_folder_id:
        body['parents'] = [parent_folder_id]
    try:
        file = service.files().create(body=body, media_body=media, fields='id').execute()
    except Exception as e:
        if 'storageQuotaExceeded' in str(e):
            raise RuntimeError(
                "Le compte de service ne peut pas créer de nouveau Google Doc pour le "
                "journal MASTER (aucun quota Drive propre à un compte de service). "
                "Bootstrap requis une fois : créer un Doc vide avec un vrai compte "
                "utilisateur dans un dossier déjà partagé avec le compte de service, "
                "puis appeler register_master_gdoc(fileId)."
            ) from e
        raise
    with open(id_path, 'w') as f:
        f.write(file['id'])
    return file['id'], True


SEPARATOR_MARKER = '======================================'


def _render_entries_fragment(entries):
    """Fragment HTML des entrées seules (pas un document complet) — styles en ligne
    uniquement, pas de bloc <style> externe (mal préservé par l'import Google Docs
    lors d'une fusion avec du contenu existant)."""
    rows = []
    for e in entries:
        details_html = ''.join(
            f'<li style="font-size:10pt">{_journal._esc(d)}</li>' for d in e.get('details', [])
        )
        rows.append(
            '<p style="margin:14pt 0 2pt 0;font-family:monospace;font-size:9pt;color:#6b7178">'
            f'{_journal._esc(e["timestamp"])} '
            f'<span style="background:#eee4d8;color:#8a5a1f;padding:1pt 4pt;font-weight:bold">{_journal._esc(e["_origin_org"])}</span> '
            f'<span style="background:#e4eeec;color:#2b6e68;padding:1pt 4pt">{_journal._esc(e["actor"])}</span>'
            '</p>'
            f'<p style="margin:2pt 0 4pt 0;font-size:12pt;font-weight:bold">{_journal._esc(e["summary"])}</p>'
            f'<ul style="margin:0 0 6pt 0">{details_html}</ul>'
        )
    return ''.join(rows) if rows else '<p><em>Aucune entrée.</em></p>'


def register_master_gdoc(file_id, parent_folder_id=None):
    """Enregistre l'id d'un Doc MASTER déjà créé par un vrai compte (contenant déjà un
    en-tête + une ligne de séparation '======...======' écrite par Stéphane), préserve
    tout ce qui est AU-DESSUS de cette ligne tel quel, et (ré)écrit uniquement ce qui
    est en dessous avec le contenu agrégé — jamais d'écrasement de l'en-tête existant."""
    with open(_journal._gdoc_id_path(MASTER_ORG_ID), 'w') as f:
        f.write(file_id)
    return sync_master_to_gdoc(parent_folder_id=parent_folder_id, preserve_header=True)


if __name__ == '__main__':
    file_id, created = sync_master_to_gdoc()
    url = f'https://docs.google.com/document/d/{file_id}/edit'
    print(f'{"Créé" if created else "Mis à jour"} : {url}')
