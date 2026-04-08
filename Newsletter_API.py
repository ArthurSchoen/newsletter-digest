"""
Daily News Digest — Plan 3 (fully automatic)

Pipeline:
  1. Connect to Gmail via IMAP (uses your existing App Password)
  2. Fetch newsletters received in the last 24 hours, filtered by sender
  3. Clean and extract text content from each email
  4. Ask Gemini to produce a structured HTML digest
  5. Wrap in a premium HTML email template
  6. Send via Gmail SMTP
  7. Repeat every day at 08:00

Usage:
  python Newsletter_API.py           # run now, once
  python Newsletter_API.py --schedule  # run at 08:00 every day (keeps process alive)

Required env vars:
  export GENAI_API_KEY="..."
  export SENDER_EMAIL="you@gmail.com"
  export SENDER_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # Gmail App Password
  export RECEIVER_EMAIL="you@gmail.com"              # can be same as sender

Configure the newsletter sources in NEWSLETTER_SOURCES below.
"""

import os
import re
import sys
import json
import imaplib
import email
import smtplib
import schedule
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header as _decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import yfinance as yf
from bs4 import BeautifulSoup
from google import genai
from google.genai import types


# ============================================================
# NEWSLETTER SOURCES — configure your senders here
# ============================================================
# Each entry: "Readable Name": ["keyword or email to match in the From field"]
# The match is case-insensitive and checks if any keyword appears in the From header.
NEWSLETTER_SOURCES = {
    "Aktionnaire":      ["aktionnaire", "team@aktionnaire.com"],
    "Financial Times":  ["financialtimes", "noreply@mail.wsj.com", "wsj.com", "thewallstreetjournal"],
    "Rundown AI":       ["rundown", "jack@therundown.ai", "therundown.ai"],
    "Bloomberg":        ["noreply@news.bloomberg.com"],
    "Practical Engineering":       ["pragmaticengineer@substack.com", "pragmaticengineer+the-pulse@substack.com"],
    "Swirl AI":       ["swirlai@substack.com"],
    "ByteByteGo":     ["bytebytego@substack.com"],
    "Substack (other)": ["no-reply@substack.com"],
}

# How many hours back to look for newsletters (default: last 24h)
LOOKBACK_HOURS = 24


# ============================================================
# API CONFIG
# ============================================================
GENAI_API_KEY = os.getenv("GENAI_API_KEY")
if not GENAI_API_KEY:
    raise ValueError("GENAI_API_KEY is not set. Run: export GENAI_API_KEY=your_key")

client = genai.Client(api_key=GENAI_API_KEY)


# ============================================================
# EMAIL CONFIG
# ============================================================
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_APP_PASSWORD = os.getenv("SENDER_APP_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")

if not SENDER_EMAIL or not SENDER_APP_PASSWORD or not RECEIVER_EMAIL:
    raise ValueError(
        "Missing email env vars. Set:\n"
        "  export SENDER_EMAIL=you@gmail.com\n"
        "  export SENDER_APP_PASSWORD='xxxx xxxx xxxx xxxx'\n"
        "  export RECEIVER_EMAIL=you@gmail.com"
    )


# ============================================================
# GMAIL IMAP — FETCH NEWSLETTERS
# ============================================================
def connect_to_gmail() -> imaplib.IMAP4_SSL:
    """Open an authenticated IMAP connection to Gmail."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
    return mail


def decode_header(value: str) -> str:
    """Decode an encoded email header to a plain string."""
    parts = _decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def extract_text_from_message(msg: email.message.Message) -> str:
    """
    Extract readable text from an email message.
    Prefers text/plain; falls back to stripping text/html with BeautifulSoup.
    """
    plain_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return "\n".join(plain_parts)

    # Fall back to HTML stripping
    if html_parts:
        return clean_html_content("\n".join(html_parts))

    return ""


def clean_html_content(html: str) -> str:
    """
    Strip HTML to plain readable text.
    Removes scripts, styles, and common newsletter noise (footers, unsubscribe links).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content elements
    for tag in soup(["script", "style", "head", "meta", "link", "img"]):
        tag.decompose()

    # Remove elements that are likely footer/unsubscribe noise
    noise_keywords = ["unsubscribe", "view in browser", "view online", "manage preferences",
                      "privacy policy", "terms of service", "update your preferences"]
    for element in soup.find_all(string=True):
        if any(kw in element.lower() for kw in noise_keywords):
            parent = element.parent
            if parent and parent.name in ("p", "div", "td", "span", "a"):
                parent.decompose()

    text = soup.get_text(separator="\n")
    # Collapse excessive whitespace
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def sender_matches(from_header: str, keywords: list) -> bool:
    """Return True if any keyword appears in the From header (case-insensitive)."""
    from_lower = from_header.lower()
    return any(kw.lower() in from_lower for kw in keywords)


