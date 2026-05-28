"""Entry point for scooter_project."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from scrapers.ruten_scraper import (
    get_listing_urls as ruten_get_listing_urls,
)
from scrapers.ruten_scraper import (
    scrape_listing_urls_to_csv as ruten_scrape_listing_urls_to_csv,
)
from scrapers.ruten_scraper import (
    scrape_ruten_listings_to_csv,
)
from scrapers.yahoo_scraper import (
    get_listing_urls as yahoo_get_listing_urls,
)
from scrapers.yahoo_scraper import (
    scrape_listing_urls_to_csv as yahoo_scrape_listing_urls_to_csv,
)
from scrapers.yahoo_scraper import (
    scrape_yahoo_listings_to_csv,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape second-hand scooter listings from supported marketplaces")
    parser.add_argument(
        "--source",
        type=str,
        choices=["ruten", "yahoo"],
        default="ruten",
        help="Scraper source to use",
    )
    parser.add_argument("--max-pages", type=int, default=3, help="How many index pages per query to scrape")
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="Search keyword (repeat flag for multiple). Defaults to 二手機車 + 中古機車",
    )
    parser.add_argument("--max-listings", type=int, default=50, help="Maximum number of detail pages to parse")
    parser.add_argument("--proxy", type=str, default=None, help="Optional proxy URL")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output CSV path. Default: data/raw/{source}_YYYYMMDD_HHMMSS.csv",
    )
    parser.add_argument(
        "--failed-log",
        type=str,
        default="data/failed_urls.txt",
        help="Path to append failed listing URLs and errors",
    )
    parser.add_argument(
        "--urls-file",
        type=str,
        default=None,
        help="Optional text file with one listing URL per line; skips index-page discovery",
    )
    parser.add_argument(
        "--diagnose-index",
        action="store_true",
        help="Run one-page listing URL discovery diagnostic and exit",
    )
    parser.add_argument(
        "--diagnose-query",
        type=str,
        default="二手機車",
        help="Query to use with --diagnose-index",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    args = build_parser().parse_args()

    if args.source == "yahoo":
        source_get_listing_urls = yahoo_get_listing_urls
        source_scrape_listing_urls_to_csv = yahoo_scrape_listing_urls_to_csv
        source_scrape_listings_to_csv = scrape_yahoo_listings_to_csv
    else:
        source_get_listing_urls = ruten_get_listing_urls
        source_scrape_listing_urls_to_csv = ruten_scrape_listing_urls_to_csv
        source_scrape_listings_to_csv = scrape_ruten_listings_to_csv

    if args.diagnose_index:
        urls = source_get_listing_urls(
            max_pages=1,
            query=args.diagnose_query,
            proxy_url=args.proxy,
            diagnostics=True,
        )
        print(f"Found {len(urls)} URLs")
        for url in urls[:10]:
            print(url)
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = args.output or f"data/raw/{args.source}_{timestamp}.csv"

    if args.urls_file:
        urls_path = Path(args.urls_file)
        if not urls_path.exists():
            raise SystemExit(
                f"URLs file not found: {urls_path}. "
                "Create it with one listing URL per line, then rerun."
            )

        url_lines = urls_path.read_text(encoding="utf-8-sig").splitlines()
        listing_urls: list[str] = []
        for line in url_lines:
            cleaned = line.lstrip("\ufeff").strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            listing_urls.append(cleaned)
        if not listing_urls:
            raise SystemExit(
                f"No listing URLs found in: {urls_path}. "
                "Add one URL per line (lines starting with # are ignored)."
            )

        result = source_scrape_listing_urls_to_csv(
            listing_urls=listing_urls,
            output_path=output_path,
            proxy_url=args.proxy,
            failed_url_path=args.failed_log,
        )
    else:
        result = source_scrape_listings_to_csv(
            output_path=output_path,
            max_pages=args.max_pages,
            queries=args.queries,
            max_listings=args.max_listings,
            proxy_url=args.proxy,
            failed_url_path=args.failed_log,
        )

    print(f"Total listing URLs: {result['total_urls']}")
    print(f"Scraped successfully: {result['success_count']}")
    print(f"Failed: {result['failure_count']}")
    print(f"Saved to: {result['output_path']}")
    print(f"Failed URL log: {result['failed_url_path']}")


if __name__ == "__main__":
    main()
