"""Site ici kelime sayaci.

Verilen bir web sitesini ayni domain icinde tarar (crawl) ve istenen
kelime/kelimelerin kac kez gectigini sayfa bazinda ve toplamda raporlar.

Statik sitelerde hizli HTTP istekleri kullanir; icerigi JavaScript ile
olusturulan sitelerde (SPA) otomatik olarak gercek tarayici render'ina
(Playwright/Chromium) gecer.

Ornek kullanim:
    python word_counter.py https://www.aibusinessschool.com adoption
    python word_counter.py https://ornek.com "ai adoption" copilot --max-pages 100
    python word_counter.py https://ornek.com adoption --case-sensitive --output rapor.csv
    python word_counter.py https://ornek.com adoption --render always
"""

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 WordCounterBot/1.0")

# Sayfa metnine dahil edilmeyecek etiketler
SKIP_TAGS = {"script", "style", "noscript", "template"}

# Taranmayacak dosya uzantilari
SKIP_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".zip", ".rar", ".mp4", ".mp3", ".avi", ".mov",
    ".woff", ".woff2", ".ttf", ".eot", ".xml", ".json", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx",
}

# Sayfada bundan az gorunur karakter varsa "JS kabugu" kabul edilir
SPA_TEXT_THRESHOLD = 300


def normalize_url(url: str) -> str:
    """Fragment'i atar, sondaki / isaretini sadelestirir."""
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}" + (f"?{parsed.query}" if parsed.query else "")


