#!/usr/bin/env python3
"""
events.py – build a live-events playlist from DaddyLive’s schedule feed
----------------------------------------------------------------------
• Fetch the schedule (needs Referer header → 200 OK)
• Extract every channel_id even when “channels” / “channels2” are
  lists, dicts, or single strings
• Generate 5 candidate URLs per id, validate them concurrently
• Base-64-encode the first working URL, prepend ddproxy prefix
• Emit one 4-line M3U block per event to schedule_playlist.m3u8
• -v / --verbose shows per-URL checks
"""

import argparse
import base64
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ───────────────────────── constants ─────────────────────────
SCHEDULE_URL = "https://daddylive.dad/schedule/schedule-generated.php"
PROXY_PREFIX = "https://josh9456-ddproxy.hf.space/watch/"
OUTPUT_FILE = "schedule_playlist.m3u8"

URL_TEMPLATES = [
    "https://nfsnew.newkso.ru/nfs/premium{num}/mono.m3u8",
    "https://windnew.newkso.ru/wind/premium{num}/mono.m3u8",
    "https://zekonew.newkso.ru/zeko/premium{num}/mono.m3u8",
    "https://dokko1new.newkso.ru/dokko1/premium{num}/mono.m3u8",
    "https://ddy6new.newkso.ru/ddy6/premium{num}/mono.m3u8",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    # DaddyLive returns 403 without this referer
    "Referer": "https://daddylive.dad/24-7-channels.php",
}

VLC_HEADERS = [
    "#EXTVLCOPT:http-origin=https://lefttoplay.xyz",
    "#EXTVLCOPT:http-referrer=https://lefttoplay.xyz/",
    "#EXTVLCOPT:http-user-agent="
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1",
]

# ─────────────────────── fetch schedule ───────────────────────
def get_schedule():
    logging.info("Fetching schedule JSON …")
    r = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    logging.info("✓ schedule obtained (%d bytes)", len(r.content))
    return r.json()

# ─────────────────────── helpers ──────────────────────────────
def _extract_cid(item):
    """Accept dicts with channel_id or bare strings/ints."""
    if isinstance(item, dict):
        return str(item.get("channel_id"))
    if item is not None:
        return str(item)
    return None

def _channel_entries(event):
    """
    Yield every channel entry from 'channels' / 'channels2' irrespective
    of whether the field is a list, dict, or single object.
    """
    for key in ("channels", "channels2"):
        val = event.get(key)
        if not val:
            continue
        if isinstance(val, list):
            for ch in val:
                yield ch
        elif isinstance(val, dict):
            # Either a mapping of index → object or single object
            if "channel_id" in val or "channel_name" in val:
                yield val
            else:
                for ch in val.values():
                    yield ch
        else:
            yield val

def extract_channel_ids(schedule):
    ids = set()
    for _day, cats in schedule.items():
        for _cat, events in cats.items():
            for ev in events:
                for ch in _channel_entries(ev):
                    if cid := _extract_cid(ch):
                        ids.add(cid)
    logging.info("Found %d unique channel IDs", len(ids))
    return ids

# ────────────────────── validation ────────────────────────────
def validate_single(url):
    for attempt in range(1, 4):
        try:
            logging.debug("HEAD %s (try %d)", url, attempt)
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
        except requests.RequestException as e:
            logging.debug("Request error %s: %s", url, e)
            return None
    return None

def build_stream_map(channel_ids, workers=20):
    logging.info("Validating candidate URLs …")
    candidates = {tpl.format(num=i): i for i in channel_ids for tpl in URL_TEMPLATES}
    id_to_url = {}

    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(validate_single, u): u for u in candidates}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                cid = candidates[res]
                if cid not in id_to_url:
                    id_to_url[cid] = res
                    logging.info("✓ %s", res)

    logging.info("Obtained %d working streams", len(id_to_url))
    return id_to_url

# ─────────────────────── playlist build ───────────────────────
def make_playlist(schedule, stream_map):
    logging.info("Assembling playlist …")
    lines = ["#EXTM3U"]
    grouped = defaultdict(list)

    for day, cats in schedule.items():
        for cat, events in cats.items():
            for ev in events:
                grouped[cat.upper()].append((day, ev))

    for group in sorted(grouped):
        for day, ev in grouped[group]:
            title = ev["event"]
            for ch in _channel_entries(ev):
                cid = _extract_cid(ch)
                if not cid:
                    continue
                stream = stream_map.get(cid)
                if not stream:
                    logging.debug("No stream for channel %s", cid)
                    continue

                cname = ch.get("channel_name") if isinstance(ch, dict) else "Unknown"
                encoded = base64.b64encode(stream.encode()).decode()
                proxy_url = f"{PROXY_PREFIX}{encoded}.m3u8"

                extinf = (
                    f'#EXTINF:-1 tvg-id="{cid}" '
                    f'tvg-logo="https://raw.githubusercontent.com/'
                    f'pigzillaaaaa/iptv-scraper/main/imgs/tv-logo.png" '
                    f'group-title="{group}",{title} ({cname})'
                )

                lines.append(extinf)
                lines.extend(VLC_HEADERS)
                lines.append(proxy_url)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")

    logging.info("Playlist written to %s (%d entries)",
                 OUTPUT_FILE, (len(lines) - 1) // 5)

# ───────────────────────── main ───────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Build live-events playlist from DaddyLive schedule")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="verbose (DEBUG) output")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s │ %(message)s")

    schedule = get_schedule()
    chan_ids = extract_channel_ids(schedule)
    stream_map = build_stream_map(chan_ids)
    make_playlist(schedule, stream_map)

if __name__ == "__main__":
    main()
