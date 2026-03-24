# Conversion Growth — Review Mining Tool

A CRO audit tool that scrapes **all** Trustpilot reviews for any brand, runs Claude-powered sentiment analysis, and surfaces friction points, top themes, and improvement opportunities in a clean dashboard.

---

## Features

- **Full review scraping** — authenticated cookie-based access bypasses Trustpilot's 200-review anonymous cap
- **Multi-sort anonymous fallback** — scrapes up to ~600 unique reviews across 3 sort passes (recency / ratingHigh / ratingLow) when no cookie is set
- **Claude AI analysis** — smart 150-review sample (60 negative / 40 neutral / 50 positive) analyzed concurrently; star-rating classification for the rest
- **Real-time streaming** — SSE progress updates during scraping and analysis
- **Dashboard** — sentiment distribution, NPS estimate, rating breakdown, top themes, friction points, improvement opportunities, individual review cards
- **Load More** — paginated review cards (50 at a time) with sentiment filter

---

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Python 3.11+ |
| Scraping | httpx (async) + Playwright (stealth browser fallback) |
| AI | Anthropic Claude (`claude-haiku-4-5-20251001`) |
| Frontend | Vanilla JS + CSS (no framework) |
| Streaming | Server-Sent Events (SSE) |

---

## Getting Started

### 1. Clone the repo

```bash
git clone https://github.com/behichemben-star/conversion-growth-review-mining-tool.git
cd conversion-growth-review-mining-tool
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Edit `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...

# Optional — unlocks full review scraping (no 200-review cap)
# See "Trustpilot Cookie Auth" section below
TRUSTPILOT_COOKIE=
```

### 4. Run the server

```bash
uvicorn app.main:app --reload
```

Open **http://localhost:8000** in your browser.

---

## Trustpilot Cookie Auth

Trustpilot caps anonymous access to 200 reviews (10 pages). To scrape all reviews:

1. Log into [trustpilot.com](https://www.trustpilot.com) in Chrome
2. Press `F12` → **Network** tab → refresh the page
3. Click any request to `www.trustpilot.com` → **Headers** → **Request Headers**
4. Find the `cookie:` line → copy the entire value
5. Paste it as `TRUSTPILOT_COOKIE=...` in your `.env`

The session JWT is valid for ~90 days. After expiry, repeat the steps above.

---

## Usage

1. Paste any Trustpilot brand URL:
   ```
   https://www.trustpilot.com/review/brand.com
   ```
2. Click **Analyze** — the tool will:
   - Scrape all reviews (authenticated) or up to ~600 (anonymous)
   - Run Claude sentiment analysis on a representative sample
   - Build the full dashboard with themes, friction points, and NPS estimate

---

## Project Structure

```
app/
├── main.py          # FastAPI app, SSE streaming, job store
├── scraper.py       # Trustpilot scraper (httpx + Playwright)
├── analyzer.py      # Claude sentiment analysis
├── aggregator.py    # Report aggregation
├── models.py        # Pydantic data models
├── job_store.py     # In-memory job state
└── static/
    ├── index.html   # Single-page frontend
    ├── app.js       # Dashboard logic
    └── style.css    # Deep space navy design
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Your Anthropic API key |
| `TRUSTPILOT_COOKIE` | Optional | Browser session cookie for full scraping |

---

## Cost

Analysis uses `claude-haiku-4-5-20251001` on a 150-review sample per brand.
Estimated cost: **~$0.03–0.05 per brand analysis**.

---

## Roadmap

This is Module 1 of a larger CRO audit system. Planned modules:

- [ ] Technical QA audit
- [ ] Positioning audit
- [ ] Competitor analysis
- [ ] Copy & social proof analysis
- [ ] Hypothesis generation
- [ ] Experimentation tracking

---

## License

MIT
