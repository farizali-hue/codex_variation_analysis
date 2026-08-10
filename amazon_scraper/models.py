from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass(frozen=True)
class RunFieldContract:
    requested_label: str
    internal_key: str
    output_type: str
    merge_similar_values: bool = False
    matched_by: str = ""


@dataclass(frozen=True)
class ScrapeJob:
    asin: str
    product_type: str = ""
    requested_fields: List[str] = field(default_factory=list)
    field_contracts: List[RunFieldContract] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkRecord:
    asin: str
    section: str
    text: str
    dom_hint: str
    order: int
    source_priority: int


@dataclass
class FieldResult:
    value: str = ""
    source: str = ""
    confidence: float = 0.0
    evidence: str = ""


@dataclass
class SupplementFactsMetadataEntry:
    label: str
    raw_metadata_text: str = ""
    final_csv_value: str = ""


@dataclass
class SupplementFactsIngredient:
    name: str
    raw_amount_text: str = ""
    normalized_amount_mg: float | None = None
    csv_value: str = ""
    category: str = "ingredient"
    parent_blend_name: str = ""
    daily_value_text: str = ""
    footnote_markers: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class SupplementFactsDetail:
    metadata: List[SupplementFactsMetadataEntry] = field(default_factory=list)
    ingredients: List[SupplementFactsIngredient] = field(default_factory=list)
    other_ingredients: str = ""
    unparsed_fragments: List[str] = field(default_factory=list)
    source: str = ""
    confidence: float = 0.0
    evidence: str = ""


@dataclass
class ScrapeResult:
    asin: str
    product_type: str
    status: str
    url: str
    scraped_at: datetime
    title: str = ""
    fields: Dict[str, FieldResult] = field(default_factory=dict)
    error: Optional[str] = None
    supplement_facts_detail: SupplementFactsDetail | None = None
    # Panel extraction is tracked apart from the page fetch: a product must never read
    # as fully successful while its Supplement Facts data is missing.
    page_scrape_status: str = ""
    facts_extraction_status: str = ""   # not_requested|image|html_fallback|no_panel_found|failed
    facts_failure_reason: str = ""
