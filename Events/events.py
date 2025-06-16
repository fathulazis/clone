#!/usr/bin/env python3
"""
events.py ─ Build a live–events playlist with
            • validated streams
            • logo matching
            • robust EPG (tvg-id) matching        ⇐ fixed

Drop the file in an empty directory, run

    python3 events.py -v

and the playlist will be written to  ▸ schedule_playlist.m3u8
"""
from __future__ import annotations

import argparse
import base64
import difflib          # ← NEW
import logging
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ───────────────────────────── constants ────────────────────────────────
SCHEDULE_URL      = "https://daddylive.dad/schedule/schedule-generated.php"
PROXY_PREFIX      = "https://josh9456-ddproxy.hf.space/watch/"
OUTPUT_FILE       = "schedule_playlist.m3u8"

EPG_IDS_URL       = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.txt"
EPG_XML_URL       = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"

TVLOGO_RAW_ROOT   = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/"
TVLOGO_API_ROOT   = "https://api.github.com/repos/tv-logo/tv-logos/contents/countries"

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
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
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

# ─────────────── enhanced country name → ISO-2 lookup ────────────────
COUNTRY_CODES = {
    'usa': 'us', 'united states': 'us', 'america': 'us',
    'uk': 'uk', 'united kingdom': 'uk', 'britain': 'uk', 'england': 'uk',
    'canada': 'ca', 'can': 'ca',
    'australia': 'au', 'aus': 'au',
    'new zealand': 'nz', 'newzealand': 'nz',
    'germany': 'de', 'deutschland': 'de',
    'france': 'fr',
    'spain': 'es', 'españa': 'es',
    'italy': 'it', 'italia': 'it',
    'croatia': 'hr',
    'serbia': 'rs',
    'netherlands': 'nl', 'holland': 'nl',
    'portugal': 'pt',
    'poland': 'pl',
    'greece': 'gr',
    'bulgaria': 'bg',
    'israel': 'il',
    'malaysia': 'my',
}

# ───────────────────────────── helpers ────────────────────────────────
def extract_channel_info(channel_name: str) -> tuple[str, str]:
    """
    Split a channel string into (brand, country_code)
    Recognises both “… (UK)” and “… UK” styles.
    """
    name = channel_name.strip()

    # “BBC Two (UK)”
    m = re.search(r'^(.+?)\s*\(([^)]+)\)$', name)
    if m:
        return m.group(1).strip(), COUNTRY_CODES.get(m.group(2).lower(), 'unknown')

    # “… UK”
    parts = name.split()
    for i in range(len(parts) - 1, 0, -1):
        maybe_country = ' '.join(parts[i:]).lower()
        if maybe_country in COUNTRY_CODES:
            return ' '.join(parts[:i]).strip(), COUNTRY_CODES[maybe_country]

    # keyword inside string
    lower = name.lower()
    for country, code in COUNTRY_CODES.items():
        if country in lower:
            brand = re.sub(rf'\b{re.escape(country)}\b', '', name, flags=re.I)
            return brand.strip(), code

    return name, 'unknown'


def build_epg_lookup(epg_lines: list[str]) -> dict[str, list[str]]:
    """
    Build a dictionary with MANY possible keys that can reference an EPG id.
    Keys added for each line:
      • whole line   (case-insensitive)
      • cleaned brand
      • brand slug without spaces
      • brand.country (if country present)
    """
    epg = defaultdict(list)

    for line in epg_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        parts = line.split('.')
        if len(parts) >= 2 and len(parts[-1]) == 2:
            country = parts[-1]
            brand   = '.'.join(parts[:-1])
        else:
            country = None
            brand   = line

        # human clean
        clean_brand = re.sub(r'[^A-Za-z0-9 ]', ' ', brand)
        clean_brand = re.sub(r'\s+', ' ', clean_brand).strip().lower()
        slug        = clean_brand.replace(' ', '')

        for key in {line.lower(), clean_brand, slug}:
            epg[key].append(line)

        if country:
            epg[f"{clean_brand}.{country}"].append(line)
            epg[f"{slug}.{country}"].append(line)

    return epg