def fetch_newsletters_from_gmail(
    sources: dict = NEWSLETTER_SOURCES,
    lookback_hours: int = LOOKBACK_HOURS,
) -> dict:
    """
    Connect to Gmail, find emails from each configured source sent in the
    last `lookback_hours` hours, and return {source_name: combined_text}.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    # IMAP SINCE uses day granularity — search one extra day back, filter by time
    since_str = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")

    print(f"📬 Connecting to Gmail IMAP...")
    mail = connect_to_gmail()
    mail.select("INBOX")

    newsletters = {}

    for source_name, keywords in sources.items():
        found_texts = []

        # Build a broad SINCE search, then filter by sender keywords in Python
        status, data = mail.search(None, f'SINCE "{since_str}"')
        if status != "OK" or not data[0]:
            continue

        msg_ids = data[0].split()
        # Search newest first — avoid loading entire inbox if large
        for msg_id in reversed(msg_ids[-200:]):
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            from_header = decode_header(msg.get("From", ""))
            if not sender_matches(from_header, keywords):
                continue

            # Check timestamp
            date_str = msg.get("Date", "")
            try:
                msg_date = email.utils.parsedate_to_datetime(date_str)
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                if msg_date < cutoff:
                    continue
            except Exception:
                pass  # If parsing fails, keep the email

            subject = decode_header(msg.get("Subject", "(no subject)"))
            print(f"   ✅ {source_name}: found «{subject}»")

            text = extract_text_from_message(msg)
            if text.strip():
                found_texts.append(f"[{subject}]\n{text}")

        if found_texts:
            newsletters[source_name] = "\n\n---\n\n".join(found_texts)
        else:
            print(f"   ⚠️  {source_name}: no email found in the last {lookback_hours}h")

    mail.logout()

    if not newsletters:
        print("⚠️  No newsletters fetched from Gmail. Using embedded samples as fallback.")
        newsletters = _embedded_sample_newsletters()

    return newsletters


# ============================================================
# LIVE MARKET DATA (Yahoo Finance)
# ============================================================
def get_market_data() -> list:
    """
    Fetch live prices with daily, weekly, and monthly comparisons.
    Returns a list of dicts:
      {label, price, change, direction, week_change, week_dir, month_change, month_dir}
    Numbers come from yfinance — never from the LLM.
    """
    tickers = {
        "S&P 500":    "^GSPC",
        "Nasdaq 100": "^NDX",
        "CAC 40":     "^FCHI",
        "Brent Oil":  "BZ=F",
        "Gold":       "GC=F",
        "Bitcoin":    "BTC-USD",
        "10Y UST":    "^TNX",
    }
    results = []
    for label, symbol in tickers.items():
        try:
            hist = yf.Ticker(symbol).history(period="1mo")
            if len(hist) < 2:
                continue
            last = hist["Close"].iloc[-1]
            prev = hist["Close"].iloc[-2]
            is_tnx = "TNX" in symbol

            # Daily change
            chg_pct = (last - prev) / prev * 100
            direction = "up" if chg_pct >= 0 else "down"
            if is_tnx:
                price_str = f"{last:.2f}%"
                bps = abs(last - prev) * 100
                change_str = f"{'+'if chg_pct>=0 else '−'}{bps:.0f} bps"
            else:
                price_str = f"{last:,.2f}"
                sign = "+" if chg_pct >= 0 else ""
                change_str = f"{sign}{chg_pct:.2f}%"

            # Weekly change (vs ~5 trading days ago)
            week_idx = min(5, len(hist) - 1)
            week_prev = hist["Close"].iloc[-1 - week_idx]
            week_pct = (last - week_prev) / week_prev * 100
            week_dir = "up" if week_pct >= 0 else "down"
            week_sign = "+" if week_pct >= 0 else ""
            week_change = f"{week_sign}{week_pct:.1f}%"

            # Monthly change (vs first available in 1mo window)
            month_prev = hist["Close"].iloc[0]
            month_pct = (last - month_prev) / month_prev * 100
            month_dir = "up" if month_pct >= 0 else "down"
            month_sign = "+" if month_pct >= 0 else ""
            month_change = f"{month_sign}{month_pct:.1f}%"

            results.append({
                "label": label, "price": price_str,
                "change": change_str, "direction": direction,
                "week_change": week_change, "week_dir": week_dir,
                "month_change": month_change, "month_dir": month_dir,
            })
        except Exception:
            pass
    return results


def _market_data_for_prompt(data: list) -> str:
    """Format structured market data as plain text for the prompt."""
    if not data:
        return "Market data unavailable."
    return "\n".join(f"  {d['label']}: {d['price']} ({d['change']})" for d in data)


def build_market_table_html(data: list) -> str:
    """
    Build the market table entirely in Python from structured data.
    Shows price, daily move, weekly change, and monthly change.
    Numbers are never touched by the LLM.
    """
    if not data:
        return "<p>Market data unavailable.</p>"
    rows = ""
    for d in data:
        rows += (
            f'    <tr><td>{d["label"]}</td><td>{d["price"]}</td>'
            f'<td><span class="pill {d["direction"]}">{d["change"]}</span></td>'
            f'<td><span class="pill {d["week_dir"]}">{d["week_change"]}</span></td>'
            f'<td><span class="pill {d["month_dir"]}">{d["month_change"]}</span></td></tr>\n'
        )
    return (
        f'<table class="market-table"><thead>'
        f'<tr><th>Asset</th><th>Price</th><th>Daily</th><th>vs 1W</th><th>vs 1M</th></tr>'
        f'</thead><tbody>\n{rows}</tbody></table>'
    )


# ============================================================
# PROMPT BUILDER
# ============================================================
def build_prompt(newsletters: dict, market_data: list) -> str:
    today = date.today().strftime("%A %d %B %Y")
    source_count = len(newsletters)

    combined = ""
    for source, text in newsletters.items():
        combined += f"\n\n=== SOURCE: {source.upper()} ===\n{text.strip()}\n"

    market_summary = _market_data_for_prompt(market_data)

    return f"""