def same_domain(url: str, root_netloc: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    root = root_netloc.lower()
    # www'li ve www'suz halleri ayni site say
    return netloc == root or netloc == f"www.{root}" or f"www.{netloc}" == root


def canonicalize(url: str, root_netloc: str) -> str:
    """www'li/www'suz ayni sayfanin iki kez sayilmamasi icin host'u sabitler."""
    parsed = urlparse(url)
    if parsed.netloc.lower() != root_netloc.lower() and same_domain(url, root_netloc):
        url = url.replace(f"//{parsed.netloc}", f"//{root_netloc}", 1)
    return url


def is_crawlable(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def extract_visible_text(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(SKIP_TAGS):
        tag.decompose()
    return soup.get_text(separator=" ")


def build_pattern(word: str, case_sensitive: bool, whole_word: bool) -> re.Pattern:
    escaped = re.escape(word)
    if whole_word:
        escaped = rf"\b{escaped}\b"
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(escaped, flags)


class RequestsFetcher:
    """Statik siteler icin hizli HTTP istemcisi."""

    name = "requests"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def fetch(self, url: str) -> str:
        resp = self.session.get(url, timeout=15)
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        if "text/html" not in content_type:
            raise RuntimeError(f"HTML degil: {content_type.split(';')[0]}")
        return resp.text

    def close(self):
        self.session.close()


class PlaywrightFetcher:
    """JavaScript ile olusan sayfalar icin gercek tarayici (Chromium)."""

    name = "playwright"

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=USER_AGENT)
        # Hiz icin gorsel/font/medya isteklerini engelle
        self._context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media")
            else route.continue_(),
        )
        self._page = self._context.new_page()

    def fetch(self, url: str) -> str:
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if resp and resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        try:
            self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # analytics vb. arka plan istekleri hic durmayabilir
        self._page.wait_for_timeout(300)
        return self._page.content()

    def close(self):
        self._browser.close()
        self._pw.stop()


def looks_like_js_shell(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    text = extract_visible_text(soup).strip()
    links = soup.find_all("a", href=True)
    return len(text) < SPA_TEXT_THRESHOLD and len(links) < 3


def make_fetcher(render_mode: str, start_url: str):
    """render_mode: auto | always | never"""
    if render_mode == "always":
        print("Tarayici render'i kullaniliyor (Playwright/Chromium).\n")
        return PlaywrightFetcher()
    if render_mode == "never":
        return RequestsFetcher()

    # auto: baslangic sayfasini yokla
    probe = RequestsFetcher()
    try:
        html = probe.fetch(start_url)
        if looks_like_js_shell(html):
            probe.close()
            print("Site JavaScript ile render ediliyor (SPA tespit edildi) — "
                  "tarayici moduna geciliyor.\n")
            return PlaywrightFetcher()
        return probe
    except Exception:
        probe.close()
        print("HTTP istegi basarisiz — tarayici moduna geciliyor.\n")
        return PlaywrightFetcher()


def crawl_and_count(start_url, words, fetcher, max_pages, max_depth, delay,
                    case_sensitive, whole_word, include_html, verbose=True):
    root_netloc = urlparse(start_url).netloc
    patterns = {w: build_pattern(w, case_sensitive, whole_word) for w in words}

    start = normalize_url(start_url)
    queue = [(start, 0)]
    visited = set()
    results = []          # sayfa bazli sonuclar
    variant_totals = {w: Counter() for w in words}  # buyuk/kucuk harf varyant dagilimi
    errors = []

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            html = fetcher.fetch(url)
        except Exception as exc:
            errors.append((url, str(exc)))
            continue

        soup = BeautifulSoup(html, "lxml")
        text = html if include_html else extract_visible_text(soup)

        page_counts = {}
        for w, pattern in patterns.items():
            matches = pattern.findall(text)
            page_counts[w] = len(matches)
            variant_totals[w].update(matches)

        title = soup.title.get_text(strip=True) if soup.title else ""
        results.append({"url": url, "title": title, "depth": depth, **page_counts})

        if verbose:
            counts_str = ", ".join(f"{w}: {c}" for w, c in page_counts.items())
            print(f"  [{len(visited):>3}] {url}  ->  {counts_str}")

        # Linkleri kuyruga ekle
        if depth < max_depth:
            for a in soup.find_all("a", href=True):
                link = canonicalize(normalize_url(urljoin(url, a["href"])), root_netloc)
                if (link.startswith("http")
                        and same_domain(link, root_netloc)
                        and is_crawlable(link)
                        and link not in visited):
                    queue.append((link, depth + 1))

        if delay:
            time.sleep(delay)

    return results, variant_totals, errors


def print_report(results, variant_totals, errors, words):
    print("\n" + "=" * 70)
    print("SONUC RAPORU")
    print("=" * 70)
    print(f"Taranan sayfa sayisi : {len(results)}")
    if errors:
        print(f"Atlanan/hatali URL   : {len(errors)}")

    for w in words:
        total = sum(r[w] for r in results)
        print(f"\n'{w}' toplam: {total}")
        variants = variant_totals[w]
        if len(variants) > 1 or (variants and next(iter(variants)) != w):
            for variant, count in variants.most_common():
                print(f"    {variant!r}: {count}")
        top_pages = sorted((r for r in results if r[w] > 0), key=lambda r: -r[w])[:10]
        if top_pages:
            print("  En cok gectigi sayfalar:")
            for r in top_pages:
                print(f"    {r[w]:>4}  {r['url']}")


def save_output(results, path):
    if path.lower().endswith(".json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    else:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
    print(f"\nSayfa bazli dokum kaydedildi: {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Bir web sitesinde kelime(ler)in kac kez gectigini sayar.")
    parser.add_argument("url", help="Baslangic URL'i (ör. https://www.site.com)")
    parser.add_argument("words", nargs="+", help="Aranacak kelime(ler) veya ifade(ler)")
    parser.add_argument("--max-pages", type=int, default=200,
                        help="Taranacak azami sayfa sayisi (varsayilan: 200)")
    parser.add_argument("--max-depth", type=int, default=5,
                        help="Link takip derinligi (varsayilan: 5)")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="Istekler arasi bekleme saniyesi (varsayilan: 0.3)")
    parser.add_argument("--render", choices=["auto", "always", "never"], default="auto",
                        help="Tarayici render'i: auto = SPA tespit edilirse kullan "
                             "(varsayilan), always = hep kullan, never = kullanma")
    parser.add_argument("--case-sensitive", action="store_true",
                        help="Buyuk/kucuk harf ayrimi yap (varsayilan: yapma)")
    parser.add_argument("--no-word-boundary", action="store_true",
                        help="Kelime siniri arama ('adoption' araması 'adoptions' icinde de sayilsin)")
    parser.add_argument("--include-html", action="store_true",
                        help="Gorunur metin yerine ham HTML kaynaginda say")
    parser.add_argument("--output", metavar="DOSYA",
                        help="Sayfa bazli dokumu .csv veya .json olarak kaydet")
    parser.add_argument("--quiet", action="store_true", help="Sayfa sayfa cikti gosterme")
    args = parser.parse_args()

    url = args.url if args.url.startswith("http") else f"https://{args.url}"

    print(f"Taraniyor: {url}")
    print(f"Aranan: {', '.join(args.words)} "
          f"(harf duyarli: {'evet' if args.case_sensitive else 'hayir'}, "
          f"tam kelime: {'hayir' if args.no_word_boundary else 'evet'}, "
          f"kaynak: {'ham HTML' if args.include_html else 'gorunur metin'})\n")

    fetcher = make_fetcher(args.render, url)
    try:
        results, variant_totals, errors = crawl_and_count(
            url, args.words, fetcher,
            max_pages=args.max_pages, max_depth=args.max_depth, delay=args.delay,
            case_sensitive=args.case_sensitive,
            whole_word=not args.no_word_boundary,
            include_html=args.include_html,
            verbose=not args.quiet,
        )
    finally:
        fetcher.close()

    if not results:
        print("Hic sayfa taranamadi.")
        for u, e in errors[:5]:
            print(f"  {u}: {e}")
        sys.exit(1)

    print_report(results, variant_totals, errors, args.words)

    if args.output:
        save_output(results, args.output)


if __name__ == "__main__":
    main()