def generate_brand_variations(brand: str) -> list[str]:
    """
    A handful of quick substitutions that cover most sports-network quirks.
    """
    out: set[str] = set()
    b = brand.lower()

    # drop filler words
    cleaned = re.sub(r'\b(tv|hd|sd|channel|network|sports?|news)\b', '', b).strip()
    if cleaned:
        out.add(cleaned)

    # written numbers → digits
    num_map = {'one':'1', 'two':'2', 'three':'3', 'four':'4'}
    for word, digit in num_map.items():
        if word in b:
            out.add(b.replace(word, digit))

    # sport(s) singular/plural
    if 'sports' in b:
        out.add(b.replace('sports', 'sport'))
    if 'sport' in b and 'sports' not in b:
        out.add(b.replace('sport', 'sports'))

    # network abbreviations
    net_abbr = {
        'espn': 'espn',
        'fox sports': 'foxsports',
        'sky sports': 'skysports',
        'tnt sports': 'tntsports',
        'bein sports': 'beinsports',
    }
    for full, short in net_abbr.items():
        if full in b:
            out.add(b.replace(full, short))

    # remove spaces altogether
    out.add(b.replace(' ', ''))

    return list(out)


def find_best_epg_match(channel_name: str, epg_lookup: dict[str, list[str]]) -> str:
    """
    1. Exact “brand.country” then “brand”
    2. Variations
    3. Fuzzy similarity ≥ 0.60 (skip keys < 4 chars to avoid ‘a’, ‘tv’, …)
    Returns the FIRST matching EPG id or "".
    """
    brand, country = extract_channel_info(channel_name)
    brand_lc       = brand.lower()
    slug           = brand_lc.replace(' ', '')

    search_keys: list[str] = []

    if country != 'unknown':
        search_keys.extend([f"{brand_lc}.{country}", f"{slug}.{country}", f"{brand_lc}.{country}.hd"])

    search_keys.extend([brand_lc, slug])
    for v in generate_brand_variations(brand):
        search_keys.append(v)
        if country != 'unknown':
            search_keys.append(f"{v}.{country}")

    for key in search_keys:
        if key in epg_lookup:
            hits = epg_lookup[key]
            # pick the hit whose tail matches the country if possible
            if country != 'unknown':
                by_country = [h for h in hits if h.lower().endswith(f".{country}")]
                if by_country:
                    return by_country[0]
            return hits[0]

    # ─── fuzzy rescue ─────────────────────────────────────────────
    candidates = [k for k in epg_lookup if len(k) >= 4]
    best       = difflib.get_close_matches(slug, candidates, n=1, cutoff=0.60)
    if best:
        return epg_lookup[best[0]][0]

    return ""


def download_epg_ids(session: requests.Session) -> dict[str, list[str]]:
    logging.info("Downloading EPG id list …")
    try:
        r = session.get(EPG_IDS_URL, timeout=30)
        r.raise_for_status()
    except Exception as e:
        logging.warning("EPG download failed: %s", e)
        return {}

    epg_lookup = build_epg_lookup(r.text.splitlines())
    logging.info("✓ %d unique EPG lookup keys", len(epg_lookup))
    return epg_lookup


# ╭──────────────────────────── logos ─────────────────────────────╮
def slugify(text: str) -> str:
    txt = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    txt = txt.replace("&", "-and-").replace("+", "-plus-")
    txt = re.sub(r"[^\w\s-]", "", txt)
    return re.sub(r"\s+", "-", txt).strip("-")


def build_logo_index(session: requests.Session) -> dict[str, str]:
    logging.info("Building logo index …")
    index: dict[str, str] = {}
    try:
        r = session.get(TVLOGO_API_ROOT, timeout=30)
        r.raise_for_status()
        countries = [it["name"] for it in r.json() if it["type"] == "dir"]

        for country in countries:
            try:
                files = session.get(f"{TVLOGO_API_ROOT}/{country}", timeout=30).json()
                for entry in files:
                    if entry["type"] != "file" or not entry["name"].endswith(".png"):
                        continue
                    name     = entry["name"]
                    base     = name[:-4]
                    url      = f"{TVLOGO_RAW_ROOT}{country}/{name}"
                    index[name]  = url
                    index[base]  = url
                    # strip country suffixes for generic matching
                    for suf in ("-us", "-uk", "-ca", "-au", "-de", "-fr", "-es", "-it"):
                        if base.endswith(suf):
                            index[base[:-len(suf)]] = url
            except Exception:
                continue
    except Exception as e:
        logging.warning("Logo index build failed: %s", e)

    logging.info("✓ %d logo variants", len(index))
    return index


