#!/usr/bin/env python3
"""
events.py – live-events playlist with tv-logo repo artwork
"""

import argparse
import base64
import logging
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ─── constants ──────────────────────────────────────────────
SCHEDULE_URL = "https://daddylive.dad/schedule/schedule-generated.php"
PROXY_PREFIX = "https://josh9456-ddproxy.hf.space/watch/"
OUTPUT_FILE  = "schedule_playlist.m3u8"

TVLOGO_RAW_ROOT = (
    "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/"
)

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

GENERIC_LOGO = (
    "https://raw.githubusercontent.com/tv-logo/tv-logos/main/unknown.png"
)  # safe fallback

COUNTRY_HINTS = {
    "usa": "united-states",
    "us": "united-states",
    "uk": "united-kingdom",
    "canada": "canada",
    "au": "australia",
    "aus": "australia",
    "australia": "australia",
    "nz": "new-zealand",
    "new zealand": "new-zealand",
    "my": "malaysia",
    "mys": "malaysia",
}


# ─── helpers ────────────────────────────────────────────────
def slugify(channel: str) -> str:
    """Turn channel name into tv-logo style slug (lowercase, hyphens, “and”, cc)."""
    txt = (
        unicodedata.normalize("NFKD", channel)
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    txt = txt.replace("&", " and ")
    txt = re.sub(r"[^\w\s-]", "", txt)
    txt = re.sub(r"\s+", "-", txt).strip("-")
    return txt + ".png"


def get_country_folder(name: str) -> str | None:
    key = name.lower().replace(" ", " ").strip()
    for k, folder in COUNTRY_HINTS.items():
        if k in key:
            return folder
    return None


def build_logo_index(session: requests.Session) -> set[str]:
    """Download **once** the list of all logo paths in the repo."""
    logging.info("Fetching full logo index (GitHub API pagination)…")
    index = set()
    page = 1
    while True:
        api_url = (
            "https://api.github.com/repos/tv-logo/tv-logos/contents/countries"
            f"?per_page=100&page={page}"
        )
        r = session.get(api_url, timeout=15)
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        for country in items:
            if country["type"] != "dir":
                continue
            country_dir = country["path"]
            # fetch each country dir (max 1000 files per dir is fine)
            r2 = session.get(
                f"https://api.github.com/repos/tv-logo/tv-logos/contents/{country_dir}",
                timeout=30,
            )
            r2.raise_for_status()
            for f in r2.json():
                if f["type"] == "file" and f["name"].endswith(".png"):
                    index.add(f["path"])
        page += 1
    logging.info("✓ logo index ready (%d files)", len(index))
    return index


def find_logo(channel_name: str, index: set[str]) -> str:
    slug = slugify(channel_name)
    # 1. try with country hint
    if folder := get_country_folder(channel_name):
        path = f"{folder}/{slug}"
        if f"countries/{path}" in index:
            return TVLOGO_RAW_ROOT + path
    # 2. brute-search (rare, but still quick with in-memory set)
    for p in index:
        if p.endswith("/" + slug):
            return TVLOGO_RAW_ROOT + "/".join(p.split("/")[1:])
    return GENERIC_LOGO


def get_schedule() -> dict:
    r = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def _extract_cid(item):
    return str(item["channel_id"]) if isinstance(item, dict) else str(item)


def _channel_entries(event):
    for key in ("channels", "channels2"):
        val = event.get(key)
        if not val:
            continue
        if isinstance(val, list):
            yield from val
        elif isinstance(val, dict):
            yield from val.values() if "channel_id" not in val else [val]
        else:
            yield val


def extract_channel_ids(schedule):
    ids = set()
    for cats in schedule.values():
        for events in cats.values():
            for ev in events:
                ids.update(_extract_cid(ch) for ch in _channel_entries(ev))
    return ids


def validate_single(url):
    for _ in range(3):
        try:
            r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return url
            if r.status_code in (404,):
                return None
            if r.status_code == 429:
                time.sleep(5)
                continue
            r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
            if r.status_code == 200:
                return url
            if r.status_code == 404:
                return None
        except requests.RequestException:
            return None
    return None


def build_stream_map(channel_ids, workers=20):
    candidates = {tpl.format(num=i): i for i in channel_ids for tpl in URL_TEMPLATES}
    id_to_url = {}
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(validate_single, u): u for u in candidates}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                id_to_url.setdefault(candidates[res], res)
    logging.info("✓ %d working streams", len(id_to_url))
    return id_to_url


def make_playlist(schedule, stream_map, logo_index):
    lines = ["#EXTM3U"]
    grouped = defaultdict(list)
    for day, cats in schedule.items():
        for cat, events in cats.items():
            grouped[cat.upper()].extend(events)

    for group in sorted(grouped):
        for ev in grouped[group]:
            title = ev["event"]
            for ch in _channel_entries(ev):
                cname = ch["channel_name"] if isinstance(ch, dict) else str(ch)
                cid = _extract_cid(ch)
                stream = stream_map.get(cid)
                if not stream:
                    continue

                logo_url = find_logo(cname, logo_index)
                extinf = (
                    f'#EXTINF:-1 tvg-id="{cid}" '
                    f'tvg-logo="{logo_url}" '
                    f'group-title="{group}",{title} ({cname})'
                )

                encoded = base64.b64encode(stream.encode()).decode()
                proxy = f"{PROXY_PREFIX}{encoded}.m3u8"

                lines.append(extinf)
                lines.extend(VLC_HEADERS)
                lines.append(proxy)

    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    logging.info("Playlist written to %s (%d events)", OUTPUT_FILE, (len(lines) - 1) // 5)


# ─── main ───────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Build live-events playlist with logos")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s │ %(message)s",
    )

    schedule = get_schedule()
    chan_ids = extract_channel_ids(schedule)
    stream_map = build_stream_map(chan_ids)

    with requests.Session() as s:
        logo_index = build_logo_index(s)
        make_playlist(schedule, stream_map, logo_index)


if __name__ == "__main__":
    main()
