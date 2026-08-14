"""
Moteur générique de réconciliation Sheet -> Journal (partie double).

Aucune règle spécifique à une organisation. Opère sur une liste de postings
{compte, label, date, libelle, side, amount} extraits (par ex. via Docling)
depuis n'importe quel classeur. La clé de tout appariement est le TEMPS
(même jour) et le MONTANT (exact, ou somme exacte d'un groupe) - jamais un
mot-clé de libellé.
"""

import re
import datetime
from collections import defaultdict

LEDGER_HEADER_PATTERNS = {
    'date', 'libellé', 'libelle', 'débit', 'debit', 'crédit', 'credit', 'solde'
}


def extract_account_code(tab_name):
    """Code numérique en tête d'un nom d'onglet/table, générique à tout PCG.
    Ex: '1 - 102004 Travaux MUR' -> '102004', '401 Fournisseurs' -> '401'."""
    m = re.match(r'^\s*(?:\d+\s*-\s*)?(\d{2,6})\b', tab_name)
    return m.group(1) if m else None


def extract_account_label(tab_name):
    """Texte qui suit le code de compte dans le nom d'onglet, générique.
    Ex: '1 - 451003 BENRHOUMA' -> 'BENRHOUMA'. None si rien après le code."""
    m = re.match(r'^\s*(?:\d+\s*-\s*)?\d{2,6}\b\s*(.*)$', tab_name)
    label = m.group(1).strip() if m else ''
    return label or None


def extract_group_prefix(tab_name):
    """Préfixe de groupe d'onglets ('1 - xxx' -> '1'), générique à toute
    convention de classeur qui range ses onglets en groupes numérotés.
    Renvoie None si le nom ne suit pas cette convention."""
    m = re.match(r'^\s*(\d+)\s*-\s*\S', tab_name)
    return m.group(1) if m else None


def looks_like_ledger_table(headers, patterns=None, min_matches=3):
    """Vrai si les en-têtes ressemblent à un grand livre (Date/Libellé/Débit/Crédit/Solde),
    peu importe l'ordre ou la langue exacte des libellés de colonnes reconnus.

    `patterns`/`min_matches` optionnels : viennent normalement de la table de
    config résolue (config_resolver.resolve_table_config), avec le défaut
    codé ici comme repli si aucune brique ne définit encore ces clés."""
    normalized = {str(h).strip().lower() for h in headers}
    ref = set(patterns) if patterns else LEDGER_HEADER_PATTERNS
    return len(normalized & ref) >= min_matches


def looks_like_grouped_account_blocks(tables, min_blocks=2):
    """Vrai si l'onglet ressemble à une collection de blocs de compte groupés
    (pas un grand livre unique) : au moins `min_blocks` lignes, réparties dans
    les tables de l'onglet, de la forme [code_compte (2-6 chiffres), libellé, total].
    Générique - détecte la forme, pas un onglet particulier."""
    n = 0
    for t in tables:
        for r in [t.headers] + t.rows:
            cells = [str(c).strip() for c in r]
            if not cells:
                continue
            code = _norm_code(cells[0])
            if code and len(cells) > 2 and cells[1] and _try_float(cells[2]) is not None:
                n += 1
                if n >= min_blocks:
                    return True
    return False


CODE_RE = re.compile(r'^\d{2,6}$')
DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})')
SKIP_LABELS = {'ran', 'solde', ''}


def _try_float(cell):
    s = str(cell).strip().replace(',', '.')
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _try_date(cell):
    m = DATE_RE.match(str(cell).strip())
    if not m:
        return None
    try:
        return datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _norm_code(cell):
    c = str(cell).strip()
    if c.endswith('.0'):
        c = c[:-2]
    return c if CODE_RE.match(c) else None


