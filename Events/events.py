#!/usr/bin/env python3
"""
events.py ─ build a DaddyLive “live events” playlist

• pulls the JSON schedule
• validates every candidate stream (same logic you already use)
• keeps **only English-speaking feeds** (USA, UK, Canada, Australia,
  New Zealand, Malaysia)
• auto-adds a tvg-logo if a file with the channel slug exists in the
  iptv-org/logo repo
• writes one 4-line block per event to schedule_playlist.m3u8
• ‑v / --verbose prints DEBUG detail
"""

import argparse
import base64
import logging
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests

# ───── constants ──────────────────────────────────────────────────────
SCHEDULE_URL  = "https://daddylive.dad/schedule/schedule-generated.php"
PROXY_PREFIX  = "https://josh9456-ddproxy.hf.space/watch/"
OUTPUT_FILE   = "schedule_playlist.m3u8"

URL_TEMPLATES = [
    "https://nfsnew.newkso.ru/nfs/premium{num}/mono.m3u8",
    "https://windnew.newkso.ru/wind/premium{num}/mono.m3u8",
    "https://zekonew.newkso.ru/zeko/premium{num}/mono.m3u8",
    "https://dokko1new.newkso.ru/dokko1/premium{num}/mono.m3u8",
    "https://ddy6new.newkso.ru/ddy6/premium{num}/mono.m3u8",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/137.0.0.0 Safari/537.36",
    "Referer": "https://daddylive.dad/24-7-channels.php",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

VLC_HEADERS = [
    "#EXTVLCOPT:http-origin=https://lefttoplay.xyz",
    "#EXTVLCOPT:http-referrer=https://lefttoplay.xyz/",
    "#EXTVLCOPT:http-user-agent="
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1",
]

# countries whose channels we want to keep
ALLOWED_COUNTRY_KEYWORDS = {
    "USA", "US", "UK", "EN", "ENG", "CAN", "CANADA",
    "AU", "AUS", "AUSTRALIA", "NZ", "NEWZEALAND", "MYS", "MY", "MALAYSIA"
}

LOGO_RAW = "https://raw.githubusercontent.com/iptv-org/logos/master/tv/{slug}.png"

# ───── helpers ────────────────────────────────────────────────────────
def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).lower()
    text = re.sub(r"\s+", "-", text).strip("-")
    return text

def is_allowed_channel(name: str) -> bool:
    upper = name.upper().replace(" ", "")
    return any(k in upper for k in ALLOWED_COUNTRY_KEYWORDS)

@lru_cache(maxsize=None)
def logo_exists(slug: str, session: requests.Session) -> bool:
    url = LOGO_RAW.format(slug=slug)
    try:
        r = session.head(url, timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        return False

def get_schedule():
    logging.info("Fetching schedule JSON …")
    r = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    logging.info("✓ schedule obtained (%d bytes)", len(r.content))
    return r.json()

def _extract_cid(item):
    if isinstance(item, dict):
        return str(item.get("channel_id"))
    return str(item)

def _channel_entries(ev):
    for key in ("channels", "channels2"):
        val = ev.get(key)
        if not val:
            continue
        if isinstance(val, list):
            yield from val
        elif isinstance(val, dict):
            # mapping (idx → obj) or single object
            if "channel_id" in val:
                yield val
            else:
                yield from val.values()
        else:
            yield val

def extract_channel_ids(schedule):
    ids = set()
    for cats in schedule.values():
        for events in cats.values():
            for ev in events:
                for ch in _channel_entries(ev):
                    ids.add(_extract_cid(ch))
    return ids

def validate_single(url):
    for _ in range(3):
        try:
            r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return url
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(5)
                continue
            # fallback GET
            r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
            if r.status_code == 200:
                return url
            if r.status_code == 404:
                return None
        except requests.RequestException:
            return None
    return None

def build_stream_map(channel_ids, workers=20):
    logging.info("Validating %d×5 candidate URLs …", len(channel_ids))
    candidates = {tpl.format(num=i): i for i in channel_ids for tpl in URL_TEMPLATES}
    id_to_url = {}
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(validate_single, u): u for u in candidates}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                cid = candidates[res]
                id_to_url.setdefault(cid, res)
                logging.debug("✓ %s", res)
    logging.info("✓ %d working streams", len(id_to_url))
    return id_to_url

# ───── playlist builder ───────────────────────────────────────────────
def make_playlist(schedule, stream_map):
    logging.info("Assembling playlist (English-speaking channels only)…")
    lines = ["#EXTM3U"]
    grouped = defaultdict(list)

    for day, cats in schedule.items():
        for cat, events in cats.items():
            for ev in events:
                grouped[cat.upper()].append(ev)

    session = requests.Session()

    for group in sorted(grouped):
        for ev in grouped[group]:
            title = ev["event"]
            for ch in _channel_entries(ev):
                cname = ch["channel_name"] if isinstance(ch, dict) else str(ch)
                if not is_allowed_channel(cname):
                    continue
                cid    = _extract_cid(ch)
                stream = stream_map.get(cid)
                if not stream:
                    continue

                slug = slugify(cname)
                logo_url = LOGO_RAW.format(slug=slug) if logo_exists(slug, session) else ""

                extinf = f'#EXTINF:-1 tvg-id="{cid}" '
                if logo_url:
                    extinf += f'tvg-logo="{logo_url}" '
                extinf += f'group-title="{group}",{title} ({cname})'

                encoded = base64.b64encode(stream.encode()).decode()
                proxy   = f"{PROXY_PREFIX}{encoded}.m3u8"

                lines.append(extinf)
                lines.extend(VLC_HEADERS)
                lines.append(proxy)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")

    logging.info("Playlist written to %s (%d English entries)",
                 OUTPUT_FILE, (len(lines) - 1) // 5)

# ───── main ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Build English-only live-events playlist with logos")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="verbose (DEBUG) output")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s │ %(message)s")

    schedule   = get_schedule()
    chan_ids   = extract_channel_ids(schedule)
    stream_map = build_stream_map(chan_ids)
    make_playlist(schedule, stream_map)

if __name__ == "__main__":
    main()
