#!/usr/bin/env python3
"""
events.py — live-events playlist builder
• validates DaddyLive streams
• assigns channel logos
• maps the **correct** tvg-id from epgshare01   
  – now copes with abbreviations (SkySp, TNTSp, …)
  – ranks countries so “.uk” wins over “.ie”, etc.
"""

from __future__ import annotations
import argparse
import base64
import difflib
import logging
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ═════════════════════════════ constants ═══════════════════════════════════
SCHEDULE_URL   = "https://daddylive.dad/schedule/schedule-generated.php"
PROXY_PREFIX   = "https://josh9456-ddproxy.hf.space/watch/"
OUTPUT_FILE    = "schedule_playlist.m3u8"

EPG_IDS_URL    = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.txt"
EPG_XML_URL    = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"

TVLOGO_RAW     = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/"
TVLOGO_API     = "https://api.github.com/repos/tv-logo/tv-logos/contents/countries"

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

# ═════ country helper ══════════════════════════════════════════════════════
COUNTRY_CODES = {
    'usa': 'us', 'united states': 'us', 'america': 'us',
    'uk': 'uk', 'united kingdom': 'uk', 'britain': 'uk', 'england': 'uk',
    'canada': 'ca', 'can': 'ca',
    'australia': 'au', 'aus': 'au',
    'new zealand': 'nz', 'newzealand': 'nz',
    'germany': 'de', 'deutschland': 'de', 'german': 'de',
    'france': 'fr', 'french': 'fr',
    'spain': 'es', 'españa': 'es', 'spanish': 'es',
    'italy': 'it', 'italia': 'it', 'italian': 'it',
    'croatia': 'hr', 'serbia': 'rs', 'netherlands': 'nl', 'holland': 'nl',
    'portugal': 'pt', 'poland': 'pl', 'greece': 'gr', 'bulgaria': 'bg',
    'israel': 'il', 'malaysia': 'my',
}

# ═════ NEW: abbreviation map used both ways ════════════════════════════════
ABBR_MAP = {
    "sp":     "sports",
    "sp1":    "sports1",
    "sp2":    "sports2",
    "sn":     "sportsnetwork",
    "soc":    "soccer",
    "mn":     "mainevent",
    "nw":     "network",
}

# ═════════════════════════════ helpers ═════════════════════════════════════
def extract_channel_info(name: str) -> tuple[str, str]:
    """
    Return (brand, ISO-2 country) from strings like
    “Sky Sports Racing UK”, “BBC Two (UK)”, …
    """
    name = name.strip()
    m = re.search(r'^(.*?)\s*\(([^)]+)\)$', name)
    if m:
        country = COUNTRY_CODES.get(m.group(2).lower(), 'unknown')
        return m.group(1).strip(), country

    parts = name.split()
    for i in range(len(parts) - 1, 0, -1):
        maybe = ' '.join(parts[i:]).lower()
        if maybe in COUNTRY_CODES:
            return ' '.join(parts[:i]).strip(), COUNTRY_CODES[maybe]
    lower = name.lower()
    for label, code in COUNTRY_CODES.items():
        if label in lower:
            brand = re.sub(rf'\b{re.escape(label)}\b', '', name, flags=re.I)
            return brand.strip(), code
    return name, 'unknown'

# ── abbreviation utils ────────────────────────────────────────────────────
def _expand_abbr(slug: str) -> list[str]:
    res = {slug}
    for ab, full in ABBR_MAP.items():
        if ab in slug:
            res.add(slug.replace(ab, full))
    return list(res)

def _compress_long(slug: str) -> list[str]:
    res = {slug}
    for ab, full in ABBR_MAP.items():
        if full in slug:
            res.add(slug.replace(full, ab))
    return list(res)

