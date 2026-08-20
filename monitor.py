#!/usr/bin/env python3
"""
Govt website new-post/notification watcher (v2 - uses a real headless
browser via Playwright so JavaScript-rendered sites like DRDO/FCI/SSC/NTA
also work, not just plain static HTML).

Fetches each site in sites.json, extracts link+text items that look like
notices/posts, compares to previously saved state, and sends a Telegram
message for every NEW item found. State is saved to state.json so the
next run only reports genuinely new items.
"""

import json
import os
import re
import sys
import time
import hashlib
import base64
import urllib.request
import urllib.error
from urllib.parse import urljoin
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from pypdf import PdfReader
import io

STATE_FILE = "state.json"
RECENT_FEED_FILE = "recent_feed.json"
WP_DAILY_COUNT_FILE = "wp_daily_count.json"
WP_DAILY_LIMIT = 20
SITES_FILE = "sites.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

WP_URL = os.environ.get("WP_URL", "").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Words that make a link text look like a real notice/post (helps filter
# out menu items like "Home", "Contact Us", "Sitemap" etc.)
NOTICE_HINTS = re.compile(
    r"(notif|notice|advt|advertisement|recruit|result|admit|exam|vacan|"
    r"circular|press release|tender|walk-?in|interview|answer key|"
    r"corrigendum|apply|schedule|syllabus|cut ?off|merit|selection|"
    r"appoint|update|latest|new\b)",
    re.IGNORECASE,
)

# Real, specific notifications almost always mention a year (e.g. "2026").
# Generic permanent menu items ("Examinations", "Apply Online", "Download
# Syllabus") do NOT include a year — requiring one filters out the site's
# static navigation menu and keeps only genuine, dated announcements.
YEAR_HINT = re.compile(r"\b20\d{2}\b")

# A short blocklist of common bare menu-label phrases that sometimes DO
# contain a year-like number by coincidence but are still just navigation,
# not an actual post.
GENERIC_BLOCKLIST = {
    "examinations", "active examinations", "forthcoming examinations",
    "examination calendar", "online notifications", "apply online",
    "download admit card", "download syllabus", "view answer key",
    "all notifications/ advertisements", "all notifications advertisements",
    "apply for post", "interview only", "examination only",
}

MIN_TEXT_LEN = 12
MAX_TEXT_LEN = 220
MAX_NEW_ITEMS_PER_SITE_PER_RUN = 4
PAGE_LOAD_TIMEOUT_MS = 15000
SLOW_PAGE_LOAD_TIMEOUT_MS = 25000

# These specific sites are unusually heavy/slow JavaScript apps that need
# to fully finish loading (networkidle) to show their notices — the fast
# "domcontentloaded + short wait" approach used for everyone else isn't
# enough for these. Keeping this list small means the overall run still
# stays fast; only these few sites take longer.
SLOW_SITES = {
    "SSC", "NTA", "National Career Service", "CISCE (ICSE/ISC)",
    "UP Police Board (UPPBPB)", "ICAR", "SBI Careers", "SEBI",
    "AIIMS Exams", "Employment News",
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_rendered_html(page, url, slow=False, retries=2):
    """Load a page in the headless browser and return fully-rendered HTML.
    For most sites we use 'domcontentloaded' (fast) + a short pause, which
    is enough. For known heavy JS apps (see SLOW_SITES) we wait for the
    page to go fully idle (networkidle) so their notice widgets finish
    loading before we read the content.

    Retries once (or more) on transient network errors (timeouts, DNS
    hiccups, connection resets) before giving up — many of these are
    momentary blips, not permanent failures."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            if slow:
                page.goto(url, timeout=SLOW_PAGE_LOAD_TIMEOUT_MS, wait_until="networkidle")
                page.wait_for_timeout(2000)
            else:
                page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(800)
            return page.content()
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(3)  # short pause before retrying
    raise last_error


def normalize_title(text):
    """Lowercase + collapse whitespace so trivial formatting changes don't
    make the same notice look like a brand-new one."""
    return re.sub(r"\s+", " ", text.strip().lower())


def extract_items(html, base_url):
    """Return list of (item_id, title, link) for notice-like <a> tags.

    IMPORTANT: item_id is derived from the TITLE TEXT ONLY (not the link).
    Some government sites append a changing token/timestamp to their links
    on every page load, which used to make the same notice look "new" on
    every run and caused repeated duplicate alerts. Identifying purely by
    title fixes that — each distinct notice now alerts exactly once.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_titles = set()
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = a["href"].strip()
        if not text or len(text) < MIN_TEXT_LEN or len(text) > MAX_TEXT_LEN:
            continue
        if href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        if not NOTICE_HINTS.search(text):
            continue
        if not YEAR_HINT.search(text):
            continue  # skip generic menu items with no year (not a real post)
        if text.strip().lower() in GENERIC_BLOCKLIST:
            continue
        norm = normalize_title(text)
        if norm in seen_titles:
            continue
        seen_titles.add(norm)
        full_link = urljoin(base_url, href)
        item_id = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        items.append({"id": item_id, "title": text, "link": full_link})
    return items


def send_telegram(message):
    token = TELEGRAM_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    if not token or not chat_id:
        print("Telegram credentials missing, skipping send.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        # Catch everything (URLError, InvalidURL, etc.) so one bad message
        # never crashes the whole run.
        print(f"Telegram send failed: {e}", file=sys.stderr)


# Best-effort label for the Important Links row, based on the notice title.
LINK_TYPE_PATTERNS = [
    (re.compile(r"answer key", re.IGNORECASE), "Answer Key"),
    (re.compile(r"admit card", re.IGNORECASE), "Admit Card"),
    (re.compile(r"result", re.IGNORECASE), "Result"),
    (re.compile(r"syllabus", re.IGNORECASE), "Syllabus"),
    (re.compile(r"apply", re.IGNORECASE), "Apply Online"),
    (re.compile(r"merit|selection", re.IGNORECASE), "Merit List"),
    (re.compile(r"cut ?off", re.IGNORECASE), "Cut Off"),
    (re.compile(r"corrigendum", re.IGNORECASE), "Corrigendum"),
    (re.compile(r"notif|advt|advertisement|recruit|vacan", re.IGNORECASE), "Notification"),
]


def guess_link_type(title):
    for pattern, label in LINK_TYPE_PATTERNS:
        if pattern.search(title):
            return label
    return "Details"


def fetch_source_text(page, link, max_chars=14000):
    """Fetch the notification's own page/PDF and return plain text content
    for the AI to summarize from. Returns "" if it can't be read (e.g. a
    scanned PDF with no text layer) — caller should fall back gracefully.

    We can't rely on the URL ending in ".pdf" — many government sites serve
    PDFs from URLs like "download.aspx?id=123" with no extension — so we
    check the real Content-Type header from the server instead."""
    is_pdf = link.lower().endswith(".pdf")
    pdf_bytes = None

    try:
        req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            if "pdf" in content_type:
                is_pdf = True
            if is_pdf:
                pdf_bytes = resp.read()
    except Exception as e:
        print(f"Could not fetch headers for {link}: {e}", file=sys.stderr)

    try:
        if is_pdf:
            if pdf_bytes is None:
                req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    pdf_bytes = resp.read()
            reader = PdfReader(io.BytesIO(pdf_bytes))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:6])
            return text.strip()[:max_chars]
        else:
            page.goto(link, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            body_text = page.inner_text("body")
            return body_text.strip()[:max_chars]
    except Exception as e:
        print(f"Could not read source content for AI summary: {e}", file=sys.stderr)
        return ""


def call_ai_for_summary(title, site_name, source_text):
    """Ask a free LLM (Groq if configured, else OpenRouter) to extract
    Important Dates / Fee / Eligibility from the raw notification text and
    return ready-to-use HTML. Returns None on any failure so the caller
    falls back to the basic template."""
    api_key = GROQ_API_KEY or OPENROUTER_API_KEY
    if not (api_key or CF_API_TOKEN) or not source_text:
        print(
            f"[AI SKIP] {title}: "
            f"groq={bool(GROQ_API_KEY)} openrouter={bool(OPENROUTER_API_KEY)} "
            f"cloudflare={bool(CF_API_TOKEN)}, "
            f"source_text_chars={len(source_text) if source_text else 0}"
        )
        return None

    use_cloudflare = bool(CF_API_TOKEN and CF_ACCOUNT_ID) and not api_key

    if use_cloudflare:
        url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    elif GROQ_API_KEY:
        url = "https://api.groq.com/openai/v1/chat/completions"
        model = "llama-3.3-70b-versatile"
    else:
        url = "https://openrouter.ai/api/v1/chat/completions"
        model = "meta-llama/llama-3.3-70b-instruct:free"

    prompt = f"""You are a content writer for an Indian government job/exam alert
website called SarkariResults.com.tc. Below is the raw text of an official
notification titled "{title}" from {site_name}. Write a complete, ready-to-
publish article in the exact structure below, in the same style as
established "Sarkari Result" style sites.

RULES:
- Write factual summaries IN YOUR OWN WORDS — never copy sentences verbatim
  from the source text (this is for copyright safety).
- Use ONLY information that is actually present in the source text. If a
  whole section has no relevant information, OMIT that entire section
  (heading and table) — never write "not available", never guess or invent
  dates/numbers.
- Output ONLY raw HTML fragments (tables, headings, paragraphs, lists). No
  markdown, no code fences, no commentary before or after.
- Use <h4> for each section heading, and standard <table><thead><tbody> HTML
  for every table (no inline CSS needed, the website already styles tables).

SECTIONS TO PRODUCE, IN THIS ORDER (skip any with no source data):

1. A 2-3 sentence introductory summary paragraph.
2. <h4>Quick Info</h4> table — rows for whichever of these are present:
   Organization, Post Name, Department, Advertisement No., Total Vacancies,
   Application Start Date, Last Date to Apply, Official Website.
3. <h4>Important Dates</h4> table — rows for whichever are present:
   Application Start, Last Date to Apply, Fee Payment Last Date,
   Correction Window, Exam Date, Admit Card Release, Answer Key Date,
   Result Date.
4. <h4>Vacancy Details</h4> table — post-wise vacancy breakdown, only if the
   source lists specific posts/numbers.
5. <h4>Age Limit</h4> table — Minimum Age, Maximum Age, Age Relaxation
   (as-on date if mentioned).
6. <h4>Eligibility Criteria</h4> — short paragraph or bullet list covering
   required qualification/experience.
7. <h4>Application Fee</h4> table — by category (General/OBC/SC/ST/PwBD etc.)
   if mentioned.
8. <h4>Mode of Selection</h4> table — stages (e.g. Written Exam, Merit,
   Interview) if mentioned.
9. <h4>How to Apply</h4> — a short numbered/bulleted list of the application
   steps, only if the source describes a process.
10. <h4>Frequently Asked Questions</h4> — 3 to 5 short Q&A pairs a candidate
    would realistically ask about THIS specific notice (e.g. "What is the
    last date to apply for {{post name}}?"), answered using only facts found
    in the source text. Format as <p><strong>Q: ...</strong><br>A: ...</p>
    for each pair.

SOURCE TEXT:
{source_text}"""

    if use_cloudflare:
        payload = json.dumps({
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 3000,
        }).encode("utf-8")
        auth_header = f"Bearer {CF_API_TOKEN}"
    else:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 3000,
        }).encode("utf-8")
        auth_header = f"Bearer {api_key}"

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_header,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if use_cloudflare:
            html = result["result"]["response"].strip()
        else:
            html = result["choices"][0]["message"]["content"].strip()
        html = re.sub(r"^```html\s*|\s*```$", "", html.strip())
        if html:
            print(f"[AI OK] Got {len(html)} chars of rich content for: {title}")
        else:
            print(f"[AI EMPTY] No content returned for: {title}")
        return html or None
    except Exception as e:
        print(f"AI summary failed: {e}", file=sys.stderr)
        return None


