from __future__ import annotations

import argparse
import csv
from pathlib import Path

from bs4 import BeautifulSoup

from amazon_scraper.html_extractors import (
    detect_page_state,
    extract_bullets,
    extract_description_text,
    extract_facts_table,
    extract_title,
    extract_variations,
)
from amazon_scraper.openai_client import OpenAIConfig
from amazon_scraper.scraper import AmazonScraper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Amazon PDP scraping and HTML extraction from CSV ASINs without calling OpenAI."
    )
    parser.add_argument("--input-csv", default="inputs/sample_product_asins.csv", help="CSV file containing ASINs.")
    parser.add_argument("--save-html", help="Optional directory to save fetched HTML files by ASIN.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode.")
    parser.add_argument("--delivery-zip", default="10001", help="Amazon delivery ZIP to apply for live fetches.")
    return parser


def read_asins(input_csv: Path) -> list[str]:
    asins: list[str] = []
    with input_csv.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            asin = (row.get("asin") or "").strip()
            if asin:
                asins.append(asin)
    if not asins:
        raise SystemExit(f"No valid ASINs found in {input_csv}")
    return asins


def fetch_html(scraper: AmazonScraper, driver, asin: str, save_dir: Path | None = None) -> str:
    url = f"{scraper.base_url}/dp/{asin}"
    html = scraper._fetch_page_source(driver, url)

    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"{asin}.html").write_text(html, encoding="utf-8")

    return html


def print_section(title: str, content: str) -> None:
    print(f"\n=== {title} ===")
    print(content or "[empty]")


def print_report(asin: str, html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")

    page_state = detect_page_state(html, soup)
    title = extract_title(soup)
    bullets = extract_bullets(soup)
    description = extract_description_text(soup)
    facts_table = extract_facts_table(soup)
    variations = extract_variations(soup)

    print(f"\n{'=' * 24} ASIN {asin} {'=' * 24}")
    print_section("PAGE STATE", page_state)
    print_section("TITLE", title)
    print_section("BULLETS", "\n".join(f"{index}. {bullet}" for index, bullet in enumerate(bullets, start=1)))
    print_section("FACTS TABLE", facts_table)
    print_section("VARIATIONS", variations)
    print_section("DESCRIPTION", description)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    asins = read_asins(input_csv)
    save_dir = Path(args.save_html) if args.save_html else None

    dummy_config = OpenAIConfig(
        api_key="dummy",
        embedding_model="dummy",
        chat_model="dummy",
    )
    scraper = AmazonScraper(
        headless=args.headless,
        delivery_zip=args.delivery_zip,
        openai_config=dummy_config,
    )
    driver = scraper._build_driver()
    try:
        scraper._prepare_session(driver)
        for asin in asins:
            html = fetch_html(scraper, driver, asin, save_dir=save_dir)
            print_report(asin, html)
    finally:
        driver.quit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
