from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List

from bs4 import BeautifulSoup
from bs4.element import Tag

from .value_parsers import normalize_whitespace

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactEntry:
    label: str
    value: str
    normalized_label: str


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
        "#productFactsDesktop_feature_div li",
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
    ]
    chunks: List[str] = []
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        text = normalize_whitespace(node.get_text(" ", strip=True))
        if text:
            chunks.append(text)
    return " ".join(chunks).strip()


def extract_facts_table(soup: BeautifulSoup) -> List[FactEntry]:
    """Extract row-based key/value facts from product overview and detail sections."""
    rows = soup.select(
        "#productOverview_feature_div tr, "
        "#prodDetails tr, "
        "#detailBullets_feature_div li, "
        "#technicalSpecifications_feature_div tr"
    )
    entries: List[FactEntry] = []
    seen = set()
    for row in rows:
        label, value = _extract_label_value_from_row(row)
        if not label or not value:
            continue

        normalized_label = normalize_label(label)
        key = (normalized_label, value.lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append(FactEntry(label=label, value=value, normalized_label=normalized_label))
    LOGGER.debug("Parsed facts table entries=%d", len(entries))
    return entries


def extract_variations(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract currently selected variation values using ARIA selected-state markers."""
    variations: Dict[str, str] = {}
    selected_inputs = soup.select("input[aria-checked='true'][aria-labelledby]")
    for selected_input in selected_inputs:
        label_id = selected_input.get("aria-labelledby")
        if not label_id:
            continue
        label_node = soup.select_one(f"#{label_id}")
        if not label_node:
            continue
        value = normalize_whitespace(label_node.get_text(" ", strip=True))
        if not value:
            continue

        dimension = _extract_dimension_hint(label_id, selected_input)
        variations[dimension] = value
    LOGGER.debug("Parsed selected variations count=%d", len(variations))
    return variations


def normalize_label(label: str) -> str:
    normalized = normalize_whitespace(label).lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _extract_label_value_from_row(row: Tag) -> tuple[str, str]:
    if row.name == "li":
        label_node = row.select_one(".a-text-bold")
        if label_node:
            label = normalize_whitespace(label_node.get_text(" ", strip=True)).rstrip(":")
            text = normalize_whitespace(row.get_text(" ", strip=True))
            value = normalize_whitespace(text.replace(label_node.get_text(" ", strip=True), "", 1)).strip(": ")
            return label, value
        return "", ""

    cells = row.find_all(["th", "td"], recursive=False)
    if len(cells) >= 2:
        raw_label = normalize_whitespace(cells[0].get_text(" ", strip=True)).rstrip(":")
        raw_value = normalize_whitespace(cells[1].get_text(" ", strip=True))
        # Some rows keep semantic keys only in class names (e.g., po-caffeine_content_description).
        if not raw_label:
            raw_label = _label_from_row_classes(row)
        if raw_label and raw_value:
            return raw_label, raw_value

    all_cells = row.find_all(["th", "td"])
    if len(all_cells) >= 2:
        raw_label = normalize_whitespace(all_cells[0].get_text(" ", strip=True)).rstrip(":")
        raw_value = normalize_whitespace(all_cells[1].get_text(" ", strip=True))
        if not raw_label:
            raw_label = _label_from_row_classes(row)
        if raw_label and raw_value:
            return raw_label, raw_value

    label = _label_from_row_classes(row)
    value = normalize_whitespace(row.get_text(" ", strip=True))
    if label and value and label.lower() != value.lower():
        return label, value

    return "", ""


def _label_from_row_classes(row: Tag) -> str:
    for class_name in row.get("class", []):
        if class_name.startswith("po-"):
            token = class_name.replace("po-", "").replace("_", " ").replace("-", " ")
            return normalize_whitespace(token)
    return ""


def _extract_dimension_hint(label_id: str, selected_input: Tag) -> str:
    lower_id = label_id.lower()
    if "flavor" in lower_id or "flavour" in lower_id:
        return "flavour"
    if "roast" in lower_id:
        return "roast_level"

    name_attr = (selected_input.get("name") or "").lower()
    if "flavor" in name_attr or "flavour" in name_attr:
        return "flavour"
    return "variation"
