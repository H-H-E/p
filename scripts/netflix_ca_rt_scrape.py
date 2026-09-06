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
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://can.newonnetflix.info"
CATALOGUE = f"{BASE}/catalogue"
WORKERS = int(os.environ.get("SCRAPE_WORKERS", "12"))
TIMEOUT = 40
MAX_RETRIES = 7
PAGE_SIZE = 120
OUT = Path("results")
OUT.mkdir(exist_ok=True)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
_tls = threading.local()


def translate_proxy(url):
    """Fetch through Google Translate so Google, rather than the runner IP, hits origin."""
    p = urlparse(url)
    # Google Translate host encoding: dots -> hyphens, literal hyphens doubled.
    host = p.netloc.replace("-", "--").replace(".", "-") + ".translate.goog"
    q = list(parse_qsl(p.query, keep_blank_values=True))
    q.extend([("_x_tr_sl", "auto"), ("_x_tr_tl", "en"), ("_x_tr_hl", "en")])
    return urlunparse(("https", host, p.path, p.params, urlencode(q), ""))


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
    target = translate_proxy(url)
    try:
        r = session().get(target, timeout=TIMEOUT)
    except requests.RequestException as exc:
        if attempt >= MAX_RETRIES:
            raise
        wait = min(20, 0.6 * (2 ** attempt)) + random.random() * 0.5
        print(f"network retry {attempt+1}/{MAX_RETRIES} {url} in {wait:.1f}s: {exc!r}", flush=True)
        time.sleep(wait)
        return fetch(url, attempt + 1)

    text = r.text
    if r.status_code == 200 and "Performing security verification" not in text and "Just a moment" not in text:
        return text

    if r.status_code in {403, 408, 429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
        wait = min(30, 0.8 * (2 ** attempt)) + random.random() * 0.8
        print(f"HTTP retry {attempt+1}/{MAX_RETRIES}: {r.status_code} {url} in {wait:.1f}s", flush=True)
        time.sleep(wait)
        return fetch(url, attempt + 1)

    raise RuntimeError(
        f"HTTP {r.status_code} for proxy {target}; content-type={r.headers.get('content-type')}; body={text[:300]!r}"
    )


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
        href = a.get("href", "")
        m = re.search(r"/info/(\d+)(?:[/?#]|$)", href)
        if not m:
            continue
        text = a.get_text(" ", strip=True)
        if not text:
            img = a.find("img")
            text = (img.get("alt") or "").strip() if img else ""
        if not text or text.lower() in {"image", "login or register to subscribe!"}:
            continue
        title, year = parse_title(text)
        if not title:
            continue
        nid = m.group(1)
        old = out.get(nid)
        row = {"netflix_id": nid, "title": title, "year": year, "url": f"{BASE}/info/{nid}"}
        if old is None or (old.get("year") is None and year is not None):
            out[nid] = row
    return list(out.values())


def section_urls(root_html):
    soup = BeautifulSoup(root_html, "html.parser")
    paths = {"/catalogue"}
    for a in soup.select('a[href*="/catalogue/a2z/all/"]'):
        href = a.get("href", "")
        m = re.search(r"(/catalogue/a2z/all/[^/?#]+)", href)
        if m:
            paths.add(m.group(1))
    return sorted(urljoin(BASE, path) for path in paths)


def title_count(html):
    # Google Translate may introduce harmless whitespace/span tags, so parse text.
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = re.search(r"\[(\d+)\s+titles(?:\s+from\s+this\s+year)?\]", text, re.I)
    if not m:
        m = re.search(r"(\d+)\s+titles\s*-\s*Showing", text, re.I)
    return int(m.group(1)) if m else None


def enumerate_catalogue():
    print(f"probe: {translate_proxy(CATALOGUE)}", flush=True)
    root = fetch(CATALOGUE)
    print(f"proxy root fetched: {len(root):,} bytes", flush=True)
    sections = section_urls(root)
    print(f"catalogue sections: {len(sections)}", flush=True)
    titles = {}

    for idx, base_url in enumerate(sections, 1):
        first_html = root if base_url == CATALOGUE else fetch(base_url)
        count = title_count(first_html)
        if count is None:
            # Fallback: follow fixed pagination until a short/empty page.
            page = 0
            while True:
                html = first_html if page == 0 else fetch(f"{base_url}?start={page * PAGE_SIZE}")
                rows = extract_titles(html)
                before = len(titles)
                for row in rows:
                    old = titles.get(row["netflix_id"])
                    if old is None or (old.get("year") is None and row.get("year") is not None):
                        titles[row["netflix_id"]] = row
                if page > 0 and (not rows or len(rows) < PAGE_SIZE // 2 or len(titles) == before):
                    break
                page += 1
                if page > 20:
                    raise RuntimeError(f"Pagination runaway for {base_url}")
        else:
            pages = max(1, math.ceil(count / PAGE_SIZE))
            for page in range(pages):
                html = first_html if page == 0 else fetch(f"{base_url}?start={page * PAGE_SIZE}")
                for row in extract_titles(html):
                    old = titles.get(row["netflix_id"])
                    if old is None or (old.get("year") is None and row.get("year") is not None):
                        titles[row["netflix_id"]] = row
        print(f"section {idx}/{len(sections)} count={count} -> {len(titles)} unique", flush=True)

    return sorted(titles.values(), key=lambda x: (x["title"].casefold(), x.get("year") or 0, x["netflix_id"]))


def parse_rt(html):
    # Alt attributes normally survive Google Translate unchanged. Add text fallback.
    m = re.search(r"Rotten Tomatoes rating\s*(\d{1,3})%", html, re.I)
    if not m:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        m = re.search(r"Rotten Tomatoes(?:\s+rating)?\s*(\d{1,3})%", text, re.I)
    if not m:
        return None
    v = int(m.group(1))
    return v if 0 <= v <= 100 else None


def scrape_one(row):
    html = fetch(row["url"])
    return row["netflix_id"], parse_rt(html)


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
        w.writeheader(); w.writerows(hits)

    (OUT / "netflix_canada_rt_over_80.json").write_text(json.dumps({
        "generated_at": generated,
        "source": CATALOGUE,
        "fetch_transport": "Google Translate proxy",
        "filter": "Rotten Tomatoes > 80%",
        "catalogue_count": len(catalogue),
        "rt_scored_count": sum(isinstance(v, int) for v in score_map.values()),
        "matched_count": len(hits),
        "failed_detail_requests": failures,
        "results": hits,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    with (OUT / "all_catalogue_rt_scores.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["title", "year", "rotten_tomatoes", "netflix_id", "url"])
        w.writeheader(); w.writerows(all_rows)

    (OUT / "summary.json").write_text(json.dumps({
        "generated_at": generated,
        "catalogue_count": len(catalogue),
        "rt_scored_count": sum(isinstance(v, int) for v in score_map.values()),
        "over_80_count": len(hits),
        "failed_detail_requests": len(failures),
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
        futures = {pool.submit(scrape_one, r): r for r in catalogue}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                nid, score = fut.result()
                score_map[nid] = score
            except Exception as exc:
                failures.append({"netflix_id": row["netflix_id"], "title": row["title"], "error": repr(exc)})
            done += 1
            if done % 250 == 0 or done == len(catalogue):
                rt = sum(isinstance(v, int) for v in score_map.values())
                high = sum(isinstance(v, int) and v > 80 for v in score_map.values())
                print(f"details {done}/{len(catalogue)} | RT-scored {rt} | >80 {high} | failures {len(failures)}", flush=True)

    if failures:
        print(f"serial retry for {len(failures)} failures", flush=True)
        remaining = []
        rows_by_id = {r["netflix_id"]: r for r in catalogue}
        for item in failures:
            row = rows_by_id[item["netflix_id"]]
            try:
                nid, score = scrape_one(row)
                score_map[nid] = score
            except Exception as exc:
                item["retry_error"] = repr(exc)
                remaining.append(item)
        failures = remaining

    hits = write_outputs(catalogue, score_map, failures)
    print(
        f"DONE catalogue={len(catalogue)} rt_scored={sum(isinstance(v,int) for v in score_map.values())} "
        f"over80={len(hits)} failures={len(failures)} elapsed={time.time()-started:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
