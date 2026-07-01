from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Dict, Iterable, List, Sequence

from .field_hints import get_field_hint, get_supported_field_names
from .models import ScrapeJob
from .product_type_schemas import normalize_product_type


_ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_SPLIT_PATTERN = re.compile(r"[\s,;|]+")
_FIELD_SPLIT_PATTERN = re.compile(r"[\n,;|]+")
_LOOKUP_SANITIZE_PATTERN = re.compile(r"[^a-z0-9]+")
_CUSTOM_FIELD_STOPWORDS = {"the", "a", "an"}


@dataclass(frozen=True)
class NormalizedFieldSelection:
    raw_input: str
    canonical_name: str
    matched_by: str


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    message: str


@dataclass
class JobBuildResult:
    jobs: List[ScrapeJob] = field(default_factory=list)
    field_selections: List[NormalizedFieldSelection] = field(default_factory=list)
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def warnings(self) -> List[str]:
        return [issue.message for issue in self.issues if issue.level == "warning"]

    @property
    def errors(self) -> List[str]:
        return [issue.message for issue in self.issues if issue.level == "error"]


def collect_requested_field_columns(jobs: Iterable[ScrapeJob]) -> List[str]:
    columns: List[str] = []
    for job in jobs:
        for column in job.requested_fields:
            if column not in columns:
                columns.append(column)
        for contract in job.field_contracts:
            if contract.internal_key and contract.internal_key not in columns:
                columns.append(contract.internal_key)
    return columns


def parse_asin_input(raw_value: str) -> tuple[List[str], List[ValidationIssue]]:
    tokens = [token.strip() for token in _ASIN_SPLIT_PATTERN.split(raw_value or "") if token.strip()]
    return normalize_asin_tokens(tokens)


def normalize_asin_tokens(raw_tokens: Sequence[str]) -> tuple[List[str], List[ValidationIssue]]:
    issues: List[ValidationIssue] = []
    asins: List[str] = []

    for raw_token in raw_tokens:
        normalized = normalize_asin(raw_token)
        if normalized is None:
            issues.append(ValidationIssue(level="error", message=f"Invalid ASIN: {raw_token!r}"))
            continue
        if normalized in asins:
            issues.append(ValidationIssue(level="warning", message=f"Duplicate ASIN ignored: {normalized}"))
            continue
        asins.append(normalized)

    if not asins and not issues:
        issues.append(ValidationIssue(level="error", message="No ASINs were provided."))

    return asins, issues


def normalize_asin(raw_value: str) -> str | None:
    cleaned = (raw_value or "").strip().upper()
    if not cleaned:
        return None
    if not _ASIN_PATTERN.fullmatch(cleaned):
        return None
    return cleaned


def split_raw_field_input(raw_value: str) -> List[str]:
    return [token.strip() for token in _FIELD_SPLIT_PATTERN.split(raw_value or "") if token.strip()]


def normalize_requested_field_input(
    raw_value: str,
) -> tuple[List[str], List[NormalizedFieldSelection], List[ValidationIssue]]:
    return normalize_requested_fields(split_raw_field_input(raw_value))


def normalize_requested_fields(raw_fields: Sequence[str]) -> tuple[List[str], List[NormalizedFieldSelection], List[ValidationIssue]]:
    normalized_fields: List[str] = []
    selections: List[NormalizedFieldSelection] = []
    issues: List[ValidationIssue] = []

    for raw_field in raw_fields:
        try:
            selection = resolve_field_name(raw_field)
        except ValueError as exc:
            issues.append(ValidationIssue(level="error", message=str(exc)))
            continue

        if selection.canonical_name in normalized_fields:
            issues.append(
                ValidationIssue(
                    level="warning",
                    message=f"Duplicate field ignored: {selection.canonical_name}",
                )
            )
            continue

        normalized_fields.append(selection.canonical_name)
        selections.append(selection)

    return normalized_fields, selections, issues


