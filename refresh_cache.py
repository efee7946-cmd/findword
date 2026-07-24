"""Re-crawls a site and updates its cache/<domain>.json file.

Used by the GitHub Actions "Refresh site cache" workflow; can also be
run locally:

    python refresh_cache.py https://www.aibusinessschool.com
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from word_counter import (canonicalize, extract_visible_text, is_crawlable,
                          make_fetcher, normalize_url, same_domain)

CACHE_DIR = Path(__file__).resolve().parent / "cache"
MAX_PAGES = 200
MAX_DEPTH = 6

import re


def site_key(url):
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def cache_filename(key):
    return re.sub(r"[^a-z0-9.-]", "_", key) + ".json"


def crawl(start_url):
    root_netloc = urlparse(start_url).netloc
    fetcher = make_fetcher("auto", start_url)
    pages = []
    try:
        queue = [(canonicalize(normalize_url(start_url), root_netloc), 0)]
        visited = set()
        while queue and len(visited) < MAX_PAGES:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                html = fetcher.fetch(url)
            except Exception as exc:
                print(f"  skip {url}: {exc}")
                continue
            soup = BeautifulSoup(html, "lxml")
            title = soup.title.get_text(strip=True) if soup.title else ""
            pages.append({"url": url, "title": title,
                          "text": extract_visible_text(soup)})
            print(f"  [{len(pages):>3}] {url}")
            if depth < MAX_DEPTH:
                for a in soup.find_all("a", href=True):
                    link = canonicalize(normalize_url(urljoin(url, a["href"])), root_netloc)
                    if (link.startswith("http") and same_domain(link, root_netloc)
                            and is_crawlable(link) and link not in visited):
                        queue.append((link, depth + 1))
    finally:
        fetcher.close()
    return pages


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python refresh_cache.py <site-url>")
    raw = sys.argv[1].strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        sys.exit("Invalid site URL")
    start_url = f"{parsed.scheme}://{parsed.netloc}/"

    print(f"Re-indexing {start_url}")
    pages = crawl(start_url)
    if not pages:
        sys.exit("No pages could be fetched — cache left unchanged.")

    crawled_at = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    CACHE_DIR.mkdir(exist_ok=True)
    out = CACHE_DIR / cache_filename(site_key(start_url))
    out.write_text(
        json.dumps({"start_url": start_url, "crawled_at": crawled_at,
                    "pages": pages}, ensure_ascii=False),
        encoding="utf-8")
    print(f"Wrote {out} ({len(pages)} pages, {crawled_at})")


if __name__ == "__main__":
    main()