# ── EPG lookup build ───────────────────────────────────────────────────────
def build_epg_lookup(lines: list[str]) -> dict[str, list[str]]:
    """
    For every EPG line create MANY aliases, so
      TNT.Sports.4.HD.uk  →  tnt sports 4 hd, tnt sports 4, tnt sports …
    All aliases also exist with the country suffix: “… uk”.
    """
    table: dict[str, list[str]] = defaultdict(list)

    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue

        # split “… .uk”  or keep whole line if no country code
        parts    = raw.split(".")
        country  = parts[-1].lower() if len(parts[-1]) == 2 else None
        brand    = parts[:-1] if country else parts           # every block except cc
        brand_sp = " ".join(brand)                            # dotted → spaced words
        brand_cl = re.sub(r"[^a-z0-9 ]", " ", brand_sp.lower())
        brand_cl = re.sub(r"\s+", " ", brand_cl).strip()      # normalised

        # progressive prefixes:  "tnt sports 4 hd" →  full, drop "hd", drop "4", …
        words = brand_cl.split()
        for i in range(len(words), 0, -1):
            frag = " ".join(words[:i])
            for key in (frag, frag.replace(" ", "")):         # spaced and slug form
                table[key].append(raw)
                if country:
                    table[f"{key}.{country}"].append(raw)

        # original full lower-cased line for safety
        table[raw.lower()].append(raw)

    return table

# ── brand variation generator ──────────────────────────────────────────────
def generate_brand_variations(brand: str) -> list[str]:
    out: set[str] = set()
    b = brand.lower()

    out.add(re.sub(r'\b(tv|hd|sd|channel|network|sports?|news)\b', '', b).strip())
    num = {'one':'1','two':'2','three':'3','four':'4'}
    for word,dig in num.items():
        if word in b:
            out.add(b.replace(word, dig))
    if 'sports' in b:
        out.add(b.replace('sports', 'sport'))
    if 'sport' in b and 'sports' not in b:
        out.add(b.replace('sport', 'sports'))

    nets = {'espn':'espn', 'fox sports':'foxsports',
            'sky sports':'skysports', 'tnt sports':'tntsports',
            'bein sports':'beinsports'}
    for full, short in nets.items():
        if full in b:
            out.add(b.replace(full, short))

    slug = b.replace(' ', '')
    out |= set(_compress_long(slug))
    out.add(slug)
    return [v for v in out if v]

# ── country ranking for competing IDs ──────────────────────────────────────
def _best_by_country(matches: list[str], prefer: str | None) -> str:
    if prefer:
        for m in matches:
            if m.lower().endswith(f".{prefer}"):
                return m
    prio = ['uk', 'gb', 'ie', 'us', 'ca', 'au']
    for c in prio:
        for m in matches:
            if m.lower().endswith(f".{c}"):
                return m
    return matches[0]

# ── EPG match ───────────────────────────────────────────────────────────────
def find_best_epg_match(channel_name: str, lookup: dict[str, list[str]]) -> str:
    brand, country = extract_channel_info(channel_name)
    brand_lc = brand.lower()
    slug     = brand_lc.replace(' ', '')

    keys: list[str] = []
    if country != 'unknown':
        keys += [f"{brand_lc}.{country}", f"{slug}.{country}",
                 f"{brand_lc}.{country}.hd"]
    keys += [brand_lc, slug]
    for v in generate_brand_variations(brand):
        keys.append(v)
        if country != 'unknown':
            keys.append(f"{v}.{country}")

    for k in keys:
        if k in lookup:
            return _best_by_country(lookup[k], None if country=='unknown' else country)

    # fuzzy safety net
    candidates = [k for k in lookup if len(k) >= 4]
    best = difflib.get_close_matches(slug, candidates, n=1, cutoff=0.60)
    if best:
        return lookup[best[0]][0]
    return ""

# ═════ logo helpers ════════════════════════════════════════════════════════
def slugify(text: str) -> str:
    txt = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    txt = txt.replace("&", "-and-").replace("+", "-plus-")
    txt = re.sub(r"[^\w\s-]", "", txt)
    return re.sub(r"\s+", "-", txt).strip("-")

def build_logo_index(sess: requests.Session) -> dict[str, str]:
    index: dict[str, str] = {}
    try:
        countries = [d["name"] for d in sess.get(TVLOGO_API, timeout=30).json()
                     if d["type"] == "dir"]
        for c in countries:
            r = sess.get(f"{TVLOGO_API}/{c}", timeout=30)
            for f in r.json():
                if f["type"] != "file" or not f["name"].endswith(".png"):
                    continue
                base = f["name"][:-4]
                url  = f"{TVLOGO_RAW}{c}/{f['name']}"
                index.update({f["name"]: url, base: url})
                for suf in ("-us","-uk","-ca","-au","-de","-fr","-es","-it"):
                    if base.endswith(suf):
                        index[base[:-len(suf)]] = url
    except Exception as e:
        logging.warning("logo index build failed: %s", e)
    logging.info("✓ %d logo variants", len(index))
    return index

