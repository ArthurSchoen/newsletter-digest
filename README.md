# Daily Newsletter Digest

Reads my newsletter inbox every morning, condenses it with Gemini, checks its own numbers, and emails me one HTML briefing. Runs on a GitHub Actions cron, so there is no server.

## The problem

Eight newsletters, Bloomberg, the WSJ, The Rundown AI, Pragmatic Engineer and others, all landing between 5am and 8am. Reading them properly takes forty minutes, so in practice I skimmed the subject lines. The useful part is about fifteen facts and a handful of numbers. This extracts that and drops the rest.

## How it works

```
Gmail (IMAP)
     |
     |  one pass, last 24h, 8 known senders
     v
Text extraction          text/plain preferred, HTML stripped with BeautifulSoup
     |
     +-- Yahoo Finance --> 7 tickers, daily / weekly / monthly moves
     |                     computed in Python, never by the model
     v
Gemini, pass 1           all newsletter text in one prompt
     |                   returns the digest as HTML, fixed CSS classes
     v
Post-processing          inject the Python market table, normalize URLs,
     |                   fetch the person-of-the-day photo from Wikipedia
     v
Gemini, pass 2           every number cross-checked against the source text
     |
     v
HTML email -> Gmail (SMTP)
```

About ninety seconds per run, a fraction of a cent.

## Design decisions

**IMAP rather than an API.** Newsletters share no API and most have no feed. The one interface they all have is the inbox.

**Numbers never come from the model.** Market data is fetched from Yahoo Finance and the table is built in Python. The prompt forbids the model from producing a market table, and the post-processing deletes one anyway if it appears. A language model will write a plausible S&P close, and a plausible close is worse than none.

**A second pass checks the first.** Every number in the digest is extracted and cross-checked against the source text at temperature 0. Unsupported figures are marked as uncertain rather than deleted, since the check is a filter and not an oracle.

**The model writes HTML, not JSON.** The template is fixed and the model fills known CSS classes, so a rendering layer would add nothing.

**GitHub Actions rather than a server.** Ninety seconds a day on a free runner, nothing to patch. The cron fires at 10:30 UTC rather than the intuitive time because Actions cron runs late, typically two to three hours, when the shared runners are busy.

## Running it

```bash
pip install -r requirements.txt
python Newsletter_API.py
```

| Variable | What it is |
|---|---|
| `GENAI_API_KEY` | Google Gemini API key |
| `SENDER_EMAIL` | Gmail address the job reads and sends from |
| `SENDER_APP_PASSWORD` | Gmail App Password, not the account password |
| `RECEIVER_EMAIL` | Where the briefing goes, can be the same address |

In production these are GitHub Actions secrets, supplied by `.github/workflows/daily-digest.yml`. Locally, put them in a `.env` and run `run_digest.sh`.

Sources are configured in `NEWSLETTER_SOURCES` at the top of `Newsletter_API.py`: a display name mapped to strings matched case insensitively against the `From` header.
