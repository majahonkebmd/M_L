"""Yahoo scraper module."""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from utils.delay import build_headers, human_sleep
from utils.proxy import build_playwright_proxy, build_requests_proxies

logger = logging.getLogger(__name__)

YAHOO_SEARCH_URL = "https://tw.bid.yahoo.com/search/auction/product?p={query}&pg={page}"
DEFAULT_QUERIES = ["二手機車", "中古機車"]
DEFAULT_PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

CONDITION_MAP = {
    1: "全新",
    2: "二手",
}

FIELDNAMES = [
    "url",
    "title",
    "price_ntd",
    "brand",
    "model",
    "year",
    "mileage_km",
    "condition",
    "location",
    "scraped_at",
]


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _extract_numeric(text: str | int | float | None) -> int | None:
    if text is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(text))
    return int(digits) if digits else None


def _extract_year_from_text(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(19\d{2}|20\d{2})", text)
    return int(match.group(1)) if match else None


def _normalize_mileage_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    cleaned = cleaned.replace("ＫＭ", "km").replace("Km", "km").replace("KM", "km").replace("㎞", "km")
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned or None


def _extract_mileage_from_text(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = _clean_text(text)
    if not cleaned:
        return None

    patterns = [
        r"(?:行駛(?:里|哩)程|(?:總)?里程|公里數)\s*[:：]?\s*([0-9][0-9,\.]*\s*(?:萬)?\s*(?:公里|km|KM)?)",
        r"(?:行駛(?:里|哩)程|(?:總)?里程|公里數)\s*[:：]?\s*([一二三四五六七八九十百千萬兩零〇]+\s*(?:萬|千)?\s*(?:公里|km|KM)?)",
        r"([0-9][0-9,\.]*\s*(?:萬)?\s*(?:公里|km|KM))",
        r"([一二三四五六七八九十百千萬兩零〇]+\s*(?:萬|千)?\s*(?:公里|km|KM))",
    ]

    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        mileage_value = _normalize_mileage_value(match.group(1))
        if not mileage_value:
            continue
        if not re.search(r"(公里|km)", mileage_value, flags=re.IGNORECASE):
            mileage_value = f"{mileage_value}公里"
        return mileage_value

    unknown_match = re.search(r"(?:里程|公里).{0,6}(?:不明|未知|未提供|無法提供)", cleaned)
    if unknown_match:
        return _normalize_mileage_value(unknown_match.group(0))

    return None


def _extract_brand_model(title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    title_clean = _clean_text(title)
    if not title_clean:
        return None, None

    known_brands = [
        "YAMAHA",
        "HONDA",
        "SYM",
        "KYMCO",
        "SUZUKI",
        "KAWASAKI",
        "PGO",
        "AEON",
        "山葉",
        "本田",
        "三陽",
        "光陽",
        "鈴木",
    ]

    upper_title = title_clean.upper()
    for brand in known_brands:
        if brand in upper_title or brand in title_clean:
            model = _clean_text(title_clean.replace(brand, ""))
            return brand, model

    parts = title_clean.split(" ")
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _normalize_listing_url(href: str) -> str | None:
    if not href:
        return None
    absolute = urljoin("https://tw.bid.yahoo.com", href)
    parsed = urlparse(absolute)

    if "tw.bid.yahoo.com" not in parsed.netloc:
        return None
    if not re.match(r"^/item/[0-9]{6,}$", parsed.path):
        return None

    return parsed._replace(fragment="", query="").geturl()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _extract_isoredux_data_from_html(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("script#isoredux-data")
    if not node:
        return None
    payload = node.get_text(strip=True)
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_listing_urls_from_state(state: dict[str, Any] | None) -> list[str]:
    if not state:
        return []

    hits = state.get("search", {}).get("ecsearch", {}).get("hits", [])
    if not isinstance(hits, list):
        return []

    urls: list[str] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        normalized = _normalize_listing_url(hit.get("ec_item_url", ""))
        if normalized:
            urls.append(normalized)

    return _dedupe_preserve_order(urls)


def _extract_listing_urls_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []

    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        if "/item/" not in href:
            continue
        normalized = _normalize_listing_url(href)
        if normalized:
            urls.append(normalized)

    patterns = [
        r"https?://tw\.bid\.yahoo\.com/item/[0-9]{6,}",
        r"/item/[0-9]{6,}",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html):
            normalized = _normalize_listing_url(match.group(0))
            if normalized:
                urls.append(normalized)

    return _dedupe_preserve_order(urls)


def _extract_listing_urls_with_playwright(
    index_url: str,
    timeout_ms: int = 20000,
    proxy_url: str | None = None,
) -> list[str]:
    playwright_proxy = build_playwright_proxy(proxy_url)
    html = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=playwright_proxy,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=DEFAULT_PLAYWRIGHT_USER_AGENT,
            locale="zh-TW",
            viewport={"width": 1366, "height": 768},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()
        page.goto(index_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=max(5000, timeout_ms // 2))
        except Exception:
            logger.debug("Playwright networkidle timeout for %s", index_url)
        for _ in range(3):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(800)
        html = page.content()
        context.close()
        browser.close()

    state = _extract_isoredux_data_from_html(html)
    urls = _extract_listing_urls_from_state(state)
    if urls:
        return urls
    return _extract_listing_urls_from_html(html)


def get_listing_urls(
    max_pages: int = 10,
    query: str = "二手機車",
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    proxy_url: str | None = None,
    use_playwright_fallback: bool = True,
    diagnostics: bool = False,
) -> list[str]:
    """Step 1: paginate search results and collect Yahoo listing URLs."""
    session = requests.Session()
    proxies = build_requests_proxies(proxy_url)

    urls: list[str] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        search_url = YAHOO_SEARCH_URL.format(query=quote_plus(query), page=page)
        page_urls: list[str] = []

        logger.info("Fetching Yahoo index page %s", search_url)
        try:
            response = session.get(
                search_url,
                headers=build_headers(headers),
                timeout=timeout,
                proxies=proxies,
            )
            response.raise_for_status()
            response.encoding = "utf-8"

            if diagnostics:
                logger.info("Status: %s", response.status_code)
                logger.info("Content length: %s", len(response.text))
                logger.info("First 500 chars: %s", response.text[:500].replace("\n", " "))

            state = _extract_isoredux_data_from_html(response.text)
            page_urls = _extract_listing_urls_from_state(state)
            if not page_urls:
                page_urls = _extract_listing_urls_from_html(response.text)

            logger.info("Yahoo page %s yielded %s listing URLs before dedupe", page, len(page_urls))

            if not page_urls and use_playwright_fallback:
                logger.info("No links via requests on Yahoo page %s; trying Playwright fallback", page)
                page_urls = _extract_listing_urls_with_playwright(search_url, proxy_url=proxy_url)
                logger.info("Playwright found %s listing URLs on Yahoo page %s", len(page_urls), page)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch/parse Yahoo index page %s: %s", search_url, exc)

        for listing_url in page_urls:
            if listing_url not in seen:
                seen.add(listing_url)
                urls.append(listing_url)

        logger.info("Yahoo page %s yielded %s total unique listing URLs", page, len(urls))
        human_sleep(2, 4)

    return urls


def _fetch_rendered_html_with_playwright(
    url: str,
    timeout_ms: int = 12000,
    proxy_url: str | None = None,
) -> str:
    playwright_proxy = build_playwright_proxy(proxy_url)
    html = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=playwright_proxy,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=DEFAULT_PLAYWRIGHT_USER_AGENT,
            locale="zh-TW",
            viewport={"width": 1366, "height": 768},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)
        html = page.content()
        context.close()
        browser.close()

    return html


def _extract_item_data_from_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    item = state.get("item")
    return item if isinstance(item, dict) else None


def _extract_price(item: dict[str, Any]) -> tuple[int | None, str | None]:
    has_multiple = bool(item.get("hasMultiplePrice"))
    price_range = item.get("priceRange")

    if has_multiple and isinstance(price_range, dict):
        low = _extract_numeric(price_range.get("lowPrice"))
        high = _extract_numeric(price_range.get("highPrice"))
        if low and high and low != high:
            return low, f"{low}-{high}"
        if low:
            return low, str(low)
        if high:
            return high, str(high)

    price_ntd = _extract_numeric(item.get("price"))
    price_text = _clean_text(str(item.get("price"))) if item.get("price") is not None else None
    return price_ntd, price_text


def parse_listing(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    proxy_url: str | None = None,
    enable_playwright_fallback: bool = True,
) -> dict[str, Any]:
    """Step 2: parse one Yahoo listing detail page."""
    session = requests.Session()
    proxies = build_requests_proxies(proxy_url)

    response = session.get(
        url,
        headers=build_headers(headers),
        timeout=timeout,
        proxies=proxies,
    )
    response.raise_for_status()
    response.encoding = "utf-8"

    html = response.text
    state = _extract_isoredux_data_from_html(html)
    item = _extract_item_data_from_state(state)

    if not item and enable_playwright_fallback:
        try:
            html = _fetch_rendered_html_with_playwright(url=url, proxy_url=proxy_url)
            state = _extract_isoredux_data_from_html(html)
            item = _extract_item_data_from_state(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Playwright fallback failed for Yahoo listing %s: %s", url, exc)

    if not item:
        raise ValueError(f"Unable to parse Yahoo item state for {url}")

    title = _clean_text(item.get("title"))
    price_ntd, _ = _extract_price(item)

    condition_value = item.get("condition")
    condition_code = _extract_numeric(condition_value)
    condition = CONDITION_MAP.get(condition_code) if condition_code is not None else None
    if condition is None and condition_value is not None:
        condition = _clean_text(str(condition_value))

    location = _clean_text(item.get("location"))

    description_html = item.get("description")
    description_text = None
    if isinstance(description_html, str):
        description_text = _clean_text(BeautifulSoup(description_html, "lxml").get_text(" ", strip=True))

    combined_text = " ".join([part for part in [title, description_text] if part])

    year = _extract_year_from_text(title)
    usedcar_data = item.get("usedcar")
    if isinstance(usedcar_data, dict):
        for key in ("year", "manufactureYear", "modelYear"):
            usedcar_year = _extract_numeric(usedcar_data.get(key))
            if usedcar_year:
                year = usedcar_year
                break
    if not year and description_text:
        desc_year_match = re.search(
            r"(?:年份|出廠|年式|車齡)\s*[:：]?\s*(19\d{2}|20\d{2})",
            description_text,
        )
        if desc_year_match:
            year = int(desc_year_match.group(1))

    mileage_km = None
    if isinstance(usedcar_data, dict):
        for key in ("mileage", "mileageKm", "mileage_km"):
            raw_mileage = usedcar_data.get(key)
            if raw_mileage is None:
                continue
            mileage_km = _normalize_mileage_value(str(raw_mileage))
            if mileage_km and not re.search(r"(公里|km)", mileage_km, flags=re.IGNORECASE):
                mileage_km = f"{mileage_km}公里"
            if mileage_km:
                break
    mileage_km = mileage_km or _extract_mileage_from_text(combined_text)

    brand, model = _extract_brand_model(title)

    return {
        "url": _normalize_listing_url(item.get("url", "")) or _normalize_listing_url(url) or url,
        "title": title,
        "price_ntd": price_ntd,
        "price_text": _clean_text(str(item.get("price"))) if item.get("price") is not None else None,
        "brand": brand,
        "model": model,
        "year": year,
        "mileage_km": mileage_km,
        "condition": condition,
        "location": location,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def scrape_yahoo_listings(
    max_pages: int = 10,
    queries: list[str] | None = None,
    max_listings: int | None = None,
    proxy_url: str | None = None,
) -> list[dict[str, Any]]:
    """Run end-to-end Yahoo scraping over one or more keywords."""
    query_list = queries or DEFAULT_QUERIES
    all_urls: list[str] = []
    seen: set[str] = set()

    for query in query_list:
        query_urls = get_listing_urls(max_pages=max_pages, query=query, proxy_url=proxy_url)
        for listing_url in query_urls:
            if listing_url not in seen:
                seen.add(listing_url)
                all_urls.append(listing_url)

    if max_listings is not None:
        all_urls = all_urls[:max_listings]

    rows: list[dict[str, Any]] = []
    for idx, listing_url in enumerate(all_urls, start=1):
        try:
            logger.info("Parsing Yahoo listing %s/%s: %s", idx, len(all_urls), listing_url)
            rows.append(parse_listing(url=listing_url, proxy_url=proxy_url))
            human_sleep(1.5, 3.5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse Yahoo listing %s: %s", listing_url, exc)

    return rows


def _append_failed_url(failed_url_path: Path, url: str, exc: Exception) -> None:
    failed_url_path.parent.mkdir(parents=True, exist_ok=True)
    with failed_url_path.open("a", encoding="utf-8") as err_file:
        err_file.write(f"{url} | {exc}\n")


def scrape_listing_urls_to_csv(
    listing_urls: list[str],
    output_path: str | Path,
    proxy_url: str | None = None,
    failed_url_path: str | Path = "data/failed_urls.txt",
) -> dict[str, Any]:
    """Scrape known Yahoo listing URLs and write rows incrementally to CSV."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    failed_file = Path(failed_url_path)

    unique_urls = _dedupe_preserve_order([url for url in listing_urls if isinstance(url, str) and url.strip()])

    success_count = 0
    failure_count = 0

    with output.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        writer.writeheader()

        for idx, listing_url in enumerate(unique_urls, start=1):
            try:
                logger.info("Parsing Yahoo listing %s/%s: %s", idx, len(unique_urls), listing_url)
                row = parse_listing(url=listing_url, proxy_url=proxy_url)
                writer.writerow({field: row.get(field) for field in FIELDNAMES})
                csv_file.flush()
                success_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse Yahoo listing %s: %s", listing_url, exc)
                _append_failed_url(failed_file, listing_url, exc)
                failed_row = {field: "" for field in FIELDNAMES}
                failed_row["url"] = listing_url
                failed_row["scraped_at"] = datetime.now().isoformat(timespec="seconds")
                writer.writerow(failed_row)
                csv_file.flush()
                failure_count += 1
            finally:
                human_sleep(2, 5)

    return {
        "output_path": output,
        "failed_url_path": failed_file,
        "total_urls": len(unique_urls),
        "success_count": success_count,
        "failure_count": failure_count,
    }


def scrape_yahoo_listings_to_csv(
    output_path: str | Path,
    max_pages: int = 10,
    queries: list[str] | None = None,
    max_listings: int | None = None,
    proxy_url: str | None = None,
    failed_url_path: str | Path = "data/failed_urls.txt",
) -> dict[str, Any]:
    """Scrape Yahoo listings and write rows incrementally to CSV."""
    query_list = queries or DEFAULT_QUERIES
    all_urls: list[str] = []
    seen: set[str] = set()

    for query in query_list:
        query_urls = get_listing_urls(max_pages=max_pages, query=query, proxy_url=proxy_url)
        for listing_url in query_urls:
            if listing_url not in seen:
                seen.add(listing_url)
                all_urls.append(listing_url)

    if max_listings is not None:
        all_urls = all_urls[:max_listings]

    return scrape_listing_urls_to_csv(
        listing_urls=all_urls,
        output_path=output_path,
        proxy_url=proxy_url,
        failed_url_path=failed_url_path,
    )