You are writing a premium daily briefing for {today}.
Reader: loves finance, AI, business strategy, geopolitics, sharp synthesis.
Goal: scan in 10 seconds — what matters, what number to remember, what story dominates.

RULES:
- Every sentence earns its place. No filler.
- Numbers in every section: %, $, bps, multiples, dates.
- Ignore ads, partnerships, promotional content in newsletters.
- Section weight follows today's actual news importance.
- Opinions fine when evidence-based.
- ZERO REPETITION: A fact/topic used in "5 Things to Know" MUST NOT reappear in "AI & Tech", "Big Story", or any other section. Each section covers DIFFERENT stories. Treat each section as drawing from a shrinking pool of stories.
- Return VALID HTML ONLY. No markdown. No code fences.

MARKET NUMBERS FOR CONTEXT (reference only — the market table is built separately):
{market_summary}

══════════════════════════════════════════
OUTPUT — every field must be filled. Never write "N/A", "no data", or leave a placeholder.
══════════════════════════════════════════

<!-- 5 THINGS TO KNOW: 5 bold one-liners. Each: bold headline + one supporting sentence. Max 20 words total per item. No emojis. -->
<h2>5 Things to Know</h2>
<ul class="five-things">
  <li><strong>Bold headline.</strong> Supporting sentence with a number.</li>
  <li><strong>Bold headline.</strong> Supporting sentence with a number.</li>
  <li><strong>Bold headline.</strong> Supporting sentence.</li>
  <li><strong>Bold headline.</strong> Supporting sentence with a number.</li>
  <li><strong>Bold headline.</strong> Supporting sentence.</li>
</ul>

<!-- NUMBER OF THE DAY: the single most striking number from today's news. -->
<h2>Number of the Day</h2>
<div class="number-card">
  <div class="number-label">Short label — what it refers to</div>
  <div class="number-big">$X / X% / Xbn</div>
  <div class="number-why"><strong>Why it matters:</strong> One direct sentence. Max 20 words.</div>
</div>

<!-- BIG STORY: the most important story. Keep it tight — max 100 words total across paragraphs. -->
<h2>Big Story</h2>
<div class="big-story-card">
  <div class="big-story-title">Strong, specific headline — not generic</div>
  <p>First paragraph: what happened. Facts, context, numbers. 2-3 sentences max.</p>
  <p>Second paragraph: why it matters. Second-order implications. Be concrete.</p>
  <div class="investor-lens"><strong>Investor lens:</strong> What this means for positioning or sector exposure. One sentence.</div>
</div>

<!-- SO WHAT? callout: why the Big Story matters to a general reader, not just investors. Written accessibly. -->
<div class="so-what-card">
  <div class="so-what-label">Why this matters to you</div>
  <p>One paragraph, 2-3 sentences. Written for a general reader. Explain the real-world impact beyond markets.</p>
</div>

<!-- AI & TECH: 4–5 mini cards. Company/topic + key metric + one-line implication. MUST use DIFFERENT stories than 5 Things — no topic overlap allowed. -->
<h2>AI &amp; Tech</h2>
<div class="ai-grid">
  <div class="ai-card">
    <div class="ai-company">Company or Topic</div>
    <div class="ai-number">Key metric or number</div>
    <div class="ai-line">One-line implication. Max 15 words.</div>
  </div>
  <!-- 4–5 ai-cards -->
</div>

<!-- PERSON OF THE DAY: one relevant figure. Keep it short — name, title, quote, one-line "why today". No long bio. -->
<h2>Person of the Day</h2>
<div class="person-card">
  <div class="person-avatar">XX</div>
  <div class="person-body">
    <div class="person-name">Full Name</div>
    <div class="person-title">Title · Organization</div>
    <div class="person-why">Why today: one sentence connecting this person to today's news.</div>
    <div class="person-quote">"A real or widely attributed quote that captures their thinking."</div>
  </div>
</div>

<!-- VIDEO OF THE DAY: pick a real, recent YouTube video worth watching. Finance, AI, geopolitics, business. -->
<h2>Video of the Day</h2>
<div class="media-card video-card">
  <div class="media-title">Real video title</div>
  <div class="media-source">Channel name · Duration if known</div>
  <div class="media-why">Why watch: one specific sentence on what makes this worth 20 minutes.</div>
  <a class="cta-btn" href="https://www.youtube.com/results?search_query=REAL+SEARCH+TERMS">Watch on YouTube →</a>
