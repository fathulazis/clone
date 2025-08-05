# main.py – validate links, rebuild direct URLs, rewrite tivimate_playlist.m3u8

import argparse
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import requests

PREMIUM_RE = re.compile(r'premium(\d+)/mono\.m3u8')

URL_TEMPLATES = [
    "https://nfsnew.newkso.ru/nfs/premium{num}/mono.m3u8",
    "https://windnew.newkso.ru/wind/premium{num}/mono.m3u8",
    "https://zekonew.newkso.ru/zeko/premium{num}/mono.m3u8",
    "https://dokko1new.newkso.ru/dokko1/premium{num}/mono.m3u8",
    "https://ddy6new.newkso.ru/ddy6/premium{num}/mono.m3u8"
]

INPUT_PLAYLIST = "tivimate_playlist.m3u8"
VALID_LINKS_OUT = "links.m3u8"

# -----------------------------------------------------------------------------

# 1. Validate every possible premium URL extracted from tivimate_playlist.m3u8

# -----------------------------------------------------------------------------

def validate_links(src=INPUT_PLAYLIST, out=VALID_LINKS_OUT, workers=10):
    log = logging.getLogger("validate_links")
    log.info("Stage 1 ▸ scanning %s", src)

    current_urls = []
    with open(src, encoding="utf-8") as fin:
        lines = fin.read().splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF") and i + 1 < len(lines):
            stream = lines[i + 1].strip()
            if PREMIUM_RE.search(stream):
                current_urls.append(stream)
                log.debug("found ⇒ %s", stream)
            i += 2
        else:
            i += 1

    ids = {m.group(1) for u in current_urls if (m := PREMIUM_RE.search(u))}
    if not ids:
        log.error("No premium{num} identifiers found – aborting.")
        raise SystemExit(1)

    log.info("Found %d unique premium IDs: %s", len(ids), sorted(ids))
    candidates = [tpl.format(num=i) for i in ids for tpl in URL_TEMPLATES]
    log.info("Generated %d candidate URLs to test", len(candidates))

    def check(url):
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Origin': 'https://jxoplay.xyz',
            'Referer': 'https://jxoplay.xyz
/'
        }
        for attempt in range(1, 4):
            try:
                log.debug("HEAD %s (try %d)", url, attempt)
                r = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
                if r.status_code == 200:
                    return url
                if r.status_code == 429:
                    log.debug("429 – sleeping 5 s before retry")
                    time.sleep(5)
                    continue
                if r.status_code == 404:
                    return None
                # fallback to GET for odd responses
                log.debug("GET %s (try %d)", url, attempt)
                r = requests.get(url, headers=headers, timeout=10, stream=True, allow_redirects=True)
                if r.status_code == 200:
                    return url
                if r.status_code == 404:
                    return None
            except requests.RequestException as e:
                log.debug("Request error %s: %s", url, e)
                return None
        return None

    valid = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check, u): u for u in candidates}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                valid.append(res)
                log.info("✓ %s", res)

    with open(out, "w", encoding="utf-8") as fout:
        fout.write("\n".join(valid))

    log.info("Stage 1 complete – %d valid URLs written to %s", len(valid), out)
    return valid

# -----------------------------------------------------------------------------

# 2. Build {ID → list of valid direct links} mapping from the validated URLs

# -----------------------------------------------------------------------------

def build_map(valid_links):
    log = logging.getLogger("build_map")
    id_to_valids = defaultdict(list)
    for link in valid_links:
        m = PREMIUM_RE.search(link)
        if m:
            id_ = m.group(1)
            id_to_valids[id_].append(link)
            log.debug("%s → ID %s", link, id_)
    log.info("Stage 2 complete – %d IDs with valid links", len(id_to_valids))
    return id_to_valids

# -----------------------------------------------------------------------------

# 3. Rewrite only stream lines inside tivimate_playlist.m3u8 if a new link is found

# -----------------------------------------------------------------------------

def rewrite_streams(src=INPUT_PLAYLIST, id_to_valids=None):
    log = logging.getLogger("rewrite_streams")
    lines = open(src, encoding="utf-8").read().splitlines()
    out_lines, replaced = [], 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF") and i + 1 < len(lines):
            out_lines.append(line)  # keep EXTINF
            stream = lines[i + 1].strip()
            new_stream = stream  # default: keep
            m = PREMIUM_RE.search(stream)
            if m:
                id_ = m.group(1)
                if id_ in id_to_valids:
                    valids = id_to_valids[id_]
                    if stream not in valids and valids:  # current invalid, but new valid exists
                        new_stream = valids[0]  # pick the first valid one
                        log.debug("Replaced %s → %s", stream, new_stream)
                        replaced += 1
                    else:
                        log.debug("Kept valid %s", stream)
                else:
                    log.debug("No valid links for ID %s, kept %s", id_, stream)
            out_lines.append(new_stream)
            i += 2
        else:
            out_lines.append(line)
            i += 1

    with open(src, "w", encoding="utf-8") as fout:
        fout.write("\n".join(out_lines) + "\n")

    log.info("Stage 3 complete – %d stream URLs replaced", replaced)

# -----------------------------------------------------------------------------

# entry-point

# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Refresh tivimate_playlist.m3u8 with working direct links")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show DEBUG-level detail (per-URL checks, replacements)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s │ %(name)s │ %(message)s")

    logging.info("▶️ Starting playlist refresh (verbose=%s)", args.verbose)
    valid = validate_links()
    id_to_valids = build_map(valid)
    rewrite_streams(id_to_valids=id_to_valids)
    logging.info("✅ Done – playlist refreshed")

if __name__ == "__main__":
    main()