def find_best_logo(name: str, logos: dict[str, str]) -> str:
    if not logos:
        return f"{TVLOGO_RAW}misc/no-logo.png"
    slug = slugify(name)
    for var in (slug, slug + ".png", slug.replace("-hd",""), slug.replace("-sd","")):
        if var in logos:
            return logos[var]
    return f"{TVLOGO_RAW}misc/no-logo.png"

# ═════ schedule / streams ══════════════════════════════════════════════════
def get_schedule():
    r = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()

def _extract_cid(item) -> str:
    return str(item["channel_id"]) if isinstance(item, dict) else str(item)

def _channel_entries(event):
    for key in ("channels", "channels2"):
        val = event.get(key)
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

def extract_channel_ids(schedule) -> set[str]:
    out = set()
    for cats in schedule.values():
        for events in cats.values():
            for ev in events:
                out.update(_extract_cid(ch) for ch in _channel_entries(ev))
    return out

# stream validation ---------------------------------------------------------
def validate_single(url: str) -> str | None:
    for _ in range(3):
        try:
            r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return url
            if r.status_code in (404,410):
                return None
            if r.status_code == 429:
                time.sleep(5); continue
            r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
            if r.status_code == 200:
                return url
            if r.status_code in (404,410):
                return None
        except requests.RequestException:
            return None
    return None

def build_stream_map(ids: set[str], workers: int = 30) -> dict[str, str]:
    cand = {tpl.format(num=i): i for i in ids for tpl in URL_TEMPLATES}
    id2url: dict[str, str] = {}
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(validate_single, u): u for u in cand}
        for fut in as_completed(futs):
            url = fut.result()
            if url:
                id2url.setdefault(str(cand[url]), url)
    logging.info("✓ %d working streams", len(id2url))
    return id2url

# ═════ main playlist build ═════════════════════════════════════════════════
def make_playlist(schedule, streams, logos, epg_lookup):
    lines = ["#EXTM3U", f'#EXTM3U url-tvg="{EPG_XML_URL}"']
    grouped = defaultdict(list)
    for cats in schedule.values():
        for cat, events in cats.items():
            grouped[cat.upper()].extend(events)

    total = epg_ok = 0
    for group in sorted(grouped):
        for ev in grouped[group]:
            title = ev["event"]
            for ch in _channel_entries(ev):
                cname = ch["channel_name"] if isinstance(ch, dict) else str(ch)
                cid   = _extract_cid(ch)
                url   = streams.get(cid)
                if not url:
                    continue
                total += 1
                tvg_id = find_best_epg_match(cname, epg_lookup) or cid
                if tvg_id != cid:
                    epg_ok += 1
                logo   = find_best_logo(cname, logos)
                lines.append(
                    f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}" '
                    f'group-title="{group}",{title} ({cname})'
                )
                lines.extend(VLC_HEADERS)
                lines.append(f"{PROXY_PREFIX}{base64.b64encode(url.encode()).decode()}.m3u8")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fp:
        fp.write('\n'.join(lines) + '\n')

    pct = epg_ok / total * 100 if total else 0
    logging.info("Playlist %s   items:%d  epg:%d (%.1f%%)",
                 OUTPUT_FILE, total, epg_ok, pct)

# ═════ download helpers ════════════════════════════════════════════════════
def download_epg_lookup(sess: requests.Session):
    logging.info("Downloading EPG id list …")
    try:
        txt = sess.get(EPG_IDS_URL, timeout=30).text
    except Exception as e:
        logging.warning("EPG list download failed: %s", e)
        return {}
    lookup = build_epg_lookup(txt.splitlines())
    logging.info("✓ %d unique lookup keys", len(lookup))
    return lookup

# ═════ main entry ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="Build live playlist with robust EPG matching")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s │ %(message)s")

    schedule = get_schedule()
    ids      = extract_channel_ids(schedule)
    streams  = build_stream_map(ids)

    with requests.Session() as s:
        logos = build_logo_index(s)
        epg   = download_epg_lookup(s)

    make_playlist(schedule, streams, logos, epg)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