</div>

<!-- PAPER OF THE DAY: a real paper, report, or deep analysis. Relevant to today's themes. -->
<h2>Paper of the Day</h2>
<div class="media-card paper-card">
  <div class="media-title">Real paper or report title</div>
  <div class="media-source">Authors / Institution / Year</div>
  <div class="media-why">Why it matters: one sentence.</div>
  <div class="media-summary">4–5 line summary of key findings or argument.</div>
  <a class="cta-btn" href="https://scholar.google.com/scholar?q=REAL+PAPER+TITLE">Read paper →</a>
</div>

<!-- QUIZ: exactly 5 questions. Varied difficulty. Based on today's content. Compact. -->
<h2>Quiz of the Day</h2>
<div class="quiz-box">
  <ol class="quiz-list">
    <li class="quiz-q"><span class="quiz-diff easy">Easy</span> Question?<br>
      <span class="quiz-opt">A. ...</span><span class="quiz-opt">B. ...</span><span class="quiz-opt">C. ...</span><span class="quiz-opt">D. ...</span>
    </li>
    <li class="quiz-q"><span class="quiz-diff easy">Easy</span> Question?<br>
      <span class="quiz-opt">A. ...</span><span class="quiz-opt">B. ...</span><span class="quiz-opt">C. ...</span><span class="quiz-opt">D. ...</span>
    </li>
    <li class="quiz-q"><span class="quiz-diff medium">Medium</span> Question?<br>
      <span class="quiz-opt">A. ...</span><span class="quiz-opt">B. ...</span><span class="quiz-opt">C. ...</span><span class="quiz-opt">D. ...</span>
    </li>
    <li class="quiz-q"><span class="quiz-diff medium">Medium</span> Question?<br>
      <span class="quiz-opt">A. ...</span><span class="quiz-opt">B. ...</span><span class="quiz-opt">C. ...</span><span class="quiz-opt">D. ...</span>
    </li>
    <li class="quiz-q"><span class="quiz-diff numbers">Numbers</span> Question requiring a specific figure?<br>
      <span class="quiz-opt">A. ...</span><span class="quiz-opt">B. ...</span><span class="quiz-opt">C. ...</span><span class="quiz-opt">D. ...</span>
    </li>
  </ol>
</div>

<h2>Answers</h2>
<div class="answers-box">
  <ol>
    <li><strong>X</strong> — brief explanation</li>
    <li><strong>X</strong> — brief explanation</li>
    <li><strong>X</strong> — brief explanation</li>
    <li><strong>X</strong> — brief explanation</li>
    <li><strong>X</strong> — brief explanation</li>
  </ol>
</div>

STRICT RULES:
- ABSOLUTE RULE: Do NOT repeat any topic, company, or fact across sections. If a topic appears in 5 Things, it CANNOT appear in AI & Tech or Big Story. Each section draws from DIFFERENT stories.
- The market table is NOT in your output — it is injected separately. Do NOT output a market table.
- All <a href> must be real absolute URLs starting with https://. Never use "#".
- For Video: use href="https://www.youtube.com/results?search_query=VIDEO+TITLE+KEYWORDS"
- For Paper: use href="https://scholar.google.com/scholar?q=PAPER+TITLE+KEYWORDS"
- Return HTML only. No markdown fences. No code blocks.

