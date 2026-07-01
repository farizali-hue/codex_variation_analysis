from __future__ import annotations

from typing import Iterable, Mapping, TYPE_CHECKING

from .field_hints import get_field_hint, get_supported_field_output_type
from .models import RunFieldContract, ScrapeJob

if TYPE_CHECKING:  # pragma: no cover
    from .input_normalization import NormalizedFieldSelection


OUTPUT_TYPE_LABELS = {
    "number": "Number",
    "short_list": "Short list",
    "text": "Text",
}
OUTPUT_TYPE_HELP = {
    "number": "One numeric value only, like 30 or 500.",
    "short_list": "Multiple items stored in one cell and joined with ' | '.",
    "text": "One cleaned phrase or value, like vanilla or unflavored.",
}
MERGE_SIMILAR_VALUES_HELP = (
    "Controls whether similar text values for this field should be conservatively "
    "canonicalized together within the current run."
)
UNSELECTED_OUTPUT_TYPE_LABEL = "Select data type"


def build_supported_field_contract(field_name: str) -> RunFieldContract | None:
    internal_key = str(field_name or "").strip()
    if not internal_key:
        return None
    output_type = get_supported_field_output_type(internal_key)
    if output_type not in OUTPUT_TYPE_LABELS:
        return None
    hint = get_field_hint(internal_key)
    return RunFieldContract(
        requested_label=hint.display_name or internal_key,
        internal_key=internal_key,
        output_type=output_type,
        merge_similar_values=False,
        matched_by="supported",
    )


def serialize_run_field_contracts(
    contracts: Mapping[str, RunFieldContract],
) -> dict[str, dict[str, object]]:
    return {
        field_name: {
            "requested_label": contract.requested_label,
            "internal_key": contract.internal_key,
            "output_type": contract.output_type,
            "merge_similar_values": contract.merge_similar_values,
            "matched_by": contract.matched_by,
        }
        for field_name, contract in contracts.items()
    }


def deserialize_run_field_contracts(
    raw_contracts: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, RunFieldContract]:
    contracts: dict[str, RunFieldContract] = {}
    for field_name, payload in (raw_contracts or {}).items():
        if not isinstance(payload, Mapping):
            continue
        internal_key = str(payload.get("internal_key", field_name) or field_name).strip()
        output_type = str(payload.get("output_type", "") or "").strip().lower()
        if output_type not in OUTPUT_TYPE_LABELS:
            continue
        contracts[internal_key] = RunFieldContract(
            requested_label=str(payload.get("requested_label", internal_key) or internal_key).strip(),
            internal_key=internal_key,
            output_type=output_type,
            merge_similar_values=bool(payload.get("merge_similar_values", False)),
            matched_by=str(payload.get("matched_by", "") or ""),
        )
    return contracts


def build_generated_contract_editor_rows(
    selections: Iterable["NormalizedFieldSelection"],
    current_contracts: Mapping[str, RunFieldContract],
    existing_rows: Iterable[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    existing_rows_by_key: dict[str, Mapping[str, object]] = {}
    for row in existing_rows or ():
        internal_key = str(row.get("Internal Key", "") or "").strip()
        if internal_key:
            existing_rows_by_key[internal_key] = row

    rows: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for selection in selections:
        if selection.matched_by != "generated":
            continue
        if selection.canonical_name in seen_keys:
            continue
        seen_keys.add(selection.canonical_name)

        contract = current_contracts.get(selection.canonical_name)
        existing_row = existing_rows_by_key.get(selection.canonical_name)

        data_type_label = UNSELECTED_OUTPUT_TYPE_LABEL
        if contract:
            data_type_label = OUTPUT_TYPE_LABELS.get(
                contract.output_type,
                UNSELECTED_OUTPUT_TYPE_LABEL,
            )
        if existing_row:
            existing_label = str(
                existing_row.get("Data Type", data_type_label) or data_type_label
            ).strip()
            if (
                existing_label == UNSELECTED_OUTPUT_TYPE_LABEL
                or existing_label in OUTPUT_TYPE_LABELS.values()
            ):
                data_type_label = existing_label

        merge_similar_values = contract.merge_similar_values if contract else False
        if existing_row:
            merge_similar_values = bool(
                existing_row.get("Merge Similar Values", merge_similar_values)
            )

        rows.append(
            {
                "Requested Label": selection.raw_input,
                "Internal Key": selection.canonical_name,
                "Matched By": selection.matched_by,
                "Data Type": data_type_label,
                "Merge Similar Values": merge_similar_values,
            }
        )
    return rows


def parse_run_field_contract_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[dict[str, RunFieldContract], list[str]]:
    contracts: dict[str, RunFieldContract] = {}
    missing_fields: list[str] = []
    output_type_by_label = {label: key for key, label in OUTPUT_TYPE_LABELS.items()}

    for row in rows:
        requested_label = str(row.get("Requested Label", "") or "").strip()
        internal_key = str(row.get("Internal Key", "") or "").strip()
        matched_by = str(row.get("Matched By", "") or "").strip()
        output_label = str(row.get("Data Type", UNSELECTED_OUTPUT_TYPE_LABEL) or UNSELECTED_OUTPUT_TYPE_LABEL).strip()
        merge_similar_values = bool(row.get("Merge Similar Values", False))

        if not internal_key:
            continue
        if output_label == UNSELECTED_OUTPUT_TYPE_LABEL or output_label not in output_type_by_label:
            missing_fields.append(requested_label or internal_key)
            continue

        contracts[internal_key] = RunFieldContract(
            requested_label=requested_label or internal_key,
            internal_key=internal_key,
            output_type=output_type_by_label[output_label],
            merge_similar_values=merge_similar_values,
            matched_by=matched_by,
        )
    return contracts, missing_fields


def attach_run_field_contracts_to_jobs(
    jobs: Iterable[ScrapeJob],
    contracts: Mapping[str, RunFieldContract],
) -> list[ScrapeJob]:
    enriched_jobs: list[ScrapeJob] = []
    for job in jobs:
        job_contracts = [
            contracts[field_name]
            for field_name in job.requested_fields
            if field_name in contracts
        ]
        enriched_jobs.append(
            ScrapeJob(
                asin=job.asin,
                product_type=job.product_type,
                requested_fields=list(job.requested_fields),
                field_contracts=job_contracts,
            )
        )
    return enriched_jobs


def get_job_field_contract_map(
    job: ScrapeJob,
    *,
    run_scoped_schema_enabled: bool,
) -> dict[str, RunFieldContract]:
    contract_map: dict[str, RunFieldContract] = {}
    for field_name in job.requested_fields:
        supported_contract = build_supported_field_contract(field_name)
        if supported_contract is not None:
            contract_map[supported_contract.internal_key] = supported_contract

    if not run_scoped_schema_enabled:
        return contract_map

    for contract in job.field_contracts:
        if not contract.internal_key:
            continue
        contract_map[contract.internal_key] = contract
    return contract_map


def resolve_job_requested_fields(
    job: ScrapeJob,
    *,
    run_scoped_schema_enabled: bool,
) -> list[str]:
    resolved_fields: list[str] = []
    for field_name in job.requested_fields:
        normalized = str(field_name or "").strip()
        if normalized and normalized not in resolved_fields:
            resolved_fields.append(normalized)

    if run_scoped_schema_enabled:
        for contract in job.field_contracts:
            normalized = str(contract.internal_key or "").strip()
            if normalized and normalized not in resolved_fields:
                resolved_fields.append(normalized)

    if resolved_fields:
        return resolved_fields

    raise RuntimeError(f"Job for asin {job.asin} must include required_fields")
