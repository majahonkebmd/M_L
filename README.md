# Scooter Project

Scraper project for collecting second-hand scooter listing data from Ruten into CSV files for ML workflows.

## Project Structure

```text
scooter_project/
|-- scrapers/
|   |-- ruten_scraper.py
|   `-- yahoo_scraper.py
|-- utils/
|   |-- delay.py
|   `-- proxy.py
|-- data/
|   |-- raw/
|   |-- debug/
|   `-- failed_urls.txt
|-- main.py
`-- requirements.txt
```

## Features

- Index scraping over paginated search results
- Detail-page parsing with Playwright fallback for JS-rendered pages
- Incremental CSV writing (row-by-row, safer on crash)
- Failed URL logging to `data/failed_urls.txt`
- Diagnostic mode for index extraction issues
- Optional proxy support

## Requirements

- Python 3.10+ (you are currently using Python 3.14)
- `pip`

Dependencies are listed in `requirements.txt`.

## Setup

### 1) Create and activate virtual environment (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2) Install Python packages

```powershell
python -m pip install -r requirements.txt
```

### 3) Install Playwright browser

```powershell
python -m playwright install chromium
```

## Usage

### Run normal scrape

```powershell
python main.py --max-pages 3 --max-listings 50
```

Optional output path:

```powershell
python main.py --max-pages 3 --max-listings 50 --output data/raw/my_run.csv
```

### Run with custom queries

```powershell
python main.py --query "keyword1" --query "keyword2" --max-pages 2 --max-listings 40
```

### Scrape from known listing URLs file

```powershell
python main.py --urls-file data/raw/known_urls.txt --output data/raw/manual_urls_test.csv
```

`known_urls.txt` format:

```text
# one URL per line
https://www.ruten.com.tw/item/12345678901234/
https://www.ruten.com.tw/item/23456789012345/
```

### Diagnostic mode (index discovery)

```powershell
python main.py --diagnose-index --diagnose-query "keyword"
```

Useful when listing URL extraction returns zero links.

## Output

Default output file:

- `data/raw/ruten_YYYYMMDD_HHMMSS.csv`

CSV columns:

- `url`
- `title`
- `price_ntd`
- `brand`
- `model`
- `year`
- `mileage_km`
- `condition`
- `location`
- `scraped_at`

Failed URLs are appended to:

- `data/failed_urls.txt`

## Command Options

`main.py` supports:

- `--max-pages`
- `--query` (repeatable)
- `--max-listings`
- `--proxy`
- `--output`
- `--failed-log`
- `--urls-file`
- `--diagnose-index`
- `--diagnose-query`

## Troubleshooting

### Found 0 URLs

- Run diagnostic mode.
- Check files in `data/debug/`.
- Confirm Playwright browser is installed (`python -m playwright install chromium`).

### CSV has only header

- Usually means no listing URLs were discovered.
- Confirm `Total listing URLs` in terminal output is greater than 0.

### Proxy usage

Use either:

- `--proxy http://user:pass@host:port`

or environment variables:

- `SCRAPER_PROXY`
- `HTTPS_PROXY`
- `HTTP_PROXY`
- `ALL_PROXY`

## Notes

- Scraping targets can change HTML structure at any time.
- If extraction breaks, update selectors and/or fallback logic in `scrapers/ruten_scraper.py`.