NEWSLETTERS ({source_count} sources):
{combined}
"""


# ============================================================
# WIKIPEDIA PHOTO LOOKUP
# ============================================================
def get_wikipedia_photo(name: str) -> str | None:
    """
    Fetch a person's thumbnail photo URL from Wikipedia's free REST API.
    Returns the image URL string, or None if not found.
    No API key required.
    """
    try:
        slug = urllib.parse.quote(name.replace(" ", "_"))
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "NewsDigest/1.0 (personal)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
            # Prefer the larger originalimage, fall back to thumbnail
            img = data.get("originalimage") or data.get("thumbnail")
            if img:
                return img["source"]
    except Exception:
        pass
    return None


# ============================================================
# POST-PROCESSING: market table rebuild + URL fix
# ============================================================
def post_process_html(html: str, market_data: list) -> str:
    """
    1. Remove any market table Gemini may have generated (prompt says not to, but safety).
    2. Inject the Python-built market table after the So What? callout.
    3. Re-encode all href URLs so query strings are properly formatted.
    """
    # ── Remove any market table Gemini may have output ──
    html = re.sub(r'<table class="market-table".*?</table>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove any stray Market Dashboard heading Gemini might output
    html = re.sub(r'<h2[^>]*>Market Dashboard</h2>\s*', '', html, flags=re.IGNORECASE)

    # ── Build and inject market dashboard after the So What? card ──
    real_table = build_market_table_html(market_data)
    market_section = f'\n<h2>Market Dashboard</h2>\n{real_table}\n'
    # Try to inject after the so-what-card
    so_what_end = re.search(r'</div>\s*(?=\s*<h2[^>]*>AI)', html, re.IGNORECASE)
    if so_what_end:
        pos = so_what_end.start() + len(so_what_end.group(0))
        html = html[:pos] + market_section + html[pos:]
    else:
        # Fallback: inject before AI & Tech heading
        html = re.sub(
            r'(<h2[^>]*>AI\s*&amp;\s*Tech</h2>)',
            market_section + r'\1',
            html, flags=re.IGNORECASE
        )

    # ── Fix URL encoding on all hrefs ──
    def fix_href(m):
        raw = m.group(1)
        try:
            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            fixed_qs = urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
            fixed = urllib.parse.urlunparse(parsed._replace(query=fixed_qs))
            return f'href="{fixed}"'
        except Exception:
            return m.group(0)

    html = re.sub(r'href="(https?://[^"]+)"', fix_href, html)

    # ── Inject Wikipedia photo for Person of the Day ──
    name_match = re.search(r'class="person-name"[^>]*>(.*?)</div>', html, re.DOTALL)
    if name_match:
        person_name = re.sub(r'<[^>]+>', '', name_match.group(1)).strip()
        print(f"🖼️  Fetching photo for: {person_name}")
        photo_url = get_wikipedia_photo(person_name)
        if photo_url:
            print(f"   ✅ Found: {photo_url[:60]}...")
            # Replace the initials avatar div with a real photo
            img_tag = (
                f'<img src="{photo_url}" alt="{person_name}" '
                f'class="person-avatar-img" '
                f'style="width:72px;height:72px;border-radius:50%;object-fit:cover;'
                f'flex-shrink:0;border:2px solid #e4e0fb;">'
            )
            html = re.sub(
                r'<div class="person-avatar">[^<]*</div>',
                img_tag,
                html
            )
        else:
            print(f"   ⚠️  No Wikipedia photo found, keeping initials avatar.")

    return html


# ============================================================
# PASS 2: FACT-CHECK
# ============================================================
def verify_facts(html: str, newsletters_combined: str) -> str:
    """
    Second Gemini pass: extract all numbers from the digest, cross-check against
    newsletter sources, and bold any that are not supported. Returns the full HTML
    unchanged except for <span class="uncertain"> wrappers on suspicious values.
    """
    # Extract just the numbers present in the digest for a lightweight check
    numbers_found = re.findall(r'[\$€£]?[\d,]+\.?\d*\s*(?:%|bn|tn|bps|k|M|B)?', html)
    numbers_str = ", ".join(sorted(set(numbers_found)))[:1500]
    sources_excerpt = newsletters_combined[:3000]

    verify_prompt = f"""You are a fact-checker for a financial newsletter.

The following numbers appear in a digest: {numbers_str}

Cross-check these against the newsletter sources below.
Return a JSON list of numbers that are NOT present in the sources and are likely hallucinated:
["number1", "number2", ...]
Return an empty list [] if everything checks out.
Do not explain. JSON only.

NEWSLETTER SOURCES:
{sources_excerpt}"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=verify_prompt,
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=500),
        )
        raw = response.text.strip()
        # Parse the list of suspicious numbers
        suspicious = re.findall(r'"([^"]+)"', raw)
        if suspicious:
            print(f"   ⚠️  Flagged as unverified: {suspicious}")
            # Bold them in the HTML (wrap with <span class="uncertain">)
            for val in suspicious:
                escaped = re.escape(val)
                html = re.sub(
                    rf'(?<!["\w]){escaped}(?![\w"])',
                    f'<span class="uncertain">{val}</span>',
                    html
                )
    except Exception as e:
        print(f"   ⚠️  Fact-check pass failed ({e}), skipping.")

    # Always return the full original HTML (never truncated)
    return html


