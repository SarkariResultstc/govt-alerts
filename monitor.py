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
import urllib.request
import urllib.error
from urllib.parse import urljoin
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

STATE_FILE = "state.json"
SITES_FILE = "sites.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
PAGE_LOAD_TIMEOUT_MS = 15000


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_rendered_html(page, url):
    """Load a page in the headless browser and return fully-rendered HTML.
    Uses 'domcontentloaded' (fast) instead of 'networkidle' (slow) to keep
    checks quick, but waits a bit longer afterwards so JS-heavy sites
    (SSC, NTA, National Career Service etc.) have time to render their
    notice widgets before we read the page content."""
    page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    return page.content()


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


def main():
    sites = load_json(SITES_FILE, [])
    state = load_json(STATE_FILE, {})  # {site_name: [item_id, ...]}

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
                html = fetch_rendered_html(page, url)
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

            for it in new_items:
                now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
                msg = (
                    f"🔔 <b>New post: {name}</b>\n"
                    f"{it['title']}\n"
                    f"{it['link']}\n"
                    f"🕒 {now_ist} (IST)"
                )
                send_telegram(msg)
                print(f"[ALERT] {name}: {it['title']}")
                time.sleep(1)  # be gentle with Telegram rate limits

            # Keep state bounded (last 500 items per site) and updated
            all_ids = list(known_ids.union(it["id"] for it in items))
            state[name] = all_ids[-500:]

        browser.close()

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
