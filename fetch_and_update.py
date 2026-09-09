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
from datetime import datetime
from collections import defaultdict

BASE_SOCRATA_URL = 'https://agtransport.usda.gov/resource/wnn7-29tu.json'
BASE_FAS_URL = 'https://apps.fas.usda.gov/esrqs/api/reports/WeeklyHistorialReportData'

# Directories and files
REPO_DIR = Path(__file__).parent.resolve()
PAYLOAD_FILE = REPO_DIR / "multi_commodity_payload.json"
TEMPLATE_FILE = REPO_DIR / "dashboard_template.html"
INDEX_FILE = REPO_DIR / "index.html"

# SMTP & Notification Config
ENV_FILE = Path(r"C:\Users\guang\.gemini\antigravity\scratch\farmdoc_agent\.env")
local_config = {}
if ENV_FILE.exists():
    try:
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
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
    """Queries USDA APIs to discover the latest week ending date available online."""
    # 1. Socrata Soybeans check
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

    # 2. Fallback to FAS ESRQS API (Soybean Meal ID 15)
    try:
        url_fas = f'{BASE_FAS_URL}?WeekEndingDate=08/27/2026&CommodityId=15'
        req_fas = urllib.request.Request(url_fas, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://apps.fas.usda.gov/esrqs/'})
        with urllib.request.urlopen(req_fas, timeout=15) as resp:
            fas_data = json.loads(resp.read().decode('utf-8'))
            if fas_data:
                return fas_data[-1]['weekEndingDate'][:10]
    except Exception as e:
        print(f"[WARN] FAS ESRQS date check failed: {e}")

    return None


def fetch_socrata_week_records(commodity_name, date_str):
    """Fetches all country records for a specific commodity and date from Socrata."""
    full_date = f"{date_str}T00:00:00.000" if len(date_str) == 10 else date_str
    query = f"""
    SELECT date, myear, my, country,
           totcommcmy, outsalescmy, accexportscmy, netsalescmy,
           outsalesnmy, netsalesnmy
    WHERE commodity = '{commodity_name}' AND date = '{full_date}'
    LIMIT 5000
    """
    params = {'$query': query}
    url = BASE_SOCRATA_URL + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


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


def update_grain_commodity(comm_key, comm_name, new_date, payload):
    """Updates Corn, Soybeans, or Wheat for the new week."""
    recs = fetch_socrata_week_records(comm_name, new_date)
    if not recs:
        print(f"[WARN] No Socrata records found for {comm_name} on {new_date}")
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
    rem_tot = max(0.0, tot_commit - top10_tot - unknown_data['tot_cmy'])
    rem_acc = max(0.0, tot_acc - top10_acc - unknown_data['acc_cmy'])
    rem_out = max(0.0, tot_out - top10_out - unknown_data['out_cmy'])
    rem_net_cmy = tot_net_cmy - top10_net_cmy - unknown_data['net_cmy']
    rem_net_nmy = tot_net_nmy - top10_net_nmy - unknown_data['net_nmy']
    rem_out_nmy = max(0.0, tot_out_nmy - top10_out_nmy - unknown_data['out_nmy'])

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
        if not any(p.get('date') == new_date for p in pts):
            pts.append({
                'date': new_date,
                'mnt': round(tot_commit / 1e6, 4),
                'kmt': round(tot_commit / 1e3, 1),
                'tot_kmt': round(tot_commit / 1e3, 1),
                'acc_kmt': round(tot_acc / 1e3, 1),
                'out_kmt': round(tot_out / 1e3, 1),
                'net_kmt': round(tot_net_cmy / 1e3, 1)
            })

    print(f"[SUCCESS] Updated {comm_name} with release {new_date}: Total Commit = {tot_commit/1e3:,.1f} k MT")


def update_processed_commodity(comm_key, cid, comm_name, new_date, payload):
    """Updates Soybean Meal or Soybean Oil from FAS ESRQS."""
    rec = fetch_fas_record(cid, new_date)
    if not rec:
        print(f"[WARN] No FAS record found for {comm_name} on {new_date}")
        return

    acc_mt = float(rec.get('accumulatedExport') or 0)
    out_mt = float(rec.get('outstandingSales') or 0)
    tot_mt = acc_mt + out_mt
    net_mt = float(rec.get('netSales') or 0)
    out_nmy_mt = float(rec.get('nextYearOutstandingSales') or 0)
    net_nmy_mt = float(rec.get('nextYearNetSales') or 0)

    pkg = payload[comm_key]
    old_rows = pkg.get('table_8rows', [])
    if old_rows and old_rows[-1].get('is_total'):
        old_tot_mt = old_rows[-1]['tot_cmy']
        scale_ratio = (tot_mt / old_tot_mt) if old_tot_mt > 0 else 1.0
        scale_nmy = (out_nmy_mt / old_rows[-1]['out_nmy']) if old_rows[-1].get('out_nmy', 0) > 0 else 1.0

        for r in old_rows[:-1]:
            r['acc_cmy'] = round(r['acc_cmy'] * scale_ratio)
            r['out_cmy'] = round(r['out_cmy'] * scale_ratio)
            r['tot_cmy'] = round(r['tot_cmy'] * scale_ratio)
            r['out_nmy'] = round(r.get('out_nmy', 0) * scale_nmy)

        old_rows[-1]['acc_cmy'] = acc_mt
        old_rows[-1]['out_cmy'] = out_mt
        old_rows[-1]['tot_cmy'] = tot_mt
        old_rows[-1]['net_cmy'] = net_mt
        old_rows[-1]['out_nmy'] = out_nmy_mt
        old_rows[-1]['net_nmy'] = net_nmy_mt

    pkg['latest_date'] = new_date

    # Append to total curve
    active_year = '2026-27' if new_date >= '2026-10-01' else '2025-26'
    total_curves = pkg['data']['total']['curves']
    if active_year in total_curves:
        pts = total_curves[active_year]
        if not any(p.get('date') == new_date for p in pts):
            pts.append({
                'date': new_date,
                'mnt': round(tot_mt / 1e6, 4),
                'kmt': round(tot_mt / 1e3, 1),
                'tot_kmt': round(tot_mt / 1e3, 1),
                'acc_kmt': round(acc_mt / 1e3, 1),
                'out_kmt': round(out_mt / 1e3, 1),
                'net_kmt': round(net_mt / 1e3, 1)
            })

    print(f"[SUCCESS] Updated {comm_name} with release {new_date}: Total Commit = {tot_mt/1e3:,.1f} k MT")


def recalculate_pacing_tracker(payload):
    """Recalculates USDA Pacing and Weekly Run-Rates for all commodities."""
    for cKey, item in payload.items():
        if 'usda_pacing' not in item or 'table_8rows' not in item:
            continue
        total_row = item['table_8rows'][-1]
        
        # 1. 2026/27 New Crop Pace
        nmy = item['usda_pacing'].get('nmy_2026_27')
        if nmy:
            # Commitments = new crop outstanding
            commit_kmt = round(total_row['out_nmy'] / 1e3, 1)
            tgt_kmt = nmy['target_kmt']
            pct = round((commit_kmt / tgt_kmt * 100), 1) if tgt_kmt > 0 else 0.0
            nmy['commitments_kmt'] = commit_kmt
            nmy['commitments_mnt'] = round(commit_kmt / 1e3, 3)
            nmy['commitments_pct'] = pct
            
            rem_weeks = nmy.get('remaining_weeks', 52)
            rem_sales_kmt = max(0.0, tgt_kmt - commit_kmt)
            req_sales = round(rem_sales_kmt / rem_weeks, 1) if rem_weeks > 0 else 0.0
            req_ship = round(tgt_kmt / 52.0, 1)
            
            nmy['req_weekly_sales_pace_kmt'] = req_sales
            nmy['req_weekly_sales_pace_mnt'] = round(req_sales / 1e3, 3)
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

    comm_configs = [
        ('soybeans', 'Soybeans', '🌿', 'MY 2025/2026 Closeout & 2026/27 Forward Sales'),
        ('corn', 'Corn', '🌽', 'MY 2025/2026 Closeout & 2026/27 Forward Sales'),
        ('wheat', 'Wheat', '🌾', 'MY 2026/2027 Active Season (Week 13)'),
        ('meal', 'Soybean Meal', '📦', 'MY 2025/2026 Closeout & 2026/27 Forward Sales'),
        ('oil', 'Soybean Oil', '🫗', 'MY 2025/2026 Closeout & 2026/27 Forward Sales'),
    ]

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
        print(f"No update required. Exiting cleanly.")
        return

    active_date = online_date if is_new_release else current_date
    if is_new_release:
        print(f"[UPDATE] New USDA release detected: {active_date}! Pulling new data...")
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
        print(f"[INFO] Force update requested for date {active_date}.")
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
