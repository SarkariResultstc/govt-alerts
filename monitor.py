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
from urllib.parse import urljoin, urlparse
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
# EVENT_HINTS: words that represent a genuine "just happened" event — a
# result being DECLARED, an admit card being RELEASED, etc. These are
# valid "current news" no matter how old the underlying exam/batch year
# is (e.g. a 2023-batch exam's Result can legitimately be announced today).
EVENT_HINTS = re.compile(
    r"(notif|notice|advt|advertisement|recruit|result|admit|vacan|"
    r"circular|press release|tender|walk-?in|interview|answer key|"
    r"corrigendum|cut ?off|merit|selection|appoint|counsel|"
    r"final list|shortlist)",
    re.IGNORECASE,
)

# REFERENCE_HINTS: static/reference content (syllabus, exam pattern, etc.)
# that sits unchanged on a page for years — NOT a "just happened" event by
# itself. A page matching ONLY these (no EVENT_HINTS word) is treated as
# stale reference material, not fresh news, even if newly detected.
REFERENCE_HINTS = re.compile(
    r"(syllabus|exam pattern|previous year paper|study material|"
    r"eligibility criteria|apply|schedule|exam\b|update|latest|new\b)",
    re.IGNORECASE,
)

# Kept for backward compatibility with any code still referencing it.
NOTICE_HINTS = re.compile(EVENT_HINTS.pattern + "|" + REFERENCE_HINTS.pattern, re.IGNORECASE)

# Real, specific notifications almost always mention a year (e.g. "2026").
# Generic permanent menu items ("Examinations", "Apply Online", "Download
# Syllabus") do NOT include a year — requiring one filters out the site's
# static navigation menu and keeps only genuine, dated announcements.
#
# IMPORTANT: we only accept the CURRENT or NEXT year, not just "any year".
# Otherwise old notices (e.g. "...2024") that a flaky site only just now
# exposed to us would incorrectly pass as "fresh". CURRENT_YEAR is computed
# at run time so this never needs manual updating.
# Real, specific notifications almost always mention a year (e.g. "2026").
# Generic permanent menu items ("Examinations", "Apply Online", "Download
# Syllabus") do NOT include a year — requiring one filters out the site's
# static navigation menu and keeps only genuine, dated announcements.
#
# NOTE: we accept ANY year here, not just current/next year — a "2023"
# exam's Result/Admit Card can genuinely be announced today in 2026, so
# the year in the title reflects the EXAM BATCH, not the publish date.
# Restricting to current-year-only would incorrectly reject real news.
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

# Sites where we do an EXTRA check: visit the item's own post page and read
# its actual published date/time (WordPress-style "Month DD, YYYY / HH:MM
# AM/PM"), and only treat it as fresh news if that date is recent. This is
# ONLY applied to sites listed here — every other site keeps using the
# normal (title + year + event-keyword) detection, unchanged.
SITES_NEEDING_PUBLISH_DATE_CHECK = {"SarkariResult.com.cm"}
FRESHNESS_MAX_AGE_HOURS = 48

PUBLISH_DATE_PATTERN = re.compile(
    r"([A-Z][a-z]+\s+\d{1,2},\s*\d{4})\s*(?:/\s*(\d{1,2}:\d{2}\s*[AP]M))?",
)
MONTHS = (
    "January February March April May June July August "
    "September October November December"
).split()


