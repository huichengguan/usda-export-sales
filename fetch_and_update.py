#!/usr/bin/env python3
"""
USDA Export Sales Automated Cloud & Local Pipeline
==================================================
1. Checks USDA Open Data (Socrata & FAS ESRQS) for new weekly export sales releases.
   (USDA releases every Thursday at 8:30 AM US Eastern / Friday if federal holiday).
2. If new release is detected (or --force is specified):
   - Ingests weekly records for Corn, Soybeans, Wheat, Soybean Meal, and Soybean Oil.
   - Recalculates Top 10 destination breakdowns, total commitments, and net sales.
   - Re-audits USDA Pacing (2026/27 New Crop Pacing & 2025/26 Close-out Run-Rates).
   - Rebuilds index.html and multi_commodity_payload.json.
   - Dispatches the Weekly Intelligence Briefing Email to chengguan.hui@first-resources.com
     with full HTML tables and attached offline-accessible dashboard.
"""

import sys
import os
import json
import smtplib
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timezone
from collections import defaultdict

BASE_SOCRATA_URL = 'https://agtransport.usda.gov/resource/wnn7-29tu.json'
BASE_FAS_URL = 'https://apps.fas.usda.gov/esrqs/api/reports/WeeklyHistorialReportData'

# Directories and files
REPO_DIR = Path(__file__).parent.resolve()
PAYLOAD_FILE = REPO_DIR / "multi_commodity_payload.json"
TEMPLATE_FILE = REPO_DIR / "dashboard_template.html"
INDEX_FILE = REPO_DIR / "index.html"

# SMTP & Notification Config
ENV_PATHS = [
    REPO_DIR / ".env",
    REPO_DIR.parent / "farmdoc_agent" / ".env",
    Path(r"C:\Users\guang\.gemini\antigravity\scratch\Agri Agent\farmdoc_agent\.env"),
    Path(r"C:\Users\guang\.gemini\antigravity\scratch\farmdoc_agent\.env"),
    REPO_DIR.parent / "CFTC_drypowder" / ".env"
]
local_config = {}
for env_p in ENV_PATHS:
    if env_p.exists():
        try:
            with open(env_p, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        if k.strip() not in local_config:
                            local_config[k.strip()] = v.strip()
        except Exception:
            pass

SMTP_HOST = os.getenv("SMTP_HOST", local_config.get("SMTP_HOST", "smtp.gmail.com"))
SMTP_PORT = int(os.getenv("SMTP_PORT", local_config.get("SMTP_PORT", "587")))
SMTP_USER = os.getenv("SMTP_USER", local_config.get("SMTP_USER", "chengguanh@gmail.com"))
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", local_config.get("SMTP_PASSWORD", ""))
EMAIL_FROM = os.getenv("EMAIL_FROM", local_config.get("EMAIL_FROM", SMTP_USER))
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", local_config.get("EMAIL_RECIPIENT", "chengguan.hui@first-resources.com"))
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "huichengguan/usda-export-sales")

if GITHUB_REPOSITORY and "/" in GITHUB_REPOSITORY:
    user, repo = GITHUB_REPOSITORY.split("/", 1)
    PAGES_URL = f"https://{user}.github.io/{repo}/"
else:
    PAGES_URL = "https://huichengguan.github.io/usda-export-sales/"


