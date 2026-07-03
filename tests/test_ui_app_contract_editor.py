from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


def _workspace_temp_dir() -> str:
    temp_dir = Path(__file__).resolve().parents[1] / ".cache" / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return str(temp_dir)


class UiAppContractEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_tempdir = tempfile.tempdir
        cls._original_tmp = os.environ.get("TMP")
        cls._original_temp = os.environ.get("TEMP")

        workspace_temp = _workspace_temp_dir()
        tempfile.tempdir = workspace_temp
        os.environ["TMP"] = workspace_temp
        os.environ["TEMP"] = workspace_temp

    @classmethod
    def tearDownClass(cls) -> None:
        tempfile.tempdir = cls._original_tempdir
        if cls._original_tmp is None:
            os.environ.pop("TMP", None)
        else:
            os.environ["TMP"] = cls._original_tmp
        if cls._original_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = cls._original_temp

    def test_run_field_contract_editor_refreshes_after_first_edit(self) -> None:
        app = AppTest.from_file("ui_app.py")
        app.session_state["job_rows"] = [
            {
                "asin": "B0TEST1234",
                "product_type": "",
                "required_fields": "capsule counts, flavor",
            }
        ]
        app.session_state["run-field-contract-editor"] = {
            "edited_rows": {
                0: {"Data Type": "Number"},
                1: {"Data Type": "Text", "Merge Similar Values": True},
            },
            "added_rows": [],
            "deleted_rows": [],
        }

        app.run(timeout=10)

        editor_table = self._find_contract_editor_table(app)
        capsule_counts_row = editor_table.loc[editor_table["Internal Key"] == "capsule_counts"].iloc[0]
        flavor_row = editor_table.loc[editor_table["Internal Key"] == "flavor"].iloc[0]

        self.assertEqual(capsule_counts_row["Data Type"], "Number")
        self.assertEqual(flavor_row["Data Type"], "Text")
        self.assertTrue(bool(flavor_row["Merge Similar Values"]))

    def test_sidebar_exposes_extract_supplement_facts_details_checkbox(self) -> None:
        app = AppTest.from_file("ui_app.py")

        app.run(timeout=10)

        checkbox_labels = [checkbox.label for checkbox in app.checkbox]
        self.assertIn("Extract Supplement Facts Details", checkbox_labels)
        self.assertFalse(bool(app.session_state["extract_supplement_facts_details"]))

    def _find_contract_editor_table(self, app: AppTest):
        for dataframe in app.dataframe:
            value = dataframe.value
            if {"Requested Label", "Internal Key", "Matched By", "Data Type", "Merge Similar Values"}.issubset(
                set(value.columns)
            ):
                return value
        self.fail("Could not find the run field contract editor table.")


if __name__ == "__main__":
    unittest.main()
