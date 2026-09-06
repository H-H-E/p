#!/usr/bin/env python3
import base64
import csv
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://can.newonnetflix.info"
OUT = Path("results")
OUT.mkdir(exist_ok=True)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
SCORES = range(81, 101)
VARIANTS = ["", "movie", "series", "documentary"]
MAX_PAGES = 8
COUNT = 50

s = requests.Session()
s.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
})


def fetch(url, retries=5):
    for attempt in range(retries + 1):
        try:
            r = s.get(url, timeout=35)
            if r.status_code == 200:
                return r.text
            if r.status_code not in {403, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"HTTP {r.status_code}: {url}")
        except requests.RequestException as e:
            if attempt == retries:
                raise
        if attempt == retries:
            raise RuntimeError(f"HTTP {getattr(r, 'status_code', '?')}: {url}")
        wait = min(15, 0.7 * (2 ** attempt)) + random.random()
        print(f"retry {attempt+1}/{retries} in {wait:.1f}s: {url}", flush=True)
        time.sleep(wait)


def decode_bing_href(href):
    if not href:
        return None
    if href.startswith("https://can.newonnetflix.info/info/"):
        return href
    try:
        p = urlparse(href)
        q = parse_qs(p.query)
        u = q.get("u", [None])[0]
        if u and u.startswith("a1"):
            raw = u[2:]
            raw += "=" * (-len(raw) % 4)
            decoded = base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")
            if decoded.startswith("https://can.newonnetflix.info/info/"):
                return decoded
    except Exception:
        pass
    return None


def clean_title(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    patterns = [
        r"^Is ['\"](.+?)['\"] on Netflix in Canada\?",
        r"^Everything you need to know about ['\"](.+?)['\"] on Netflix in Canada!?",
        r"^Is ['\"](.+?)['\"] on Netflix",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip()
    text = re.sub(r"\s*[-–|]\s*New On Netflix Canada.*$", "", text, flags=re.I)
    return text.strip()


def search_page(score, variant, first):
    phrase = f'site:can.newonnetflix.info/info/ "Rotten Tomatoes rating {score}%" "available on Netflix in Canada"'
    if variant:
        phrase += f' "{variant}"'
    url = (
        "https://www.bing.com/search?q=" + quote_plus(phrase) +
        f"&count={COUNT}&first={first}&setlang=en-CA&cc=ca&FORM=PERE"
    )
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        href = decode_bing_href(a.get("href", ""))
        if not href:
            continue
        m = re.search(r"/info/(\d+)", href)
        if not m:
            continue
        title = clean_title(a.get_text(" ", strip=True))
        snippet = li.get_text(" ", strip=True)
        out.append({
            "netflix_id": m.group(1),
            "title": title,
            "rotten_tomatoes": score,
            "url": f"{BASE}/info/{m.group(1)}",
            "bing_snippet": snippet[:1000],
            "query_variant": variant or "base",
        })
    return out, html


def main():
    started = time.time()
    found = {}
    conflicts = []
    query_log = []

    for score in SCORES:
        before_score = len(found)
        for variant in VARIANTS:
            empty_streak = 0
            variant_new = 0
            for page in range(MAX_PAGES):
                first = 1 + page * COUNT
                rows, html = search_page(score, variant, first)
                page_new = 0
                for row in rows:
                    nid = row["netflix_id"]
                    old = found.get(nid)
                    if old and old["rotten_tomatoes"] != score:
                        conflicts.append({"id": nid, "old": old["rotten_tomatoes"], "new": score, "title": row["title"]})
                        continue
                    if not old:
                        found[nid] = row
                        page_new += 1
                        variant_new += 1
                query_log.append({"score": score, "variant": variant or "base", "page": page+1, "results": len(rows), "new": page_new})
                print(f"RT {score}% {variant or 'base'} page {page+1}: {len(rows)} results, {page_new} new, total {len(found)}", flush=True)

                if not rows or page_new == 0:
                    empty_streak += 1
                else:
                    empty_streak = 0
                # Search engines generally stop yielding useful new pages quickly.
                if empty_streak >= 2:
                    break
                time.sleep(0.15 + random.random() * 0.15)

        print(f"RT {score}% complete: +{len(found)-before_score} unique pages", flush=True)

    rows = sorted(found.values(), key=lambda r: (-r["rotten_tomatoes"], r["title"].casefold(), r["netflix_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    generated = datetime.now(timezone.utc).isoformat()
    with (OUT / "netflix_canada_rt_over_80_search_index.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "title", "rotten_tomatoes", "netflix_id", "url", "query_variant"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})

    (OUT / "netflix_canada_rt_over_80_search_index.json").write_text(json.dumps({
        "generated_at": generated,
        "method": "Bing search-index reconstruction; origin is Cloudflare-blocked from datacenter runners",
        "query_rule": "NewOnNetflix /info pages indexed with exact Rotten Tomatoes score 81-100 and availability phrase",
        "guaranteed_complete": False,
        "matched_indexed_pages": len(rows),
        "conflicts": conflicts,
        "results": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    (OUT / "search_query_log.json").write_text(json.dumps(query_log, indent=2), encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps({
        "generated_at": generated,
        "matched_indexed_pages": len(rows),
        "conflicts": len(conflicts),
        "elapsed_seconds": round(time.time() - started, 1),
    }, indent=2), encoding="utf-8")
    print(f"DONE indexed_matches={len(rows)} conflicts={len(conflicts)} elapsed={time.time()-started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
