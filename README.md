# Daily Newsletter Digest

Automated pipeline that reads your newsletter emails, summarizes them with Gemini, and sends you a premium HTML briefing every morning.

## How it works

1. **Fetch** — Connects to Gmail via IMAP and pulls newsletters from the last 24h
2. **Summarize** — Gemini generates a structured digest (5 Things to Know, Big Story, AI & Tech, Market Dashboard, Quiz, etc.)
3. **Enrich** — Injects live market data from Yahoo Finance and Wikipedia photos
4. **Fact-check** — A second Gemini pass flags potentially hallucinated numbers
5. **Send** — Delivers the final HTML email via Gmail SMTP

Runs daily via **GitHub Actions** (see `.github/workflows/daily-digest.yml`).

## Setup

```bash
pip install -r requirements.txt
```

Set these as GitHub Actions secrets (or local env vars):

| Secret | Description |
|--------|-------------|
| `GENAI_API_KEY` | Google Gemini API key |
| `SENDER_EMAIL` | Gmail address used to send |
| `SENDER_APP_PASSWORD` | Gmail App Password (not your regular password) |
| `RECEIVER_EMAIL` | Email address that receives the digest |

Configure newsletter sources in `NEWSLETTER_SOURCES` at the top of `Newsletter_API.py`.

## Run locally

```bash
python Newsletter_API.py          # run once
python Newsletter_API.py --schedule  # run daily at 08:00
```