def build_jobs_from_user_input(
    *,
    raw_asins: str | Sequence[str],
    raw_fields: str | Sequence[str] | None = None,
    product_type: str = "",
    product_type_defaults: Dict[str, List[str]] | None = None,
) -> JobBuildResult:
    if isinstance(raw_asins, str):
        asins, asin_issues = parse_asin_input(raw_asins)
    else:
        asins, asin_issues = normalize_asin_tokens(list(raw_asins))

    issues = list(asin_issues)
    defaults = product_type_defaults or {}
    normalized_product_type = normalize_product_type(product_type)

    if isinstance(raw_fields, str):
        explicit_fields = split_raw_field_input(raw_fields)
    else:
        explicit_fields = [str(field).strip() for field in (raw_fields or []) if str(field).strip()]

    field_selections: List[NormalizedFieldSelection] = []
    if explicit_fields:
        normalized_fields, field_selections, field_issues = normalize_requested_fields(explicit_fields)
        issues.extend(field_issues)
    else:
        normalized_fields = list(dict.fromkeys(defaults.get(normalized_product_type, [])))
        field_selections = [
            NormalizedFieldSelection(
                raw_input=field_name,
                canonical_name=field_name,
                matched_by="product_type_default",
            )
            for field_name in normalized_fields
        ]
        if not normalized_fields:
            issues.append(
                ValidationIssue(
                    level="error",
                    message="No required fields were provided and no product-type defaults were found.",
                )
            )

    jobs = [
        ScrapeJob(asin=asin, product_type=product_type.strip(), requested_fields=list(normalized_fields))
        for asin in asins
        if normalized_fields
    ]

    return JobBuildResult(jobs=jobs, field_selections=field_selections, issues=issues)


def build_jobs_from_table_rows(
    rows: Sequence[dict[str, object]],
    product_type_defaults: Dict[str, List[str]] | None = None,
) -> JobBuildResult:
    result = JobBuildResult()
    defaults = product_type_defaults or {}

    for row_index, row in enumerate(rows, start=1):
        asin = str(row.get("asin", "") or "").strip()
        product_type = str(row.get("product_type", "") or "").strip()
        raw_fields = row.get("required_fields", "") or row.get("fields", "") or ""

        row_result = build_jobs_from_user_input(
            raw_asins=[asin],
            raw_fields=str(raw_fields),
            product_type=product_type,
            product_type_defaults=defaults,
        )
        result.jobs.extend(row_result.jobs)
        result.field_selections.extend(row_result.field_selections)

        row_prefix = f"Row {row_index}"
        if asin:
            row_prefix = f"{row_prefix} ({asin})"
        for issue in row_result.issues:
            result.issues.append(
                ValidationIssue(level=issue.level, message=f"{row_prefix}: {issue.message}")
            )

    if not rows:
        result.issues.append(ValidationIssue(level="error", message="No job rows were provided."))

    return result


def resolve_field_name(raw_field: str) -> NormalizedFieldSelection:
    raw_input = (raw_field or "").strip()
    if not raw_input:
        raise ValueError("Field name cannot be empty.")

    normalized_input = _normalize_lookup_token(raw_input)
    alias_map = _build_field_resolution_alias_map()
    exact_fields = alias_map.get(normalized_input, set())
    if len(exact_fields) == 1:
        canonical_name = next(iter(exact_fields))
        matched_by = "canonical" if canonical_name == raw_input.strip().lower() else "alias"
        return NormalizedFieldSelection(raw_input=raw_input, canonical_name=canonical_name, matched_by=matched_by)
    if len(exact_fields) > 1:
        choices = ", ".join(sorted(exact_fields))
        raise ValueError(f"Ambiguous field name {raw_input!r}. Choose one of: {choices}.")

    fuzzy_fields = _resolve_fuzzy_field_matches(normalized_input, alias_map)
    if len(fuzzy_fields) == 1:
        canonical_name = next(iter(fuzzy_fields))
        return NormalizedFieldSelection(raw_input=raw_input, canonical_name=canonical_name, matched_by="fuzzy")
    if len(fuzzy_fields) > 1:
        choices = ", ".join(sorted(fuzzy_fields))
        raise ValueError(f"Ambiguous field name {raw_input!r}. Did you mean one of: {choices}?")

    canonical_name = _generate_custom_field_name(raw_input)
    if not canonical_name:
        suggestions = ", ".join(_suggest_fields(normalized_input, alias_map))
        if suggestions:
            raise ValueError(f"Unknown field {raw_input!r}. Did you mean: {suggestions}?")
        raise ValueError(f"Unknown field {raw_input!r}.")
    return NormalizedFieldSelection(
        raw_input=raw_input,
        canonical_name=canonical_name,
        matched_by="generated",
    )


