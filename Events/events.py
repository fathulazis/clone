#!/usr/bin/env python3
"""
events.py – better EPG matching for live events playlist
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
PROXY_PREFIX = "https://josh9456-ddproxy.hf.space/watch/"
OUTPUT_FILE  = "schedule_playlist.m3u8"

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

# Enhanced country mapping for better EPG matching
COUNTRY_CODES = {
    'usa': 'us', 'united states': 'us', 'america': 'us',
    'uk': 'uk', 'united kingdom': 'uk', 'britain': 'uk', 'england': 'uk',
    'canada': 'ca', 'can': 'ca',
    'australia': 'au', 'aus': 'au',
    'new zealand': 'nz', 'newzealand': 'nz',
    'germany': 'de', 'deutschland': 'de', 'german': 'de',
    'france': 'fr', 'french': 'fr',
    'spain': 'es', 'spanish': 'es', 'españa': 'es',
    'italy': 'it', 'italian': 'it', 'italia': 'it',
    'croatia': 'hr', 'hrvatska': 'hr',
    'serbia': 'rs', 'srbija': 'rs',
    'netherlands': 'nl', 'holland': 'nl', 'dutch': 'nl',
    'portugal': 'pt', 'portuguese': 'pt',
    'poland': 'pl', 'polish': 'pl', 'polska': 'pl',
    'greece': 'gr', 'greek': 'gr',
    'bulgaria': 'bg', 'bulgarian': 'bg',
    'israel': 'il', 'hebrew': 'il',
    'malaysia': 'my', 'malay': 'my',
}

# ─── helpers ────────────────────────────────────────────────
def extract_channel_info(channel_name):
    """Extract channel brand and country from full channel name."""
    # Common patterns: "Channel Name Country" or "Channel Name (Country)"
    name = channel_name.strip()
    
    # Handle parentheses format: "BBC Two (UK)" -> "BBC Two", "UK"
    paren_match = re.search(r'^(.+?)\s*\(([^)]+)\)$', name)
    if paren_match:
        channel_brand = paren_match.group(1).strip()
        country_part = paren_match.group(2).strip().lower()
        country = COUNTRY_CODES.get(country_part, country_part[:2])
        return channel_brand, country
    
    # Handle space-separated format: "BBC Two UK" -> "BBC Two", "UK"
    words = name.split()
    if len(words) >= 2:
        # Check if last word(s) are countries
        for i in range(len(words)-1, 0, -1):
            potential_country = ' '.join(words[i:]).lower()
            if potential_country in COUNTRY_CODES:
                channel_brand = ' '.join(words[:i]).strip()
                country = COUNTRY_CODES[potential_country]
                return channel_brand, country
    
    # Fallback: try to detect country from common patterns
    name_lower = name.lower()
    for country_name, code in COUNTRY_CODES.items():
        if country_name in name_lower:
            channel_brand = re.sub(rf'\b{re.escape(country_name)}\b', '', name_lower, flags=re.IGNORECASE).strip()
            return channel_brand.title(), code
    
    return name, 'unknown'

def build_epg_lookup(epg_text_lines):
    """Build comprehensive EPG lookup with channel brand extraction."""
    epg_lookup = defaultdict(list)
    
    for line in epg_text_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
            
        # Parse EPG ID format: usually "ChannelName.country" or similar
        parts = line.split('.')
        if len(parts) >= 2:
            # Extract potential channel name and country
            potential_channel = parts[0]
            potential_country = parts[-1] if len(parts[-1]) == 2 else None
            
            # Clean channel name
            clean_channel = re.sub(r'[^a-zA-Z0-9\s]', ' ', potential_channel)
            clean_channel = re.sub(r'\s+', ' ', clean_channel).strip()
            
            # Store multiple lookup keys
            epg_lookup[clean_channel.lower()].append(line)
            epg_lookup[line.lower()].append(line)
            
            # If we have a country, also store country-specific lookups
            if potential_country:
                key = f"{clean_channel.lower()}.{potential_country}"
                epg_lookup[key].append(line)
        else:
            # Handle simple format
            clean = re.sub(r'[^a-zA-Z0-9\s]', ' ', line)
            clean = re.sub(r'\s+', ' ', clean).strip()
            epg_lookup[clean.lower()].append(line)
            epg_lookup[line.lower()].append(line)
    
    return epg_lookup

def find_best_epg_match(channel_name, epg_lookup):
    """Find best EPG match using intelligent channel brand + country matching."""
    channel_brand, country = extract_channel_info(channel_name)
    
    # Priority order for matching
    search_terms = []
    
    # 1. Exact brand + country match
    if country != 'unknown':
        search_terms.append(f"{channel_brand.lower()}.{country}")
        search_terms.append(f"{channel_brand.lower()}.{country}.hd")
    
    # 2. Brand variations
    brand_clean = re.sub(r'\s+', '', channel_brand.lower())
    search_terms.append(brand_clean)
    search_terms.append(channel_brand.lower())
    
    # 3. Handle common network name variations
    brand_variations = generate_brand_variations(channel_brand)
    for variation in brand_variations:
        if country != 'unknown':
            search_terms.append(f"{variation}.{country}")
        search_terms.append(variation)
    
    # Try each search term
    for term in search_terms:
        if term in epg_lookup:
            matches = epg_lookup[term]
            # Prefer matches with correct country
            if country != 'unknown':
                country_matches = [m for m in matches if f".{country}" in m.lower()]
                if country_matches:
                    logging.debug(f"EPG match for {channel_name}: {country_matches[0]}")
                    return country_matches[0]
            
            # Return first match if no country preference
            logging.debug(f"EPG match for {channel_name}: {matches[0]}")
            return matches[0]
    
    # Fallback: fuzzy matching
    for epg_id, _ in epg_lookup.items():
        if brand_clean in epg_id or epg_id in brand_clean:
            return epg_lookup[epg_id][0]
    
    return ""

def generate_brand_variations(brand):
    """Generate common variations of channel brand names."""
    variations = set()
    brand_lower = brand.lower()
    
    # Remove common words
    clean_brand = re.sub(r'\b(tv|hd|sd|channel|network|sports?|news)\b', '', brand_lower).strip()
    if clean_brand:
        variations.add(clean_brand)
    
    # Handle abbreviations: "BBC Two" -> "bbc2", "BBC 2"
    if 'two' in brand_lower:
        variations.add(brand_lower.replace('two', '2'))
        variations.add(brand_lower.replace(' two', '2'))
    elif 'three' in brand_lower:
        variations.add(brand_lower.replace('three', '3'))
        variations.add(brand_lower.replace(' three', '3'))
    
    # Handle "Sports" variations
    if 'sports' in brand_lower:
        variations.add(brand_lower.replace('sports', 'sport'))
    elif 'sport' in brand_lower:
        variations.add(brand_lower.replace('sport', 'sports'))
    
    # Handle network abbreviations
    network_abbrevs = {
        'espn': 'espn',
        'fox sports': 'foxsports',
        'sky sports': 'skysports',
        'tnt sports': 'tntsports',
        'bein sports': 'beinsports',
    }
    
    for full_name, abbrev in network_abbrevs.items():
        if full_name in brand_lower:
            variations.add(brand_lower.replace(full_name, abbrev))
    
    return list(variations)

def download_epg_ids_from_txt(session: requests.Session):
    """Download and parse EPG IDs with improved matching."""
    logging.info("Downloading EPG IDs from text file...")
    
    try:
        r = session.get(EPG_IDS_URL, timeout=30)
        r.raise_for_status()
        
        logging.info("✓ EPG IDs downloaded (%d bytes)", len(r.content))
        lines = r.text.splitlines()
        epg_lookup = build_epg_lookup(lines)
        
        logging.info("✓ EPG lookup built with %d unique keys", len(epg_lookup))
        return epg_lookup
        
    except Exception as e:
        logging.warning("Failed to download EPG IDs: %s", e)
        return {}

# [Rest of the functions remain the same: slugify, build_comprehensive_logo_index, 
#  find_best_logo, get_schedule, _extract_cid, _channel_entries, extract_channel_ids,
#  validate_single, build_stream_map]

def slugify(channel: str) -> str:
    txt = unicodedata.normalize("NFKD", channel).encode("ascii", "ignore").decode().lower()
    txt = txt.replace("&", "-and-").replace("+", "-plus-")
    txt = re.sub(r"[^\w\s-]", "", txt)
    txt = re.sub(r"\s+", "-", txt).strip("-")
    return txt

def build_comprehensive_logo_index(session: requests.Session) -> dict:
    logging.info("Building comprehensive logo index from tv-logo repo...")
    logo_index = {}
    
    try:
        r = session.get(TVLOGO_API_ROOT, timeout=30)
        r.raise_for_status()
        countries = [item["name"] for item in r.json() if item["type"] == "dir"]
        
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
                        
            except Exception as e:
                logging.debug("Failed to fetch logos for %s: %s", country, e)
                continue
        
        logging.info("✓ Logo index built with %d entries", len(logo_index))
        return logo_index
        
    except Exception as e:
        logging.warning("Failed to build logo index: %s", e)
        return {}

def find_best_logo(channel_name: str, logo_index: dict) -> str:
    if not logo_index:
        return "https://raw.githubusercontent.com/tv-logo/tv-logos/main/misc/no-logo.png"
        
    slug = slugify(channel_name)
    variations = [slug, slug + ".png", slug.replace("-hd", ""), slug.replace("-sd", "")]
    
    for variant in variations:
        if variant in logo_index:
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

def make_playlist(schedule, stream_map, logo_index, epg_lookup):
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
                
                # Use improved EPG matching
                epg_id = find_best_epg_match(cname, epg_lookup)
                if epg_id:
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

                encoded = base64.b64encode(stream.encode()).decode()
                proxy = f"{PROXY_PREFIX}{encoded}.m3u8"

                lines.append(extinf)
                lines.extend(VLC_HEADERS)
                lines.append(proxy)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    
    logging.info("Playlist written to %s (%d events, %d EPG matches %.1f%%)", 
                 OUTPUT_FILE, total_entries, epg_matches, 
                 (epg_matches/total_entries*100) if total_entries > 0 else 0)

def main():
    ap = argparse.ArgumentParser(description="Build live-events playlist with smart EPG matching")
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
        epg_lookup = download_epg_ids_from_txt(session)
        make_playlist(schedule, stream_map, logo_index, epg_lookup)

if __name__ == "__main__":
    main()
