#!/usr/bin/env python3
"""
events.py – build live-events playlist with country-smart EPG matching
"""

import argparse
import base64
import logging
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ─── constants ──────────────────────────────────────────────
SCHEDULE_URL = "https://daddylive.dad/schedule/schedule-generated.php"
PROXY_PREFIX = "https://josh9456-myproxy.hf.space/watch/"
OUTPUT_FILE  = "schedule_playlist.m3u8"

# Use the lightweight text file for ID mapping, XML for TiviMate EPG data
EPG_IDS_URL = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.txt"
EPG_XML_URL = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"

TVLOGO_RAW_ROOT = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/"
TVLOGO_API_ROOT = "https://api.github.com/repos/tv-logo/tv-logos/contents/countries"

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

# Country preference for EPG matching (highest priority first)
COUNTRY_PRIORITY = [
    'uk', 'gb', 'en', 'us', 'usa', 'au', 'australia', 'ca', 'canada', 'nz', 'newzealand'
]

# ─── helpers ────────────────────────────────────────────────
def slugify(channel: str) -> str:
    """Convert channel name to lowercase slug with hyphens."""
    txt = unicodedata.normalize("NFKD", channel).encode("ascii", "ignore").decode().lower()
    txt = txt.replace("&", "-and-").replace("+", "-plus-")
    txt = re.sub(r"[^\w\s-]", "", txt)
    txt = re.sub(r"\s+", "-", txt).strip("-")
    return txt

def normalize_channel_name(name: str) -> str:
    """Normalize channel name for EPG matching."""
    name = name.upper().strip()
    suffixes = [" HD", " SD", " US", " UK", " CA", " AU", " DE", " FR", " ES", " IT", " NL"]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return re.sub(r'[^\w\s]', '', name).strip()

def get_country_priority(tvg_id: str) -> int:
    """Return priority score for country preference (lower = better)."""
    tvg_lower = tvg_id.lower()
    for i, country in enumerate(COUNTRY_PRIORITY):
        if country in tvg_lower:
            return i
    return 999  # No preferred country found

def is_valid_tvg_id(tvg_id: str) -> bool:
    """Check if a string looks like a valid TVG-ID."""
    if not tvg_id or len(tvg_id) < 3:
        return False
    
    # Reject patterns like "A+.fr"
    if re.match(r'^[A-Z]\+\.[a-z]{2}$', tvg_id):
        return False
    
    # Accept domain-like or longer alphanumeric IDs
    if re.match(r'^[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}$', tvg_id):
        return True
        
    if re.match(r'^[a-zA-Z0-9._-]{5,}$', tvg_id):
        return True
        
    return False

def download_epg_ids_from_txt(session: requests.Session) -> dict:
    """Download the lightweight text file containing EPG channel IDs."""
    logging.info("Downloading EPG IDs from text file...")
    
    try:
        r = session.get(EPG_IDS_URL, timeout=30)
        r.raise_for_status()
        
        logging.info("✓ EPG IDs downloaded (%d bytes)", len(r.content))
        
        # Store multiple matches per normalized name for country prioritization
        epg_channels = defaultdict(list)
        lines = r.text.splitlines()
        valid_ids = 0
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            if is_valid_tvg_id(line):
                normalized = normalize_channel_name(line)
                # Store all matches, we'll sort by country priority later
                epg_channels[normalized].append(line)
                epg_channels[line.upper().strip()].append(line)
                epg_channels[line.lower().strip()].append(line)
                valid_ids += 1
                
                if '.' in line or '_' in line:
                    clean = line.replace('.', ' ').replace('_', ' ')
                    clean_norm = normalize_channel_name(clean)
                    epg_channels[clean_norm].append(line)
            else:
                # Create TVG-ID from channel name
                normalized = normalize_channel_name(line)
                tvg_id = slugify(line).replace('-', '.')
                if tvg_id:
                    epg_channels[normalized].append(tvg_id)
                    epg_channels[line.upper().strip()].append(tvg_id)
                    epg_channels[line.lower().strip()].append(tvg_id)
        
        # Convert defaultdict to regular dict and sort by country priority
        final_channels = {}
        for key, id_list in epg_channels.items():
            # Sort by country priority (English/UK first)
            sorted_ids = sorted(set(id_list), key=get_country_priority)
            final_channels[key] = sorted_ids[0]  # Take the best match
        
        logging.info("✓ EPG IDs parsed: %d mappings (%d valid IDs)", len(final_channels), valid_ids)
        return final_channels
        
    except Exception as e:
        logging.warning("Failed to download EPG IDs: %s", e)
        return {}

def find_epg_id(channel_name: str, epg_channels: dict) -> str:
    """Find the best matching EPG channel ID with country preference."""
    if not epg_channels:
        return ""
    
    normalized = normalize_channel_name(channel_name)
    original_upper = channel_name.upper().strip()
    original_lower = channel_name.lower().strip()
    
    # Direct matches first (already country-prioritized)
    for candidate in [normalized, original_upper, original_lower]:
        if candidate in epg_channels:
            tvg_id = epg_channels[candidate]
            if is_valid_tvg_id(tvg_id):
                logging.debug("✓ EPG match for %s: %s", channel_name, tvg_id)
                return tvg_id
    
    # Fuzzy matching with country preference
    matches = []
    for epg_name, epg_id in epg_channels.items():
        if (normalized in epg_name or epg_name in normalized) and is_valid_tvg_id(epg_id):
            priority = get_country_priority(epg_id)
            matches.append((priority, epg_id))
    
    if matches:
        # Sort by country priority and take the best
        matches.sort()
        best_match = matches[0][1]
        logging.debug("✓ EPG fuzzy match for %s: %s", channel_name, best_match)
        return best_match
    
    # First word matching with country preference
    first_word = normalized.split()[0] if normalized.split() else ""
    if first_word and len(first_word) > 2:
        matches = []
        for epg_name, epg_id in epg_channels.items():
            if first_word in epg_name.lower() and is_valid_tvg_id(epg_id):
                priority = get_country_priority(epg_id)
                matches.append((priority, epg_id))
        
        if matches:
            matches.sort()
            best_match = matches[0][1]
            logging.debug("✓ EPG first-word match for %s: %s", channel_name, best_match)
            return best_match
    
    return ""