def get_supported_field_options() -> List[dict[str, str]]:
    options: List[dict[str, str]] = []
    for field_name in get_supported_field_names():
        hint = get_field_hint(field_name)
        options.append(
            {
                "canonical_name": field_name,
                "display_name": hint.display_name or field_name.replace("_", " ").title(),
            }
        )
    return options


def _build_field_resolution_alias_map() -> Dict[str, set[str]]:
    alias_map: Dict[str, set[str]] = {}
    for field_name in get_supported_field_names():
        for alias in _iter_field_resolution_aliases(field_name):
            alias_map.setdefault(_normalize_lookup_token(alias), set()).add(field_name)
    return alias_map


def _iter_field_resolution_aliases(field_name: str) -> Iterable[str]:
    hint = get_field_hint(field_name)
    canonical_variants = {
        field_name,
        field_name.replace("_", " "),
        field_name.replace("_", "-"),
        field_name.replace("_", "").lower(),
    }
    for alias in canonical_variants:
        if alias:
            yield alias
    for alias in hint.input_aliases:
        if alias:
            yield alias


def _normalize_lookup_token(value: str) -> str:
    collapsed = _LOOKUP_SANITIZE_PATTERN.sub(" ", (value or "").strip().lower())
    return " ".join(collapsed.split())


def _resolve_fuzzy_field_matches(normalized_input: str, alias_map: Dict[str, set[str]]) -> set[str]:
    if len(normalized_input) < 5:
        return set()
    if " " not in normalized_input:
        return set()

    close_aliases = get_close_matches(normalized_input, alias_map.keys(), n=3, cutoff=0.84)
    matched_fields: set[str] = set()
    for alias in close_aliases:
        matched_fields.update(alias_map.get(alias, set()))
    return matched_fields


def _suggest_fields(normalized_input: str, alias_map: Dict[str, set[str]]) -> List[str]:
    suggestions: List[str] = []
    close_aliases = get_close_matches(normalized_input, alias_map.keys(), n=5, cutoff=0.65)
    for alias in close_aliases:
        for field_name in sorted(alias_map.get(alias, set())):
            if field_name not in suggestions:
                suggestions.append(field_name)
    return suggestions[:3]


def _generate_custom_field_name(raw_input: str) -> str:
    normalized_input = _normalize_lookup_token(raw_input)
    tokens = [token for token in normalized_input.split() if token and token not in _CUSTOM_FIELD_STOPWORDS]
    if not tokens:
        return ""

    if len(tokens) >= 2 and tokens[0] in {"no", "num", "number"}:
        if tokens[1] == "of":
            tokens = ["number", "of"] + tokens[2:]
        else:
            tokens = ["number", "of"] + tokens[1:]

    if len(tokens) >= 2 and tokens[0] == "qty":
        tokens = ["quantity", "of"] + tokens[1:]

    canonical_name = "_".join(tokens).strip("_")
    if len(canonical_name) < 2:
        return ""
    return canonical_name