def extract_ledger_postings(compte, headers, rows, label=None, skip_labels=None):
    """Passe une table de grand livre (en-têtes Date/Libellé/Débit/Crédit/Solde,
    ordre quelconque) en liste de postings {compte, label, date, libelle, side, amount}.
    `label` (optionnel) est le nom lisible du compte, ex. tiré du nom d'onglet.
    `skip_labels` (optionnel, vient de la table de config résolue) : libellés à
    ignorer car ce sont des reports/soldes affichés, pas de vraies écritures.

    Ignore les lignes de report à nouveau (RAN) et les lignes de solde
    intermédiaire (pas de vraie écriture, juste un total affiché).
    """
    skip = set(skip_labels) if skip_labels is not None else SKIP_LABELS
    normalized = [str(h).strip().lower() for h in headers]
    col = {}
    for i, h in enumerate(normalized):
        if h in ('date',):
            col.setdefault('date', i)
        elif h in ('libellé', 'libelle'):
            col.setdefault('libelle', i)
        elif h in ('débit', 'debit'):
            col.setdefault('debit', i)
        elif h in ('crédit', 'credit'):
            col.setdefault('credit', i)

    if 'date' not in col or 'debit' not in col or 'credit' not in col:
        return []

    postings = []
    for r in rows:
        cells = [str(c).strip() for c in r]
        libelle = cells[col['libelle']] if 'libelle' in col and col['libelle'] < len(cells) else ''
        if libelle.strip().lower() in skip:
            continue
        d = _try_date(cells[col['date']]) if col['date'] < len(cells) else None
        if d is None:
            continue
        debit = _try_float(cells[col['debit']]) if col['debit'] < len(cells) else None
        credit = _try_float(cells[col['credit']]) if col['credit'] < len(cells) else None
        if debit:
            postings.append({'compte': compte, 'label': label or compte, 'date': d, 'libelle': libelle, 'side': 'debit', 'amount': round(debit, 2)})
        elif credit:
            postings.append({'compte': compte, 'label': label or compte, 'date': d, 'libelle': libelle, 'side': 'credit', 'amount': round(credit, 2)})
    return postings


def extract_grouped_expense_postings(tables, block_total_tolerance=0.02, partial_block_policy='keep_with_flag'):
    """Passe la liste des tables d'un même onglet au format 'bloc de compte groupé' :
    une ligne d'en-tête [code, libellé, total, ...] (le code peut manquer sur les
    tables de continuation - alors le libellé, déjà vu ailleurs dans le classeur,
    permet de retrouver le compte), suivie de lignes [date, montant, commentaire].
    Générique - aucun mot-clé métier, seulement position/forme des cellules.

    Chaque bloc (une table, un compte) est comparé au total qu'il déclare lui-même
    (tolérance `block_total_tolerance`, normalement résolue depuis une Rule -
    voir config_resolver.resolve_table_config - le défaut ici n'est qu'un repli).
    Si la somme est INFÉRIEURE au total déclaré (souvent un montant budgété pas
    encore totalement réalisé) : `partial_block_policy='keep_with_flag'` (défaut)
    garde les lignes réellement observées et signale l'écart dans `uncertain` ;
    `'discard'` retrouve l'ancien comportement tout-ou-rien. Si la somme est
    SUPÉRIEURE au total déclaré (signe probable d'un rattachement erroné), le
    bloc est toujours écarté, quelle que soit la politique - jamais de
    suppositions sur de vrais chiffres.

    Retourne (postings, uncertain_rows).
    """
    label_to_account = {}
    for t in tables:
        rows = [t.headers] + t.rows
        for r in rows:
            cells = [str(c).strip() for c in r]
            code = _norm_code(cells[0]) if cells else None
            if code and len(cells) > 1 and cells[1]:
                label_to_account[cells[1].strip().lower()] = code

    postings = []
    uncertain_rows = []

    for t in tables:
        rows = [t.headers] + t.rows
        blocks = []  # (compte, label, expected_total, [raw_rows])
        current = None
        for r in rows:
            cells = [str(c).strip() for c in r]
            if not cells:
                continue
            code = _norm_code(cells[0])
            if code:
                label = cells[1].strip() if len(cells) > 1 else ''
                current = [code, label, _try_float(cells[2]) if len(cells) > 2 else None, []]
                blocks.append(current)
                continue
            if cells[0] and _try_float(cells[0]) is None and _try_date(cells[0]) is None and len(cells) > 1:
                maybe_total = _try_float(cells[1])
                if maybe_total is not None:
                    label = cells[0].strip()
                    acct = label_to_account.get(label.lower())
                    if acct:
                        current = [acct, label, maybe_total, []]
                        blocks.append(current)
                        continue
            if current is not None:
                current[3].append(cells)
            else:
                uncertain_rows.append({'raw': cells, 'reason': 'aucun compte de rattachement identifié'})

        for compte, label, expected_total, raw_rows in blocks:
            block_postings = []
            for cells in raw_rows:
                d, amount, comment = None, None, ''
                for c in cells:
                    if d is None:
                        dd = _try_date(c)
                        if dd:
                            d = dd
                            continue
                    if amount is None:
                        fv = _try_float(c)
                        if fv is not None:
                            amount = fv
                            continue
                for c in reversed(cells):
                    if c and _try_date(c) is None and _try_float(c) is None:
                        comment = c
                        break
                if d is not None and amount is not None:
                    side = 'debit' if amount >= 0 else 'credit'
                    block_postings.append({
                        'compte': compte, 'label': label or compte, 'date': d,
                        'libelle': comment or label or compte, 'side': side, 'amount': round(abs(amount), 2),
                    })
            if expected_total is None:
                postings.extend(block_postings)
                uncertain_rows.append({'raw': [compte] + raw_rows, 'reason': 'pas de total déclaré pour validation (lignes tout de même retenues)'})
                continue
            got_total = round(sum(
                p['amount'] if p['side'] == 'debit' else -p['amount'] for p in block_postings
            ), 2)
            ecart = round(expected_total - got_total, 2)
            if abs(ecart) <= block_total_tolerance:
                # Total déclaré == somme des lignes : confiance maximale.
                postings.extend(block_postings)
            elif (expected_total >= 0) == (ecart > 0) or got_total == 0:
                # La somme extraite est INFÉRIEURE (en valeur) au total déclaré : chaque
                # ligne reste un fait observé dans la source, rien n'est inventé - seule
                # une partie du total déclaré (souvent un montant budgété/prévisionnel,
                # pas encore entièrement réalisé) manque à l'appel.
                if partial_block_policy == 'keep_with_flag':
                    postings.extend(block_postings)
                uncertain_rows.append({
                    'compte': compte, 'attendu': expected_total, 'obtenu': got_total,
                    'raw': raw_rows,
                    'reason': f'lignes {"retenues mais incomplètes" if partial_block_policy == "keep_with_flag" else "écartées (bloc incomplet)"} : {ecart:+.2f} manquant vs le total déclaré (probable montant budgété non totalement réalisé)',
                })
            else:
                # La somme extraite DÉPASSE le total déclaré : signe probable d'une
                # ligne mal rattachée à ce compte - on ne retient rien de ce bloc.
                uncertain_rows.append({
                    'compte': compte, 'attendu': expected_total, 'obtenu': got_total,
                    'raw': raw_rows, 'reason': 'somme extraite > total déclaré - rattachement suspect, bloc écarté',
                })

    return postings, uncertain_rows