def fetch_actual_publish_time(page, url):
    """Visit a post's own page and try to read its real published
    date/time (WordPress typically shows 'Month DD, YYYY / HH:MM AM/PM'
    near the title). Returns a UTC-naive datetime, or None if it can't be
    determined (caller should then just allow the item through)."""
    try:
        page.goto(url, timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        text = page.inner_text("body")[:3000]
        match = PUBLISH_DATE_PATTERN.search(text)
        if not match:
            return None
        date_part = match.group(1)
        time_part = match.group(2) or "12:00 AM"
        dt = datetime.strptime(f"{date_part} {time_part}", "%B %d, %Y %I:%M %p")
        return dt
    except Exception as e:
        print(f"Could not read publish time from {url}: {e}", file=sys.stderr)
        return None


def is_actually_fresh(page, item_link, site_name):
    """For sites in SITES_NEEDING_PUBLISH_DATE_CHECK only: verify the post
    was genuinely published recently, using its own on-page date/time.
    Every other site is unaffected and always returns True immediately."""
    if site_name not in SITES_NEEDING_PUBLISH_DATE_CHECK:
        return True
    published_at = fetch_actual_publish_time(page, item_link)
    if published_at is None:
        return True  # couldn't read a date — don't block the item, just allow it
    age_hours = (datetime.now(timezone.utc).replace(tzinfo=None) - published_at).total_seconds() / 3600
    if age_hours > FRESHNESS_MAX_AGE_HOURS:
        print(f"[STALE] {site_name}: skipping old post (published {published_at}, {age_hours:.0f}h ago)")
        return False
    return True
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
        if not EVENT_HINTS.search(text):
            continue  # must be a genuine "just happened" event, not just static reference content
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


def is_gov_domain(url):
    """Only these domain patterns count as 'government' — used to decide
    which links are allowed to appear in the Important Links table."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    gov_patterns = (".gov.in", ".nic.in", ".ac.in", ".res.in")
    return any(host.endswith(p) or p in host for p in gov_patterns)


def extract_gov_links(html, base_url, max_links=12):
    """Pull out (text, url) pairs for links pointing to government domains
    only — competitor/private sites are never included here."""
    links = []
    seen = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            text = " ".join(a.get_text(" ", strip=True).split())
            href = a["href"].strip()
            if not text or len(text) < 3 or len(text) > 100:
                continue
            if href.startswith(("javascript:", "#", "mailto:", "tel:")):
                continue
            full_url = urljoin(base_url, href)
            if not is_gov_domain(full_url):
                continue
            key = (text.lower(), full_url)
            if key in seen:
                continue
            seen.add(key)
            links.append({"text": text, "url": full_url})
            if len(links) >= max_links:
                break
    except Exception as e:
        print(f"Could not extract gov links: {e}", file=sys.stderr)
    return links


def fetch_source_text(page, link, max_chars=14000):
    """Fetch the notification's own page/PDF and return (plain_text,
    gov_links) for the AI to summarize from. Returns ("", []) if it can't
    be read (e.g. a scanned PDF with no text layer) — caller should fall
    back gracefully.

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
            return text.strip()[:max_chars], []  # can't extract links from PDF easily
        else:
            page.goto(link, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            html = page.content()
            gov_links = extract_gov_links(html, link)
            body_text = page.inner_text("body")
            return body_text.strip()[:max_chars], gov_links
    except Exception as e:
        print(f"Could not read source content for AI summary: {e}", file=sys.stderr)
        return "", []


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

    prompt = f"""You are an experienced human content writer for an Indian
government job/exam alert website called SarkariResults.com.tc. Below is
the raw text of an official notification titled "{title}" from {site_name}.
Your job is to COMPLETELY REWRITE this into a fresh, original,
ready-to-publish article — not summarize or lightly reword the source, but
genuinely rewrite it the way a human editor would explain it to a reader
in their own voice.

CRITICAL RULES (follow strictly):
- Do NOT mirror the source's sentence structure, paragraph order, or
  phrasing. Read the source, understand the facts, then explain them in
  your own natural human words as if you're telling a friend — never copy
  or lightly-edit any sentence from the source text.
- Do NOT reproduce the source page's own layout/structure — reorganize
  everything into ONLY the sections listed below, regardless of how the
  source itself was organized.
- Write like a real human editor typing quickly, NOT like a typical AI
  assistant. Vary your sentence lengths a lot — mix short, punchy
  sentences with longer ones. Avoid stiff transition phrases like
  "Moreover", "It is important to note that", "In conclusion",
  "Furthermore", "This development is significant" — real Indian
  Sarkari-job editors don't write that way. Just state things plainly and
  directly.
- Use ONLY factual information (dates, numbers, names) that is actually
  present in the source text. If a whole section has no relevant
  information, OMIT that entire section (heading and table) — never write
  "not available", never guess or invent dates/numbers.
- Extract EVERY relevant fact you can find in the source (be thorough, not
  brief) — the goal is a complete, comprehensive article, not a short
  summary.
- Output ONLY raw HTML fragments (tables, headings, paragraphs, lists). No
  markdown, no code fences, no commentary before or after, no watermark
  text, no mention of AI or that this was auto-generated.
- Use <h4> for each section heading, and standard <table><thead><tbody>
  HTML for every table.

SECTIONS TO PRODUCE, IN THIS ORDER (skip any with no source data):

1. A 3-4 sentence introductory summary paragraph, written naturally in
   your own words.
2. <h4>Quick Info</h4> table — Organization, Post Name, Department,
   Advertisement No., Total Vacancies, Application Start Date, Last Date
   to Apply, Official Website (whichever are present).
3. An important-notice style paragraph (1-2 sentences) if there's any
   critical instruction, deadline reminder, or caveat worth highlighting —
   otherwise skip.
4. <h4>Important Dates</h4> table — every date-related event mentioned
   (Application Start, Last Date to Apply, Fee Payment Last Date,
   Correction Window, Exam City Slip, Exam Date, Admit Card Release,
   Answer Key Date, Result Date, etc.) — include ALL that are present,
   don't limit to a fixed set.
5. <h4>Vacancy Details</h4> table — post-wise vacancy breakdown, only if
   listed.
6. <h4>Age Limit</h4> table — Minimum Age, Maximum Age, Age Relaxation,
   only if present.
7. <h4>Eligibility Criteria</h4> — paragraph or bullet list covering
   qualification/experience required, written in your own words.
8. <h4>Application Fee</h4> table — by category, only if mentioned.
9. <h4>Mode of Selection</h4> table — every stage/status mentioned, only
   if described.
10. <h4>How to Check/Apply</h4> — clear numbered steps for what the
    candidate should actually do, only if the source describes a process.
11. <h4>Documents Required</h4> — bullet list, only if the source mentions
    specific documents needed.
12. <h4>Frequently Asked Questions</h4> — 4-5 realistic Q&A pairs a
    candidate would ask about THIS specific notice, answered using only
    source facts, written naturally. Format as
    <p><strong>Q: ...</strong><br>A: ...</p> for each pair.

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

    for attempt in range(1, 3):  # try up to 2 times — Cloudflare/Groq sometimes
        req = urllib.request.Request(          # give a transient/intermittent error
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
            print(f"AI summary failed (attempt {attempt}/2): {e}", file=sys.stderr)
            if attempt < 2:
                time.sleep(3)
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


def apply_styling(html):
    """Post-process AI-generated HTML into the site's visual style:
    red bars (#e83c3c) for section titles, bordered/styled tables."""
    html = re.sub(
        r"<h4>(.*?)</h4>",
        r'<div style="background:#e83c3c;color:#ffffff;font-weight:bold;'
        r'font-size:15px;padding:8px 14px;margin:22px 0 0 0;">\1</div>',
        html, flags=re.IGNORECASE | re.DOTALL,
    )
    html = re.sub(
        r"<table[^>]*>",
        '<table border="1" cellpadding="8" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;margin:0 0 16px 0;">',
        html, flags=re.IGNORECASE,
    )
    html = re.sub(
        r"<th>",
        '<th style="background:#fbe1e1;color:#111111;text-align:left;'
        'padding:8px;border:1px solid #cccccc;">',
        html, flags=re.IGNORECASE,
    )
    html = re.sub(
        r"<td>",
        '<td style="padding:8px;border:1px solid #cccccc;">',
        html, flags=re.IGNORECASE,
    )
    return html


def create_wp_draft(site_name, title, link, ai_html=None, gov_links=None):
    """Create a WordPress draft post for one detected notice, using a
    WordPress Application Password (NOT the real account password) over
    the standard WP REST API. Fails silently (logs only) so a WordPress
    hiccup never crashes the whole monitoring run."""
    if not (WP_URL and WP_USERNAME and WP_APP_PASSWORD):
        return  # WordPress posting not configured, skip quietly

    link_type = guess_link_type(title)
    now_ist = datetime.now(IST).strftime("%d %B %Y, %I:%M %p")

    if ai_html:
        intro_block = apply_styling(ai_html)
    else:
        intro_block = (
            f"<p>{title} — released by <strong>{site_name}</strong>. Full "
            f"official details are available at the source link below. "
            f"This draft was created automatically; please review and "
            f"edit before publishing.</p>"
        )

    # Important Links: source link + any government-domain links found on
    # the source page. Never competitor/private sites. All "nofollow".
    links_rows = (
        f'<tr><td style="padding:8px;border:1px solid #cccccc;">{link_type}</td>'
        f'<td style="padding:8px;border:1px solid #cccccc;">'
        f'<a href="{link}" target="_blank" rel="nofollow noopener">Click Here</a></td></tr>'
    )
    for gl in (gov_links or []):
        links_rows += (
            f'<tr><td style="padding:8px;border:1px solid #cccccc;">{gl["text"]}</td>'
            f'<td style="padding:8px;border:1px solid #cccccc;">'
            f'<a href="{gl["url"]}" target="_blank" rel="nofollow noopener">Click Here</a></td></tr>'
        )

    content_html = f"""
{intro_block}

<div style="background:#e83c3c;color:#ffffff;font-weight:bold;font-size:15px;padding:8px 14px;margin:22px 0 0 0;">Important Links</div>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;margin:0 0 16px 0;">
<thead>
<tr>
<th style="background:#fbe1e1;color:#111111;text-align:left;padding:8px;border:1px solid #cccccc;">Important Link</th>
<th style="background:#fbe1e1;color:#111111;text-align:left;padding:8px;border:1px solid #cccccc;">Link</th>
</tr>
</thead>
<tbody>
{links_rows}
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

    pending_for_ai = []  # collected here, processed AFTER all sites are scanned

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # ---- PASS 1: fast scan of every site + immediate Telegram alerts.
        # Nothing slow (AI calls, WordPress) happens in this loop, so a
        # notice found early doesn't delay discovering notices on the
        # sites checked afterward — every site gets scanned as fast as
        # possible and Telegram alerts go out the moment each is found.
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

            if is_first_run_for_site:
                state[name] = [it["id"] for it in items]
                print(f"[INIT] {name}: recorded {len(items)} existing items")
                continue

            # For sites in SITES_NEEDING_PUBLISH_DATE_CHECK (currently just
            # SarkariResult.com.cm), double-check each candidate's actual
            # on-page publish date/time before treating it as fresh news.
            # No effect on any other site.
            if name in SITES_NEEDING_PUBLISH_DATE_CHECK:
                new_items = [it for it in new_items if is_actually_fresh(page, it["link"], name)]

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
                send_telegram(msg)  # fast — goes out immediately
                print(f"[ALERT] {name}: {it['title']}")
                pending_for_ai.append({"site": name, "title": it["title"], "link": it["link"]})
                recent_feed.append({
                    "site": name,
                    "title": it["title"],
                    "link": it["link"],
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                })

            all_ids = list(known_ids.union(it["id"] for it in items))
            state[name] = all_ids[-500:]

        # ---- PASS 2: slower AI summary + WordPress draft creation, done
        # AFTER every Telegram alert has already been sent. This never
        # delays the alerts — it only affects how quickly the WordPress
        # draft appears, which isn't as time-critical.
        for it in pending_for_ai:
            if wp_count["count"] >= WP_DAILY_LIMIT:
                print(f"[WP LIMIT] Daily draft limit ({WP_DAILY_LIMIT}) reached, skipping draft for: {it['title']}")
                continue
            ai_html = None
            gov_links = []
            if WP_URL and (GROQ_API_KEY or OPENROUTER_API_KEY or CF_API_TOKEN):
                source_text, gov_links = fetch_source_text(page, it["link"])
                ai_html = call_ai_for_summary(it["title"], it["site"], source_text)
            create_wp_draft(it["site"], it["title"], it["link"], ai_html=ai_html, gov_links=gov_links)
            wp_count = increment_wp_daily_count(wp_count)
            time.sleep(1)  # be gentle with the AI/WordPress APIs

        browser.close()

    save_json(STATE_FILE, state)
    save_json(RECENT_FEED_FILE, recent_feed[-30:])  # keep only the newest 30 for the ticker


if __name__ == "__main__":
    main()
