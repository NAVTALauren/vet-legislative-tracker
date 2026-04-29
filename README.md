# 🐾 Veterinary Workforce Legislative Tracker

A fully automated, weekly-refreshed system that monitors all 50 U.S. states for legislative activity and veterinary medical board minutes related to the **veterinary paraprofessional workforce**.

## Tracked Workforce Categories

- Veterinary Technicians (CVT, RVT, LVT)
- Veterinary Technologists
- Veterinary Nurses (including Registered Veterinary Nurse)
- Veterinary Assistants
- Veterinary Technician Specialists (VTS)
- Veterinary Professional Associates (VPA)

---

## Project Structure

```
vet-tracker/
├── config/
│   └── state_boards.json        # All 50 states: legislature + VMB URLs
├── scraper/
│   ├── scrape_bills.py          # Main pipeline: bills + board minutes + AI summary
│   ├── send_digest.py           # Weekly HTML email digest sender
│   └── requirements.txt         # Python dependencies
├── frontend/
│   ├── index.html               # Dynamic single-page tracker UI
│   └── public/
│       └── tracker_data.json    # Generated output (consumed by frontend)
├── github-actions/
│   └── weekly_tracker.yml       # GitHub Actions workflow (weekly cron)
├── data/
│   └── tracker.db               # SQLite database (auto-created)
└── logs/
    └── scraper.log              # Run logs (auto-created)
```

---

## Quick Start (Local)

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_ORG/vet-tracker.git
cd vet-tracker
pip install -r scraper/requirements.txt
```

### 2. Set environment variables

```bash
cp .env.example .env
# Edit .env with your API keys
```

**.env file:**
```
LEGISCAN_API_KEY=your_legiscan_key
OPENSTATES_API_KEY=your_openstates_key
ANTHROPIC_API_KEY=your_anthropic_key
SMTP_USER=your@gmail.com
SMTP_PASS=your_app_password
DIGEST_RECIPIENTS=recipient1@org.com,recipient2@org.com
TRACKER_URL=https://your-tracker.github.io
```

### 3. Run the scraper

```bash
python scraper/scrape_bills.py
```

### 4. View the frontend

```bash
# Simple HTTP server (Python built-in)
cd frontend
python -m http.server 8080
# Open http://localhost:8080
```

---

## API Keys Required

| Service | Purpose | Free Tier | Get Key |
|---|---|---|---|
| **LegiScan** | Legislative bill data for all 50 states | 30,000 calls/month | [legiscan.com/legiscan-api](https://legiscan.com/legiscan-api) |
| **OpenStates** | Alternative/supplemental bill data | 500 calls/day | [openstates.org/accounts/register](https://openstates.org/accounts/register/) |
| **Anthropic Claude** | AI summarization + relevance scoring | Pay-per-use | [console.anthropic.com](https://console.anthropic.com) |

---

## GitHub Actions Deployment

### Setup

1. **Fork/push this repo to GitHub**

2. **Add GitHub Secrets** (Settings → Secrets → Actions):
   - `LEGISCAN_API_KEY`
   - `OPENSTATES_API_KEY`
   - `ANTHROPIC_API_KEY`
   - `SMTP_USER` (Gmail address)
   - `SMTP_PASS` (Gmail App Password — not your regular password)
   - `DIGEST_RECIPIENTS` (comma-separated email list)

3. **Add GitHub Variables** (Settings → Variables → Actions):
   - `TRACKER_URL` — your GitHub Pages URL (e.g., `https://yourorg.github.io/vet-tracker`)

4. **Enable GitHub Pages**:
   - Settings → Pages → Source: GitHub Actions

5. **Place the workflow file**:
   ```bash
   mkdir -p .github/workflows
   cp github-actions/weekly_tracker.yml .github/workflows/
   git add . && git commit -m "Add tracker workflow" && git push
   ```

The tracker will now run every Sunday at 11 PM UTC and deploy automatically to GitHub Pages.

### Manual Run

Go to **Actions → Weekly Veterinary Legislative Tracker → Run workflow**

---

## Database Schema

**bills table:**
| Column | Type | Description |
|---|---|---|
| id | TEXT PK | `{STATE}-{BILL_NUMBER}` |
| state | TEXT | Full state name |
| abbreviation | TEXT | 2-letter code |
| chamber | TEXT | H or S |
| bill_number | TEXT | e.g., HB 2341 |
| title | TEXT | Bill title |
| status | TEXT | Active/Passed/Signed/Failed |
| summary_ai | TEXT | Claude-generated summary |
| categories | TEXT | JSON array of workforce categories |
| relevance_score | INT | 1–10 (Claude-generated) |
| regulatory_type | TEXT | scope/licensure/title/etc. |
| full_text_url | TEXT | Link to full bill text |
| first_seen_date | TEXT | ISO datetime first found |
| last_updated | TEXT | ISO datetime last checked |

**board_minutes table:** Similar structure with board_name, meeting_date, source_url, excerpt_raw fields.

---

## Customization

### Add/update state board URLs
Edit `config/state_boards.json` — each state entry has:
- `vmb_minutes_url` — the minutes page to scrape
- `vmb_name` — board name shown in the UI

### Add search keywords
Edit the `search_keywords` array in `config/state_boards.json`

### Change schedule
Edit the `cron` expression in `github-actions/weekly_tracker.yml`:
```yaml
- cron: "0 23 * * 0"   # Sunday 11 PM UTC
- cron: "0 6 * * 1"    # Monday 6 AM UTC
- cron: "0 12 1 * *"   # 1st of each month, noon UTC
```

---

## Cost Estimates (Monthly)

| Item | Est. Cost |
|---|---|
| LegiScan API (free tier) | $0 |
| OpenStates API (free tier) | $0 |
| Claude API (~5,000 bill analyses/month) | ~$3–8 |
| GitHub Actions (free tier) | $0 |
| GitHub Pages hosting | $0 |
| **Total** | **~$3–8/month** |

---

## Troubleshooting

**Scraper returns no results:**
- Verify your LegiScan API key is valid: `https://api.legiscan.com/?key=YOUR_KEY&op=getSessionList&state=CA`
- Check `logs/scraper.log` for errors

**Email digest not sending:**
- Gmail requires an App Password (not your account password): [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
- Ensure 2FA is enabled on the Gmail account

**Frontend shows no data:**
- Ensure `frontend/public/tracker_data.json` exists
- Run the scraper at least once, or copy the mock data file

---

## License

MIT — free to use, modify, and deploy.
