from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from amazon_scraper.feature_flags import run_scoped_schema_enabled, run_scoped_text_merge_mode


class FeatureFlagTests(unittest.TestCase):
    def test_run_scoped_text_merge_mode_defaults_to_off(self) -> None:
        with patch("amazon_scraper.feature_flags.load_project_dotenv"):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(run_scoped_text_merge_mode(default="off"), "off")

    def test_run_scoped_text_merge_mode_accepts_shadow(self) -> None:
        with patch("amazon_scraper.feature_flags.load_project_dotenv"):
            with patch.dict(os.environ, {"RUN_SCOPED_TEXT_MERGE_MODE": "shadow"}, clear=True):
                self.assertEqual(run_scoped_text_merge_mode(default="off"), "shadow")

    def test_run_scoped_text_merge_mode_accepts_apply(self) -> None:
        with patch("amazon_scraper.feature_flags.load_project_dotenv"):
            with patch.dict(os.environ, {"RUN_SCOPED_TEXT_MERGE_MODE": "apply"}, clear=True):
                self.assertEqual(run_scoped_text_merge_mode(default="off"), "apply")

    def test_run_scoped_text_merge_mode_falls_back_on_invalid_value(self) -> None:
        with patch("amazon_scraper.feature_flags.load_project_dotenv"):
            with patch.dict(os.environ, {"RUN_SCOPED_TEXT_MERGE_MODE": "surprise"}, clear=True):
                self.assertEqual(run_scoped_text_merge_mode(default="shadow"), "shadow")

    def test_run_scoped_schema_enabled_preserves_existing_boolean_behavior(self) -> None:
        with patch("amazon_scraper.feature_flags.load_project_dotenv"):
            with patch.dict(os.environ, {"RUN_SCOPED_SCHEMA_ENABLED": "true"}, clear=True):
                self.assertTrue(run_scoped_schema_enabled(default=False))

    def test_feature_flags_load_repo_root_dotenv_when_process_env_is_empty(self) -> None:
        def _fake_load_dotenv(*, dotenv_path: Path, override: bool) -> bool:
            self.assertEqual(dotenv_path, Path("D:/fake-root/.env"))
            self.assertFalse(override)
            os.environ["RUN_SCOPED_SCHEMA_ENABLED"] = "true"
            os.environ["RUN_SCOPED_TEXT_MERGE_MODE"] = "shadow"
            return True

        with patch("amazon_scraper.project_env.project_root", return_value=Path("D:/fake-root")):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("dotenv.load_dotenv", side_effect=_fake_load_dotenv):
                    with patch.dict(os.environ, {}, clear=True):
                        self.assertTrue(run_scoped_schema_enabled(default=False))
                        self.assertEqual(run_scoped_text_merge_mode(default="off"), "shadow")


if __name__ == "__main__":
    unittest.main()
