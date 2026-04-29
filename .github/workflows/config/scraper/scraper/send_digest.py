#!/usr/bin/env python3
"""
Weekly Email Digest for Veterinary Workforce Legislative Tracker
Sends a summary email after each weekly scraper run.
"""

import os
import json
import smtplib
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "tracker.db"
OUTPUT_PATH = BASE_DIR / "frontend" / "public" / "tracker_data.json"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
RECIPIENTS = os.environ.get("DIGEST_RECIPIENTS", "").split(",")
TRACKER_URL = os.environ.get("TRACKER_URL", "https://your-tracker-url.com")


def get_new_items_since(hours: int = 168):  # 168 = 7 days
    """Pull items added in the last N hours from the database."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    bills = conn.execute("""
        SELECT * FROM bills
        WHERE first_seen_date >= ?
        ORDER BY relevance_score DESC, state ASC
    """, (cutoff,)).fetchall()

    minutes = conn.execute("""
        SELECT * FROM board_minutes
        WHERE first_seen_date >= ?
        ORDER BY relevance_score DESC, state ASC
    """, (cutoff,)).fetchall()

    conn.close()
    return [dict(b) for b in bills], [dict(m) for m in minutes]


def build_html_digest(bills: list, minutes: list) -> str:
    today = datetime.utcnow().strftime("%B %d, %Y")

    bill_rows = ""
    for b in bills[:30]:
        categories_str = ", ".join(json.loads(b.get("categories", "[]") or "[]"))
        status_color = {
            "Passed": "#16a34a",
            "Signed": "#15803d",
            "Failed": "#dc2626",
            "Introduced": "#2563eb",
            "Amended": "#d97706",
        }.get(b.get("status", ""), "#6b7280")

        bill_rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb;font-weight:600">{b['state']}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb">{b['bill_number']}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb">{b['title']}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb">
            <span style="background:{status_color};color:white;padding:2px 8px;border-radius:4px;font-size:12px">{b.get('status','')}</span>
          </td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb;font-size:13px;color:#374151">{b.get('summary_ai','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#6b7280">{categories_str}</td>
        </tr>"""

    minutes_rows = ""
    for m in minutes[:20]:
        categories_str = ", ".join(json.loads(m.get("categories", "[]") or "[]"))
        minutes_rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb;font-weight:600">{m['state']}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb">{m['board_name']}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb">{m.get('meeting_date','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb;font-size:13px;color:#374151">{m.get('summary_ai','')}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#6b7280">{categories_str}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb">
            <a href="{m.get('source_url','')}" style="color:#2563eb">View</a>
          </td>
        </tr>"""

    bills_section = f"""
    <h2 style="color:#1e3a5f;margin-top:32px">📋 New Legislative Bills ({len(bills)} this week)</h2>
    {"<p style='color:#6b7280'>No new bills found this week.</p>" if not bills else f'''
    <table style="width:100%;border-collapse:collapse;font-family:sans-serif;font-size:14px">
      <thead>
        <tr style="background:#f1f5f9">
          <th style="padding:10px;text-align:left">State</th>
          <th style="padding:10px;text-align:left">Bill #</th>
          <th style="padding:10px;text-align:left">Title</th>
          <th style="padding:10px;text-align:left">Status</th>
          <th style="padding:10px;text-align:left">AI Summary</th>
          <th style="padding:10px;text-align:left">Categories</th>
        </tr>
      </thead>
      <tbody>{bill_rows}</tbody>
    </table>'''}
    """

    minutes_section = f"""
    <h2 style="color:#1e3a5f;margin-top:32px">📝 New Board Minutes ({len(minutes)} this week)</h2>
    {"<p style='color:#6b7280'>No new board minutes found this week.</p>" if not minutes else f'''
    <table style="width:100%;border-collapse:collapse;font-family:sans-serif;font-size:14px">
      <thead>
        <tr style="background:#f1f5f9">
          <th style="padding:10px;text-align:left">State</th>
          <th style="padding:10px;text-align:left">Board</th>
          <th style="padding:10px;text-align:left">Meeting Date</th>
          <th style="padding:10px;text-align:left">AI Summary</th>
          <th style="padding:10px;text-align:left">Categories</th>
          <th style="padding:10px;text-align:left">Source</th>
        </tr>
      </thead>
      <tbody>{minutes_rows}</tbody>
    </table>'''}
    """

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Georgia,serif;max-width:900px;margin:0 auto;padding:24px;background:#f8fafc">
  <div style="background:#1e3a5f;padding:24px;border-radius:8px 8px 0 0">
    <h1 style="color:white;margin:0;font-size:22px">🐾 Veterinary Workforce Legislative Tracker</h1>
    <p style="color:#93c5fd;margin:4px 0 0 0">Weekly Digest — {today}</p>
  </div>
  <div style="background:white;padding:24px;border-radius:0 0 8px 8px;border:1px solid #e2e8f0">
    <p style="color:#374151">This week's tracker run found <strong>{len(bills)} new legislative bills</strong> and
    <strong>{len(minutes)} new board minute entries</strong> relevant to the veterinary paraprofessional workforce across all 50 states.</p>
    <p><a href="{TRACKER_URL}" style="background:#2563eb;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600">View Full Tracker →</a></p>
    {bills_section}
    {minutes_section}
    <hr style="margin-top:32px;border:none;border-top:1px solid #e5e7eb">
    <p style="color:#9ca3af;font-size:12px;margin-top:16px">
      Tracker runs weekly every Sunday at 11 PM UTC. Data sourced via LegiScan API, OpenStates API, and state veterinary board websites.
      AI summaries generated by Claude. To unsubscribe, update your DIGEST_RECIPIENTS environment variable.
    </p>
  </div>
</body>
</html>"""


def send_digest():
    if not SMTP_USER or not RECIPIENTS or not RECIPIENTS[0]:
        print("Email not configured. Set SMTP_USER, SMTP_PASS, and DIGEST_RECIPIENTS.")
        return

    bills, minutes = get_new_items_since(hours=168)
    html_body = build_html_digest(bills, minutes)
    today = datetime.utcnow().strftime("%B %d, %Y")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Vet Tracker] Weekly Digest — {today} | {len(bills)} Bills, {len(minutes)} Board Minutes"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(r.strip() for r in RECIPIENTS if r.strip())
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [r.strip() for r in RECIPIENTS if r.strip()], msg.as_string())

    print(f"Digest sent to {len(RECIPIENTS)} recipients: {len(bills)} bills, {len(minutes)} minutes")


if __name__ == "__main__":
    send_digest()
