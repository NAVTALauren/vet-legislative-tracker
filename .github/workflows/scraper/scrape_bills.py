#!/usr/bin/env python3
"""
Veterinary Workforce Legislative Tracker
Bill Scraper - uses LegiScan API + OpenStates API
Run weekly via GitHub Actions
"""

import os
import json
import time
import logging
import hashlib
import sqlite3
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ─── Config ────────────────────────────────────────────────────────────────────

LEGISCAN_API_KEY = os.environ.get("LEGISCAN_API_KEY", "")
OPENSTATES_API_KEY = os.environ.get("OPENSTATES_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "state_boards.json"
DB_PATH = BASE_DIR / "data" / "tracker.db"
OUTPUT_PATH = BASE_DIR / "frontend" / "public" / "tracker_data.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "logs" / "scraper.log"),
    ],
)
log = logging.getLogger(__name__)

SEARCH_KEYWORDS = [
    "veterinary technician",
    "veterinary technologist",
    "veterinary nurse",
    "veterinary assistant",
    "veterinary technician specialist",
    "veterinary professional associate",
    "VPA",
    "VTS",
    "CVT",
    "RVT",
    "LVT",
    "vet tech",
    "credentialed veterinary technician",
    "registered veterinary nurse",
]

WORKFORCE_CATEGORIES = {
    "veterinary technician": ["technician", "CVT", "RVT", "LVT", "vet tech", "credentialed"],
    "veterinary technologist": ["technologist"],
    "veterinary nurse": ["nurse", "registered veterinary nurse"],
    "veterinary assistant": ["assistant"],
    "veterinary technician specialist": ["specialist", "VTS"],
    "veterinary professional associate": ["professional associate", "VPA"],
}

REGULATORY_TYPES = {
    "scope of practice": ["scope", "practice act", "authorized tasks", "delegat"],
    "title protection": ["title", "credential", "designation"],
    "licensure": ["licens", "permit", "registr"],
    "supervision ratio": ["supervision", "ratio", "oversight"],
    "continuing education": ["continuing education", "CE", "CEU"],
    "examination": ["exam", "VTNE", "test", "credentialing exam"],
    "reciprocity": ["reciprocity", "endorsement", "portability"],
    "discipline": ["disciplin", "revok", "suspend"],
    "delegation": ["delegat", "direct supervision", "indirect supervision"],
}


# ─── Database Setup ─────────────────────────────────────────────────────────────

def init_db():
    """Initialize SQLite database with required schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            abbreviation TEXT NOT NULL,
            chamber TEXT,
            bill_number TEXT,
            title TEXT,
            status TEXT,
            status_date TEXT,
            sponsor TEXT,
            last_action TEXT,
            last_action_date TEXT,
            summary_ai TEXT,
            categories TEXT,
            relevance_score INTEGER,
            regulatory_type TEXT,
            full_text_url TEXT,
            legiscan_id INTEGER,
            first_seen_date TEXT,
            last_updated TEXT,
            content_hash TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS board_minutes (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            abbreviation TEXT NOT NULL,
            board_name TEXT,
            meeting_date TEXT,
            source_url TEXT,
            excerpt_raw TEXT,
            summary_ai TEXT,
            categories TEXT,
            relevance_score INTEGER,
            regulatory_type TEXT,
            first_seen_date TEXT,
            last_updated TEXT,
            content_hash TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            bills_found INTEGER,
            minutes_found INTEGER,
            bills_new INTEGER,
            minutes_new INTEGER,
            duration_seconds REAL,
            status TEXT
        )
    """)

    conn.commit()
    conn.close()
    log.info("Database initialized at %s", DB_PATH)


# ─── LegiScan API ───────────────────────────────────────────────────────────────

