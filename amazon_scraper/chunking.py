from __future__ import annotations

from typing import List

from .models import ChunkRecord
from .value_parsers import normalize_whitespace


_SECTION_PRIORITY = {
    "variations": 100,
    "facts_table": 90,
    "bullets": 75,
    "description": 60,
    "title": 50,
}


def build_chunk_records(
    asin: str,
    title: str,
    facts_table: str,
    bullets: List[str],
    description: str,
    variations: str,
) -> List[ChunkRecord]:
    chunks: List[ChunkRecord] = []
    order = 0

    def append_chunk(section: str, text: str, dom_hint: str) -> None:
        nonlocal order
        normalized_text = normalize_whitespace(text)
        if not normalized_text:
            return
        chunks.append(
            ChunkRecord(
                asin=asin,
                section=section,
                text=normalized_text,
                dom_hint=dom_hint,
                order=order,
                source_priority=_SECTION_PRIORITY.get(section, 0),
            )
        )
        order += 1

    append_chunk("title", title, "#productTitle")

    for index, row in enumerate(_split_facts_table(facts_table), start=1):
        append_chunk("facts_table", row, f"facts-table-{index}")

    for index, bullet in enumerate(bullets, start=1):
        append_chunk("bullets", bullet, f"feature-bullet-{index}")

    append_chunk("variations", variations, "selected-variations")

    for index, sentence in enumerate(_split_description(description), start=1):
        append_chunk("description", sentence, f"description-{index}")

    return chunks


def _split_facts_table(facts_table: str) -> List[str]:
    return [line for line in (normalize_whitespace(part) for part in facts_table.splitlines()) if line]


def _split_description(description: str, max_chars: int = 500) -> List[str]:
    normalized = normalize_whitespace(description)
    if not normalized:
        return []

    sentences: List[str] = []
    current = ""
    for part in normalized.split(". "):
        sentence = part.strip()
        if not sentence:
            continue
        if current:
            candidate = f"{current}. {sentence}"
        else:
            candidate = sentence
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            sentences.append(current)
        current = sentence

    if current:
        sentences.append(current)

    return [sentence if sentence.endswith(".") else f"{sentence}." for sentence in sentences]
