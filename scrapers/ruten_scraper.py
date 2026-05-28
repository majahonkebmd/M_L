"""Ruten scraper module."""

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

BASE_URL = "https://www.ruten.com.tw/find/?q={query}&p={page}"
MOBILE_BASE_URL = "https://m.ruten.com.tw/find/?q={query}&p={page}"
DEFAULT_QUERIES = ["二手機車", "中古機車"]
ANTI_BOT_MARKERS = [
    "captcha",
    "驗證碼",
    "forbidden",
    "access denied",
    "cloudflare",
    "robot",
]

DEFAULT_PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

FIELD_SELECTORS: dict[str, list[str]] = {
    "title": ["h1.title", "h1[itemprop='name']", "h1"],
    "price_ntd": [".price", "[itemprop='price']", "[class*='price']"],
    "brand": [".brand", "[class*='brand']"],
    "model": [".model", "[class*='model']"],
    "year": [".year", "[class*='year']"],
    "mileage_km": [".mileage", "[class*='mileage']"],
    "condition": [".condition", "[class*='condition']"],
    "location": [".location", "[class*='location']"],
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


def _extract_text_by_selectors(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                return text
    return None


def _extract_numeric(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def _extract_brand_model(title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    tokens = _clean_text(title)
    if not tokens:
        return None, None
    parts = tokens.split(" ")
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


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
        # Labeled mileage fields, with or without explicit unit.
        r"(?:行駛(?:里|哩)程|(?:總)?里程|公里數)\s*[:：]?\s*([0-9][0-9,\.]*\s*(?:萬)?\s*(?:公里|km|KM)?)",
        r"(?:行駛(?:里|哩)程|(?:總)?里程|公里數)\s*[:：]?\s*([一二三四五六七八九十百千萬兩零〇]+\s*(?:萬|千)?\s*(?:公里|km|KM)?)",
        # Generic mileage mentions.
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

    # Preserve explicit unknown/unspecified mileage statements if present.
    unknown_match = re.search(r"(?:里程|公里).{0,6}(?:不明|未知|未提供|無法提供)", cleaned)
    if unknown_match:
        return _normalize_mileage_value(unknown_match.group(0))

    return None


def _looks_like_template_text(text: str | None) -> bool:
    if not text:
        return True
    lowered = text.lower()
    return "${" in text or "displayname" in lowered or "{{" in text or "}}" in text


def _extract_product_jsonld(soup: BeautifulSoup) -> dict[str, Any] | None:
    def walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            node_type = node.get("@type")
            if node_type == "Product" or (
                isinstance(node_type, list) and "Product" in node_type
            ):
                return node
            for value in node.values():
                found = walk(value)
                if found:
                    return found
            return None
        if isinstance(node, list):
            for value in node:
                found = walk(value)
                if found:
                    return found
        return None

    for script in soup.select("script[type='application/ld+json']"):
        payload = script.get_text(strip=True)
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except Exception:
            continue
        product = walk(parsed)
        if product:
            return product
    return None


def _extract_price_text_from_product_jsonld(product: dict[str, Any] | None) -> str | None:
    if not product:
        return None

    offers = product.get("offers")
    if isinstance(offers, dict):
        for key in ("price", "lowPrice", "highPrice"):
            value = offers.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        nested_offers = offers.get("offers")
        if isinstance(nested_offers, list):
            for offer in nested_offers:
                if not isinstance(offer, dict):
                    continue
                value = offer.get("price")
                if value is not None and str(value).strip():
                    return str(value).strip()
    elif isinstance(offers, list):
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            value = offer.get("price")
            if value is not None and str(value).strip():
                return str(value).strip()

    return None


def _extract_condition_location_from_description(
    description: str | None,
) -> tuple[str | None, str | None]:
    if not description:
        return None, None
    condition_match = re.search(r"物品狀況[:：]\s*([^,，]+)", description)
    location_match = re.search(r"物品所在地[:：]\s*([^,，]+)", description)
    condition = _clean_text(condition_match.group(1)) if condition_match else None
    location = _clean_text(location_match.group(1)) if location_match else None
    return condition, location


def _normalize_listing_url(href: str) -> str | None:
    if not href:
        return None

    absolute = urljoin("https://www.ruten.com.tw", href)
    parsed = urlparse(absolute)
    path = parsed.path.lower()

    if "ruten.com.tw" not in parsed.netloc:
        return None
    # Legacy listing URLs:
    #   /item/show?xxxxxxxx
    # Newer listing URLs:
    #   /item/21902527386667/
    is_legacy_show = path.startswith("/item/show")
    is_item_id_path = bool(re.match(r"^/item/[0-9a-z_-]{8,}/?$", path))
    if not (is_legacy_show or is_item_id_path):
        return None

    return parsed._replace(fragment="").geturl()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _extract_listing_urls_from_html(html: str) -> list[str]:
    """Extract listing URLs from both DOM anchors and script text patterns."""
    soup = BeautifulSoup(html, "lxml")

    candidates: list[str] = []

    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        if "/item/show" in href or re.search(r"/item/[0-9a-z_-]{8,}/?$", href, flags=re.IGNORECASE):
            normalized = _normalize_listing_url(href)
            if normalized:
                candidates.append(normalized)

    text_sources = [html, html.replace("\\/", "/")]
    regexes = [
        r"https?://www\.ruten\.com\.tw/item/show\?[^\"'<>\s]+",
        r"/item/show\?[^\"'<>\s]+",
        r"/item/show/[0-9a-zA-Z_-]+",
        r"https?://www\.ruten\.com\.tw/item/[0-9a-zA-Z_-]{8,}/?",
        r"/item/[0-9a-zA-Z_-]{8,}/?",
    ]

    for source in text_sources:
        for pattern in regexes:
            for match in re.finditer(pattern, source):
                normalized = _normalize_listing_url(match.group(0))
                if normalized:
                    candidates.append(normalized)

    return _dedupe_preserve_order(candidates)


def _extract_script_urls_from_html(html: str) -> list[str]:
    """Extract JS asset URLs from a page to assist debugging."""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for node in soup.select("script[src]"):
        src = node.get("src")
        if not src:
            continue
        full = urljoin("https://www.ruten.com.tw", src)
        urls.append(full)
    return _dedupe_preserve_order(urls)


def _looks_like_spa_shell(html: str) -> bool:
    """
    Heuristic: shell page with app container but no listing links.
    """
    lower = html.lower()
    has_app_root = ('id="app"' in lower) or ("id='app'" in lower)
    has_item_links = ("/item/show" in lower) or re.search(r"/item/[0-9a-z_-]{8,}/?", lower) is not None
    return has_app_root and not has_item_links


def _extract_listing_urls_from_json_payload(payload: str) -> list[str]:
    """Extract listing URLs recursively from a JSON payload."""
    candidates: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
            return

        if isinstance(node, list):
            for value in node:
                walk(value)
            return

        if isinstance(node, str) and ("/item/show" in node or "/item/" in node):
            normalized = _normalize_listing_url(node)
            if normalized:
                candidates.append(normalized)

    try:
        data = json.loads(payload)
        walk(data)
    except Exception:
        return []

    return _dedupe_preserve_order(candidates)


def _extract_listing_urls_with_playwright(
    index_url: str,
    timeout_ms: int = 15000,
    proxy_url: str | None = None,
    diagnostics: bool = False,
) -> list[str]:
    """Load index page with browser and collect listing URLs."""
    playwright_proxy = build_playwright_proxy(proxy_url)
    network_payloads: list[str] = []

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

        def on_response(response: Any) -> None:
            try:
                url = response.url
                if "ruten.com.tw" not in url:
                    return

                resource_type = response.request.resource_type
                headers = response.headers or {}
                content_type = headers.get("content-type", "")

                useful_response = resource_type in {"xhr", "fetch", "document"}
                useful_content_type = (
                    "json" in content_type
                    or "html" in content_type
                    or "javascript" in content_type
                    or "text" in content_type
                )
                if not useful_response and not useful_content_type:
                    return

                text = response.text()
                if not text:
                    return

                if "/item/show" in text or "/item/" in text or "search" in url:
                    network_payloads.append(text)
            except Exception:
                return

        page.on("response", on_response)
        page.goto(index_url, wait_until="domcontentloaded", timeout=timeout_ms)

        try:
            page.wait_for_load_state("networkidle", timeout=max(5000, timeout_ms // 2))
        except Exception:
            logger.debug("networkidle timeout for %s", index_url)

        # Trigger lazy-loaded listings if the page renders on scroll.
        for _ in range(8):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1200)

        try:
            page.wait_for_selector("a[href*='/item/']", timeout=timeout_ms // 2)
        except Exception:
            logger.debug("No /item/ selector appeared on %s", index_url)

        hrefs = page.eval_on_selector_all("a[href]", "nodes => nodes.map(n => n.href)")
        attr_values = page.eval_on_selector_all(
            "*",
            (
                "nodes => nodes"
                ".flatMap(n => Array.from(n.attributes || []).map(a => a.value))"
                ".filter(v => typeof v === 'string' && v.includes('/item/'))"
            ),
        )
        html = page.content()
        if diagnostics:
            logger.info("Playwright rendered total anchors: %s", len(hrefs))
            logger.info("Playwright href sample: %s", hrefs[:10])
            logger.info(
                "Playwright /item/ anchors: %s",
                len([href for href in hrefs if isinstance(href, str) and "/item/" in href]),
            )
        context.close()
        browser.close()

    candidates: list[str] = []

    for href in hrefs:
        if isinstance(href, str) and "/item/" in href:
            normalized = _normalize_listing_url(href)
            if normalized:
                candidates.append(normalized)

    for value in attr_values:
        if isinstance(value, str):
            normalized = _normalize_listing_url(value)
            if normalized:
                candidates.append(normalized)

    candidates.extend(_extract_listing_urls_from_html(html))
    for payload in network_payloads:
        candidates.extend(_extract_listing_urls_from_html(payload))
        candidates.extend(_extract_listing_urls_from_json_payload(payload))

    return _dedupe_preserve_order(candidates)


def _write_debug_html(query: str, page: int, html: str) -> None:
    safe_query = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "_", query)
    debug_dir = Path("data/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    output = debug_dir / f"ruten_index_{safe_query}_p{page}.html"
    output.write_text(html, encoding="utf-8")


def _write_debug_script_urls(query: str, page: int, script_urls: list[str]) -> None:
    safe_query = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+", "_", query)
    debug_dir = Path("data/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    output = debug_dir / f"ruten_index_{safe_query}_p{page}_scripts.txt"
    output.write_text("\n".join(script_urls), encoding="utf-8")


def get_listing_urls(
    max_pages: int = 10,
    query: str = "二手機車",
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    proxy_url: str | None = None,
    use_playwright_fallback: bool = True,
    diagnostics: bool = False,
) -> list[str]:
    """Step 1: paginate listing index pages and collect detail URLs."""
    session = requests.Session()
    proxies = build_requests_proxies(proxy_url)

    urls: list[str] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        page_urls: list[str] = []
        index_templates = [BASE_URL, MOBILE_BASE_URL]

        for template in index_templates:
            index_url = template.format(query=quote_plus(query), page=page)
            logger.info("Fetching index page %s", index_url)

            try:
                response = session.get(
                    index_url,
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
                    response_lower = response.text.lower()
                    for marker in ANTI_BOT_MARKERS:
                        if marker in response_lower:
                            logger.warning("Potential anti-bot marker found on page %s: %s", page, marker)

                page_urls = _extract_listing_urls_from_html(response.text)
                logger.info("Requests extraction found %s links on page %s", len(page_urls), page)

                if page_urls:
                    break

                # If this is likely only the shell HTML, save script URLs for further endpoint debugging.
                if _looks_like_spa_shell(response.text):
                    script_urls = _extract_script_urls_from_html(response.text)
                    _write_debug_script_urls(query=query, page=page, script_urls=script_urls)
                    logger.warning(
                        "SPA shell detected for query=%s page=%s; wrote script URL list to data/debug",
                        query,
                        page,
                    )

                if use_playwright_fallback:
                    logger.info("No listing links via requests on page %s; trying Playwright fallback", page)
                    try:
                        page_urls = _extract_listing_urls_with_playwright(
                            index_url=index_url,
                            proxy_url=proxy_url,
                            diagnostics=diagnostics,
                        )
                        logger.info("Playwright extraction found %s links on page %s", len(page_urls), page)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Playwright index fallback failed for %s: %s", index_url, exc)

                if page_urls:
                    break

                _write_debug_html(query=query, page=page, html=response.text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to fetch/parse index page %s: %s", index_url, exc)

        if not page_urls:
            logger.warning(
                "Still no listing links for query=%s page=%s after desktop/mobile + Playwright fallback",
                query,
                page,
            )

        for listing_url in page_urls:
            if listing_url not in seen:
                seen.add(listing_url)
                urls.append(listing_url)

        logger.info("Page %s yielded %s total unique listing URLs", page, len(urls))

        human_sleep(2, 4)

    return urls


def scrape_with_playwright(
    url: str,
    wait_selector: str = ".price",
    timeout_ms: int = 10000,
    proxy_url: str | None = None,
) -> BeautifulSoup:
    """Step 3: load dynamically rendered pages via Playwright."""
    playwright_proxy = build_playwright_proxy(proxy_url)

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
        page.wait_for_timeout(3000)
        for _ in range(2):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(800)
        try:
            page.wait_for_selector(wait_selector, timeout=timeout_ms)
        except Exception:
            logger.debug("Wait selector not found (%s) for %s", wait_selector, url)
        html = page.content()
        context.close()
        browser.close()

    return BeautifulSoup(html, "lxml")


def parse_listing(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    proxy_url: str | None = None,
    enable_playwright_fallback: bool = True,
) -> dict[str, Any]:
    """Step 2: parse a single listing detail page."""
    session = requests.Session()
    proxies = build_requests_proxies(proxy_url)

    response = session.get(
        url,
        headers=build_headers(headers),
        timeout=timeout,
        proxies=proxies,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    title = _extract_text_by_selectors(soup, FIELD_SELECTORS["title"])
    price_text = _extract_text_by_selectors(soup, FIELD_SELECTORS["price_ntd"])

    should_retry_with_playwright = (
        not title
        or _looks_like_template_text(title)
        or not price_text
        or _looks_like_template_text(price_text)
        or _extract_numeric(price_text) in (None, 0)
    )

    if enable_playwright_fallback and should_retry_with_playwright:
        try:
            soup = scrape_with_playwright(url=url, proxy_url=proxy_url)
            title = _extract_text_by_selectors(soup, FIELD_SELECTORS["title"]) or title
            price_text = _extract_text_by_selectors(soup, FIELD_SELECTORS["price_ntd"]) or price_text
        except Exception as exc:  # noqa: BLE001
            logger.warning("Playwright detail fallback failed for %s: %s", url, exc)

    product_jsonld = _extract_product_jsonld(soup)
    jsonld_title = None
    jsonld_brand = None
    jsonld_price_text = None
    jsonld_condition = None
    jsonld_location = None
    jsonld_description = None

    if product_jsonld:
        raw_jsonld_title = product_jsonld.get("name")
        if isinstance(raw_jsonld_title, str):
            jsonld_title = _clean_text(raw_jsonld_title)

        raw_brand = product_jsonld.get("brand")
        if isinstance(raw_brand, dict):
            brand_name = raw_brand.get("name")
            if isinstance(brand_name, str):
                jsonld_brand = _clean_text(brand_name)
        elif isinstance(raw_brand, str):
            jsonld_brand = _clean_text(raw_brand)

        jsonld_price_text = _extract_price_text_from_product_jsonld(product_jsonld)
        raw_description = product_jsonld.get("description")
        if isinstance(raw_description, str):
            jsonld_description = _clean_text(raw_description)
            jsonld_condition, jsonld_location = _extract_condition_location_from_description(raw_description)

    if _looks_like_template_text(title):
        title = None
    if _looks_like_template_text(price_text):
        price_text = None

    if not title and jsonld_title:
        title = jsonld_title
    if _extract_numeric(price_text) in (None, 0) and jsonld_price_text:
        price_text = jsonld_price_text

    brand = _extract_text_by_selectors(soup, FIELD_SELECTORS["brand"])
    model = _extract_text_by_selectors(soup, FIELD_SELECTORS["model"])
    year_text = _extract_text_by_selectors(soup, FIELD_SELECTORS["year"])
    mileage_text = _extract_text_by_selectors(soup, FIELD_SELECTORS["mileage_km"])
    condition = _extract_text_by_selectors(soup, FIELD_SELECTORS["condition"])
    location = _extract_text_by_selectors(soup, FIELD_SELECTORS["location"])

    if _looks_like_template_text(brand):
        brand = None
    if _looks_like_template_text(model):
        model = None
    if _looks_like_template_text(condition):
        condition = None
    if _looks_like_template_text(location):
        location = None

    if not brand and jsonld_brand:
        brand = jsonld_brand
    if not condition and jsonld_condition:
        condition = jsonld_condition
    if not location and jsonld_location:
        location = jsonld_location

    if not brand or not model:
        inferred_brand, inferred_model = _extract_brand_model(title)
        brand = brand or inferred_brand
        model = model or inferred_model

    year = _extract_numeric(year_text) or _extract_year_from_text(title)
    mileage_km = (
        _extract_mileage_from_text(mileage_text)
        or _extract_mileage_from_text(title)
        or _extract_mileage_from_text(jsonld_description)
    )

    # If a mileage selector exists but pattern extraction missed, keep raw text.
    if not mileage_km and mileage_text and not _looks_like_template_text(mileage_text):
        mileage_km = _clean_text(mileage_text)

    return {
        "url": url,
        "title": title,
        "price_ntd": _extract_numeric(price_text),
        "price_text": price_text,
        "brand": brand,
        "model": model,
        "year": year,
        "mileage_km": mileage_km,
        "condition": condition,
        "location": location,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def scrape_ruten_listings(
    max_pages: int = 10,
    queries: list[str] | None = None,
    max_listings: int | None = None,
    proxy_url: str | None = None,
) -> list[dict[str, Any]]:
    """Run end-to-end scraping over one or more search keywords."""
    query_list = queries or DEFAULT_QUERIES
    all_urls: list[str] = []
    seen: set[str] = set()

    for query in query_list:
        query_urls = get_listing_urls(max_pages=max_pages, query=query, proxy_url=proxy_url)
        for url in query_urls:
            if url not in seen:
                seen.add(url)
                all_urls.append(url)

    if max_listings is not None:
        all_urls = all_urls[:max_listings]

    results: list[dict[str, Any]] = []
    for index, url in enumerate(all_urls, start=1):
        try:
            logger.info("Parsing listing %s/%s: %s", index, len(all_urls), url)
            record = parse_listing(url=url, proxy_url=proxy_url)
            results.append(record)
            human_sleep(1.5, 3.5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse listing %s: %s", url, exc)

    return results


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
    """Scrape known listing URLs and write rows incrementally to CSV."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    failed_file = Path(failed_url_path)

    unique_urls = _dedupe_preserve_order([url for url in listing_urls if isinstance(url, str) and url.strip()])

    success_count = 0
    failure_count = 0

    with output.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        writer.writeheader()

        for index, url in enumerate(unique_urls, start=1):
            try:
                logger.info("Parsing listing %s/%s: %s", index, len(unique_urls), url)
                row = parse_listing(url=url, proxy_url=proxy_url)
                writer.writerow({field: row.get(field) for field in FIELDNAMES})
                csv_file.flush()
                success_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse listing %s: %s", url, exc)
                _append_failed_url(failed_file, url, exc)
                # Keep traceability in the main CSV even when parsing fails.
                failed_row = {field: "" for field in FIELDNAMES}
                failed_row["url"] = url
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


def scrape_ruten_listings_to_csv(
    output_path: str | Path,
    max_pages: int = 10,
    queries: list[str] | None = None,
    max_listings: int | None = None,
    proxy_url: str | None = None,
    failed_url_path: str | Path = "data/failed_urls.txt",
) -> dict[str, Any]:
    """
    Scrape listings and write rows incrementally to CSV.

    This prevents data loss when scraping stops midway.
    """
    query_list = queries or DEFAULT_QUERIES
    all_urls: list[str] = []
    seen: set[str] = set()

    for query in query_list:
        query_urls = get_listing_urls(max_pages=max_pages, query=query, proxy_url=proxy_url)
        for url in query_urls:
            if url not in seen:
                seen.add(url)
                all_urls.append(url)

    if max_listings is not None:
        all_urls = all_urls[:max_listings]

    return scrape_listing_urls_to_csv(
        listing_urls=all_urls,
        output_path=output_path,
        proxy_url=proxy_url,
        failed_url_path=failed_url_path,
    )