class LegiScanClient:
    BASE_URL = "https://api.legiscan.com/"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "VetWorkforceTracker/1.0"})

    def _get(self, op: str, params: dict) -> Optional[dict]:
        params.update({"key": self.api_key, "op": op})
        try:
            resp = self.session.get(self.BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "OK":
                return data
            log.warning("LegiScan API error for op=%s: %s", op, data.get("alert", {}).get("message"))
            return None
        except Exception as e:
            log.error("LegiScan request failed op=%s: %s", op, e)
            return None

    def search_bills(self, state: str, query: str, year: int = 2) -> list:
        """Search bills in a state. year=2 = current+prior session."""
        result = self._get("search", {"state": state, "query": query, "year": year})
        if result and "searchresult" in result:
            sr = result["searchresult"]
            return [v for k, v in sr.items() if k != "summary" and isinstance(v, dict)]
        return []

    def get_bill(self, bill_id: int) -> Optional[dict]:
        result = self._get("getBill", {"id": bill_id})
        if result:
            return result.get("bill")
        return None

    def get_bill_text(self, doc_id: int) -> Optional[str]:
        result = self._get("getBillText", {"id": doc_id})
        if result:
            return result.get("text", {}).get("doc")
        return None


# ─── OpenStates API ─────────────────────────────────────────────────────────────

class OpenStatesClient:
    BASE_URL = "https://v3.openstates.org"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-KEY": api_key,
            "User-Agent": "VetWorkforceTracker/1.0",
        })

    def search_bills(self, jurisdiction: str, query: str) -> list:
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/bills",
                params={
                    "jurisdiction": jurisdiction,
                    "q": query,
                    "sort": "updated_desc",
                    "per_page": 20,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            log.error("OpenStates search failed for %s: %s", jurisdiction, e)
            return []


# ─── Claude AI Summarizer ───────────────────────────────────────────────────────

class ClaudeSummarizer:
    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        })

    def analyze(self, text: str, item_type: str = "bill") -> dict:
        """
        Analyze a bill or board minute for relevance to vet workforce categories.
        Returns structured JSON with summary, categories, score, regulatory_type.
        """
        prompt = f"""You are analyzing a {item_type} for relevance to the veterinary paraprofessional workforce.

TEXT TO ANALYZE:
{text[:4000]}

Determine if this text is relevant to ANY of these workforce categories:
1. Veterinary Technicians (CVT, RVT, LVT, vet tech, credentialed veterinary technician)
2. Veterinary Technologists
3. Veterinary Nurses (including Registered Veterinary Nurse)
4. Veterinary Assistants
5. Veterinary Technician Specialists (VTS)
6. Veterinary Professional Associates (VPA)

If relevant, provide a JSON response with EXACTLY these fields:
{{
  "relevant": true,
  "summary": "2-3 sentence plain-language summary of what this {item_type} does or proposes",
  "categories": ["list", "of", "matching", "categories", "from", "the", "6", "above"],
  "relevance_score": <integer 1-10>,
  "regulatory_type": "<one of: scope of practice, title protection, licensure, supervision ratio, continuing education, examination, reciprocity, discipline, delegation, other>"
}}

If NOT relevant, respond with:
{{"relevant": false, "summary": "", "categories": [], "relevance_score": 0, "regulatory_type": ""}}

Respond ONLY with valid JSON. No preamble, no markdown fences."""

        try:
            resp = self.session.post(
                self.API_URL,
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 512,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()
            # Strip markdown fences if present
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("JSON parse error in AI response: %s", e)
            return {"relevant": False, "summary": "", "categories": [], "relevance_score": 0, "regulatory_type": ""}
        except Exception as e:
            log.error("Claude API error: %s", e)
            return {"relevant": False, "summary": "", "categories": [], "relevance_score": 0, "regulatory_type": ""}


# ─── Board Minutes Scraper ──────────────────────────────────────────────────────

class BoardMinutesScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; VetWorkforceTracker/1.0)"
        })

    def scrape_minutes_page(self, url: str, keywords: list) -> list:
        """
        Scrape a board minutes page and return relevant links/excerpts.
        Returns list of dicts with {url, meeting_date, raw_text}.
        """
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")

            results = []
            # Find all links that look like minutes/agendas
            for link in soup.find_all("a", href=True):
                href = link["href"]
                link_text = link.get_text(strip=True).lower()
                # Filter for likely minutes links
                if any(kw in link_text or kw in href.lower() for kw in ["minute", "agenda", "meeting"]):
                    full_url = href if href.startswith("http") else url.rstrip("/") + "/" + href.lstrip("/")
                    # Try to extract a date from the link text
                    meeting_date = self._extract_date(link_text)
                    results.append({
                        "url": full_url,
                        "link_text": link.get_text(strip=True),
                        "meeting_date": meeting_date,
                    })

            # Also check page text for keyword mentions
            page_text = soup.get_text(separator=" ", strip=True)
            for kw in keywords:
                if kw.lower() in page_text.lower():
                    return results  # Page is relevant, return all minutes links

            return results

        except Exception as e:
            log.warning("Failed to scrape %s: %s", url, e)
            return []

    def fetch_pdf_text(self, url: str) -> str:
        """Download a PDF and extract its text."""
        try:
            import io
            import pdfplumber
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages[:10])
        except ImportError:
            log.warning("pdfplumber not installed, skipping PDF: %s", url)
            return ""
        except Exception as e:
            log.warning("Failed to extract PDF %s: %s", url, e)
            return ""

    def _extract_date(self, text: str) -> str:
        """Attempt to extract a date from link text."""
        import re
        patterns = [
            r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b",
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}\b",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(0)
        return ""


