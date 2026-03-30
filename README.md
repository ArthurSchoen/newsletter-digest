# Daily News Digest — Plan 3 (fully automatic)

## How it works

1. Connects to Gmail via IMAP at 08:00 every morning
2. Fetches emails from your configured newsletter senders (last 24h)
3. Cleans and extracts the text content
4. Asks Gemini to write a structured premium digest
5. Sends you the HTML email automatically

---

## Installation

```bash
pip install google-genai beautifulsoup4 schedule
```

---

## Setup

### 1. Environment variables

```bash
export GENAI_API_KEY="your_gemini_api_key"
export SENDER_EMAIL="you@gmail.com"
export SENDER_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # Gmail App Password (not your regular password)
export RECEIVER_EMAIL="you@gmail.com"              # can be the same
```

> **How to get a Gmail App Password:**
> myaccount.google.com → Security → 2-Step Verification → App passwords → Create one for "Mail"

> **Enable IMAP in Gmail:**
> Gmail settings → See all settings → Forwarding and POP/IMAP → Enable IMAP

### 2. Configure your newsletter senders

Open `Newsletter_API.py` and edit `NEWSLETTER_SOURCES` at the top of the file.
Add/remove sources and fill in the sender email address or a keyword that appears in the From field:

```python
NEWSLETTER_SOURCES = {
    "Actionnaire":      ["actionnaire", "bonjour@aktionnaire"],
    "Financial Times":  ["financialtimes", "wsj.com"],
    "Rundown AI":       ["therundown.ai"],
    # "Import AI":      ["jack@jack-clark.net"],
}
```

To find the exact sender address: open one of the newsletters in Gmail → click the sender name → copy the email address.

---

## Running

### Run once (test it now)

```bash
python Newsletter_API.py
```

### Run daily at 08:00 (keeps the process alive)

```bash
python Newsletter_API.py --schedule
```

This sends the first digest immediately, then again every day at 08:00.

### Run in the background (macOS — recommended for daily use)

```bash
nohup python Newsletter_API.py --schedule > digest.log 2>&1 &
```

Logs go to `digest.log`. To stop it: `kill $(lsof -t -i:0 | head -1)` or find the PID with `ps aux | grep Newsletter_API`.

### Alternative: macOS LaunchAgent (runs even after reboot)

Create `~/Library/LaunchAgents/com.yourname.digest.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yourname.digest</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/arthurschoen/Desktop/MIT Classes/DS Project/API test/Newsletter_API.py</string>
    <string>--schedule</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GENAI_API_KEY</key>       <string>YOUR_KEY_HERE</string>
    <key>SENDER_EMAIL</key>        <string>you@gmail.com</string>
    <key>SENDER_APP_PASSWORD</key> <string>xxxx xxxx xxxx xxxx</string>
    <key>RECEIVER_EMAIL</key>      <string>you@gmail.com</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/digest.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/digest.err</string>
</dict>
</plist>
```

Then load it:
```bash
launchctl load ~/Library/LaunchAgents/com.yourname.digest.plist
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `IMAP login failed` | Check that IMAP is enabled in Gmail settings and App Password is correct |
| No newsletters found | Check `NEWSLETTER_SOURCES` — open one newsletter email, look at the exact From address |
| Gemini returns markdown | Already handled — the script strips code fences automatically |
| Email not delivered | Check spam folder; make sure SENDER_APP_PASSWORD is a 16-char App Password |
