from __future__ import annotations

import types
import unittest

import ui_app


class _FakeHandle:
    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> dict:
        return self._snapshot


class SyncActiveRunSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_st = ui_app.st
        self._orig_get = ui_app.get_run_handle
        self._orig_remove = ui_app.remove_run_handle
        self._removed: list[str] = []
        ui_app.remove_run_handle = lambda run_id: self._removed.append(run_id)

    def tearDown(self) -> None:
        ui_app.st = self._orig_st
        ui_app.get_run_handle = self._orig_get
        ui_app.remove_run_handle = self._orig_remove

    def _install(self, session: dict, snapshot: dict) -> None:
        ui_app.st = types.SimpleNamespace(session_state=session)
        ui_app.get_run_handle = lambda run_id: _FakeHandle(snapshot)

    @staticmethod
    def _session_with_prior_results(active_run_id: str) -> dict:
        return {
            "active_run_id": active_run_id,
            "run_rows": [{"asin": "OLD"}],
            "run_output_path": "outputs/old.csv",
            "run_download_name": "old.csv",
            "run_download_bytes": b"old-bytes",
            "run_status": "queued",
            "run_message": "",
            "run_error": "",
            "run_started_at": "",
            "run_finished_at": "",
        }

    def test_empty_terminal_run_clears_stale_results(self) -> None:
        session = self._session_with_prior_results("run-2")
        snapshot = {
            "state": "error",
            "message": "OpenAI configuration error",
            "error": "missing api key",
            "started_at": "t0",
            "finished_at": "t1",
            "rows": [],
            "output_path_text": "",
            "download_name": "",
            "download_bytes": b"",
        }
        self._install(session, snapshot)

        ui_app._sync_active_run_snapshot()

        self.assertEqual(session["run_rows"], [])
        self.assertEqual(session["run_output_path"], "")
        self.assertEqual(session["run_download_name"], "")
        self.assertEqual(session["run_download_bytes"], b"")
        self.assertEqual(session["run_status"], "error")
        self.assertEqual(session["active_run_id"], "")
        self.assertEqual(self._removed, ["run-2"])

    def test_terminal_run_with_rows_updates_results(self) -> None:
        session = self._session_with_prior_results("run-3")
        snapshot = {
            "state": "completed",
            "message": "Done",
            "error": "",
            "started_at": "t0",
            "finished_at": "t1",
            "rows": [{"asin": "NEW"}],
            "output_path_text": "outputs/new.csv",
            "download_name": "new.csv",
            "download_bytes": b"new-bytes",
        }
        self._install(session, snapshot)

        ui_app._sync_active_run_snapshot()

        self.assertEqual(session["run_rows"], [{"asin": "NEW"}])
        self.assertEqual(session["run_output_path"], "outputs/new.csv")
        self.assertEqual(session["run_download_name"], "new.csv")
        self.assertEqual(session["run_download_bytes"], b"new-bytes")
        self.assertEqual(session["active_run_id"], "")

    def test_get_active_run_handle_exposes_terminal_handles_for_sync(self) -> None:
        # The 2s run-controls fragment resolves the handle first; if terminal
        # handles are hidden (None), its sync branch is unreachable and finished
        # results stay off-screen until an unrelated full rerun.
        session = self._session_with_prior_results("run-4")
        snapshot = {
            "state": "completed",
            "message": "Done",
            "error": "",
            "started_at": "t0",
            "finished_at": "t1",
            "rows": [{"asin": "NEW"}],
            "output_path_text": "outputs/new.csv",
            "download_name": "new.csv",
            "download_bytes": b"new-bytes",
        }
        self._install(session, snapshot)

        handle = ui_app._get_active_run_handle()

        self.assertIsNotNone(handle)
        self.assertEqual(str(handle.snapshot()["state"]), "completed")


if __name__ == "__main__":
    unittest.main()