def match_exact_pairs(postings, window_days=60):
    """Passe 1 : appariement débit<->crédit exact, comptes différents, même montant,
    dans une fenêtre de jours (tolère les décalages d'encaissement)."""
    debits = sorted([p for p in postings if p['side'] == 'debit'], key=lambda x: x['date'])
    credits = sorted([p for p in postings if p['side'] == 'credit'], key=lambda x: x['date'])
    used_credit = set()
    transactions, unmatched_debits = [], []

    for dpost in debits:
        best, best_dist = None, None
        for ci, c in enumerate(credits):
            if ci in used_credit or c['compte'] == dpost['compte']:
                continue
            if round(c['amount'], 2) != round(dpost['amount'], 2):
                continue
            dist = abs((c['date'] - dpost['date']).days)
            if dist > window_days:
                continue
            if best is None or dist < best_dist:
                best, best_dist = ci, dist
        if best is not None:
            used_credit.add(best)
            c = credits[best]
            transactions.append(_make_transaction(dpost, c, 'exact', best_dist))
        else:
            unmatched_debits.append(dpost)

    unmatched_credits = [credits[i] for i in range(len(credits)) if i not in used_credit]
    return transactions, unmatched_debits, unmatched_credits


def match_aggregate_same_day(unmatched_debits, unmatched_credits, amount_tolerance=0.5):
    """Passe 2, générique : pour chaque jour, regarde si un GROUPE d'écritures non
    affectées d'un même compte (ou de comptes de la même famille - même préfixe PCG)
    a une somme qui correspond à une écriture non affectée ailleurs, ce même jour.
    Aucun mot-clé de libellé - uniquement date + somme."""
    transactions = []
    used_debit_ids, used_credit_ids = set(), set()

    by_date_debit = defaultdict(list)
    for i, d in enumerate(unmatched_debits):
        by_date_debit[(d['date'], d['compte'][:3])].append(i)

    for (date_key, family), idxs in by_date_debit.items():
        if len(idxs) < 2:
            continue
        total = round(sum(unmatched_debits[i]['amount'] for i in idxs), 2)
        for ci, c in enumerate(unmatched_credits):
            if ci in used_credit_ids or c['date'] != date_key:
                continue
            if abs(c['amount'] - total) > amount_tolerance:
                continue
            used_credit_ids.add(ci)
            for i in idxs:
                used_debit_ids.add(i)
                d = unmatched_debits[i]
                transactions.append(_make_transaction(
                    d, c, 'aggregate', 0,
                    aggregate_amount=round(d['amount'] / total * c['amount'], 2) if total else d['amount']
                ))
            break

    # symétrique : groupes de crédits non affectés dont la somme matche un débit isolé
    by_date_credit = defaultdict(list)
    for i, c in enumerate(unmatched_credits):
        if i in used_credit_ids:
            continue
        by_date_credit[(c['date'], c['compte'][:3])].append(i)

    for (date_key, family), idxs in by_date_credit.items():
        if len(idxs) < 2:
            continue
        total = round(sum(unmatched_credits[i]['amount'] for i in idxs), 2)
        for di, d in enumerate(unmatched_debits):
            if di in used_debit_ids or d['date'] != date_key:
                continue
            if abs(d['amount'] - total) > amount_tolerance:
                continue
            used_debit_ids.add(di)
            for i in idxs:
                used_credit_ids.add(i)
                c = unmatched_credits[i]
                transactions.append(_make_transaction(
                    d, c, 'aggregate', 0,
                    aggregate_amount=round(c['amount'] / total * d['amount'], 2) if total else c['amount']
                ))
            break

    remaining_debits = [unmatched_debits[i] for i in range(len(unmatched_debits)) if i not in used_debit_ids]
    remaining_credits = [unmatched_credits[i] for i in range(len(unmatched_credits)) if i not in used_credit_ids]
    return transactions, remaining_debits, remaining_credits


