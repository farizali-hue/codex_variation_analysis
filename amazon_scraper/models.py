from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ScrapeJob:
    asin: str
    product_type: str = ""
    requested_fields: List[str] = field(default_factory=list)


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
class ScrapeResult:
    asin: str
    product_type: str
    status: str
    url: str
    scraped_at: datetime
    title: str = ""
    fields: Dict[str, FieldResult] = field(default_factory=dict)
    error: Optional[str] = None