def get_wp_daily_count():
    today = datetime.now(IST).strftime("%Y-%m-%d")
    data = load_json(WP_DAILY_COUNT_FILE, {"date": today, "count": 0})
    if data.get("date") != today:
        data = {"date": today, "count": 0}  # new day, reset counter
    return data


def increment_wp_daily_count(data):
    data["count"] += 1
    save_json(WP_DAILY_COUNT_FILE, data)
    return data


def create_wp_draft(site_name, title, link, ai_html=None):
    """Create a WordPress draft post for one detected notice, using a
    WordPress Application Password (NOT the real account password) over
    the standard WP REST API. Fails silently (logs only) so a WordPress
    hiccup never crashes the whole monitoring run."""
    if not (WP_URL and WP_USERNAME and WP_APP_PASSWORD):
        return  # WordPress posting not configured, skip quietly

    link_type = guess_link_type(title)
    now_ist = datetime.now(IST).strftime("%d %B %Y, %I:%M %p")

    if ai_html:
        intro_block = ai_html
    else:
        intro_block = (
            f"<p>{title} — released by <strong>{site_name}</strong>. Full "
            f"official details are available at the source link below. "
            f"This draft was created automatically; please review and "
            f"edit before publishing.</p>"
        )

    content_html = f"""
{intro_block}

<h4>Important Links</h4>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;">
<thead>
<tr style="background:#f2f2f2;">
<th>Important Link</th>
<th>Link</th>
</tr>
</thead>
<tbody>
<tr>
<td>{link_type}</td>
<td><a href="{link}" target="_blank" rel="noopener">Click Here</a></td>
</tr>
</tbody>
</table>

<p><em>Detected on {now_ist} (IST) from {site_name}.</em></p>
""".strip()

    payload = json.dumps({
        "title": title,
        "content": content_html,
        "status": "draft",
    }).encode("utf-8")

    credentials = f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode("utf-8")
    auth_header = "Basic " + base64.b64encode(credentials).decode("utf-8")

    req = urllib.request.Request(
        f"{WP_URL}/wp-json/wp/v2/posts",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_header,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"[WP DRAFT] Created draft for: {title}")
    except Exception as e:
        print(f"WordPress draft creation failed: {e}", file=sys.stderr)