def match_group_to_group_same_day(unmatched_debits, unmatched_credits, amount_tolerance=0.02):
    """Passe 3, générique : généralise la passe 2 au cas où la contrepartie
    elle-même se répartit sur PLUSIEURS comptes (ex. un appel de fonds réparti
    sur plusieurs lignes budgétaires, chacune vers son propre compte). Pour un
    jour donné, si la somme de tous les débits non affectés d'une même famille
    de compte égale exactement la somme de TOUS les crédits non affectés ce
    même jour (peu importe leurs comptes), regroupe le tout en une seule
    écriture multi-jambes équilibrée. Toujours date + somme exacte, jamais de
    mot-clé de libellé."""
    transactions = []
    used_debit_ids, used_credit_ids = set(), set()

    by_date_family_debit = defaultdict(list)
    for i, d in enumerate(unmatched_debits):
        by_date_family_debit[(d['date'], d['compte'][:3])].append(i)

    by_date_credit = defaultdict(list)
    for i, c in enumerate(unmatched_credits):
        by_date_credit[c['date']].append(i)

    for (date_key, family), d_idxs in by_date_family_debit.items():
        if len(d_idxs) < 2 or any(i in used_debit_ids for i in d_idxs):
            continue
        c_idxs = [i for i in by_date_credit.get(date_key, []) if i not in used_credit_ids]
        if not c_idxs:
            continue
        total_d = round(sum(unmatched_debits[i]['amount'] for i in d_idxs), 2)
        total_c = round(sum(unmatched_credits[i]['amount'] for i in c_idxs), 2)
        if abs(total_d - total_c) > amount_tolerance:
            continue

        legs = []
        for i in d_idxs:
            used_debit_ids.add(i)
            p = unmatched_debits[i]
            legs.append({'compte': p['compte'], 'label': p['label'], 'amount': p['amount']})
        for i in c_idxs:
            used_credit_ids.add(i)
            p = unmatched_credits[i]
            legs.append({'compte': p['compte'], 'label': p['label'], 'amount': -p['amount']})

        libelle = max((unmatched_debits[i]['libelle'] for i in d_idxs), key=len, default='')
        transactions.append({
            'date': date_key.isoformat(),
            'libelle': libelle,
            'legs': legs,
            'method': 'group',
            'date_gap_days': 0,
        })

    remaining_debits = [unmatched_debits[i] for i in range(len(unmatched_debits)) if i not in used_debit_ids]
    remaining_credits = [unmatched_credits[i] for i in range(len(unmatched_credits)) if i not in used_credit_ids]
    return transactions, remaining_debits, remaining_credits


def _make_transaction(debit, credit, method, date_gap, aggregate_amount=None):
    amount = aggregate_amount if aggregate_amount is not None else debit['amount']
    return {
        'date': min(debit['date'], credit['date']).isoformat(),
        'libelle': debit['libelle'] if len(debit['libelle']) >= len(credit['libelle']) else credit['libelle'],
        'legs': [
            {'compte': debit['compte'], 'label': debit['label'], 'amount': amount},
            {'compte': credit['compte'], 'label': credit['label'], 'amount': -amount},
        ],
        'method': method,
        'date_gap_days': date_gap,
    }


