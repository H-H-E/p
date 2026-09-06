#!/usr/bin/env python3
import csv
import json
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://can.newonnetflix.info"
CATALOGUE = f"{BASE}/catalogue"
WORKERS = int(os.environ.get("SCRAPE_WORKERS", "12"))
TIMEOUT = 30
MAX_RETRIES = 7
PAGE_SIZE = 120
OUT = Path("results")
OUT.mkdir(exist_ok=True)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

_tls = threading.local()


def session():
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
            "Connection": "keep-alive",
        })
        _tls.session = s
    return s


def fetch(url, attempt=0):
    try:
        r = session().get(url, timeout=TIMEOUT)
    except requests.RequestException:
        if attempt >= MAX_RETRIES:
            raise
        time.sleep(min(20, 0.6 * (2 ** attempt)) + random.random() * 0.5)
        return fetch(url, attempt + 1)

    if r.status_code == 200:
        return r.text

    if r.status_code in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
        retry_after = r.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else min(30, 0.8 * (2 ** attempt))
        except ValueError:
            wait = min(30, 0.8 * (2 ** attempt))
        wait += random.random() * 0.8
        print(f"retry {attempt+1}/{MAX_RETRIES}: HTTP {r.status_code} {url} after {wait:.1f}s", flush=True)
        time.sleep(wait)
        return fetch(url, attempt + 1)

    raise RuntimeError(f"HTTP {r.status_code} for {url}")


def parse_title(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"^(.*) \((\d{4})\)$", text)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return text, None


