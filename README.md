# Daily Newsletter Digest

A scheduled job that reads my newsletter inbox every morning, condenses it with Gemini, checks its own numbers, and emails me one HTML briefing. It runs on a GitHub Actions cron, so there is no server to maintain.

## The problem

I subscribe to eight newsletters: Bloomberg, the WSJ, The Rundown AI, Pragmatic Engineer, ByteByteGo and a few others. Each one is a 2,000 word email and they all land between 5am and 8am. Reading them properly takes about forty minutes, so in practice I skimmed the subject lines and missed most of the content I had signed up for.

The useful part of those eight emails is maybe fifteen facts and a handful of numbers. This project extracts that and throws the rest away.

## How it works

```
Gmail (IMAP)
     |
     |  fetch mail from 8 known senders, last 24h
     v
Text extraction            text/plain preferred, HTML stripped with BeautifulSoup
     |
     +---- Yahoo Finance ----> 7 tickers, daily / weekly / monthly moves
     |                         (computed in Python, never by the model)
     v
Gemini 2.5 Flash, pass 1   one prompt with all newsletter text
     |                     returns the digest as HTML, fixed CSS classes
     v
Post-processing            inject the Python market table, normalize URLs,
     |                     look up the "person of the day" photo on Wikipedia
     v
Gemini 2.5 Flash, pass 2   extract every number, check it against the source text,
     |                     mark unsupported ones instead of deleting them
     v
HTML email template        inline styles, light mode forced
     |
     v
Gmail (SMTP) -> my inbox
```

The whole run takes about ninety seconds and costs a fraction of a cent.

## Technical choices, and why

**IMAP rather than an API.** Newsletters have no common API and most have no RSS feed. The only interface they all share is the inbox, so the inbox is the interface. A Gmail App Password authenticates in one line, where OAuth would have meant a consent screen and a token refresh flow for a job with exactly one user.

**Gemini Flash rather than a larger model.** The input is eight newsletters of full text, so the context window matters more than reasoning depth, and the task is compression rather than analysis. Flash handles it for roughly a tenth of a cent per run, which is what makes a daily job sensible to leave running.

**The model writes HTML, not JSON.** The output template is fixed and the model has to fill known CSS classes. Going through JSON would have meant a rendering layer in Python for no gain, since the structure is decided in advance either way.

**Numbers never come from the model.** Market data is fetched from Yahoo Finance and the table is built in Python. The prompt is told not to produce a market table, and the post-processing deletes one anyway if it appears. A language model will happily write a plausible S&P close, and a plausible close is worse than no close.

**A second pass checks the first.** The model gets its own output back as a list of extracted numbers, plus the source text, and returns the ones it cannot find support for. Those get marked as uncertain in the final email rather than removed, because the check itself is imperfect and silently deleting a correct number is worse than flagging it.

**GitHub Actions rather than a server.** The job runs once a day for ninety seconds. A cron on a free runner costs nothing and has no machine to patch. The schedule is set to 10:30 UTC rather than the intuitive time because Actions cron fires late, typically by two to three hours, when the shared runners are busy.

**Inline styles in the email.** Gmail and Outlook strip or rewrite `<style>` blocks unpredictably, so anything that must survive, the header gradient in particular, is also written inline on the element.

## Running it

```bash
pip install -r requirements.txt
python Newsletter_API.py
```

Four environment variables are required:

| Variable | What it is |
|---|---|
| `GENAI_API_KEY` | Google Gemini API key |
| `SENDER_EMAIL` | Gmail address the job reads and sends from |
| `SENDER_APP_PASSWORD` | Gmail App Password, not the account password |
| `RECEIVER_EMAIL` | Where the briefing is sent, can be the same address |

In production these are GitHub Actions secrets and the workflow in `.github/workflows/daily-digest.yml` supplies them. Locally, put them in a `.env` and use `run_digest.sh`.

Newsletter sources are configured in `NEWSLETTER_SOURCES` at the top of `Newsletter_API.py`. Each entry maps a display name to a list of strings matched case insensitively against the `From` header.

`python Newsletter_API.py --schedule` keeps the process alive and runs daily at 08:00 local time. That was the original design, before the job moved to Actions. It still works but it is redundant.

## Limits, and what I would do differently

**The mail fetch is quadratic and I would rewrite it first.** `fetch_newsletters_from_gmail` loops over the eight sources, and inside that loop it runs a fresh `SINCE` search and downloads up to 200 full messages. That is eight full passes over the same inbox, so roughly 1,600 message downloads to find eight emails. The correct shape is one search, one pass over the results, and match each message against all sources as it goes. It works today because the run is asynchronous and nobody is waiting, which is the only reason it was never fixed.

**The fact-check is weaker than it sounds.** It compares against only the first 3,000 characters of the combined sources, so a number that is correct but appears late in a long newsletter gets flagged. It also applies its marks with a regular expression on the raw HTML, which can match the same digits inside an unrelated string. It reduces confident nonsense rather than eliminating it, and the label in the email should say that more plainly than it does.

**There are no tests.** The parts that deserve them are the ones with real logic and no external dependency: `clean_html_content`, `sender_matches`, the percentage and basis point maths in `get_market_data`, and the market table builder. Those four are pure functions and would take an afternoon to cover. The IMAP and SMTP paths need fixtures, which is a bigger job and the reason none of it exists yet.

**Only the Gemini call retries.** It retries three times with a growing delay, which was added after a 503 killed a morning run. IMAP and SMTP get no such treatment, so a transient Gmail failure loses that day's briefing silently. Nothing alerts me when a run fails, I notice because no email arrives.

**The file is 1,300 lines and about 500 of them are a CSS template.** The pipeline itself is short. The HTML belongs in its own file, loaded and formatted at send time, which would make the actual logic readable at a glance.

**The App Password is broader than it needs to be.** It grants full mailbox access to a job that only needs to read eight senders and send to one address. OAuth with a read scope would be the right answer if this ever ran for anyone other than me.

**The schedule drifts twice a year.** The cron compensates for Actions delay in UTC, so the briefing arrives an hour late once daylight saving changes. The comment in the workflow says to edit it by hand, which is a note to myself rather than a fix.
