from __future__ import annotations

import logging
from typing import List

from bs4 import BeautifulSoup, Tag

from .value_parsers import normalize_whitespace

LOGGER = logging.getLogger(__name__)


def detect_page_state(html: str, soup: BeautifulSoup) -> str:
    """Classify the rendered page into operational states used by retry logic."""
    lower_html = html.lower()
    if "enter the characters you see below" in lower_html or "type the characters you see in this image" in lower_html:
        return "BLOCKED"
    if "sorry! we couldn't find that page" in lower_html or "dogs of amazon" in lower_html:
        return "NOT_FOUND"
    if extract_title(soup):
        return "OK"
    return "PARTIAL"


def extract_title(soup: BeautifulSoup) -> str:
    """Extract product title from common Amazon title containers."""
    title_node = soup.select_one("#productTitle") or soup.select_one("#title")
    if not title_node:
        return ""
    return normalize_whitespace(title_node.get_text(" ", strip=True))


def extract_bullets(soup: BeautifulSoup) -> List[str]:
    """Extract normalized product bullets while preserving stable order."""
    bullets: List[str] = []
    selectors = [
        "#feature-bullets li span.a-list-item",
        "#feature-bullets li",
        "#productFacts_feature_div li",
        "#productFactsDesktop_feature_div li",
        "#productFactsDesktopExpander_feature_div li",
        "#productFactsMobile_feature_div li",
        "#productFactsMobileExpander_feature_div li",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            text = normalize_whitespace(node.get_text(" ", strip=True))
            if text and text not in bullets:
                bullets.append(text)
    return bullets


def extract_description_text(soup: BeautifulSoup) -> str:
    """Extract merged long-form descriptive text from known description zones."""
    selectors = [
        "#productDescription",
        "#aplus",
        "#bookDescription_feature_div",
        "#feature_div",
        "#important-information",
        "#pqv-important-information",
    ]
    chunks: List[str] = []
    seen = set()
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        text = normalize_whitespace(node.get_text(" ", strip=True))
        if text and text not in seen:
            seen.add(text)
            chunks.append(text)
    return " ".join(chunks).strip()


def extract_facts_table(soup: BeautifulSoup) -> str:
    """Extract merged plain text from product overview and detail sections."""
    selectors = [
        "#productOverview_feature_div",
        "#prodDetails",
        "#detailBullets_feature_div",
        "#technicalSpecifications_feature_div",
        "#productFacts_feature_div",
        "#productFactsDesktop_feature_div",
        "#productFactsDesktopExpander_feature_div",
        "#productFactsMobile_feature_div",
        "#productFactsMobileExpander_feature_div",
        "#nic-po-expander-content",
        "#item_details",
    ]
    chunks: List[str] = []
    seen = set()
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        rows = _extract_facts_rows(node)
        if rows:
            for row in rows:
                if row not in seen:
                    seen.add(row)
                    chunks.append(row)
            continue

        text = normalize_whitespace(node.get_text(" ", strip=True))
        if text and text not in seen:
            seen.add(text)
            chunks.append(text)
    facts_text = "\n".join(chunks).strip()
    LOGGER.debug("Parsed facts table rows=%d chars=%d", len(chunks), len(facts_text))
    return facts_text


def extract_variations(soup: BeautifulSoup) -> str:
    """Extract plain text for currently selected variation labels/values."""
    values: List[str] = []
    seen = set()
    selected_inputs = soup.select("input[aria-checked='true']")
    for selected_input in selected_inputs:
        label_ids = (selected_input.get("aria-labelledby") or "").split()
        for label_id in label_ids:
            label_node = soup.select_one(f"#{label_id}")
            if not label_node:
                continue
            text = normalize_whitespace(label_node.get_text(" ", strip=True))
            if text and text not in seen:
                seen.add(text)
                values.append(text)

        input_id = selected_input.get("id")
        if input_id:
            for label_node in soup.select(f'label[for="{input_id}"]'):
                text = normalize_whitespace(label_node.get_text(" ", strip=True))
                if text and text not in seen:
                    seen.add(text)
                    values.append(text)

    variations_text = " ".join(values).strip()
    LOGGER.debug("Parsed selected variations chars=%d", len(variations_text))
    return variations_text


def _extract_facts_rows(node: Tag) -> List[str]:
    rows: List[str] = []
    seen = set()
    candidate_groups = (
        node.find_all("tr"),
        node.find_all("li"),
        node.find_all("dl"),
        node.find_all(attrs={"role": "listitem"}),
        node.select("div.a-fixed-left-grid"),
        node.select("div.a-fixed-left-grid-inner"),
    )

    for candidates in candidate_groups:
        local_rows = []
        for candidate in candidates:
            row = _normalize_fact_row(candidate)
            if row and row not in seen:
                seen.add(row)
                local_rows.append(row)
        if local_rows:
            rows.extend(local_rows)
            break

    return rows


def _normalize_fact_row(node: Tag) -> str:
    if node.name == "tr":
        cells = [
            normalize_whitespace(cell.get_text(" ", strip=True))
            for cell in node.find_all(["th", "td"], recursive=False)
        ]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            return f"{cells[0]}: {' '.join(cells[1:])}"

    if node.name == "dl":
        label_parts = [
            normalize_whitespace(cell.get_text(" ", strip=True))
            for cell in node.find_all("dt", recursive=False)
        ]
        value_parts = [
            normalize_whitespace(cell.get_text(" ", strip=True))
            for cell in node.find_all("dd", recursive=False)
        ]
        label = " ".join(part for part in label_parts if part)
        value = " ".join(part for part in value_parts if part)
        if label and value:
            return f"{label}: {value}"

    return normalize_whitespace(node.get_text(" ", strip=True))
