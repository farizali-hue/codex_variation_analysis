from __future__ import annotations

import unittest

from amazon_scraper.input_normalization import NormalizedFieldSelection
from amazon_scraper.models import RunFieldContract, ScrapeJob
from amazon_scraper.run_field_contracts import (
    OUTPUT_TYPE_LABELS,
    UNSELECTED_OUTPUT_TYPE_LABEL,
    attach_run_field_contracts_to_jobs,
    build_supported_field_contract,
    build_generated_contract_editor_rows,
    deserialize_run_field_contracts,
    get_job_field_contract_map,
    parse_run_field_contract_rows,
    resolve_job_requested_fields,
    serialize_run_field_contracts,
)


class RunFieldContractsTests(unittest.TestCase):
    def test_serialize_and_deserialize_round_trip(self) -> None:
        contracts = {
            "flavor": RunFieldContract(
                requested_label="Flavor",
                internal_key="flavor",
                output_type="text",
                merge_similar_values=True,
                matched_by="generated",
            )
        }

        serialized = serialize_run_field_contracts(contracts)
        loaded = deserialize_run_field_contracts(serialized)

        self.assertEqual(loaded, contracts)

    def test_build_supported_field_contract_uses_fixed_builtin_output_type(self) -> None:
        self.assertEqual(
            build_supported_field_contract("ingredients"),
            RunFieldContract(
                requested_label="Ingredients",
                internal_key="ingredients",
                output_type="short_list",
                merge_similar_values=False,
                matched_by="supported",
            ),
        )

    def test_build_generated_contract_editor_rows_uses_unselected_placeholder_by_default(self) -> None:
        rows = build_generated_contract_editor_rows(
            [
                NormalizedFieldSelection(
                    raw_input="Flavor",
                    canonical_name="flavor",
                    matched_by="generated",
                )
            ],
            {},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Data Type"], UNSELECTED_OUTPUT_TYPE_LABEL)

    def test_build_generated_contract_editor_rows_preserves_existing_number_selection(self) -> None:
        rows = build_generated_contract_editor_rows(
            [
                NormalizedFieldSelection(
                    raw_input="Capsule count",
                    canonical_name="capsule_count",
                    matched_by="generated",
                )
            ],
            {},
            existing_rows=[
                {
                    "Requested Label": "Capsule count",
                    "Internal Key": "capsule_count",
                    "Matched By": "generated",
                    "Data Type": OUTPUT_TYPE_LABELS["number"],
                    "Merge Similar Values": False,
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Data Type"], OUTPUT_TYPE_LABELS["number"])

    def test_build_generated_contract_editor_rows_preserves_existing_merge_toggle(self) -> None:
        rows = build_generated_contract_editor_rows(
            [
                NormalizedFieldSelection(
                    raw_input="Flavor",
                    canonical_name="flavor",
                    matched_by="generated",
                )
            ],
            {},
            existing_rows=[
                {
                    "Requested Label": "Flavor",
                    "Internal Key": "flavor",
                    "Matched By": "generated",
                    "Data Type": UNSELECTED_OUTPUT_TYPE_LABEL,
                    "Merge Similar Values": True,
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["Merge Similar Values"])

    def test_parse_run_field_contract_rows_returns_missing_fields_for_untyped_rows(self) -> None:
        contracts, missing = parse_run_field_contract_rows(
            [
                {
                    "Requested Label": "Flavor",
                    "Internal Key": "flavor",
                    "Matched By": "generated",
                    "Data Type": UNSELECTED_OUTPUT_TYPE_LABEL,
                    "Merge Similar Values": False,
                }
            ]
        )

        self.assertEqual(contracts, {})
        self.assertEqual(missing, ["Flavor"])

    def test_parse_run_field_contract_rows_builds_contracts_for_selected_types(self) -> None:
        contracts, missing = parse_run_field_contract_rows(
            [
                {
                    "Requested Label": "Capsule counts",
                    "Internal Key": "capsule_counts",
                    "Matched By": "generated",
                    "Data Type": OUTPUT_TYPE_LABELS["number"],
                    "Merge Similar Values": False,
                }
            ]
        )

        self.assertEqual(missing, [])
        self.assertEqual(
            contracts["capsule_counts"],
            RunFieldContract(
                requested_label="Capsule counts",
                internal_key="capsule_counts",
                output_type="number",
                merge_similar_values=False,
                matched_by="generated",
            ),
        )

    def test_attach_run_field_contracts_to_jobs_enriches_requested_fields_only(self) -> None:
        jobs = [
            ScrapeJob(
                asin="B0TESTING1",
                requested_fields=["flavor", "ingredients"],
            )
        ]
        contracts = {
            "flavor": RunFieldContract(
                requested_label="Flavor",
                internal_key="flavor",
                output_type="text",
                merge_similar_values=True,
                matched_by="generated",
            ),
            "unrequested": RunFieldContract(
                requested_label="Unused",
                internal_key="unrequested",
                output_type="text",
            ),
        }

        enriched_jobs = attach_run_field_contracts_to_jobs(jobs, contracts)

        self.assertEqual(len(enriched_jobs), 1)
        self.assertEqual([contract.internal_key for contract in enriched_jobs[0].field_contracts], ["flavor"])

    def test_resolve_job_requested_fields_uses_contracts_when_enabled(self) -> None:
        job = ScrapeJob(
            asin="B0CONTRACTS",
            requested_fields=[],
            field_contracts=[
                RunFieldContract(
                    requested_label="Flavor",
                    internal_key="flavor",
                    output_type="text",
                    matched_by="generated",
                )
            ],
        )

        self.assertEqual(
            resolve_job_requested_fields(job, run_scoped_schema_enabled=True),
            ["flavor"],
        )

    def test_get_job_field_contract_map_is_empty_when_flag_disabled(self) -> None:
        job = ScrapeJob(
            asin="B0CONTRACTS",
            field_contracts=[
                RunFieldContract(
                    requested_label="Flavor",
                    internal_key="flavor",
                    output_type="text",
                    matched_by="generated",
                )
            ],
        )

        self.assertEqual(get_job_field_contract_map(job, run_scoped_schema_enabled=False), {})

    def test_get_job_field_contract_map_includes_supported_fields_when_flag_disabled(self) -> None:
        job = ScrapeJob(
            asin="B0BUILTINS",
            requested_fields=["packaging_type", "ingredients", "serving_size", "servings_per_container"],
        )

        contract_map = get_job_field_contract_map(job, run_scoped_schema_enabled=False)

        self.assertEqual(
            {field_name: contract.output_type for field_name, contract in contract_map.items()},
            {
                "packaging_type": "text",
                "ingredients": "short_list",
                "serving_size": "number",
                "servings_per_container": "number",
            },
        )


if __name__ == "__main__":
    unittest.main()
