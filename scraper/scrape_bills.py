#!/usr/bin/env python3
"""
Veterinary Workforce Legislative Tracker - v2
Bill Scraper - uses LegiScan API + Claude AI summarization
Run weekly via GitHub Actions
"""

import os
import json
import time
import logging
import hashlib
import sqlite3
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Config ───────────────────────────────────────────────────────────────────

LEGISCAN_API_KEY  = os.environ.get("LEGISCAN_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BASE_DIR    = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "state_boards.json"
DB_PATH     = BASE_DIR / "data" / "tracker.db"
OUTPUT_PATH = BASE_DIR / "frontend" / "tracker_data.json"

# Create dirs up front
(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
(BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "logs" / "scraper.log"),
    ],
)
log = logging.getLogger(__name__)

BILL_SEARCH_KEYWORDS = [
    "veterinary technician",
    "veterinary technologist",
    "veterinary nurse",
    "veterinary assistant",
    "veterinary technician specialist",
    "veterinary professional associate",
]

MINUTES_KEYWORDS = [
    "veterinary technician", "veterinary technologist", "veterinary nurse",
    "veterinary assistant", "veterinary technician specialist",
    "veterinary professional associate", "VPA", "VTS", "CVT", "RVT", "LVT",
    "vet tech", "credentialed veterinary", "registered veterinary nurse",
]


# ─── Database ─────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bills (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, abbreviation TEXT NOT NULL,
            chamber TEXT, bill_number TEXT, title TEXT, status TEXT, status_date TEXT,
            sponsor TEXT, last_action TEXT, last_action_date TEXT, summary_ai TEXT,
            categories TEXT, relevance_score INTEGER, regulatory_type TEXT,
            full_text_url TEXT, legiscan_id INTEGER, first_seen_date TEXT,
            last_updated TEXT, content_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS board_minutes (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, abbreviation TEXT NOT NULL,
            board_name TEXT, meeting_date TEXT, source_url TEXT, excerpt_raw TEXT,
            summary_ai TEXT, categories TEXT, relevance_score INTEGER,
            regulatory_type TEXT, first_seen_date TEXT, last_updated TEXT,
            content_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT,
            bills_found INTEGER, minutes_found INTEGER, bills_new INTEGER,
            minutes_new INTEGER, duration_seconds REAL, status TEXT
        );
    """)
    conn.commit()
    log.info("Database ready at %s", DB_PATH)
    return conn


def chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ─── LegiScan ─────────────────────────────────────────────────────────────────

class LegiScanClient:
    BASE = "https://api.legiscan.com/"

    def __init__(self, key: str):
        self.key = key
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "VetWorkforceTracker/2.0"

    def _get(self, op: str, extra: dict) -> Optional[dict]:
        params = {"key": self.key, "op": op, **extra}
        for attempt in range(3):
            try:
                r = self.s.get(self.BASE, params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
                if data.get("status") == "OK":
                    return data
                log.warning("LegiScan %s: %s", op, data.get("alert", {}).get("message", ""))
                return None
            except requests.exceptions.RequestException as e:
                log.warning("LegiScan attempt %d failed: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        return None

    def search(self, state: str, query: str) -> list:
        data = self._get("search", {"state": state, "query": query, "year": 2})
        if not data:
            return []
        sr = data.get("searchresult", {})
        return [v for k, v in sr.items() if k != "summary" and isinstance(v, dict)]

    def get_bill(self, bill_id: int) -> Optional[dict]:
        data = self._get("getBill", {"id": bill_id})
        return data.get("bill") if data else None


# ─── Claude AI ────────────────────────────────────────────────────────────────

class ClaudeClient:
    URL   = "https://api.anthropic.com/v1/messages"
    MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, key: str):
        self.key = key
        self.s = requests.Session()
        self.s.headers.update({
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        })

    def analyze(self, text: str, item_type: str = "bill") -> dict:
        EMPTY = {
            "relevant": False, "summary": "", "categories": [],
            "relevance_score": 0, "regulatory_type": ""
        }
        if not self.key:
            log.warning("No Anthropic API key — skipping AI analysis")
            return EMPTY

        prompt = f"""Analyze this {item_type} for relevance to the veterinary paraprofessional workforce.