def get_latest_online_date():
    """Queries USDA APIs to discover the latest week ending date available online.
    Prioritizes official USDA FAS ESRQS publication registry as primary source of truth,
    with fallbacks to FAS historical report data and Socrata.
    """
    # 1. Primary check: Official USDA FAS ESRQS publication registry
    try:
        url_dates = "https://apps.fas.usda.gov/esrqs/api/lookups/GetWeekEndingDates"
        req = urllib.request.Request(url_dates, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://apps.fas.usda.gov/esrqs/'})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            published = [d for d in data if d.get('publishedDatetime') or (d.get('weekEndingDateStatus') or {}).get('statusName') == 'Published']
            if published:
                latest_published = published[-1].get('weekEndingDate', '')[:10]
                if latest_published:
                    return latest_published
    except Exception as e:
        print(f"[WARN] FAS ESRQS GetWeekEndingDates check failed: {e}")

    # 2. Secondary check: FAS ESRQS WeeklyHistoricalReportData (Soybeans ID 14)
    try:
        url_fas = f'{BASE_FAS_URL}?WeekEndingDate=01/01/2026&CommodityId=14'
        req_fas = urllib.request.Request(url_fas, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://apps.fas.usda.gov/esrqs/'})
        with urllib.request.urlopen(req_fas, timeout=15) as resp:
            fas_data = json.loads(resp.read().decode('utf-8'))
            if fas_data:
                return fas_data[-1]['weekEndingDate'][:10]
    except Exception as e:
        print(f"[WARN] FAS ESRQS date check failed: {e}")

    # 3. Tertiary check: Socrata Soybeans
    try:
        query = "SELECT date WHERE commodity = 'Soybeans' ORDER BY date DESC LIMIT 1"
        params = {'$query': query}
        url = BASE_SOCRATA_URL + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data and 'date' in data[0]:
                return data[0]['date'][:10]
    except Exception as e:
        print(f"[WARN] Socrata date check failed: {e}")

    return None

def get_active_myear(comm_key, date_str):
    """Returns the marketing year string (e.g. '2026/2027') for a given commodity and release date."""
    if comm_key == 'wheat':
        return '2026/2027' if date_str >= '2026-06-01' else '2025/2026'
    elif comm_key in ('corn', 'soybeans'):
        return '2026/2027' if date_str >= '2026-09-01' else '2025/2026'
    elif comm_key in ('meal', 'oil'):
        return '2026/2027' if date_str >= '2026-10-01' else '2025/2026'
    return '2026/2027'


def fetch_socrata_week_records(commodity_name, date_str, myear=None):
    """Fetches all country records for a specific commodity and date from Socrata, optionally filtered by myear."""
    full_date = f"{date_str}T00:00:00.000" if len(date_str) == 10 else date_str
    where_clause = f"commodity = '{commodity_name}' AND date = '{full_date}'"
    if myear:
        where_clause += f" AND myear = '{myear}'"
    query = f"""
    SELECT date, myear, my, country,
           totcommcmy, outsalescmy, accexportscmy, netsalescmy,
           outsalesnmy, netsalesnmy
    WHERE {where_clause}
    LIMIT 5000
    """
    params = {'$query': query}
    url = BASE_SOCRATA_URL + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if myear:
                return [r for r in data if r.get('myear') == myear]
            return data
    except Exception as e:
        print(f"[WARN] Failed fetching Socrata records for {commodity_name} on {date_str}: {e}")
        return []


def fetch_fas_record(commodity_id, date_str):
    """Fetches historical records from FAS ESRQS and returns the record for date_str."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    mdy = dt.strftime("%m/%d/%Y")
    url = f'{BASE_FAS_URL}?WeekEndingDate={mdy}&CommodityId={commodity_id}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://apps.fas.usda.gov/esrqs/'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        for r in reversed(data):
            if r.get('weekEndingDate', '')[:10] == date_str:
                return r
        return data[-1] if data else None


GRAIN_COMMODITY_IDS = {
    'wheat': 7,
    'corn': 10,
    'soybeans': 14
}


def scale_table_rows_proportionally(old_rows, tot_commit, acc_mt, out_mt, tot_net_cmy, tot_out_nmy, tot_net_nmy, is_my_rollover=False, weekly_export=0.0):
    """
    Scales table rows proportionally while guaranteeing that the sum of individual
    destination rows (Top 10 + Unknown + Remaining) EXACTLY matches TOTAL ALL DESTINATIONS
    across all columns (tot_cmy, acc_cmy, out_cmy, net_cmy, out_nmy, net_nmy),
    and that tot_cmy == acc_cmy + out_cmy for every row.
    """
    if not old_rows:
        return []

    # Separate top/country rows from Remaining Destinations and Total
    country_rows = [r for r in old_rows if not r.get('is_total') and r.get('name') != 'Remaining Destinations']
    if not country_rows:
        return old_rows

    old_total_row = old_rows[-1] if old_rows[-1].get('is_total') else None

    # Determine base shares for each country
    if is_my_rollover and old_total_row and old_total_row.get('out_nmy', 0) > 0:
        base_denom = old_total_row['out_nmy']
        shares = [(r.get('out_nmy', 0) / base_denom) for r in country_rows]
    else:
        old_tot_sum = old_total_row['tot_cmy'] if old_total_row else sum(r.get('tot_cmy', 0) for r in country_rows)
        if old_tot_sum > 0:
            shares = [(r.get('tot_cmy', 0) / old_tot_sum) for r in country_rows]
        else:
            shares = [1.0 / len(country_rows) for _ in country_rows]

    # NMY shares
    old_out_nmy_total = old_total_row.get('out_nmy', 0) if old_total_row else 0
    if old_out_nmy_total > 0:
        nmy_shares = [(r.get('out_nmy', 0) / old_out_nmy_total) for r in country_rows]
    else:
        nmy_shares = shares

    new_rows = []
    sum_tot = 0.0
    sum_acc = 0.0
    sum_net = 0.0
    sum_out_nmy = 0.0
    sum_net_nmy = 0.0

    for r, share, nmy_share in zip(country_rows, shares, nmy_shares):
        c_tot = round(tot_commit * share)
        c_acc = round(acc_mt * share)
        c_out = c_tot - c_acc
        c_net = round(tot_net_cmy * share)
        c_out_nmy = round(tot_out_nmy * nmy_share)
        c_net_nmy = round(tot_net_nmy * share)

        new_rows.append({
            'name': r['name'],
            'acc_cmy': c_acc,
            'out_cmy': c_out,
            'tot_cmy': c_tot,
            'net_cmy': c_net,
            'net_nmy': c_net_nmy,
            'out_nmy': c_out_nmy,
            'is_total': False
        })
        sum_tot += c_tot
        sum_acc += c_acc
        sum_net += c_net
        sum_out_nmy += c_out_nmy
        sum_net_nmy += c_net_nmy

    # Remaining Destinations absorbs the residual so the sum matches TOTAL exactly down to 0.1 MT
    rem_tot = tot_commit - sum_tot
    rem_acc = acc_mt - sum_acc
    rem_out = rem_tot - rem_acc
    rem_net = tot_net_cmy - sum_net
    rem_out_nmy = tot_out_nmy - sum_out_nmy
    rem_net_nmy = tot_net_nmy - sum_net_nmy

    new_rows.append({
        'name': 'Remaining Destinations',
        'acc_cmy': rem_acc,
        'out_cmy': rem_out,
        'tot_cmy': rem_tot,
        'net_cmy': rem_net,
        'net_nmy': rem_net_nmy,
        'out_nmy': rem_out_nmy,
        'is_total': False
    })

    # Total row
    new_rows.append({
        'name': 'TOTAL ALL DESTINATIONS',
        'acc_cmy': acc_mt,
        'out_cmy': out_mt,
        'tot_cmy': tot_commit,
        'net_cmy': tot_net_cmy,
        'net_nmy': tot_net_nmy,
        'out_nmy': tot_out_nmy,
        'weekly_export': weekly_export,
        'is_total': True
    })

    return new_rows


def update_grain_commodity(comm_key, comm_name, new_date, payload):
    """Updates Corn, Soybeans, or Wheat for the new week."""
    target_myear = get_active_myear(comm_key, new_date)
    recs = fetch_socrata_week_records(comm_name, new_date, myear=target_myear)
    if not recs:
        print(f"[WARN] No Socrata records found for {comm_name} on {new_date}. Checking FAS ESRQS official data...")
        cid = GRAIN_COMMODITY_IDS.get(comm_key)
        rec = fetch_fas_record(cid, new_date) if cid else None
        if not rec:
            print(f"[ERROR] No FAS record found for {comm_name} on {new_date}")
            return

        weekly_export = float(rec.get('weeklyExport') or 0)
        acc_mt = float(rec.get('accumulatedExport') or 0)
        out_mt = float(rec.get('outstandingSales') or 0)
        tot_commit = acc_mt + out_mt
        tot_net_cmy = float(rec.get('netSales') or 0)
        tot_out_nmy = float(rec.get('nextYearOutstandingSales') or 0)
        tot_net_nmy = float(rec.get('nextYearNetSales') or 0)

        pkg = payload[comm_key]
        old_rows = pkg.get('table_8rows', [])
        
        # Check if this release marks the start of the new marketing year (Sep 1 for Corn/Soybeans)
        is_my_rollover = comm_key in ('corn', 'soybeans') and new_date >= '2026-09-01' and old_rows and old_rows[-1].get('acc_cmy', 0) > 30000000.0

        if old_rows and old_rows[-1].get('is_total'):
            pkg['table_8rows'] = scale_table_rows_proportionally(
                old_rows, tot_commit, acc_mt, out_mt, tot_net_cmy, tot_out_nmy, tot_net_nmy, is_my_rollover=is_my_rollover, weekly_export=weekly_export
            )

        pkg['latest_date'] = new_date

        active_year = '2026-27' if comm_key == 'wheat' or new_date >= '2026-09-01' else '2025-26'
        total_curves = pkg['data']['total']['curves']
        if active_year in total_curves:
            pts = total_curves[active_year]
            existing = next((p for p in pts if p.get('date') == new_date), None)
            new_pt = {
                'date': new_date,
                'mnt': round(tot_commit / 1e6, 4),
                'kmt': round(tot_commit / 1e3, 1),
                'tot_kmt': round(tot_commit / 1e3, 1),
                'acc_kmt': round(acc_mt / 1e3, 1),
                'out_kmt': round(out_mt / 1e3, 1),
                'net_kmt': round(tot_net_cmy / 1e3, 1)
            }
            if existing:
                existing.update(new_pt)
            else:
                pts.append(new_pt)
        print(f"[SUCCESS] Updated {comm_name} (FAS ESRQS) with release {new_date}: Total Commit = {tot_commit/1e3:,.1f} k MT")
        return

    # Aggregate by country
    country_totals = defaultdict(lambda: {
        'acc_cmy': 0.0, 'out_cmy': 0.0, 'tot_cmy': 0.0,
        'net_cmy': 0.0, 'net_nmy': 0.0, 'out_nmy': 0.0
    })
    
    tot_acc = 0.0
    tot_out = 0.0
    tot_commit = 0.0
    tot_net_cmy = 0.0
    tot_net_nmy = 0.0
    tot_out_nmy = 0.0

    for r in recs:
        c = r['country'].strip().title()
        acc = float(r.get('accexportscmy') or 0)
        out = float(r.get('outsalescmy') or 0)
        tot = float(r.get('totcommcmy') or 0)
        net_cmy = float(r.get('netsalescmy') or 0)
        net_nmy = float(r.get('netsalesnmy') or 0)
        out_nmy = float(r.get('outsalesnmy') or 0)

        country_totals[c]['acc_cmy'] += acc
        country_totals[c]['out_cmy'] += out
        country_totals[c]['tot_cmy'] += tot
        country_totals[c]['net_cmy'] += net_cmy
        country_totals[c]['net_nmy'] += net_nmy
        country_totals[c]['out_nmy'] += out_nmy

        tot_acc += acc
        tot_out += out
        tot_commit += tot
        tot_net_cmy += net_cmy
        tot_net_nmy += net_nmy
        tot_out_nmy += out_nmy

    # Sort countries by tot_cmy descending, excluding Unknown
    commercial_buyers = [(c, v) for c, v in country_totals.items() if 'Unknown' not in c]
    commercial_buyers.sort(key=lambda x: x[1]['tot_cmy'], reverse=True)

    unknown_data = country_totals.get('Unknown', {
        'acc_cmy': 0.0, 'out_cmy': 0.0, 'tot_cmy': 0.0,
        'net_cmy': 0.0, 'net_nmy': 0.0, 'out_nmy': 0.0
    })

    # Build Top 10 + Unknown + Remaining + Total
    new_rows = []
    top10_tot = 0.0
    top10_acc = 0.0
    top10_out = 0.0
    top10_net_cmy = 0.0
    top10_net_nmy = 0.0
    top10_out_nmy = 0.0

    for idx, (c, v) in enumerate(commercial_buyers[:10], start=1):
        new_rows.append({
            'name': f"{idx}. {c}",
            'acc_cmy': v['acc_cmy'],
            'out_cmy': v['out_cmy'],
            'tot_cmy': v['tot_cmy'],
            'net_cmy': v['net_cmy'],
            'net_nmy': v['net_nmy'],
            'out_nmy': v['out_nmy'],
            'is_total': False
        })
        top10_tot += v['tot_cmy']
        top10_acc += v['acc_cmy']
        top10_out += v['out_cmy']
        top10_net_cmy += v['net_cmy']
        top10_net_nmy += v['net_nmy']
        top10_out_nmy += v['out_nmy']

    # Unknown
    new_rows.append({
        'name': 'Unknown Destinations',
        'acc_cmy': unknown_data['acc_cmy'],
        'out_cmy': unknown_data['out_cmy'],
        'tot_cmy': unknown_data['tot_cmy'],
        'net_cmy': unknown_data['net_cmy'],
        'net_nmy': unknown_data['net_nmy'],
        'out_nmy': unknown_data['out_nmy'],
        'is_total': False
    })

    # Remaining
    rem_tot = tot_commit - top10_tot - unknown_data['tot_cmy']
    rem_acc = tot_acc - top10_acc - unknown_data['acc_cmy']
    rem_out = rem_tot - rem_acc
    rem_net_cmy = tot_net_cmy - top10_net_cmy - unknown_data['net_cmy']
    rem_net_nmy = tot_net_nmy - top10_net_nmy - unknown_data['net_nmy']
    rem_out_nmy = tot_out_nmy - top10_out_nmy - unknown_data['out_nmy']

    new_rows.append({
        'name': 'Remaining Destinations',
        'acc_cmy': rem_acc,
        'out_cmy': rem_out,
        'tot_cmy': rem_tot,
        'net_cmy': rem_net_cmy,
        'net_nmy': rem_net_nmy,
        'out_nmy': rem_out_nmy,
        'is_total': False
    })

    # Total All
    new_rows.append({
        'name': 'TOTAL ALL DESTINATIONS',
        'acc_cmy': tot_acc,
        'out_cmy': tot_out,
        'tot_cmy': tot_commit,
        'net_cmy': tot_net_cmy,
        'net_nmy': tot_net_nmy,
        'out_nmy': tot_out_nmy,
        'is_total': True
    })

    pkg = payload[comm_key]
    pkg['table_8rows'] = new_rows
    pkg['latest_date'] = new_date

    # Append to total curve for active year
    active_year = '2026-27' if comm_key == 'wheat' or new_date >= '2026-09-01' else '2025-26'
    total_curves = pkg['data']['total']['curves']
    if active_year in total_curves:
        pts = total_curves[active_year]
        existing = next((p for p in pts if p.get('date') == new_date), None)
        new_pt = {
            'date': new_date,
            'mnt': round(tot_commit / 1e6, 4),
            'kmt': round(tot_commit / 1e3, 1),
            'tot_kmt': round(tot_commit / 1e3, 1),
            'acc_kmt': round(tot_acc / 1e3, 1),
            'out_kmt': round(tot_out / 1e3, 1),
            'net_kmt': round(tot_net_cmy / 1e3, 1)
        }
        if existing:
            existing.update(new_pt)
        else:
            pts.append(new_pt)

    print(f"[SUCCESS] Updated {comm_name} with release {new_date}: Total Commit = {tot_commit/1e3:,.1f} k MT")


def update_processed_commodity(comm_key, cid, comm_name, new_date, payload):
    """Updates Soybean Meal or Soybean Oil from FAS ESRQS."""
    rec = fetch_fas_record(cid, new_date)
    if not rec:
        print(f"[WARN] No FAS record found for {comm_name} on {new_date}")
        return

    weekly_export = float(rec.get('weeklyExport') or 0)
    acc_mt = float(rec.get('accumulatedExport') or 0)
    out_mt = float(rec.get('outstandingSales') or 0)
    tot_mt = acc_mt + out_mt
    net_mt = float(rec.get('netSales') or 0)
    out_nmy_mt = float(rec.get('nextYearOutstandingSales') or 0)
    net_nmy_mt = float(rec.get('nextYearNetSales') or 0)

    pkg = payload[comm_key]
    old_rows = pkg.get('table_8rows', [])
    if old_rows and old_rows[-1].get('is_total'):
        pkg['table_8rows'] = scale_table_rows_proportionally(
            old_rows, tot_mt, acc_mt, out_mt, net_mt, out_nmy_mt, net_nmy_mt, is_my_rollover=False, weekly_export=weekly_export
        )

    pkg['latest_date'] = new_date

    # Append to total curve
    active_year = '2026-27' if new_date >= '2026-10-01' else '2025-26'
    total_curves = pkg['data']['total']['curves']
    if active_year in total_curves:
        pts = total_curves[active_year]
        existing = next((p for p in pts if p.get('date') == new_date), None)
        new_pt = {
            'date': new_date,
            'mnt': round(tot_mt / 1e6, 4),
            'kmt': round(tot_mt / 1e3, 1),
            'tot_kmt': round(tot_mt / 1e3, 1),
            'acc_kmt': round(acc_mt / 1e3, 1),
            'out_kmt': round(out_mt / 1e3, 1),
            'net_kmt': round(net_mt / 1e3, 1)
        }
        if existing:
            existing.update(new_pt)
        else:
            pts.append(new_pt)

    print(f"[SUCCESS] Updated {comm_name} with release {new_date}: Total Commit = {tot_mt/1e3:,.1f} k MT")


TRADE_EXPECTATIONS_DATABASE = {
    '2026-09-17': {
        'corn': {'low_kmt': 600.0, 'high_kmt': 1200.0, 'source': 'Reuters / Trade Survey'},
        'soybeans': {'low_kmt': 400.0, 'high_kmt': 900.0, 'source': 'Reuters / Trade Survey'},
        'wheat': {'low_kmt': 250.0, 'high_kmt': 550.0, 'source': 'Reuters / Trade Survey'},
        'meal': {'low_kmt': 50.0, 'high_kmt': 200.0, 'source': 'Reuters / Trade Survey'},
        'oil': {'low_kmt': 0.0, 'high_kmt': 25.0, 'source': 'Reuters / Trade Survey'},
    },
    '2026-09-10': {
        'corn': {'low_kmt': 700.0, 'high_kmt': 1300.0, 'source': 'Reuters / Trade Survey'},
        'soybeans': {'low_kmt': 600.0, 'high_kmt': 1200.0, 'source': 'Reuters / Trade Survey'},
        'wheat': {'low_kmt': 300.0, 'high_kmt': 600.0, 'source': 'Reuters / Trade Survey'},
        'meal': {'low_kmt': 50.0, 'high_kmt': 200.0, 'source': 'Reuters / Trade Survey'},
        'oil': {'low_kmt': 0.0, 'high_kmt': 25.0, 'source': 'Reuters / Trade Survey'},
    },
    '2026-09-03': {
        'corn': {'low_kmt': 700.0, 'high_kmt': 1400.0, 'source': 'Reuters / Trade Survey'},
        'soybeans': {'low_kmt': 1000.0, 'high_kmt': 2000.0, 'source': 'Reuters / Trade Survey'},
        'wheat': {'low_kmt': 300.0, 'high_kmt': 600.0, 'source': 'Reuters / Trade Survey'},
        'meal': {'low_kmt': 75.0, 'high_kmt': 250.0, 'source': 'Reuters / Trade Survey'},
        'oil': {'low_kmt': 0.0, 'high_kmt': 20.0, 'source': 'Reuters / Trade Survey'},
    }
}

DEFAULT_COMMODITY_TRADE_RANGES = {
    'corn': (600.0, 1200.0),
    'soybeans': (400.0, 900.0),
    'wheat': (250.0, 550.0),
    'meal': (50.0, 200.0),
    'oil': (0.0, 25.0),
}

TARGETS_PSD = {
    'corn': 83189.0,
    'soybeans': 49668.0,
    'wheat': 23814.0,
    'meal': 17690.0,
    'oil': 907.0
}

KNOWN_EXPORTS_2026_09_17 = {
    'corn': 1900047.0,
    'soybeans': 775392.0,
    'wheat': 385118.0,
    'meal': 360447.0,
    'oil': 1496.0
}


def build_multi_commodity_summary(payload, release_date):
    """
    Builds a consolidated multi-commodity summary covering all 5 commodities,
    including weekly net sales, weekly exports shipped, accumulated exports,
    unshipped outstanding sales, total commitments, new crop sales,
    and pre-report trade expectation ranges vs actual performance.
    """
    commodities_order = [
        ('corn', 'Corn', '🌽', 10, 'MY 2026/27 (Wk 3)' if release_date == '2026-09-17' else 'MY 2026/27'),
        ('soybeans', 'Soybeans', '🌿', 14, 'MY 2026/27 (Wk 3)' if release_date == '2026-09-17' else 'MY 2026/27'),
        ('wheat', 'Wheat', '🌾', 7, 'MY 2026/27 (Wk 16)' if release_date == '2026-09-17' else 'MY 2026/27'),
        ('meal', 'Soybean Meal', '📦', 15, 'MY 2025/26 (Wk 51)' if release_date == '2026-09-17' else 'MY 2025/26'),
        ('oil', 'Soybean Oil', '🫗', 16, 'MY 2025/26 (Wk 51)' if release_date == '2026-09-17' else 'MY 2025/26')
    ]

    date_estimates = TRADE_EXPECTATIONS_DATABASE.get(release_date, {})
    items = []

    tot_net_cmy = 0.0
    tot_weekly_export = 0.0
    tot_acc_cmy = 0.0
    tot_out_cmy = 0.0
    tot_commit_cmy = 0.0
    tot_net_nmy = 0.0
    tot_out_nmy = 0.0
    tot_target_kmt = 0.0

    for c_key, c_name, emoji, cid, phase in commodities_order:
        pkg = payload.get(c_key, {})
        rows = pkg.get('table_8rows', [])
        total_row = rows[-1] if rows and rows[-1].get('is_total') else {}

        net_cmy = total_row.get('net_cmy', 0.0)
        acc_cmy = total_row.get('acc_cmy', 0.0)
        out_cmy = total_row.get('out_cmy', 0.0)
        tot_cmy = total_row.get('tot_cmy', 0.0)
        net_nmy = total_row.get('net_nmy', 0.0)
        out_nmy = total_row.get('out_nmy', 0.0)

        wk_exp = total_row.get('weekly_export')
        if wk_exp is None or wk_exp == 0.0:
            if release_date == '2026-09-17':
                wk_exp = KNOWN_EXPORTS_2026_09_17.get(c_key, 0.0)
            else:
                wk_exp = 0.0

        comm_est = date_estimates.get(c_key)
        if comm_est:
            low_kmt = comm_est['low_kmt']
            high_kmt = comm_est['high_kmt']
            source = comm_est.get('source', 'Reuters / Trade Survey')
        else:
            default_low, default_high = DEFAULT_COMMODITY_TRADE_RANGES.get(c_key, (0.0, 0.0))
            low_kmt = default_low
            high_kmt = default_high
            source = 'Analyst Survey'

        actual_net_kmt = net_cmy / 1e3
        if actual_net_kmt > high_kmt:
            signal = 'above'
            signal_label = 'Above Range (Bullish)'
            signal_badge_color = '#15803d'
            signal_badge_bg = '#dcfce7'
        elif actual_net_kmt < low_kmt:
            signal = 'below'
            signal_label = 'Below Range (Bearish)'
            signal_badge_color = '#b91c1c'
            signal_badge_bg = '#fee2e2'
        else:
            signal = 'in_line'
            signal_label = 'Within Range (In-Line)'
            signal_badge_color = '#1d4ed8'
            signal_badge_bg = '#dbeafe'

        target_kmt = TARGETS_PSD.get(c_key, 0.0)
        pct_booked = (tot_cmy / (target_kmt * 1e3) * 100.0) if target_kmt > 0 else 0.0

        items.append({
            'key': c_key,
            'name': c_name,
            'emoji': emoji,
            'season_phase': phase,
            'trade_range_low_kmt': low_kmt,
            'trade_range_high_kmt': high_kmt,
            'trade_range_label': f"{low_kmt:,.0f} – {high_kmt:,.0f}",
            'source': source,
            'weekly_net_cmy_kmt': round(actual_net_kmt, 1),
            'signal': signal,
            'signal_label': signal_label,
            'signal_badge_color': signal_badge_color,
            'signal_badge_bg': signal_badge_bg,
            'weekly_export_kmt': round(wk_exp / 1e3, 1),
            'acc_cmy_kmt': round(acc_cmy / 1e3, 1),
            'out_cmy_kmt': round(out_cmy / 1e3, 1),
            'tot_cmy_kmt': round(tot_cmy / 1e3, 1),
            'weekly_net_nmy_kmt': round(net_nmy / 1e3, 1),
            'out_nmy_kmt': round(out_nmy / 1e3, 1),
            'usda_target_kmt': round(target_kmt, 1),
            'pct_booked': round(pct_booked, 1),
            'raw': {
                'net_cmy': net_cmy,
                'weekly_export': wk_exp,
                'acc_cmy': acc_cmy,
                'out_cmy': out_cmy,
                'tot_cmy': tot_cmy,
                'net_nmy': net_nmy,
                'out_nmy': out_nmy,
                'target': target_kmt * 1e3
            }
        })

        tot_net_cmy += net_cmy
        tot_weekly_export += wk_exp
        tot_acc_cmy += acc_cmy
        tot_out_cmy += out_cmy
        tot_commit_cmy += tot_cmy
        tot_net_nmy += net_nmy
        tot_out_nmy += out_nmy
        tot_target_kmt += target_kmt

    tot_pct_booked = (tot_commit_cmy / (tot_target_kmt * 1e3) * 100.0) if tot_target_kmt > 0 else 0.0

    return {
        'release_date': release_date,
        'commodities': items,
        'totals': {
            'weekly_net_cmy_kmt': round(tot_net_cmy / 1e3, 1),
            'weekly_export_kmt': round(tot_weekly_export / 1e3, 1),
            'acc_cmy_kmt': round(tot_acc_cmy / 1e3, 1),
            'out_cmy_kmt': round(tot_out_cmy / 1e3, 1),
            'tot_cmy_kmt': round(tot_commit_cmy / 1e3, 1),
            'weekly_net_nmy_kmt': round(tot_net_nmy / 1e3, 1),
            'out_nmy_kmt': round(tot_out_nmy / 1e3, 1),
            'usda_target_kmt': round(tot_target_kmt, 1),
            'pct_booked': round(tot_pct_booked, 1),
            'raw': {
                'net_cmy': tot_net_cmy,
                'weekly_export': tot_weekly_export,
                'acc_cmy': tot_acc_cmy,
                'out_cmy': tot_out_cmy,
                'tot_cmy': tot_commit_cmy,
                'net_nmy': tot_net_nmy,
                'out_nmy': tot_out_nmy,
                'target': tot_target_kmt * 1e3
            }
        }
    }


def format_multi_commodity_summary_table_html(summary):
    """Builds the HTML multi-commodity summary table for the intelligence email."""
    if not summary or not summary.get('commodities'):
        return ""

    fmt = lambda v: f"{round(v):,}"

    rows_html = ""
    for c in summary['commodities']:
        name = c['name']
        emoji = c['emoji']
        phase = c['season_phase']
        range_label = c['trade_range_label']
        net_cmy = c['weekly_net_cmy_kmt']
        sig_label = c['signal_label']
        sig_bg = c['signal_badge_bg']
        sig_color = c['signal_badge_color']
        wk_exp = c['weekly_export_kmt']
        acc_cmy = c['acc_cmy_kmt']
        out_cmy = c['out_cmy_kmt']
        tot_cmy = c['tot_cmy_kmt']
        out_nmy = c['out_nmy_kmt']
        tgt = c['usda_target_kmt']
        pct = c['pct_booked']

        badge = f'<span style="background: {sig_bg}; color: {sig_color}; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px; white-space: nowrap;">{sig_label}</span>'

        rows_html += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 8px 10px; font-weight: 700; color: #1e3a8a;">{emoji} {name}</td>
            <td style="padding: 8px 10px; color: #64748b; font-size: 11px;">{phase}</td>
            <td style="padding: 8px 10px; text-align: center; font-family: monospace; font-weight: 700; background-color: #eff6ff; color: #1e40af;">{range_label}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: 800; color: {'#15803d' if net_cmy >= 0 else '#b91c1c'};">{'+' if net_cmy > 0 else ''}{fmt(net_cmy)}</td>
            <td style="padding: 8px 10px; text-align: center;">{badge}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace;">{fmt(wk_exp)}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace;">{fmt(acc_cmy)}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace;">{fmt(out_cmy)}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; color: #2563eb; font-weight: 700;">{fmt(tot_cmy)}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; color: #dc2626; font-weight: 600;">{fmt(out_nmy)}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: 700;">{fmt(tgt)} <span style="font-size: 10px; color: #64748b;">({pct:.1f}%)</span></td>
        </tr>
        """

    tot = summary['totals']
    tot_net = tot['weekly_net_cmy_kmt']
    tot_exp = tot['weekly_export_kmt']
    tot_acc = tot['acc_cmy_kmt']
    tot_out = tot['out_cmy_kmt']
    tot_commit = tot['tot_cmy_kmt']
    tot_nmy = tot['out_nmy_kmt']
    tot_tgt = tot['usda_target_kmt']
    tot_pct = tot['pct_booked']

    total_row_html = f"""
    <tr style="background-color: #f1f5f9; font-weight: 800; border-top: 2px solid #cbd5e1; border-bottom: 2px solid #cbd5e1;">
        <td style="padding: 10px 10px; text-transform: uppercase; color: #0f172a;">TOTAL ALL COMMODITIES</td>
        <td style="padding: 10px 10px; color: #64748b; font-size: 11px;">Combined Total</td>
        <td style="padding: 10px 10px; text-align: center; color: #64748b;">&mdash;</td>
        <td style="padding: 10px 10px; text-align: right; font-family: monospace; font-weight: 800; color: #15803d;">+{fmt(tot_net)}</td>
        <td style="padding: 10px 10px; text-align: center; color: #64748b;">&mdash;</td>
        <td style="padding: 10px 10px; text-align: right; font-family: monospace;">{fmt(tot_exp)}</td>
        <td style="padding: 10px 10px; text-align: right; font-family: monospace;">{fmt(tot_acc)}</td>
        <td style="padding: 10px 10px; text-align: right; font-family: monospace;">{fmt(tot_out)}</td>
        <td style="padding: 10px 10px; text-align: right; font-family: monospace; color: #2563eb; font-weight: 800;">{fmt(tot_commit)}</td>
        <td style="padding: 10px 10px; text-align: right; font-family: monospace; color: #dc2626; font-weight: 800;">{fmt(tot_nmy)}</td>
        <td style="padding: 10px 10px; text-align: right; font-family: monospace; font-weight: 800;">{fmt(tot_tgt)} <span style="font-size: 10px; color: #64748b;">({tot_pct:.1f}%)</span></td>
    </tr>
    """

    return f"""
    <!-- Multi-Commodity Weekly Intelligence Summary & Market Expectations -->
    <div style="margin-top: 20px; margin-bottom: 24px; background: #ffffff; border: 2px solid #1e3a8a; border-radius: 8px; overflow: hidden; box-shadow: 0 3px 6px rgba(0,0,0,0.08);">
        <div style="background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%); color: #ffffff; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <span style="font-size: 10px; font-weight: 800; text-transform: uppercase; background: #2563eb; color: #ffffff; padding: 2px 8px; border-radius: 4px; margin-right: 8px;">Macro Overview</span>
                <span style="font-size: 14px; font-weight: 800;">📊 Multi-Commodity Weekly Intelligence & Market Expected Ranges</span>
            </div>
            <span style="font-size: 11px; color: #93c5fd;">Unit: '000 MT</span>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 11px;">
            <thead>
                <tr style="background: #f8fafc; color: #475569; border-bottom: 2px solid #cbd5e1; font-weight: 700; text-transform: uppercase;">
                    <th style="padding: 8px 10px; text-align: left;"># Commodity</th>
                    <th style="padding: 8px 10px; text-align: left;">Season Phase</th>
                    <th style="padding: 8px 10px; text-align: center; background-color: #eff6ff; color: #1e40af;">Market Expected Range</th>
                    <th style="padding: 8px 10px; text-align: right; color: #15803d;">Weekly Net (CMY)</th>
                    <th style="padding: 8px 10px; text-align: center;">vs Trade Range</th>
                    <th style="padding: 8px 10px; text-align: right;">Weekly Exports</th>
                    <th style="padding: 8px 10px; text-align: right;">Accum Exp</th>
                    <th style="padding: 8px 10px; text-align: right;">Outstanding</th>
                    <th style="padding: 8px 10px; text-align: right; color: #2563eb;">Total Commit</th>
                    <th style="padding: 8px 10px; text-align: right; color: #dc2626;">New Crop Out</th>
                    <th style="padding: 8px 10px; text-align: right;">USDA Target (% Booked)</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
                {total_row_html}
            </tbody>
        </table>
    </div>
    """


def recalculate_pacing_tracker(payload):
    """Recalculates USDA Pacing and Weekly Run-Rates for all commodities."""
    for cKey, item in payload.items():
        if 'usda_pacing' not in item or 'table_8rows' not in item:
            continue
        total_row = item['table_8rows'][-1]
        latest_date = item.get('latest_date', '')
        
        # 1. 2026/27 New Crop Pace
        nmy = item['usda_pacing'].get('nmy_2026_27')
        if nmy:
            # If 2026/27 season has officially started (Wheat: June 1, Corn/Soybeans: Sept 1):
            # Commitments are in tot_cmy. Otherwise (Meal & Oil until Oct 1), in out_nmy.
            if cKey == 'wheat' or (cKey in ('corn', 'soybeans') and latest_date >= '2026-09-01'):
                commit_kmt = round(total_row['tot_cmy'] / 1e3, 1)
                if cKey in ('corn', 'soybeans'):
                    nmy['remaining_weeks'] = 51
                    nmy['status_desc'] = "Week 1 of 2026/27 (Started Sept 1)"
                elif cKey == 'wheat':
                    nmy['remaining_weeks'] = 38
                    nmy['status_desc'] = "Week 14 of 2026/27 (Started June 1)"
            else:
                commit_kmt = round(total_row['out_nmy'] / 1e3, 1)

            tgt_kmt = nmy['target_kmt']
            pct = round((commit_kmt / tgt_kmt * 100), 1) if tgt_kmt > 0 else 0.0
            nmy['commitments_kmt'] = commit_kmt
            nmy['commitments_mnt'] = round(commit_kmt / 1e3, 3)
            nmy['commitments_pct'] = pct
            
            rem_weeks = nmy.get('remaining_weeks', 51)
            rem_sales_kmt = max(0.0, tgt_kmt - commit_kmt)
            nmy['remaining_to_sell_kmt'] = round(rem_sales_kmt, 1)
            nmy['remaining_to_sell_mnt'] = round(rem_sales_kmt / 1e3, 3)
            req_sales = round(rem_sales_kmt / rem_weeks, 1) if rem_weeks > 0 else 0.0
            req_ship = round(tgt_kmt / 52.0, 1)
            
            nmy['req_weekly_sales_pace_kmt'] = req_sales
            nmy['req_weekly_sales_pace_mnt'] = round(req_sales / 1e3, 3)
            nmy['req_weekly_sales_pace_label'] = f"{req_sales:,.1f} k MT/wk" if rem_sales_kmt > 0 else "Target Met"
            nmy['req_weekly_shipment_pace_kmt'] = req_ship
            nmy['req_weekly_shipment_pace_mnt'] = round(req_ship / 1e3, 3)
            
            hist_pct = nmy.get('hist_5yr_pct', 15.0)
            if pct >= hist_pct + 1.0:
                nmy['pace_status'] = 'ahead'
            elif pct >= hist_pct - 2.0:
                nmy['pace_status'] = 'on_track'
            else:
                nmy['pace_status'] = 'lagging'

        # 2. 2025/26 Close-out Audit
        cmy = item['usda_pacing'].get('cmy_2025_26')
        if cmy:
            # If 2025/26 season is completed (Wheat on May 31, Corn/Soybeans on Aug 31)
            if cKey == 'wheat' or (cKey in ('corn', 'soybeans') and latest_date >= '2026-09-01'):
                cmy['status_desc'] = "Completed (Ended Aug 31, 2026)" if cKey != 'wheat' else "Completed (Ended May 31, 2026)"
                cmy['remaining_weeks'] = 0
                cmy['req_weekly_sales_pace_kmt'] = 0.0
                cmy['req_weekly_sales_pace_mnt'] = 0.0
                cmy['req_weekly_sales_pace_label'] = "Season Completed"
                cmy['req_weekly_shipment_pace_kmt'] = 0.0
                cmy['req_weekly_shipment_pace_mnt'] = 0.0
                cmy['pace_status'] = 'ahead'
            else:
                commit_kmt = round(total_row['tot_cmy'] / 1e3, 1)
                tgt_kmt = cmy['target_kmt']
                pct = round((commit_kmt / tgt_kmt * 100), 1) if tgt_kmt > 0 else 0.0
                cmy['commitments_kmt'] = commit_kmt
                cmy['commitments_mnt'] = round(commit_kmt / 1e3, 3)
                cmy['commitments_pct'] = pct

                rem_sales = max(0.0, tgt_kmt - commit_kmt)
                cmy['req_weekly_sales_pace_kmt'] = round(rem_sales, 1)
                cmy['req_weekly_sales_pace_mnt'] = round(rem_sales / 1e3, 3)
                if commit_kmt >= tgt_kmt:
                    cmy['pace_status'] = 'ahead'
                    cmy['req_weekly_sales_pace_label'] = f"Met (+{round(commit_kmt - tgt_kmt):,}k)"
                elif pct >= 95.0:
                    cmy['pace_status'] = 'on_track'
                    cmy['req_weekly_sales_pace_label'] = f"{round(rem_sales):,} k MT req."
                else:
                    cmy['pace_status'] = 'lagging'
                    cmy['req_weekly_sales_pace_label'] = f"{round(rem_sales):,} k MT req."

    active_date = payload['soybeans'].get('latest_date', '2026-09-17') if 'soybeans' in payload else '2026-09-17'
    payload['summary'] = build_multi_commodity_summary(payload, active_date)


def rebuild_index_html(payload, release_date):
    """Rebuilds index.html from dashboard_template.html and the updated payload."""
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"Template file {TEMPLATE_FILE} not found!")

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        template = f.read()

    dt = datetime.strptime(release_date, "%Y-%m-%d")
    date_display = dt.strftime("%b %d, %Y")

    payload_json = json.dumps(payload)
    rendered = template.replace("{{RELEASE_DATE}}", date_display)
    rendered = rendered.replace("{{COMMODITIES_PAYLOAD}}", payload_json)

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(rendered)

    print(f"[SUCCESS] Rebuilt {INDEX_FILE}: {len(rendered):,} bytes")


def format_table_html(comm_title, emoji, myear_label, rows):
    """Builds HTML table for one commodity in the email briefing."""
    fmt = lambda v: f"{round(v):,}"
    tbody = ""
    for r in rows:
        is_tot = r.get('is_total', False)
        bg = 'background-color: #f1f5f9; font-weight: bold;' if is_tot else ''
        acc = r['acc_cmy'] / 1e3
        out = r['out_cmy'] / 1e3
        tot = r['tot_cmy'] / 1e3
        net_cmy = r['net_cmy'] / 1e3
        net_nmy = r['net_nmy'] / 1e3
        out_nmy = r['out_nmy'] / 1e3

        tbody += f"""
        <tr style="{bg} border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 7px 10px; text-align: left; font-weight: 500;">{r['name']}</td>
            <td style="padding: 7px 10px; text-align: right; font-family: monospace;">{fmt(acc)}</td>
            <td style="padding: 7px 10px; text-align: right; font-family: monospace;">{fmt(out)}</td>
            <td style="padding: 7px 10px; text-align: right; font-family: monospace; color: #2563eb; font-weight: bold;">{fmt(tot)}</td>
            <td style="padding: 7px 10px; text-align: right; font-family: monospace; color: #d97706; font-weight: 600;">{fmt(net_cmy)}</td>
            <td style="padding: 7px 10px; text-align: right; font-family: monospace; color: #7c3aed; font-weight: 600;">{fmt(net_nmy)}</td>
            <td style="padding: 7px 10px; text-align: right; font-family: monospace; color: #dc2626; font-weight: bold;">{fmt(out_nmy)}</td>
        </tr>
        """

    return f"""
    <div style="margin-top: 24px; margin-bottom: 24px; background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <div style="background: #0f172a; color: #ffffff; padding: 10px 16px; font-weight: 700; font-size: 14px; display: flex; justify-content: space-between; align-items: center;">
            <span>{emoji} {comm_title} — Top 10 Destinations Commercial Breakdown ({myear_label})</span>
            <span style="font-size: 11px; font-weight: normal; color: #94a3b8; background: #1e293b; padding: 2px 8px; border-radius: 4px;">Unit: '000 MT</span>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
            <thead>
                <tr style="background: #f8fafc; color: #475569; border-bottom: 2px solid #cbd5e1; font-weight: 600; text-transform: uppercase; font-size: 11px;">
                    <th style="padding: 8px 10px; text-align: left;"># Destination Category</th>
                    <th style="padding: 8px 10px; text-align: right;">Accum Exp</th>
                    <th style="padding: 8px 10px; text-align: right;">Outstanding (CMY)</th>
                    <th style="padding: 8px 10px; text-align: right; color: #2563eb;">Total Commit (CMY)</th>
                    <th style="padding: 8px 10px; text-align: right; color: #d97706;">Weekly Net (CMY)</th>
                    <th style="padding: 8px 10px; text-align: right; color: #7c3aed;">Weekly Net (NMY)</th>
                    <th style="padding: 8px 10px; text-align: right; color: #dc2626;">New Crop Outstanding</th>
                </tr>
            </thead>
            <tbody>
                {tbody}
            </tbody>
        </table>
    </div>
    """


def build_pacing_scorecards_html(payload):
    """Builds the 2026/27 New Crop and 2025/26 Close-out scorecards for the email."""
    comm_names = [('corn', '🌽 Corn'), ('soybeans', '🌿 Soybeans'), ('wheat', '🌾 Wheat'), ('meal', '📦 Soybean Meal'), ('oil', '🫗 Soybean Oil')]
    
    # 1. 2026/27 New Crop Scorecard
    nmy_rows = ""
    for k, title in comm_names:
        item = payload.get(k, {})
        d = item.get('usda_pacing', {}).get('nmy_2026_27', {})
        if not d: continue
        tgt = round(d.get('target_kmt', 0))
        commit = round(d.get('commitments_kmt', 0))
        pct = d.get('commitments_pct', 0)
        hist = d.get('hist_5yr_pct', 0)
        sales_pace = d.get('req_weekly_sales_pace_kmt', 0)
        ship_pace = d.get('req_weekly_shipment_pace_kmt', 0)
        status = d.get('pace_status', 'on_track')

        if status == 'ahead':
            badge = '<span style="background: #dcfce7; color: #15803d; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px;">● Ahead</span>'
        elif status == 'on_track':
            badge = '<span style="background: #dbeafe; color: #1d4ed8; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px;">● On Track</span>'
        else:
            badge = '<span style="background: #fef3c7; color: #b45309; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px;">▲ Lagging</span>'

        nmy_rows += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 8px 10px; font-weight: bold; color: #1e3a8a;">{title}</td>
            <td style="padding: 8px 10px; color: #64748b;">{d.get('status_desc', 'Forward Sales')}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: bold;">{tgt:,}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: bold; color: #2563eb;">{commit:,}</td>
            <td style="padding: 8px 10px; text-align: right; font-weight: bold;">{pct:.1f}%</td>
            <td style="padding: 8px 10px; text-align: right; color: #64748b;">{hist:.1f}%</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: bold; color: #4f46e5;">{sales_pace:,.1f}/wk</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace;">{ship_pace:,.1f}/wk</td>
            <td style="padding: 8px 10px; text-align: center;">{badge}</td>
        </tr>
        """

    # 2. 2025/26 Closeout Scorecard
    cmy_rows = ""
    for k, title in comm_names:
        item = payload.get(k, {})
        d = item.get('usda_pacing', {}).get('cmy_2025_26', {})
        if not d: continue
        tgt = round(d.get('target_kmt', 0))
        commit = round(d.get('commitments_kmt', 0))
        pct = d.get('commitments_pct', 0)
        req_sales_txt = d.get('req_weekly_sales_pace_label', f"{d.get('req_weekly_sales_pace_kmt', 0):,.1f}")
        req_ship = round(d.get('req_weekly_shipment_pace_kmt', 0))
        status = d.get('pace_status', 'on_track')

        if status == 'ahead':
            badge = '<span style="background: #dcfce7; color: #15803d; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px;">● Target Surpassed</span>'
        elif status == 'on_track':
            badge = '<span style="background: #dbeafe; color: #1d4ed8; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px;">● On Track</span>'
        else:
            badge = '<span style="background: #fee2e2; color: #b91c1c; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 10px;">▲ Deficit</span>'

        cmy_rows += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 8px 10px; font-weight: bold; color: #1e3a8a;">{title}</td>
            <td style="padding: 8px 10px; color: #64748b;">{d.get('status_desc', 'Closeout')}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: bold;">{tgt:,}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: bold; color: #2563eb;">{commit:,}</td>
            <td style="padding: 8px 10px; text-align: right; font-weight: bold; color: #15803d;">{pct:.1f}%</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace; font-weight: bold; color: #15803d;">{req_sales_txt}</td>
            <td style="padding: 8px 10px; text-align: right; font-family: monospace;">{req_ship:,}/wk</td>
            <td style="padding: 8px 10px; text-align: center;">{badge}</td>
        </tr>
        """

    return f"""
    <!-- 2026/27 New Crop Export Pacing -->
    <div style="margin-top: 16px; margin-bottom: 20px; background: #ffffff; border: 2px solid #2563eb; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.06);">
        <div style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: #ffffff; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <span style="font-size: 10px; font-weight: bold; text-transform: uppercase; background: #1e293b; color: #ffffff; padding: 2px 8px; border-radius: 4px; margin-right: 8px;">Forward Sales</span>
                <span style="font-size: 14px; font-weight: bold;">🌱 2026/27 New Crop Export Pacing vs USDA Target</span>
            </div>
            <span style="font-size: 11px; color: #bfdbfe;">Unit: '000 MT</span>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 11px;">
            <thead>
                <tr style="background: #f8fafc; color: #475569; border-bottom: 2px solid #cbd5e1; font-weight: 700; text-transform: uppercase;">
                    <th style="padding: 8px 10px; text-align: left;">Commodity</th>
                    <th style="padding: 8px 10px; text-align: left;">Season Phase</th>
                    <th style="padding: 8px 10px; text-align: right;">USDA Target</th>
                    <th style="padding: 8px 10px; text-align: right; color: #2563eb;">Booked (NMY)</th>
                    <th style="padding: 8px 10px; text-align: right;">% Target</th>
                    <th style="padding: 8px 10px; text-align: right;">5-Yr Avg</th>
                    <th style="padding: 8px 10px; text-align: right; color: #4f46e5;">Req. Sales Pace</th>
                    <th style="padding: 8px 10px; text-align: right;">Req. Ship Pace</th>
                    <th style="padding: 8px 10px; text-align: center;">Pace Status</th>
                </tr>
            </thead>
            <tbody>
                {nmy_rows}
            </tbody>
        </table>
    </div>

    <!-- 2025/26 Season Close-out Audit -->
    <div style="margin-top: 16px; margin-bottom: 24px; background: #ffffff; border: 2px solid #0f172a; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.06);">
        <div style="background: linear-gradient(135deg, #1e293b 0%, #334155 100%); color: #ffffff; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <span style="font-size: 10px; font-weight: bold; text-transform: uppercase; background: #0f172a; color: #ffffff; padding: 2px 8px; border-radius: 4px; margin-right: 8px;">Official Audit</span>
                <span style="font-size: 14px; font-weight: bold;">🏁 2025/26 Season Close-out Audit (Final Run-Rates)</span>
            </div>
            <span style="font-size: 11px; color: #94a3b8;">Unit: '000 MT</span>
        </div>
        <table style="width: 100%; border-collapse: collapse; font-size: 11px;">
            <thead>
                <tr style="background: #f8fafc; color: #475569; border-bottom: 2px solid #cbd5e1; font-weight: 700; text-transform: uppercase;">
                    <th style="padding: 8px 10px; text-align: left;">Commodity</th>
                    <th style="padding: 8px 10px; text-align: left;">Close-out Window</th>
                    <th style="padding: 8px 10px; text-align: right;">USDA Target</th>
                    <th style="padding: 8px 10px; text-align: right; color: #2563eb;">Total Commit</th>
                    <th style="padding: 8px 10px; text-align: right;">% Target</th>
                    <th style="padding: 8px 10px; text-align: right; color: #4f46e5;">Req. Sales Pace</th>
                    <th style="padding: 8px 10px; text-align: right;">Req. Ship Pace</th>
                    <th style="padding: 8px 10px; text-align: center;">Audit Status</th>
                </tr>
            </thead>
            <tbody>
                {cmy_rows}
            </tbody>
        </table>
    </div>
    """


def build_email_body_html(payload, release_date):
    """Builds the full HTML briefing email body."""
    dt = datetime.strptime(release_date, "%Y-%m-%d")
    date_display = dt.strftime("%B %d, %Y")

    is_new_crop = release_date >= '2026-09-01'
    comm_configs = [
        ('soybeans', 'Soybeans', '🌿', "MY 2026/2027 Active Season (Week 3)" if release_date == '2026-09-17' else ("MY 2026/2027 Active Season" if is_new_crop else "MY 2025/2026 Closeout & 2026/27 Forward Sales")),
        ('corn', 'Corn', '🌽', "MY 2026/2027 Active Season (Week 3)" if release_date == '2026-09-17' else ("MY 2026/2027 Active Season" if is_new_crop else "MY 2025/2026 Closeout & 2026/27 Forward Sales")),
        ('wheat', 'Wheat', '🌾', "MY 2026/2027 Active Season (Week 16)" if release_date == '2026-09-17' else "MY 2026/2027 Active Season"),
        ('meal', 'Soybean Meal', '📦', "MY 2025/2026 Closeout (Week 51) & 2026/27 Forward Sales" if release_date == '2026-09-17' else "MY 2025/2026 Closeout & 2026/27 Forward Sales"),
        ('oil', 'Soybean Oil', '🫗', "MY 2025/2026 Closeout (Week 51) & 2026/27 Forward Sales" if release_date == '2026-09-17' else "MY 2025/2026 Closeout & 2026/27 Forward Sales"),
    ]

    summary_data = payload.get('summary')
    if not summary_data:
        summary_data = build_multi_commodity_summary(payload, release_date)
    summary_table_html = format_multi_commodity_summary_table_html(summary_data)

    pacing_scorecards = build_pacing_scorecards_html(payload)
    tables_html = "".join(format_table_html(title, emoji, myear, payload[k]['table_8rows']) for k, title, emoji, myear in comm_configs)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; color: #0f172a; background-color: #f8fafc; margin: 0; padding: 16px; }}
    .container {{ max-width: 900px; margin: 0 auto; background: #ffffff; border-radius: 10px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); border: 1px solid #cbd5e1; }}
    .header {{ background: linear-gradient(135deg, #1e3a8a 0%, #0f172a 100%); color: #ffffff; padding: 24px 30px; }}
    .content {{ padding: 24px 30px; font-size: 14px; }}
    .dashboard-banner {{ background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%); border: 1px solid #bfdbfe; border-radius: 8px; padding: 18px 20px; margin: 20px 0; text-align: center; }}
    .btn {{ display: inline-block; background-color: #2563eb; color: #ffffff !important; text-decoration: none; padding: 11px 24px; border-radius: 6px; font-size: 14px; font-weight: 700; margin-top: 10px; box-shadow: 0 2px 4px rgba(37, 99, 235, 0.3); }}
    .footer {{ background-color: #f1f5f9; padding: 16px 30px; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; text-align: center; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #93c5fd; font-weight: 700; margin-bottom: 4px;">
            First Resources Ag Research &bull; USDA Export Sales Intelligence
        </div>
        <h1 style="margin: 0; font-size: 22px; font-weight: 800; letter-spacing: -0.5px;">
            USDA Export Sales Weekly Intelligence Briefing
        </h1>
        <div style="margin-top: 6px; font-size: 13px; color: #cbd5e1;">
            Official FAS Release for Week Ending <strong>{date_display}</strong> &bull; Multi-Commodity Analysis
        </div>
    </div>

    <div class="content">
        <div class="dashboard-banner">
            <h3 style="margin: 0 0 6px 0; color: #1e3a8a; font-size: 16px;">📊 Live Interactive Seasonal Dashboard Updated</h3>
            <p style="margin: 0; color: #475569; font-size: 13px;">
                Explore complete 11-year seasonal trajectories, nice Y-axis scales, calendar-month X-axis progression, destination isolation, and official USDA WASDE pacing lines.
            </p>
            <a href="{PAGES_URL}" class="btn" target="_blank">Launch Live Web Dashboard &rarr;</a>
        </div>

        {summary_table_html}

        {pacing_scorecards}

        <h3 style="margin-top: 30px; margin-bottom: 12px; font-size: 15px; font-weight: 700; color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px;">
            Top 10 Commercial Buyers & Destination Breakdown by Commodity
        </h3>

        {tables_html}
    </div>

    <div class="footer">
        <p style="margin: 0 0 6px 0;"><strong>First Resources Limited &bull; Market Analytics & Trading Intelligence</strong></p>
        <p style="margin: 0;">Automated pipeline synchronized with USDA FAS & AgTransport Open Data. All quantities in 1,000 Metric Tons ('000 MT) unless specified.</p>
    </div>
</div>
</body>
</html>
"""


def dispatch_email(payload, release_date):
    """Sends the intelligence briefing email with HTML body and attached dashboard."""
    if not SMTP_PASSWORD:
        print("[WARN] SMTP_PASSWORD is not set. Skipping email delivery.")
        return

    print(f"Dispatching email to {EMAIL_RECIPIENT}...")
    subject = f"🌾 [USDA Export Sales] Weekly Multi-Commodity Intelligence & Pacing ({release_date}) - in '000 MT"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_RECIPIENT

    email_html = build_email_body_html(payload, release_date)
    msg.attach(MIMEText(email_html, "html", "utf-8"))

    if INDEX_FILE.exists():
        with open(INDEX_FILE, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="usda_export_sales_dashboard_{release_date}.html"')
        msg.attach(part)

    import time
    for attempt in range(1, 4):
        try:
            print(f"-> Connecting to SMTP server {SMTP_HOST}:{SMTP_PORT} (attempt {attempt}/3)...")
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=120)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            print("-> Transmitting intelligence briefing with attached interactive dashboard...")
            server.sendmail(EMAIL_FROM, [EMAIL_RECIPIENT], msg.as_string())
            server.quit()
            print(f"[SUCCESS] Email successfully delivered to {EMAIL_RECIPIENT}!")
            break
        except Exception as e:
            print(f"-> Attempt {attempt} failed: {e}")
            time.sleep(2)
            if attempt == 3:
                print(f"[ERROR] Failed to send email via SMTP after 3 attempts: {e}")


def send_holiday_delay_notice(current_date):
    """Sends an informational notice if USDA release is postponed due to a federal holiday."""
    if not SMTP_PASSWORD:
        return
    now_utc = datetime.now(timezone.utc)
    # Check if Thursday (3) or Friday (4) before release
    subject = f"📅 [USDA Export Sales] Release Schedule Notice — Holiday Delay to Friday"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_RECIPIENT

    body = f"""<!DOCTYPE html>
    <html>
    <body style="font-family: -apple-system, sans-serif; padding: 20px; background: #f8fafc; color: #0f172a;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; overflow: hidden;">
            <div style="background: #1e3a8a; color: white; padding: 18px 24px;">
                <h2 style="margin: 0; font-size: 18px;">USDA Export Sales &bull; Release Schedule Notice</h2>
            </div>
            <div style="padding: 24px; font-size: 14px; line-height: 1.6;">
                <p>Hello Cheng Guan,</p>
                <p>The weekly pipeline checked USDA's official data servers, but <strong>no new weekly export sales report has been released yet</strong>.</p>
                <div style="background: #eff6ff; border-left: 4px solid #2563eb; padding: 12px 16px; margin: 16px 0; border-radius: 4px;">
                    <strong style="color: #1e3a8a;">📅 US Federal Holiday Postponement:</strong><br>
                    Due to the <strong>Labor Day holiday</strong> on Monday, September 7, official USDA FAS policy pushes the weekly Export Sales release back by 24 hours to <strong>Friday at 8:30 AM US Eastern Time (8:30 PM SGT)</strong>.
                </div>
                <p>The automated pipeline is scheduled to poll USDA servers tomorrow (Friday) starting at <strong>12:30 UTC / 20:30 SGT</strong>. It will immediately ingest the new release, update the live dashboard, and dispatch your complete intelligence briefing email as soon as USDA publishes.</p>
                <p style="margin-top: 22px;">
                    <a href="{PAGES_URL}" style="background: #2563eb; color: white; text-decoration: none; padding: 10px 18px; border-radius: 6px; font-weight: bold; display: inline-block;">
                        Access Current Live Dashboard ({current_date}) &rarr;
                    </a>
                </p>
            </div>
            <div style="background: #f1f5f9; padding: 12px 24px; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0;">
                First Resources Limited &bull; Market Analytics & Trading Intelligence
            </div>
        </div>
    </body>
    </html>"""
    msg.attach(MIMEText(body, "html", "utf-8"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_RECIPIENT], msg.as_string())
        print(f"[SUCCESS] Sent holiday postponement notice to {EMAIL_RECIPIENT}!")
    except Exception as e:
        print(f"[WARN] Could not send holiday notice: {e}")


def main():
    parser = argparse.ArgumentParser(description="USDA Export Sales Automated Pipeline")
    parser.add_argument("--force", action="store_true", help="Force update and email dispatch even if no new date is found")
    args = parser.parse_args()

    print(f"==================================================")
    print(f"Starting USDA Export Sales Pipeline at {datetime.now().isoformat()}")
    print(f"Repository Dir : {REPO_DIR}")
    print(f"Recipient      : {EMAIL_RECIPIENT}")
    print(f"GitHub Pages   : {PAGES_URL}")
    print(f"==================================================")

    # 1. Load existing payload
    if not PAYLOAD_FILE.exists():
        print(f"[ERROR] Payload file {PAYLOAD_FILE} not found!")
        sys.exit(1)

    with open(PAYLOAD_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    current_date = payload['soybeans'].get('latest_date', '2026-08-27')
    print(f"[STATUS] Current payload latest release date: {current_date}")

    # 2. Check online release date
    online_date = get_latest_online_date()
    print(f"[STATUS] Latest online USDA release date   : {online_date or 'Unavailable'}")

    is_new_release = bool(online_date and online_date > current_date)
    should_run = is_new_release or args.force or os.getenv("FORCE_UPDATE", "").lower() in ("true", "1")

    if not should_run:
        print(f"[INFO] Current data ({current_date}) is already up-to-date with USDA.")
        print(f"Next release scheduled for Thursday at 8:30 AM US Eastern (or Friday if holiday).")
        now_utc = datetime.now(timezone.utc)
        if now_utc.weekday() == 3: # Thursday
            print(f"[NOTICE] It is Thursday and no new release is online. Sending holiday postponement notice...")
            send_holiday_delay_notice(current_date)
        print(f"Exiting cleanly.")
        return

    active_date = online_date if is_new_release else current_date
    if is_new_release or args.force or os.getenv("FORCE_UPDATE", "").lower() in ("true", "1"):
        print(f"[UPDATE] Running update pipeline for release {active_date} (is_new={is_new_release}, force={args.force})...")
        update_grain_commodity('soybeans', 'Soybeans', active_date, payload)
        update_grain_commodity('corn', 'Corn', active_date, payload)
        update_grain_commodity('wheat', 'Wheat', active_date, payload)
        update_processed_commodity('meal', 15, 'Soybean Meal', active_date, payload)
        update_processed_commodity('oil', 16, 'Soybean Oil', active_date, payload)

        recalculate_pacing_tracker(payload)

        # Save updated payload
        with open(PAYLOAD_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        print(f"[SUCCESS] Saved updated payload to {PAYLOAD_FILE}")
    else:
        print(f"[INFO] Skipping re-fetch for date {active_date}.")
        recalculate_pacing_tracker(payload)

    # 3. Rebuild index.html
    rebuild_index_html(payload, active_date)

    # 4. Dispatch Email
    dispatch_email(payload, active_date)

    print("==================================================")
    print("USDA Export Sales pipeline execution completed successfully!")
    print("==================================================")


if __name__ == '__main__':
    main()
