#!/usr/bin/env python3
"""
events.py – build live-events playlist with tv-logo repo artwork and EPG matching
"""

import argparse
import base64
import gzip
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ─── constants ──────────────────────────────────────────────
SCHEDULE_URL = "https://daddylive.dad/schedule/schedule-generated.php"
PROXY_PREFIX = "https://josh9456-ddproxy.hf.space/watch/"
OUTPUT_FILE  = "schedule_playlist.m3u8"
EPG_URL = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"

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
    # Remove common suffixes that might interfere with matching
    suffixes = [" HD", " SD", " US", " UK", " CA", " AU", " DE", " FR", " ES", " IT", " NL"]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return re.sub(r'[^\w\s]', '', name).strip()

def download_and_parse_epg(session: requests.Session) -> dict:
    """Download and parse compressed EPG XML to build channel mapping."""
    logging.info("Downloading compressed EPG from %s...", EPG_URL)
    
    try:
        r = session.get(EPG_URL, timeout=120)  # Longer timeout for large file
        r.raise_for_status()
        
        logging.info("✓ EPG downloaded (%d bytes), decompressing...", len(r.content))
        
        # Decompress gzip content
        xml_content = gzip.decompress(r.content)
        logging.info("✓ EPG decompressed (%d bytes)", len(xml_content))
        
        # Parse XML
        root = ET.fromstring(xml_content)
        epg_channels = {}
        
        # Extract channel information from XMLTV format
        for channel in root.findall('.//channel'):
            channel_id = channel.get('id', '')
            display_names = [dn.text for dn in channel.findall('display-name') if dn.text]
            
            if channel_id and display_names:
                # Store all possible display names for this channel ID
                for name in display_names:
                    normalized = normalize_channel_name(name)
                    epg_channels[normalized] = channel_id
                    # Also store the original name
                    epg_channels[name.upper().strip()] = channel_id
                    # Store lowercase version too
                    epg_channels[name.lower().strip()] = channel_id
        
        logging.info("✓ EPG parsed with %d channel mappings", len(epg_channels))
        return epg_channels
        
    except Exception as e:
        logging.warning("Failed to download/parse EPG: %s", e)
        return {}

def find_epg_id(channel_name: str, epg_channels: dict) -> str:
    """Find the best matching EPG channel ID for a given channel name."""
    if not epg_channels:
        return ""
    
    normalized = normalize_channel_name(channel_name)
    original_upper = channel_name.upper().strip()
    original_lower = channel_name.lower().strip()
    
    # Direct matches first
    for candidate in [normalized, original_upper, original_lower]:
        if candidate in epg_channels:
            logging.debug("✓ EPG match for %s: %s", channel_name, epg_channels[candidate])
            return epg_channels[candidate]
    
    # Fuzzy matching - find partial matches
    for epg_name, epg_id in epg_channels.items():
        if normalized in epg_name or epg_name in normalized:
            logging.debug("✓ EPG fuzzy match for %s: %s", channel_name, epg_id)
            return epg_id
    
    # Try just the first word
    first_word = normalized.split()[0] if normalized.split() else ""
    if first_word and len(first_word) > 2:  # Only if meaningful word
        for epg_name, epg_id in epg_channels.items():
            if first_word in epg_name.lower():
                logging.debug("✓ EPG first-word match for %s: %s", channel_name, epg_id)
                return epg_id
    
    return ""

def build_comprehensive_logo_index(session: requests.Session) -> dict:
    """Build complete logo index from tv-logo repo."""
    logging.info("Building comprehensive logo index from tv-logo repo...")
    logo_index = {}
    
    try:
        # Get all country directories
        r = session.get(TVLOGO_API_ROOT, timeout=30)
        r.raise_for_status()
        countries = [item["name"] for item in r.json() if item["type"] == "dir"]
        
        logging.info("Found %d countries in logo repo", len(countries))
        
        # For each country, get all logo files
        for country in countries:
            try:
                country_url = f"{TVLOGO_API_ROOT}/{country}"
                r = session.get(country_url, timeout=30)
                r.raise_for_status()
                
                for file_info in r.json():
                    if file_info["type"] == "file" and file_info["name"].endswith(".png"):
                        filename = file_info["name"]
                        full_url = f"{TVLOGO_RAW_ROOT}{country}/{filename}"
                        
                        # Store multiple variations for flexible matching
                        base_name = filename.replace(".png", "")
                        logo_index[filename] = full_url
                        logo_index[base_name] = full_url
                        
                        # Also store without country suffix for broader matching
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
    
    # Try multiple variations
    variations = [
        slug,
        slug + ".png",
        slug.replace("-hd", ""),
        slug.replace("-sd", ""),
        slug.split("-")[0],  # Just first word
        channel_name.lower().replace(" ", "-"),
        channel_name.lower().replace(" ", "-") + ".png",
    ]
    
    for variant in variations:
        if variant in logo_index:
            logging.debug("✓ Logo found for %s: %s", channel_name, variant)
            return logo_index[variant]
    
    # Fallback to generic logo
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
    
    # Add EPG URL to playlist header
    lines.append(f"#EXTM3U url-tvg=\"{EPG_URL}\"")
    
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
                
                # Find EPG ID
                epg_id = find_epg_id(cname, epg_channels)
                if epg_id:
                    epg_matches += 1
                tvg_id = epg_id if epg_id else cid
                
                logo_url = find_best_logo(cname, logo_index)
                extinf = (
                    f'#EXTINF:-1 tvg-id="{tvg_id}" '
                    f'tvg-logo="{logo_url}" '
                    f'group-title="{group}",{title} ({cname})'
                )

                encoded = base64.b64encode(stream.encode()).decode()
                proxy = f"{PROXY_PREFIX}{encoded}.m3u8"

                lines.append(extinf)
                lines.extend(VLC_HEADERS)
                lines.append(proxy)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    
    logging.info("Playlist written to %s (%d events, %d EPG matches)", 
                 OUTPUT_FILE, total_entries, epg_matches)

def main():
    ap = argparse.ArgumentParser(description="Build live-events playlist with logos and EPG")
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
        epg_channels = download_and_parse_epg(session)
        make_playlist(schedule, stream_map, logo_index, epg_channels)

if __name__ == "__main__":
    main()