# ============================================================
# GEMINI SUMMARIZATION
# ============================================================
def generate_digest_html(newsletters: dict) -> tuple:
    """Returns (digest_html, source_count)."""
    print("📊 Fetching live market data...")
    market_data = get_market_data()
    summary = _market_data_for_prompt(market_data)
    print(f"   Done:\n{summary}")

    prompt = build_prompt(newsletters, market_data)

    print("✍️  Pass 1: generating digest...")
    html = None
    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=16000,
                    system_instruction=(
                        "You are a top-tier international editor producing a refined daily briefing. "
                        "Return only valid HTML using the exact CSS classes specified. No markdown fences."
                    ),
                ),
            )
            html = response.text.strip()
            break
        except Exception as e:
            print(f"   ⚠️  Attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                wait = 30 * attempt
                print(f"   ⏳ Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    if html.startswith("```"):
        html = html.split("\n", 1)[-1].rsplit("```", 1)[0]
    html = html.strip()

    print("🔧  Post-processing: rebuilding market table + fixing URLs...")
    html = post_process_html(html, market_data)

    print("🔍  Pass 2: fact-checking numbers...")
    newsletters_combined = "\n\n".join(newsletters.values())
    html = verify_facts(html, newsletters_combined)

    return html, len(newsletters)


# ============================================================
# HTML EMAIL TEMPLATE
# ============================================================
def wrap_in_email_template(content_html: str, source_count: int = 3) -> str:
    today = date.today().strftime("%A, %B %d %Y")
    generated_at = datetime.now().strftime("%H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light only">
  <meta name="supported-color-schemes" content="light only">
  <style>
    /* Force light mode in email clients */
    :root {{ color-scheme: light only; supported-color-schemes: light only; }}
    [data-ogsc] body, [data-ogsb] body {{ background: #e8edf3 !important; color: #1a202c !important; }}
    /* ── Reset ── */
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
      background: #e8edf3;
      color: #1a202c;
      padding: 20px 12px 40px;
    }}

    /* ── Shell ── */
    .container {{
      max-width: 720px;
      margin: auto;
      background: #ffffff;
      border-radius: 18px;
      box-shadow: 0 12px 50px rgba(0,0,0,0.12);
      overflow: hidden;
    }}

    /* ── Header ── */
    .header {{
      background: linear-gradient(135deg, #0a1f3c 0%, #0f3460 100%);
      color: white;
      padding: 20px 30px 18px;
    }}
    .header-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .header h1 {{
      font-size: 20px;
      font-weight: 800;
      letter-spacing: -0.3px;
      line-height: 1.2;
      color: #ffffff;
    }}
    .header-sub {{
      font-size: 10px;
      color: rgba(255,255,255,0.55);
      margin-top: 3px;
      letter-spacing: 0.8px;
      text-transform: uppercase;
    }}
    .header-badges {{
      display: flex;
      gap: 6px;
    }}
    .badge {{
      background: rgba(255,255,255,0.1);
      border: 1px solid rgba(255,255,255,0.18);
      color: rgba(255,255,255,0.85);
      font-size: 10px;
      padding: 3px 9px;
      border-radius: 20px;
      white-space: nowrap;
      font-weight: 600;
    }}
    .header-date {{
      font-size: 11px;
      color: rgba(255,255,255,0.45);
      margin-top: 8px;
      letter-spacing: 0.3px;
    }}

    /* ── Content wrapper ── */
    .content {{ padding: 32px 36px 24px; }}

    /* ── Section headings ── */
    h2 {{
      font-size: 15px;
      font-weight: 800;
      color: #0a1f3c;
      background: none;
      border: none;
      padding: 0;
      border-radius: 0;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 40px 0 14px;
      display: block;
    }}
    h2:first-child {{ margin-top: 0; }}

    /* ── Body text ── */
    p {{
      font-size: 14px;
      line-height: 1.75;
      color: #374151;
      margin-bottom: 8px;
    }}

    /* ── 5 Things to Know ── */
    .five-things {{
      list-style: none;
      padding: 0;
      background: #ffffff;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
      padding: 8px 0;
      margin: 0;
    }}
    .five-things li {{
      font-size: 14px;
      line-height: 1.6;
      color: #1e3a5f;
      padding: 10px 20px;
      border-bottom: 1px solid #f3f4f6;
      margin: 0;
    }}
    .five-things li:last-child {{ border-bottom: none; }}
    .five-things li::before {{
      content: "›";
      color: #2563eb;
      font-weight: 800;
      font-size: 18px;
      margin-right: 10px;
    }}

    /* ── Number of the Day ── */
    .number-card {{
      background: #eaf2ff;
      border: 1px solid #bfdbfe;
      border-radius: 14px;
      padding: 24px 28px;
      text-align: center;
      margin: 4px 0;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .number-label {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 1.2px;
      color: #4b5563;
      margin-bottom: 8px;
      font-weight: 700;
    }}
    .number-big {{
      font-size: 52px;
      font-weight: 800;
      color: #0f2747;
      letter-spacing: -2px;
      line-height: 1;
      margin-bottom: 10px;
    }}
    .number-why {{
      font-size: 13px;
      color: #374151;
      line-height: 1.55;
    }}
    .number-why strong {{ color: #172033; }}

    /* ── Big Story ── */
    .big-story-card {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 14px;
      padding: 24px 26px;
      margin: 4px 0;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .big-story-title {{
      font-size: 17px;
      font-weight: 800;
      color: #0f172a;
      margin-bottom: 14px;
      line-height: 1.3;
    }}
    .big-story-card p {{
      font-size: 14px;
      line-height: 1.7;
      color: #374151;
      margin-bottom: 10px;
    }}
    .big-story-card p:last-of-type {{ margin-bottom: 14px; }}
    .investor-lens {{
      font-size: 13px;
      font-style: italic;
      color: #4b5563;
      border-top: 1px solid #e2e8f0;
      padding-top: 12px;
      line-height: 1.55;
    }}
    .investor-lens strong {{ color: #0f3460; font-style: normal; }}

    /* ── So What? Callout ── */
    .so-what-card {{
      background: linear-gradient(135deg, #fef3c7, #fde68a);
      border: 1px solid #f59e0b;
      border-radius: 14px;
      padding: 20px 24px;
      margin: 16px 0 4px;
      box-shadow: 0 2px 8px rgba(245,158,11,0.15);
    }}
    .so-what-label {{
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 1.2px;
      color: #92400e;
      margin-bottom: 8px;
    }}
    .so-what-card p {{
      font-size: 14px;
      line-height: 1.7;
      color: #78350f;
      margin: 0;
    }}

    /* ── Market Table ── */
    .market-table {{
      width: 100%;
      border-collapse: collapse;
      margin: 4px 0;
      font-size: 13px;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .market-table th {{
      background: #0f2544;
      color: white;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 1px;
      padding: 10px 10px;
      text-align: left;
      font-weight: 700;
    }}
    .market-table th:nth-child(n+2) {{ text-align: right; }}
    .market-table td {{
      padding: 10px 10px;
      border-bottom: 1px solid #f3f4f6;
      color: #374151;
      vertical-align: middle;
    }}
    .market-table td:nth-child(n+2) {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .market-table td:first-child {{ font-weight: 600; color: #111827; }}
    .market-table tr:last-child td {{ border-bottom: none; }}
    .market-table tr:nth-child(even) td {{ background: #f9fafb; }}
    .pill {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .pill.up   {{ color: #059669; background: #ecfdf5; }}
    .pill.down {{ color: #dc2626; background: #fef2f2; }}

    /* ── AI Grid ── */
    .ai-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin: 4px 0;
    }}
    .ai-card {{
      background: #f5f3ff;
      border: 1px solid #ede9fe;
      border-top: 3px solid #7c3aed;
      border-radius: 12px;
      padding: 16px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .ai-company {{
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: #6d28d9;
      margin-bottom: 4px;
    }}
    .ai-number {{
      font-size: 18px;
      font-weight: 800;
      color: #1f2937;
      margin-bottom: 4px;
    }}
    .ai-line {{
      font-size: 12px;
      line-height: 1.5;
      color: #4b5563;
    }}

    /* ── Media Cards (Video / Paper) ── */
    .media-card {{
      border-radius: 14px;
      padding: 20px 22px;
      margin: 4px 0;
      border: 1px solid #e5e7eb;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .video-card {{ background: #fffbeb; border-color: #fcd34d; }}
    .paper-card {{ background: #fafaf7; border-color: #d1d5db; }}
    .media-title {{
      font-size: 15px;
      font-weight: 700;
      color: #111827;
      margin-bottom: 4px;
      line-height: 1.4;
    }}
    .media-source {{
      font-size: 11px;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 8px;
    }}
    .media-why {{
      font-size: 13px;
      color: #374151;
      line-height: 1.6;
      margin-bottom: 10px;
    }}
    .media-summary {{
      font-size: 13px;
      color: #4b5563;
      line-height: 1.65;
      margin-bottom: 12px;
      padding-top: 8px;
      border-top: 1px solid #e5e7eb;
    }}
    .cta-btn {{
      display: inline-block;
      background: #0f2544;
      color: white !important;
      font-size: 12px;
      font-weight: 700;
      padding: 8px 18px;
      border-radius: 8px;
      text-decoration: none;
      letter-spacing: 0.3px;
    }}

    /* ── Person of the Day ── */
    .person-card {{
      display: flex;
      gap: 18px;
      align-items: flex-start;
      background: #f8f7ff;
      border: 1px solid #e4e0fb;
      border-radius: 14px;
      padding: 20px 22px;
      margin: 4px 0;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .person-avatar {{
      flex-shrink: 0;
      width: 56px;
      height: 56px;
      border-radius: 50%;
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      color: #ffffff;
      font-size: 18px;
      font-weight: 800;
      display: flex;
      align-items: center;
      justify-content: center;
      letter-spacing: 1px;
    }}
    .person-body {{ flex: 1; min-width: 0; }}
    .person-name {{
      font-size: 16px;
      font-weight: 800;
      color: #1f2937;
      margin-bottom: 2px;
      line-height: 1.2;
    }}
    .person-title {{
      font-size: 11px;
      font-weight: 700;
      color: #6d28d9;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      margin-bottom: 10px;
    }}
    .person-why {{
      font-size: 13px;
      color: #374151;
      font-style: italic;
      line-height: 1.55;
      margin-bottom: 10px;
      padding: 8px 12px;
      background: rgba(124,58,237,0.06);
      border-radius: 8px;
    }}
    .person-quote {{
      font-size: 13px;
      color: #374151;
      font-style: italic;
      border-left: 3px solid #7c3aed;
      padding-left: 12px;
      line-height: 1.5;
    }}

    /* ── Quiz ── */
    .quiz-box {{
      background: #eff6ff;
      border-radius: 14px;
      padding: 20px 22px;
      margin: 4px 0;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .quiz-list {{
      padding-left: 18px;
      margin: 0;
    }}
    .quiz-q {{
      font-size: 13px;
      color: #172033;
      margin-bottom: 18px;
      line-height: 1.6;
    }}
    .quiz-q:last-child {{ margin-bottom: 0; }}
    .quiz-diff {{
      display: inline-block;
      font-size: 9px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 1px;
      padding: 2px 7px;
      border-radius: 4px;
      margin-right: 6px;
    }}
    .easy   {{ background: #dcfce7; color: #166534; }}
    .medium {{ background: #fef9c3; color: #854d0e; }}
    .numbers {{ background: #fee2e2; color: #991b1b; }}
    .quiz-opt {{
      display: inline-block;
      margin-right: 14px;
      font-size: 12px;
      color: #374151;
    }}

    /* ── Answers ── */
    .answers-box {{
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      padding: 18px 22px;
      margin: 4px 0;
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }}
    .answers-box ol {{
      padding-left: 18px;
      margin: 0;
    }}
    .answers-box li {{
      font-size: 13px;
      color: #374151;
      line-height: 1.6;
      margin-bottom: 6px;
    }}
    .answers-box li:last-child {{ margin-bottom: 0; }}

    /* ── Uncertain / unverified values ── */
    .uncertain {{
      font-weight: 700;
    }}

    /* ── Mobile-first: stack flex layouts on narrow screens ── */
    @media (max-width: 600px) {{
      .content {{ padding: 20px 16px 16px; }}
      .ai-grid {{ grid-template-columns: 1fr; }}
      .person-card {{ flex-direction: column; align-items: center; text-align: center; }}
      .person-quote {{ text-align: left; }}
      .market-table {{ font-size: 11px; }}
      .market-table th, .market-table td {{ padding: 8px 6px; }}
      .number-big {{ font-size: 40px; }}
      h2 {{ display: block; }}
    }}

    /* ── Footer ── */
    .footer {{
      background: #0a1f3c;
      color: rgba(255,255,255,0.5);
      font-size: 11px;
      padding: 18px 36px;
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .footer span {{ white-space: nowrap; }}
  </style>
</head>
<body>
  <div class="container">

    <!-- HEADER: inline styles are the Gmail-safe fallback — CSS gradients can be stripped -->
    <div class="header" style="background-color:#0a1f3c;background-image:linear-gradient(135deg,#0a1f3c 0%,#0f3460 100%);color:#ffffff;padding:20px 30px 18px;">
      <div class="header-top" style="display:flex;justify-content:space-between;align-items:center;">
        <div>
          <h1 style="font-size:20px;font-weight:800;color:#ffffff;margin:0;line-height:1.2;">Morning Briefing</h1>
          <div class="header-sub" style="font-size:10px;color:rgba(255,255,255,0.65);margin-top:3px;letter-spacing:0.8px;text-transform:uppercase;">Markets &middot; AI &middot; Tech &middot; Strategy</div>
        </div>
        <div class="header-badges" style="display:flex;gap:6px;">
          <span class="badge" style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.25);color:#ffffff;font-size:10px;padding:3px 9px;border-radius:20px;font-weight:600;">5 min read</span>
          <span class="badge" style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.25);color:#ffffff;font-size:10px;padding:3px 9px;border-radius:20px;font-weight:600;">{source_count} sources</span>
        </div>
      </div>
      <div class="header-date" style="font-size:11px;color:rgba(255,255,255,0.55);margin-top:8px;">{today}</div>
    </div>

    <!-- CONTENT -->
    <div class="content">
      {content_html}
    </div>

    <!-- FOOTER -->
    <div class="footer">
      <span>✦ Built for Arthur</span>
      <span>{source_count} curated sources &bull; Generated at {generated_at}</span>
      <span>Next brief tomorrow at 9:00 AM</span>
    </div>

  </div>
</body>
</html>"""


# ============================================================
# SEND EMAIL
# ============================================================
def send_email_html(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.attach(MIMEText("Your email client does not support HTML.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())



# ============================================================
# CORE PIPELINE
# ============================================================
def run_digest() -> None:
    """Fetch newsletters, generate digest, send email. Full pipeline."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*55}")
    print(f"  Running digest — {now}")
    print(f"{'='*55}")

    print("\n📬 Fetching newsletters from Gmail...")
    newsletters = fetch_newsletters_from_gmail()
    print(f"   Sources collected: {list(newsletters.keys())}")

    print("\n🤖 Generating digest with Gemini...")
    digest_html, source_count = generate_digest_html(newsletters)

    print("💌 Wrapping in email template...")
    final_html = wrap_in_email_template(digest_html, source_count)

    today_str = date.today().strftime("%B %d %Y")
    subject = f"Your Daily News Digest — {today_str}"

    print("📤 Sending email...")
    send_email_html(subject, final_html)
    print("✅ Done. Email sent.\n")


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    if "--schedule" in sys.argv:
        # ── Daily scheduler at 08:00 ──────────────────────────
        schedule_time = "08:00"
        print(f"⏰ Scheduler started. Digest will run every day at {schedule_time}.")
        print("   Press Ctrl+C to stop.\n")
        schedule.every().day.at(schedule_time).do(run_digest)

        # Run once immediately on first launch so you don't wait until tomorrow
        run_digest()

        while True:
            schedule.run_pending()
            time.sleep(30)

    else:
        # ── Run once immediately ──────────────────────────────
        run_digest()
