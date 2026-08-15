#!/usr/bin/env python3
"""
Govt website new-post/notification watcher.
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

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependency: pip install beautifulsoup4", file=sys.stderr)
    raise

STATE_FILE = "state.json"
SITES_FILE = "sites.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Words that make a link text look like a real notice/post (helps filter
# out menu items like "Home", "Contact Us", "Sitemap" etc.)
NOTICE_HINTS = re.compile(
    r"(notif|notice|advt|advertisement|recruit|result|admit|exam|vacan|"
    r"circular|press release|tender|walk-?in|interview|answer key|"
    r"corrigendum|apply|schedule|syllabus|cut ?off|merit|selection|"
    r"appoint|update|latest|new\b)",
    re.IGNORECASE,
)

MIN_TEXT_LEN = 12
MAX_TEXT_LEN = 220


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def extract_items(html, base_url):
    """Return list of (item_id, title, link) for notice-like <a> tags."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = a["href"].strip()
        if not text or len(text) < MIN_TEXT_LEN or len(text) > MAX_TEXT_LEN:
            continue
        if href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        if not NOTICE_HINTS.search(text):
            continue
        full_link = urljoin(base_url, href)
        item_id = hashlib.sha256((text + "|" + full_link).encode("utf-8")).hexdigest()
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append({"id": item_id, "title": text, "link": full_link})
    return items


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing, skipping send.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
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
    except urllib.error.URLError as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


def main():
    sites = load_json(SITES_FILE, [])
    state = load_json(STATE_FILE, {})  # {site_name: [item_id, ...]}
    first_run_overall = len(state) == 0

    for site in sites:
        name = site["name"]
        url = site["url"]
        known_ids = set(state.get(name, []))
        is_first_run_for_site = name not in state

        try:
            html = fetch(url)
        except Exception as e:
            print(f"[SKIP] {name}: could not fetch ({e})", file=sys.stderr)
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
            msg = (
                f"🔔 <b>New post: {name}</b>\n"
                f"{it['title']}\n"
                f"{it['link']}"
            )
            send_telegram(msg)
            print(f"[ALERT] {name}: {it['title']}")
            time.sleep(1)  # be gentle with Telegram rate limits

        # Keep state bounded (last 500 items per site) and updated
        all_ids = list(known_ids.union(it["id"] for it in items))
        state[name] = all_ids[-500:]

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