# ─── Main Orchestrator ──────────────────────────────────────────────────────────

class TrackerPipeline:
    def __init__(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

        self.legiscan = LegiScanClient(LEGISCAN_API_KEY)
        self.openstates = OpenStatesClient(OPENSTATES_API_KEY)
        self.claude = ClaudeSummarizer(ANTHROPIC_API_KEY)
        self.minutes_scraper = BoardMinutesScraper()
        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row

    def content_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def run(self):
        start = time.time()
        log.info("=== Tracker pipeline started ===")
        bills_found = bills_new = minutes_found = minutes_new = 0

        for state_cfg in self.config["states"]:
            state = state_cfg["state"]
            abbr = state_cfg["abbreviation"]
            log.info("Processing %s (%s)", state, abbr)

            # ── Bills ──────────────────────────────────────────────────
            for keyword in self.config["search_keywords"][:6]:  # Top 6 keywords to limit API calls
                results = self.legiscan.search_bills(abbr, keyword)
                time.sleep(0.5)  # Rate limiting

                for result in results[:5]:  # Limit per keyword
                    bill_id = result.get("bill_id")
                    if not bill_id:
                        continue
                    bill = self.legiscan.get_bill(bill_id)
                    if not bill:
                        continue
                    time.sleep(0.3)

                    title = bill.get("title", "")
                    description = bill.get("description", "")
                    text_to_analyze = f"TITLE: {title}\n\nDESCRIPTION: {description}"

                    chash = self.content_hash(text_to_analyze)
                    existing = self.db.execute(
                        "SELECT content_hash FROM bills WHERE legiscan_id = ?", (bill_id,)
                    ).fetchone()

                    if existing and existing["content_hash"] == chash:
                        continue  # No change

                    ai = self.claude.analyze(text_to_analyze, "legislative bill")
                    if not ai.get("relevant"):
                        continue

                    bills_found += 1
                    sponsor = ""
                    if bill.get("sponsors"):
                        sponsor = bill["sponsors"][0].get("name", "")

                    texts = bill.get("texts", [])
                    url = texts[0].get("state_link", state_cfg["bill_search_url"]) if texts else state_cfg["bill_search_url"]

                    row_id = f"{abbr}-{bill.get('bill_number', bill_id)}"
                    is_new = not existing
                    if is_new:
                        bills_new += 1

                    self.db.execute("""
                        INSERT OR REPLACE INTO bills
                        (id, state, abbreviation, chamber, bill_number, title, status,
                         status_date, sponsor, last_action, last_action_date,
                         summary_ai, categories, relevance_score, regulatory_type,
                         full_text_url, legiscan_id, first_seen_date, last_updated, content_hash)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        row_id, state, abbr,
                        bill.get("chamber", ""),
                        bill.get("bill_number", ""),
                        title,
                        bill.get("status", ""),
                        bill.get("status_date", ""),
                        sponsor,
                        bill.get("history", [{}])[-1].get("action", "") if bill.get("history") else "",
                        bill.get("last_action_date", ""),
                        ai.get("summary", ""),
                        json.dumps(ai.get("categories", [])),
                        ai.get("relevance_score", 0),
                        ai.get("regulatory_type", ""),
                        url,
                        bill_id,
                        datetime.utcnow().isoformat() if is_new else (existing["first_seen_date"] if existing else datetime.utcnow().isoformat()),
                        datetime.utcnow().isoformat(),
                        chash,
                    ))

            # ── Board Minutes ──────────────────────────────────────────
            minutes_links = self.minutes_scraper.scrape_minutes_page(
                state_cfg["vmb_minutes_url"],
                self.config["search_keywords"],
            )

            for link in minutes_links[:5]:  # Limit per state
                url = link["url"]
                chash = self.content_hash(url)
                existing = self.db.execute(
                    "SELECT content_hash FROM board_minutes WHERE source_url = ?", (url,)
                ).fetchone()

                if existing and existing["content_hash"] == chash:
                    continue

                # Try to get text from PDF or HTML
                raw_text = ""
                if url.lower().endswith(".pdf"):
                    raw_text = self.minutes_scraper.fetch_pdf_text(url)
                else:
                    try:
                        resp = requests.get(url, timeout=20)
                        from bs4 import BeautifulSoup
                        raw_text = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True)[:5000]
                    except Exception:
                        pass

                if not raw_text:
                    continue

                # Quick keyword filter before calling Claude
                text_lower = raw_text.lower()
                if not any(kw.lower() in text_lower for kw in self.config["search_keywords"]):
                    continue

                ai = self.claude.analyze(raw_text, "board meeting minutes")
                if not ai.get("relevant"):
                    continue

                minutes_found += 1
                is_new = not existing
                if is_new:
                    minutes_new += 1

                row_id = f"{abbr}-minutes-{chash}"
                self.db.execute("""
                    INSERT OR REPLACE INTO board_minutes
                    (id, state, abbreviation, board_name, meeting_date, source_url,
                     excerpt_raw, summary_ai, categories, relevance_score,
                     regulatory_type, first_seen_date, last_updated, content_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    row_id, state, abbr,
                    state_cfg["vmb_name"],
                    link.get("meeting_date", ""),
                    url,
                    raw_text[:1000],
                    ai.get("summary", ""),
                    json.dumps(ai.get("categories", [])),
                    ai.get("relevance_score", 0),
                    ai.get("regulatory_type", ""),
                    datetime.utcnow().isoformat() if is_new else "",
                    datetime.utcnow().isoformat(),
                    chash,
                ))

            self.db.commit()
            log.info("%s done: %d bills, %d minutes this state", state, bills_found, minutes_found)

        # ── Generate output JSON ──────────────────────────────────────
        self._export_json()

        duration = time.time() - start
        self.db.execute("""
            INSERT INTO run_log (run_date, bills_found, minutes_found, bills_new, minutes_new, duration_seconds, status)
            VALUES (?,?,?,?,?,?,?)
        """, (datetime.utcnow().isoformat(), bills_found, minutes_found, bills_new, minutes_new, duration, "success"))
        self.db.commit()
        log.info("=== Pipeline complete in %.1fs | Bills: %d (%d new) | Minutes: %d (%d new) ===",
                 duration, bills_found, bills_new, minutes_found, minutes_new)

    def _export_json(self):
        """Export all data to tracker_data.json for the frontend."""
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        bills = [dict(r) for r in self.db.execute("SELECT * FROM bills ORDER BY last_updated DESC")]
        minutes = [dict(r) for r in self.db.execute("SELECT * FROM board_minutes ORDER BY last_updated DESC")]
        last_run = self.db.execute("SELECT run_date FROM run_log ORDER BY id DESC LIMIT 1").fetchone()

        # Parse JSON fields
        for b in bills:
            try:
                b["categories"] = json.loads(b["categories"] or "[]")
            except Exception:
                b["categories"] = []
        for m in minutes:
            try:
                m["categories"] = json.loads(m["categories"] or "[]")
            except Exception:
                m["categories"] = []

        output = {
            "last_updated": datetime.utcnow().isoformat(),
            "last_run": last_run["run_date"] if last_run else None,
            "total_bills": len(bills),
            "total_minutes": len(minutes),
            "bills": bills,
            "board_minutes": minutes,
        }
        with open(OUTPUT_PATH, "w") as f:
            json.dump(output, f, indent=2, default=str)
        log.info("Exported tracker_data.json: %d bills, %d minutes", len(bills), len(minutes))


# ─── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    Path(BASE_DIR / "logs").mkdir(exist_ok=True)
    pipeline = TrackerPipeline()
    pipeline.run()