def reconcile(postings):
    """Point d'entrée : postings -> (transactions, non_affectés).

    Ordre des passes délibéré : les passes groupées (compte exact + fenêtre de
    jours) tournent AVANT l'appariement glouton par paires. Testé et confirmé
    nécessaire (2026-07-20/21, test_copro.xlsx) : dans l'ordre inverse, la passe
    par paires "vole" parfois une ligne d'un groupe légitime (ex. deux appels de
    charges du même jour/compte formant ensemble le vrai montant réglé) via une
    coïncidence de montant avec un crédit sans rapport, avant que les passes
    groupées n'aient la moindre chance de reconstituer le groupe complet - ça
    dégrade le taux au lieu de l'améliorer. Faire tourner les groupes en premier
    laisse la paire simple (passe finale) nettoyer ce qui reste, sans jamais
    pouvoir démembrer un groupe déjà formé."""
    transactions, unmatched_debits, unmatched_credits = match_exact_pairs(postings)
    agg_transactions, unmatched_debits, unmatched_credits = match_aggregate_same_day(
        unmatched_debits, unmatched_credits
    )
    transactions += agg_transactions
    group_transactions, unmatched_debits, unmatched_credits = match_group_to_group_same_day(
        unmatched_debits, unmatched_credits
    )
    transactions += group_transactions
    return transactions, unmatched_debits, unmatched_credits


COMPENSATION_ACCOUNT = ('599999', 'Compensation:AVérifier')


def _ledger_leg_line(compte, label, amount):
    account_part = f"{compte}:{label}" if label and label != compte else compte
    return f"    {account_part:<45}{amount:>10.2f} EUR"


def build_ledger_entries(transactions, unmatched_debits, unmatched_credits):
    """(transactions, non_affectés) -> liste triée par date de
    {date (ISO), libelle, legs: [{compte, label, amount}], is_unmatched}.
    Les non-affectés sont individuellement équilibrés contre un compte de
    compensation, marqués 'à vérifier', pour que rien ne disparaisse
    silencieusement (ni les postings orphelins, ni la trace qu'ils n'ont pas
    pu être appariés automatiquement). Forme structurée partagée entre la
    sérialisation ledger-cli locale et l'envoi à un cœur comptable distant."""
    entries = []
    for tx in transactions:
        entries.append({'date': tx['date'], 'libelle': tx['libelle'], 'legs': tx['legs'], 'is_unmatched': False})
    for p in unmatched_debits:
        entries.append({'date': p['date'].isoformat(), 'libelle': p['libelle'], 'is_unmatched': True, 'legs': [
            {'compte': p['compte'], 'label': p['label'], 'amount': p['amount']},
            {'compte': COMPENSATION_ACCOUNT[0], 'label': COMPENSATION_ACCOUNT[1], 'amount': -p['amount']},
        ]})
    for p in unmatched_credits:
        entries.append({'date': p['date'].isoformat(), 'libelle': p['libelle'], 'is_unmatched': True, 'legs': [
            {'compte': p['compte'], 'label': p['label'], 'amount': -p['amount']},
            {'compte': COMPENSATION_ACCOUNT[0], 'label': COMPENSATION_ACCOUNT[1], 'amount': p['amount']},
        ]})
    entries.sort(key=lambda e: e['date'])
    return entries


def build_ledger_text(transactions, unmatched_debits, unmatched_credits, header_comment=None):
    """Sérialise (transactions, non_affectés) au format ledger-cli (usage local/debug -
    l'écriture réelle dans coeur_comptable passe par build_ledger_entries + /api/ledger/import,
    pas par ce texte)."""
    lines = []
    if header_comment:
        for line in header_comment.strip('\n').split('\n'):
            lines.append(f"; {line}")
        lines.append('')

    for entry in build_ledger_entries(transactions, unmatched_debits, unmatched_credits):
        date_ledger = entry['date'].replace('-', '/')
        suffix = "  ; à vérifier - non apparié automatiquement" if entry['is_unmatched'] else ""
        lines.append(f"\n{date_ledger} * {entry['libelle']}{suffix}")
        for leg in entry['legs']:
            lines.append(_ledger_leg_line(leg['compte'], leg['label'], leg['amount']))

    return '\n'.join(lines) + '\n'
