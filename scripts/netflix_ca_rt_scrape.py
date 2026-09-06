#!/usr/bin/env python3
import asyncio
import csv
import json
import math
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE = "https://can.newonnetflix.info"
CATALOGUE = f"{BASE}/catalogue"
WORKERS = int(os.environ.get("SCRAPE_WORKERS", "12"))
PAGE_SIZE = 120
MAX_RETRIES = 7
OUT = Path("results")
OUT.mkdir(exist_ok=True)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
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
        href = urljoin(BASE, a.get("href", ""))
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
        if old is None or (old.get("year") is None and year is not None):
            out[nid] = {
                "netflix_id": nid,
                "title": title,
                "year": year,
                "url": f"{BASE}/info/{nid}",
            }
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
    m = re.search(r"\[(\d+)\s+titles(?:\s+from\s+this\s+year)?\]", html, re.I)
    return int(m.group(1)) if m else None


def parse_rt(html):
    m = re.search(r"Rotten Tomatoes rating\s*(\d{1,3})%", html, re.I)
    if not m:
        return None
    score = int(m.group(1))
    return score if 0 <= score <= 100 else None


class BrowserFetcher:
    def __init__(self, context, request):
        self.context = context
        self.request = request
        self.sem = asyncio.Semaphore(WORKERS)

    async def get(self, url, attempt=0):
        async with self.sem:
            try:
                r = await self.request.get(
                    url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-CA,en;q=0.9",
                        "Referer": CATALOGUE,
                    },
                    timeout=45000,
                )
                status = r.status
                text = await r.text()
            except Exception as exc:
                if attempt >= MAX_RETRIES:
                    raise
                delay = min(20, 0.7 * (2 ** attempt)) + random.random() * 0.5
                print(f"network retry {attempt+1}/{MAX_RETRIES}: {url} in {delay:.1f}s ({exc!r})", flush=True)
                await asyncio.sleep(delay)
                return await self.get(url, attempt + 1)

        if status == 200 and "Please wait while your request is being verified" not in text:
            return text

        if status in {403, 429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
            delay = min(25, 0.8 * (2 ** attempt)) + random.random() * 0.8
            print(f"HTTP retry {attempt+1}/{MAX_RETRIES}: {status} {url} in {delay:.1f}s", flush=True)
            await asyncio.sleep(delay)
            return await self.get(url, attempt + 1)

        raise RuntimeError(f"HTTP {status} for {url}; body={text[:160]!r}")


async def bootstrap_browser(pw):
    browser = await pw.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = await browser.new_context(
        user_agent=UA,
        locale="en-CA",
        timezone_id="America/Vancouver",
        viewport={"width": 1365, "height": 900},
        java_script_enabled=True,
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = await context.new_page()
    print("opening catalogue in Chromium to establish browser session...", flush=True)
    response = await page.goto(CATALOGUE, wait_until="domcontentloaded", timeout=90000)
    print(f"initial browser status={response.status if response else None} title={await page.title()!r}", flush=True)

    solved = False
    for i in range(45):
        body = await page.content()
        if "Full Catalogue" in body and "Please wait while your request is being verified" not in body:
            solved = True
            break
        await page.wait_for_timeout(1000)

    if not solved:
        body_text = (await page.locator("body").inner_text())[:800]
        raise RuntimeError(f"Browser challenge did not clear. body={body_text!r}")

    cookies = await context.cookies()
    print("browser session established; cookies=" + ",".join(sorted(c["name"] for c in cookies)), flush=True)
    root_html = await page.content()
    return browser, context, root_html


async def enumerate_catalogue(fetcher, root_html):
    sections = section_urls(root_html)
    print(f"catalogue sections: {len(sections)}", flush=True)
    titles = {}

    for idx, base_url in enumerate(sections, 1):
        first_html = root_html if base_url == CATALOGUE else await fetcher.get(base_url)
        count = title_count(first_html)
        pages = 1 if not count else max(1, math.ceil(count / PAGE_SIZE))

        html_pages = [first_html]
        if pages > 1:
            urls = [f"{base_url}?start={p * PAGE_SIZE}" for p in range(1, pages)]
            html_pages += await asyncio.gather(*(fetcher.get(u) for u in urls))

        for html in html_pages:
            for row in extract_titles(html):
                nid = row["netflix_id"]
                old = titles.get(nid)
                if old is None or (old.get("year") is None and row.get("year") is not None):
                    titles[nid] = row
        print(f"section {idx}/{len(sections)} -> {len(titles)} unique titles", flush=True)

    return sorted(titles.values(), key=lambda x: (x["title"].casefold(), x.get("year") or 0, x["netflix_id"]))


async def scrape_scores(fetcher, catalogue):
    score_map = {}
    failures = []
    completed = 0

    async def one(row):
        html = await fetcher.get(row["url"])
        return row, parse_rt(html)

    tasks = [asyncio.create_task(one(r)) for r in catalogue]
    for task in asyncio.as_completed(tasks):
        completed += 1
        try:
            row, score = await task
            score_map[row["netflix_id"]] = score
        except Exception as exc:
            failures.append(repr(exc))
        if completed % 250 == 0 or completed == len(catalogue):
            rt_scored = sum(isinstance(v, int) for v in score_map.values())
            hits = sum(isinstance(v, int) and v > 80 for v in score_map.values())
            print(f"details {completed}/{len(catalogue)} | RT-scored {rt_scored} | >80 {hits} | failures {len(failures)}", flush=True)

    return score_map, failures


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
        "filter": "Rotten Tomatoes > 80%",
        "catalogue_count": len(catalogue),
        "detail_pages_with_rt_score": sum(isinstance(v, int) for v in score_map.values()),
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


async def main():
    started = time.time()
    async with async_playwright() as pw:
        browser, context, root_html = await bootstrap_browser(pw)
        try:
            fetcher = BrowserFetcher(context, context.request)
            # Confirm the shared browser request context can fetch after challenge clearance.
            probe = await fetcher.get(CATALOGUE)
            if "Full Catalogue" not in probe:
                raise RuntimeError("Browser API request context did not retain catalogue access")
            print("shared browser request context verified", flush=True)

            catalogue = await enumerate_catalogue(fetcher, root_html)
            print(f"enumerated {len(catalogue)} current titles", flush=True)
            if len(catalogue) < 8000:
                raise RuntimeError(f"Catalogue enumeration suspiciously small: {len(catalogue)}")

            score_map, failures = await scrape_scores(fetcher, catalogue)
            hits = write_outputs(catalogue, score_map, failures)
            elapsed = time.time() - started
            print(
                f"DONE catalogue={len(catalogue)} rt_scored={sum(isinstance(v,int) for v in score_map.values())} "
                f"over80={len(hits)} failures={len(failures)} elapsed={elapsed:.1f}s",
                flush=True,
            )
            known = {r["title"]: r["rotten_tomatoes"] for r in hits}
            print(f"sanity Breaking Bad={known.get('Breaking Bad')}", flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