TEXT:
{text[:3500]}

Workforce categories:
1. Veterinary Technicians (CVT, RVT, LVT, vet tech)
2. Veterinary Technologists
3. Veterinary Nurses (Registered Veterinary Nurse)
4. Veterinary Assistants
5. Veterinary Technician Specialists (VTS)
6. Veterinary Professional Associates (VPA)

If relevant respond ONLY with this JSON (no markdown, no extra text):
{{"relevant":true,"summary":"2-3 sentence plain-language summary","categories":["matching categories"],"relevance_score":8,"regulatory_type":"scope of practice"}}

Valid regulatory_type values: scope of practice, title protection, licensure, supervision ratio, continuing education, examination, reciprocity, discipline, delegation, other

If NOT relevant respond ONLY with:
{{"relevant":false,"summary":"","categories":[],"relevance_score":0,"regulatory_type":""}}"""

        for attempt in range(3):
            try:
                r = self.s.post(
                    self.URL,
                    json={
                        "model": self.MODEL,
                        "max_tokens": 400,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=60,
                )
                r.raise_for_status()
                raw = r.json()["content"][0]["text"].strip()
                raw = raw.replace("```json", "").replace("```", "").strip()
                return json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Claude returned non-JSON on attempt %d", attempt + 1)
                return EMPTY
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                log.error("Claude HTTP %d on attempt %d: %s", status, attempt + 1, e)
                if status in (400, 404):
                    return EMPTY
                time.sleep(2 ** attempt)
            except Exception as e:
                log.error("Claude error attempt %d: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        return EMPTY


# ─── Board Minutes Fetcher ────────────────────────────────────────────────────

class MinutesFetcher:
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(self.HEADERS)

    def fetch_for_state(self, state_cfg: dict) -> list:
        results = []
        try:
            results = self._scrape_minutes_page(state_cfg["vmb_minutes_url"])
        except Exception as e:
            log.warning(
                "%s direct scrape failed (%s) — trying DuckDuckGo fallback",
                state_cfg["state"], e
            )

        if not results:
            results = self._ddg_fallback(state_cfg)

        return results[:4]

    def _scrape_minutes_page(self, url: str) -> list:
        try:
            resp = self.s.get(url, timeout=20, allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"GET failed: {e}")

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []

        for a in soup.find_all("a", href=True):
            href  = a["href"].strip()
            label = a.get_text(" ", strip=True).lower()
            is_doc = any(
                kw in label or kw in href.lower()
                for kw in ["minute", "agenda", "meeting", ".pdf"]
            )
            if not is_doc:
                continue
            full_url = href if href.startswith("http") else self._resolve(url, href)
            text = self._fetch_text(full_url)
            if text and self._has_keyword(text):
                results.append({
                    "url": full_url,
                    "meeting_date": self._extract_date(label),
                    "raw_text": text[:4000],
                })
            if len(results) >= 4:
                break

        return results

    def _ddg_fallback(self, state_cfg: dict) -> list:
        """Use DuckDuckGo HTML search as fallback — no API key, no rate limiting."""
        state  = state_cfg["state"]
        domain = self._domain(state_cfg["vmb_url"])
        query  = (
            f'site:{domain} minutes '
            f'"veterinary technician" OR "vet tech" OR "veterinary nurse" '
            f'OR "veterinary assistant" OR "VPA" OR "VTS"'
        )
        try:
            resp = self.s.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning("DuckDuckGo fallback failed for %s: %s", state, e)
            return []

        from bs4 import BeautifulSoup
        soup    = BeautifulSoup(resp.text, "html.parser")
        results = []

        for a in soup.select("a.result__url, a.result__a"):
            href = a.get("href", "")
            if not href.startswith("http"):
                continue
            if domain not in href:
                continue
            text = self._fetch_text(href)
            if text and self._has_keyword(text):
                results.append({
                    "url": href,
                    "meeting_date": "",
                    "raw_text": text[:4000],
                })
            if len(results) >= 4:
                break

        return results

    def _fetch_text(self, url: str) -> str:
        try:
            resp = self.s.get(url, timeout=25, allow_redirects=True)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "pdf" in ct or url.lower().endswith(".pdf"):
                return self._pdf_text(resp.content)
            from bs4 import BeautifulSoup
            return BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)[:5000]
        except Exception as e:
            log.debug("_fetch_text failed %s: %s", url, e)
            return ""

    def _pdf_text(self, content: bytes) -> str:
        try:
            import io
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages[:8])
        except Exception as e:
            log.debug("PDF extraction failed: %s", e)
            return ""

    def _has_keyword(self, text: str) -> bool:
        tl = text.lower()
        return any(kw.lower() in tl for kw in MINUTES_KEYWORDS)

    def _extract_date(self, text: str) -> str:
        import re
        m = re.search(
            r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
            r"(?:january|february|march|april|may|june|july|august|"
            r"september|october|november|december)\s+\d{1,2},?\s+\d{4})\b",
            text, re.IGNORECASE,
        )
        return m.group(0) if m else ""

    def _resolve(self, base: str, href: str) -> str:
        from urllib.parse import urljoin
        return urljoin(base, href)

    def _domain(self, url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc


# ─── Main Pipeline ────────────────────────────────────────────────────────────

class TrackerPipeline:
    def __init__(self, db: sqlite3.Connection):
        with open(CONFIG_PATH) as f:
            self.cfg = json.load(f)
        self.legiscan = LegiScanClient(LEGISCAN_API_KEY)
        self.claude   = ClaudeClient(ANTHROPIC_API_KEY)
        self.minutes  = MinutesFetcher()
        self.db       = db

    def process_bills(self, state_cfg: dict) -> tuple:
        found = new = 0
        state = state_cfg["state"]
        abbr  = state_cfg["abbreviation"]
        seen: set = set()

        for keyword in BILL_SEARCH_KEYWORDS:
            results = self.legiscan.search(abbr, keyword)
            time.sleep(0.4)

            for result in results[:4]:
                bill_id = result.get("bill_id")
                if not bill_id or bill_id in seen:
                    continue
                seen.add(bill_id)

                bill = self.legiscan.get_bill(bill_id)
                if not bill:
                    continue
                time.sleep(0.3)

                title        = bill.get("title", "")
                description  = bill.get("description", "")
                analyze_text = f"TITLE: {title}\n\nDESCRIPTION: {description}"
                h            = chash(analyze_text)

                existing = self.db.execute(
                    "SELECT content_hash, first_seen_date FROM bills WHERE legiscan_id=?",
                    (bill_id,)
                ).fetchone()

                if existing and existing["content_hash"] == h:
                    continue

                ai = self.claude.analyze(analyze_text, "legislative bill")
                if not ai.get("relevant"):
                    continue

                found  += 1
                is_new  = not existing
                if is_new:
                    new += 1

                sponsor     = (bill.get("sponsors") or [{}])[0].get("name", "")
                texts       = bill.get("texts") or []
                url         = (
                    texts[0].get("state_link") or state_cfg["bill_search_url"]
                ) if texts else state_cfg["bill_search_url"]
                history     = bill.get("history") or []
                last_action = history[-1].get("action", "") if history else ""
                row_id      = f"{abbr}-{bill.get('bill_number', bill_id)}"
                first_seen  = (
                    existing["first_seen_date"] if existing
                    else datetime.utcnow().isoformat()
                )

                self.db.execute("""
                    INSERT OR REPLACE INTO bills
                    (id,state,abbreviation,chamber,bill_number,title,status,
                     status_date,sponsor,last_action,last_action_date,summary_ai,
                     categories,relevance_score,regulatory_type,full_text_url,
                     legiscan_id,first_seen_date,last_updated,content_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    row_id, state, abbr,
                    bill.get("chamber", ""), bill.get("bill_number", ""), title,
                    bill.get("status", ""), bill.get("status_date", ""), sponsor,
                    last_action, bill.get("last_action_date", ""),
                    ai.get("summary", ""), json.dumps(ai.get("categories", [])),
                    ai.get("relevance_score", 0), ai.get("regulatory_type", ""),
                    url, bill_id, first_seen, datetime.utcnow().isoformat(), h,
                ))

        return found, new

    def process_minutes(self, state_cfg: dict) -> tuple:
        found = new = 0
        state = state_cfg["state"]
        abbr  = state_cfg["abbreviation"]

        try:
            items = self.minutes.fetch_for_state(state_cfg)
        except Exception as e:
            log.warning("%s minutes fetch failed: %s", state, e)
            return 0, 0

        for item in items:
            url      = item["url"]
            raw_text = item.get("raw_text", "")
            h        = chash(url + raw_text[:200])

            existing = self.db.execute(
                "SELECT content_hash, first_seen_date FROM board_minutes WHERE source_url=?",
                (url,)
            ).fetchone()

            if existing and existing["content_hash"] == h:
                continue

            ai = self.claude.analyze(raw_text, "board meeting minutes")
            if not ai.get("relevant"):
                continue

            found  += 1
            is_new  = not existing
            if is_new:
                new += 1

            row_id     = f"{abbr}-minutes-{h}"
            first_seen = (
                existing["first_seen_date"] if existing
                else datetime.utcnow().isoformat()
            )

            self.db.execute("""
                INSERT OR REPLACE INTO board_minutes
                (id,state,abbreviation,board_name,meeting_date,source_url,
                 excerpt_raw,summary_ai,categories,relevance_score,regulatory_type,
                 first_seen_date,last_updated,content_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                row_id, state, abbr,
                state_cfg["vmb_name"], item.get("meeting_date", ""), url,
                raw_text[:1000], ai.get("summary", ""),
                json.dumps(ai.get("categories", [])),
                ai.get("relevance_score", 0), ai.get("regulatory_type", ""),
                first_seen, datetime.utcnow().isoformat(), h,
            ))

        return found, new

    def export_json(self):
        bills   = [dict(r) for r in self.db.execute(
            "SELECT * FROM bills ORDER BY last_updated DESC")]
        minutes = [dict(r) for r in self.db.execute(
            "SELECT * FROM board_minutes ORDER BY last_updated DESC")]
        last_run_row = self.db.execute(
            "SELECT run_date FROM run_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

        for b in bills:
            try:    b["categories"] = json.loads(b["categories"] or "[]")
            except: b["categories"] = []
        for m in minutes:
            try:    m["categories"] = json.loads(m["categories"] or "[]")
            except: m["categories"] = []

        output = {
            "last_updated":  datetime.utcnow().isoformat(),
            "last_run":      last_run_row["run_date"] if last_run_row else None,
            "total_bills":   len(bills),
            "total_minutes": len(minutes),
            "bills":         bills,
            "board_minutes": minutes,
        }

        with open(OUTPUT_PATH, "w") as f:
            json.dump(output, f, indent=2, default=str)
        log.info(
            "Exported tracker_data.json — %d bills, %d minutes",
            len(bills), len(minutes)
        )

    def run(self):
        start = time.time()
        log.info("=== Tracker pipeline v2 started ===")
        total_bills = total_bills_new = total_min = total_min_new = 0

        for state_cfg in self.cfg["states"]:
            state = state_cfg["state"]
            abbr  = state_cfg["abbreviation"]
            log.info("── %s (%s)", state, abbr)

            b = bn = m = mn = 0
            try:
                b, bn = self.process_bills(state_cfg)
                total_bills     += b
                total_bills_new += bn
            except Exception as e:
                log.error("%s bills error: %s", state, e)

            try:
                m, mn = self.process_minutes(state_cfg)
                total_min     += m
                total_min_new += mn
            except Exception as e:
                log.error("%s minutes error: %s", state, e)

            self.db.commit()
            log.info("%s done — bills: %d, minutes: %d", state, b, m)
            time.sleep(1)

        self.export_json()

        duration = time.time() - start
        self.db.execute("""
            INSERT INTO run_log
            (run_date,bills_found,minutes_found,bills_new,minutes_new,
             duration_seconds,status)
            VALUES (?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            total_bills, total_min,
            total_bills_new, total_min_new,
            duration, "success",
        ))
        self.db.commit()
        log.info(
            "=== Done in %.1fs | Bills: %d (%d new) | Minutes: %d (%d new) ===",
            duration, total_bills, total_bills_new, total_min, total_min_new,
        )


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = init_db()
    TrackerPipeline(db).run()