def main():
    sites = load_json(SITES_FILE, [])
    state = load_json(STATE_FILE, {})  # {site_name: [item_id, ...]}
    recent_feed = load_json(RECENT_FEED_FILE, [])  # rolling list for the public live ticker
    wp_count = get_wp_daily_count()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        for site in sites:
            name = site["name"]
            url = site["url"]
            known_ids = set(state.get(name, []))
            is_first_run_for_site = name not in state

            try:
                html = fetch_rendered_html(page, url, slow=(name in SLOW_SITES))
            except Exception as e:
                print(f"[SKIP] {name}: could not load ({e})", file=sys.stderr)
                continue

            items = extract_items(html, url)
            new_items = [it for it in items if it["id"] not in known_ids]

            # On the very first run for a site, just record what's there —
            # don't spam alerts for everything already on the page.
            if is_first_run_for_site:
                state[name] = [it["id"] for it in items]
                print(f"[INIT] {name}: recorded {len(items)} existing items")
                continue

            # SAFETY CAP: if a site was flaky/slow before and suddenly loads
            # its FULL notice list, dozens of old/backlog notices can look
            # "new" all at once. Notice boards list newest items first, so
            # we only alert on the first few (top of the list) and quietly
            # mark the rest as known — this avoids ever flooding old news.
            if len(new_items) > MAX_NEW_ITEMS_PER_SITE_PER_RUN:
                print(
                    f"[CAP] {name}: {len(new_items)} new items found, "
                    f"only alerting top {MAX_NEW_ITEMS_PER_SITE_PER_RUN} "
                    f"(rest treated as old backlog, not alerted)",
                    file=sys.stderr,
                )
            new_items = new_items[:MAX_NEW_ITEMS_PER_SITE_PER_RUN]

            for it in new_items:
                now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
                msg = (
                    f"🔔 <b>New post: {name}</b>\n"
                    f"{it['title']}\n"
                    f"{it['link']}\n"
                    f"🕒 {now_ist} (IST)"
                )
                send_telegram(msg)
                if wp_count["count"] < WP_DAILY_LIMIT:
                    ai_html = None
                    if WP_URL and (GROQ_API_KEY or OPENROUTER_API_KEY or CF_API_TOKEN):
                        source_text = fetch_source_text(page, it["link"])
                        ai_html = call_ai_for_summary(it["title"], name, source_text)
                    create_wp_draft(name, it["title"], it["link"], ai_html=ai_html)
                    wp_count = increment_wp_daily_count(wp_count)
                else:
                    print(f"[WP LIMIT] Daily draft limit ({WP_DAILY_LIMIT}) reached, skipping draft for: {it['title']}")
                recent_feed.append({
                    "site": name,
                    "title": it["title"],
                    "link": it["link"],
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                })
                print(f"[ALERT] {name}: {it['title']}")
                time.sleep(1)  # be gentle with Telegram/WP rate limits

            # Keep state bounded (last 500 items per site) and updated
            all_ids = list(known_ids.union(it["id"] for it in items))
            state[name] = all_ids[-500:]

        browser.close()

    save_json(STATE_FILE, state)
    save_json(RECENT_FEED_FILE, recent_feed[-30:])  # keep only the newest 30 for the ticker


if __name__ == "__main__":
    main()
