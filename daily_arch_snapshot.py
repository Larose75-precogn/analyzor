#!/usr/bin/env python3
"""Snapshot quotidien de l'architecture VPS → journal_tech (toutes les orgs + _master)."""

import glob
import sqlite3
import subprocess
from datetime import datetime, timezone

import requests

JOURNAL_URL  = "http://127.0.0.1:8000/api/journal/log"
SUBS_DB      = "/home/ubuntu/subscriptions_api/subscriptions.db"
JOURNALS_DIR = "/home/ubuntu/analyzor/journals"
ARCHI_URL    = "http://213.32.16.118:8000/archi"

SERVICES = [
    ("Coeur comptable", 8080, "ledger_api/app.py"),
    ("subscriptions_api", 8082, "subscriptions_api/app.py"),
    ("analyzor", 8000, "analyzor/main.py"),
    ("llmprecogn", 8001, "llmprecogn"),
]


def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def all_journal_orgs():
    orgs = []
    for path in glob.glob(f"{JOURNALS_DIR}/*/gdoc_file_id.txt"):
        org_id = path.split("/")[-2]
        if org_id != "_master":
            orgs.append(org_id)
    return orgs


def all_registered_orgs():
    try:
        conn = sqlite3.connect(SUBS_DB)
        rows = conn.execute("SELECT org_id, name FROM orgs").fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def build_summary():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    svc = []
    for name, port, pattern in SERVICES:
        pid = run(f"pgrep -f '{pattern}' | head -1")
        svc.append(f"{name}:{'✓' if pid else '✗'}")

    orgs = all_registered_orgs()
    org_names = ", ".join(
        f"{name} ({oid[:12]}...)" if len(oid) > 16 else f"{name} ({oid})"
        for oid, name in orgs
    )

    ledger_count = len(run("ls /home/ubuntu/ledger_api/orgs/ 2>/dev/null").split())
    disk = run("df -h / | tail -1 | awk '{print $3\"/\"$2\" (\"$5\")\"}' ")

    details = [
        "Services : " + "  ".join(svc),
        f"Orgs ({len(orgs)}) : {org_names}",
        f"Ledger : {ledger_count} journaux actifs",
        f"Disque / : {disk}",
        f"Architecture : {ARCHI_URL}",
    ]
    return details, today


def post(org_id, details, summary_line):
    payload = {
        "orgId":   org_id,
        "actor":   "cron:daily-arch-snapshot",
        "summary": summary_line,
        "details": details,
    }
    try:
        r = requests.post(JOURNAL_URL, json=payload, timeout=60)
        ok = r.json().get("success")
        print(f"  {'✓' if ok else '✗'} {org_id}")
    except Exception as e:
        print(f"  ✗ {org_id} : {e}")


if __name__ == "__main__":
    details, today = build_summary()
    print(f"Snapshot {today}")
    print("\n".join(f"  {d}" for d in details))

    # Post dans tous les journaux par org
    for org_id in all_journal_orgs():
        post(org_id, details, f"Snapshot VPS — {today}")

    # Post dans _master avec le lien architecture en évidence
    master_details = details + [f"→ Diagramme live : {ARCHI_URL}"]
    post("_master", master_details, f"Snapshot VPS + architecture — {today}")