def find_best_logo(channel: str, logos: dict[str, str]) -> str:
    if not logos:
        return f"{TVLOGO_RAW_ROOT}misc/no-logo.png"

    slug = slugify(channel)
    for key in (slug, slug.replace("-hd", ""), slug.replace("-sd", ""), slug + ".png"):
        if key in logos:
            return logos[key]

    return f"{TVLOGO_RAW_ROOT}misc/no-logo.png"


# ╭────────────────────────── schedule utils ─────────────────────╮
def get_schedule() -> dict:
    r = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def _extract_cid(item) -> str:
    return str(item["channel_id"]) if isinstance(item, dict) else str(item)


def _channel_entries(ev: dict):
    for key in ("channels", "channels2"):
        val = ev.get(key)
        if not val:
            continue
        if isinstance(val, list):
            yield from val
        elif isinstance(val, dict):
            if "channel_id" in val:
                yield val
            else:
                yield from val.values()
        else:
            yield val


def extract_channel_ids(schedule: dict) -> set[str]:
    ids = set()
    for cats in schedule.values():
        for events in cats.values():
            for ev in events:
                ids.update(_extract_cid(ch) for ch in _channel_entries(ev))
    return ids


# ╭───────────────────────────── streams ─────────────────────────╮
def validate_single(url: str) -> str | None:
    for _ in range(3):
        try:
            r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return url
            if r.status_code in (404, 410):
                return None
            if r.status_code == 429:
                time.sleep(5)
                continue
            # fall back to GET
            r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
            if r.status_code == 200:
                return url
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            return None
    return None


def build_stream_map(channel_ids: set[str], workers: int = 30) -> dict[str, str]:
    cand = {tpl.format(num=i): i for i in channel_ids for tpl in URL_TEMPLATES}
    id2url: dict[str, str] = {}
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(validate_single, u): u for u in cand}
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                id2url.setdefault(str(cand[res]), res)
    logging.info("✓ %d working streams", len(id2url))
    return id2url


# ╭────────────────────────── playlist build ─────────────────────╮
def make_playlist(schedule: dict,
                  stream_map: dict[str, str],
                  logos: dict[str, str],
                  epg_lookup: dict[str, list[str]]) -> None:

    lines: list[str] = [
        "#EXTM3U",
        f'#EXTM3U url-tvg="{EPG_XML_URL}"'
    ]

    grouped = defaultdict(list)
    for day, cats in schedule.items():
        for cat, events in cats.items():
            grouped[cat.upper()].extend(events)

    epg_hits = 0
    total    = 0

    for group in sorted(grouped):
        for ev in grouped[group]:
            title = ev["event"]
            for ch in _channel_entries(ev):
                cname = ch["channel_name"] if isinstance(ch, dict) else str(ch)
                cid   = _extract_cid(ch)
                stream = stream_map.get(cid)
                if not stream:
                    continue

                total += 1
                epg_id = find_best_epg_match(cname, epg_lookup)
                if epg_id:
                    epg_hits += 1
                    tvg_id = epg_id
                else:
                    tvg_id = cid  # fallback: raw channel id

                logo_url = find_best_logo(cname, logos)

                lines.append(
                    f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo_url}" '
                    f'group-title="{group}",{title} ({cname})'
                )
                lines.extend(VLC_HEADERS)
                encoded = base64.b64encode(stream.encode()).decode()
                lines.append(f"{PROXY_PREFIX}{encoded}.m3u8")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fp:
        fp.write('\n'.join(lines) + '\n')

    pct = (epg_hits / total * 100) if total else 0
    logging.info("Playlist written: %s  (%d items, %d matched EPG  %.1f%%)",
                 OUTPUT_FILE, total, epg_hits, pct)


# ╭────────────────────────────── main ───────────────────────────╮
def main() -> None:
    ap = argparse.ArgumentParser(description="Build live-events playlist with smart EPG matching")
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose / debug logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s │ %(message)s",
    )

    logging.info("Fetching schedule …")
    schedule = get_schedule()

    chan_ids  = extract_channel_ids(schedule)
    stream_map = build_stream_map(chan_ids)

    with requests.Session() as s:
        logo_index = build_logo_index(s)
        epg_lookup = download_epg_ids(s)

    make_playlist(schedule, stream_map, logo_index, epg_lookup)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