def build_comprehensive_logo_index(session: requests.Session) -> dict:
    """Build complete logo index from tv-logo repo."""
    logging.info("Building comprehensive logo index from tv-logo repo...")
    logo_index = {}
    
    try:
        r = session.get(TVLOGO_API_ROOT, timeout=30)
        r.raise_for_status()
        countries = [item["name"] for item in r.json() if item["type"] == "dir"]
        
        logging.info("Found %d countries in logo repo", len(countries))
        
        for country in countries:
            try:
                country_url = f"{TVLOGO_API_ROOT}/{country}"
                r = session.get(country_url, timeout=30)
                r.raise_for_status()
                
                for file_info in r.json():
                    if file_info["type"] == "file" and file_info["name"].endswith(".png"):
                        filename = file_info["name"]
                        full_url = f"{TVLOGO_RAW_ROOT}{country}/{filename}"
                        
                        base_name = filename.replace(".png", "")
                        logo_index[filename] = full_url
                        logo_index[base_name] = full_url
                        
                        for suffix in ["-us", "-uk", "-ca", "-au", "-de", "-fr", "-es", "-it"]:
                            if base_name.endswith(suffix):
                                clean_name = base_name.replace(suffix, "")
                                logo_index[clean_name] = full_url
                                logo_index[clean_name + ".png"] = full_url
                        
            except Exception as e:
                logging.debug("Failed to fetch logos for %s: %s", country, e)
                continue
        
        logging.info("✓ Logo index built with %d entries", len(logo_index))
        return logo_index
        
    except Exception as e:
        logging.warning("Failed to build logo index: %s", e)
        return {}

def find_best_logo(channel_name: str, logo_index: dict) -> str:
    """Find the best matching logo for a channel name."""
    if not logo_index:
        return "https://raw.githubusercontent.com/tv-logo/tv-logos/main/misc/no-logo.png"
        
    slug = slugify(channel_name)
    variations = [
        slug, slug + ".png", slug.replace("-hd", ""), slug.replace("-sd", ""),
        slug.split("-")[0], channel_name.lower().replace(" ", "-"),
        channel_name.lower().replace(" ", "-") + ".png",
    ]
    
    for variant in variations:
        if variant in logo_index:
            logging.debug("✓ Logo found for %s: %s", channel_name, variant)
            return logo_index[variant]
    
    return "https://raw.githubusercontent.com/tv-logo/tv-logos/main/misc/no-logo.png"

def get_schedule():
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
                ids.update(_extract_cid(ch) for ch in _channel_entries(ev))
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

def make_playlist(schedule, stream_map, logo_index, epg_channels):
    lines = ["#EXTM3U"]
    lines.append(f"#EXTM3U url-tvg=\"{EPG_XML_URL}\"")
    
    grouped = defaultdict(list)
    for day, cats in schedule.items():
        for cat, events in cats.items():
            grouped[cat.upper()].extend(events)

    epg_matches = 0
    total_entries = 0

    for group in sorted(grouped):
        for ev in grouped[group]:
            title = ev["event"]
            for ch in _channel_entries(ev):
                cname = ch["channel_name"] if isinstance(ch, dict) else str(ch)
                cid = _extract_cid(ch)
                stream = stream_map.get(cid)
                if not stream:
                    continue

                total_entries += 1
                
                # Find EPG ID with country prioritization
                epg_id = find_epg_id(cname, epg_channels)
                if epg_id and is_valid_tvg_id(epg_id):
                    epg_matches += 1
                    tvg_id = epg_id
                else:
                    tvg_id = cid
                
                logo_url = find_best_logo(cname, logo_index)
                extinf = (
                    f'#EXTINF:-1 tvg-id="{tvg_id}" '
                    f'tvg-logo="{logo_url}" '
                    f'group-title="{group}",{title} ({cname})'
                )

                encoded = base64.b64decode(stream.encode()).decode()
                proxy = f"{PROXY_PREFIX}{encoded}.m3u8"

                lines.append(extinf)
                lines.extend(VLC_HEADERS)
                lines.append(proxy)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    
    logging.info("Playlist written to %s (%d events, %d EPG matches)", 
                 OUTPUT_FILE, total_entries, epg_matches)

def main():
    ap = argparse.ArgumentParser(description="Build live-events playlist with country-smart EPG")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s │ %(message)s",
    )

    schedule = get_schedule()
    chan_ids = extract_channel_ids(schedule)
    stream_map = build_stream_map(chan_ids)

    with requests.Session() as session:
        logo_index = build_comprehensive_logo_index(session)
        epg_channels = download_epg_ids_from_txt(session)
        make_playlist(schedule, stream_map, logo_index, epg_channels)

if __name__ == "__main__":
    main()