def extract_titles(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.select('a[href*="/info/"]'):
        href = urljoin(BASE, a.get("href", ""))
        m = re.search(r"/info/(\d+)(?:[/?#]|$)", href)
        if not m:
            continue
        text = a.get_text(" ", strip=True)
        if not text:
            img = a.find("img")
            text = (img.get("alt") or "").strip() if img else ""
        if not text:
            continue
        # Skip generic image/link labels that occasionally appear in alt text.
        if text.lower() in {"image", "login or register to subscribe!"}:
            continue
        title, year = parse_title(text)
        if not title:
            continue
        nid = m.group(1)
        # Prefer the text heading version because it reliably includes year.
        old = out.get(nid)
        if old is None or (old.get("year") is None and year is not None):
            out[nid] = {"netflix_id": nid, "title": title, "year": year, "url": f"{BASE}/info/{nid}"}
    return list(out.values())


def section_urls(root_html):
    soup = BeautifulSoup(root_html, "html.parser")
    urls = {CATALOGUE}
    for a in soup.select('a[href*="/catalogue/a2z/all/"]'):
        href = urljoin(BASE, a.get("href", ""))
        p = urlparse(href)
        urls.add(f"{p.scheme}://{p.netloc}{p.path}")
    return sorted(urls)


def title_count(html):
    # Current site formats section count as: [367 titles] - Showing 1 to 120
    m = re.search(r"\[(\d+)\s+titles(?:\s+from\s+this\s+year)?\]", html, re.I)
    if m:
        return int(m.group(1))
    return None


def enumerate_catalogue():
    root = fetch(CATALOGUE)
    sections = section_urls(root)
    print(f"catalogue sections: {len(sections)}", flush=True)
    titles = {}

    for idx, base_url in enumerate(sections, 1):
        first_html = root if base_url == CATALOGUE else fetch(base_url)
        count = title_count(first_html)
        pages = 1 if not count else max(1, math.ceil(count / PAGE_SIZE))

        for page in range(pages):
            if page == 0:
                html = first_html
            else:
                sep = "&" if "?" in base_url else "?"
                html = fetch(f"{base_url}{sep}start={page * PAGE_SIZE}")
            for row in extract_titles(html):
                nid = row["netflix_id"]
                old = titles.get(nid)
                if old is None or (old.get("year") is None and row.get("year") is not None):
                    titles[nid] = row

        print(f"section {idx}/{len(sections)} -> {len(titles)} unique titles", flush=True)

    rows = sorted(titles.values(), key=lambda x: (x["title"].casefold(), x.get("year") or 0, x["netflix_id"]))
    return rows


def parse_rt(html):
    m = re.search(r"Rotten Tomatoes rating\s*(\d{1,3})%", html, re.I)
    if not m:
        return None
    score = int(m.group(1))
    return score if 0 <= score <= 100 else None


def scrape_one(row):
    html = fetch(row["url"])
    score = parse_rt(html)
    return row["netflix_id"], score


def write_outputs(catalogue, score_map, failures):
    generated = datetime.now(timezone.utc).isoformat()
    all_rows = []
    for row in catalogue:
        r = dict(row)
        r["rotten_tomatoes"] = score_map.get(row["netflix_id"])
        all_rows.append(r)

    hits = [r for r in all_rows if isinstance(r["rotten_tomatoes"], int) and r["rotten_tomatoes"] > 80]
    hits.sort(key=lambda r: (-r["rotten_tomatoes"], r["title"].casefold(), -(r.get("year") or 0), r["netflix_id"]))

    for i, r in enumerate(hits, 1):
        r["rank"] = i

    with (OUT / "netflix_canada_rt_over_80.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "title", "year", "rotten_tomatoes", "netflix_id", "url"])
        w.writeheader()
        w.writerows(hits)

    payload = {
        "generated_at": generated,
        "source": CATALOGUE,
        "filter": "Rotten Tomatoes > 80%",
        "catalogue_count": len(catalogue),
        "detail_pages_with_rt_score": sum(isinstance(v, int) for v in score_map.values()),
        "matched_count": len(hits),
        "failed_detail_requests": failures,
        "results": hits,
    }
    (OUT / "netflix_canada_rt_over_80.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with (OUT / "all_catalogue_rt_scores.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["title", "year", "rotten_tomatoes", "netflix_id", "url"])
        w.writeheader()
        w.writerows(all_rows)

    (OUT / "summary.json").write_text(json.dumps({
        "generated_at": generated,
        "catalogue_count": len(catalogue),
        "rt_scored_count": sum(isinstance(v, int) for v in score_map.values()),
        "over_80_count": len(hits),
        "failed_detail_requests": failures,
        "workers": WORKERS,
    }, indent=2), encoding="utf-8")

    return hits


def main():
    started = time.time()
    catalogue = enumerate_catalogue()
    print(f"enumerated {len(catalogue)} current titles", flush=True)

    if len(catalogue) < 8000:
        raise RuntimeError(f"Catalogue enumeration suspiciously small: {len(catalogue)}")

    score_map = {}
    failures = []
    done = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(scrape_one, row): row for row in catalogue}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                nid, score = fut.result()
                score_map[nid] = score
            except Exception as e:
                failures.append({"netflix_id": row["netflix_id"], "title": row["title"], "error": repr(e)})
            done += 1
            if done % 250 == 0 or done == len(catalogue):
                hits = sum(1 for v in score_map.values() if isinstance(v, int) and v > 80)
                rt_scored = sum(1 for v in score_map.values() if isinstance(v, int))
                print(f"details {done}/{len(catalogue)} | RT-scored {rt_scored} | >80 {hits} | failures {len(failures)}", flush=True)

    # Retry any failures serially once more after the concurrent pass.
    if failures:
        print(f"retrying {len(failures)} failed detail pages serially", flush=True)
        retry_failures = []
        for item in failures:
            row = next(r for r in catalogue if r["netflix_id"] == item["netflix_id"])
            try:
                _, score = scrape_one(row)
                score_map[row["netflix_id"]] = score
            except Exception as e:
                retry_failures.append({**item, "retry_error": repr(e)})
        failures = retry_failures

    hits = write_outputs(catalogue, score_map, failures)
    elapsed = time.time() - started
    print(f"DONE catalogue={len(catalogue)} rt_scored={sum(isinstance(v,int) for v in score_map.values())} over80={len(hits)} failures={len(failures)} elapsed={elapsed:.1f}s", flush=True)

    # A few sanity checks against known current pages.
    known = {r["title"]: r["rotten_tomatoes"] for r in hits}
    if known.get("Breaking Bad") != 96:
        print(f"warning: expected Breaking Bad RT 96, saw {known.get('Breaking Bad')}", flush=True)


if __name__ == "__main__":
    main()
