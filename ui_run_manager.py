"""Background execution helpers for Streamlit batch runs.

This module keeps the UI responsive while a scraper batch is running. It owns
run state, cancellation, progress snapshots, and download-ready results.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from amazon_scraper.csv_export import build_result_rows, write_results_csv
from amazon_scraper.logging_utils import configure_logging
from amazon_scraper.openai_client import OpenAIConfig, OpenAIConfigError, OpenAIBudgetExceededError
from amazon_scraper.scraper import AmazonScraper, DriverSessionLostError, ScrapeCancelledError

ACTIVE_RUN_STATES = {"queued", "running", "stopping"}
TERMINAL_RUN_STATES = {"completed", "stopped", "error"}

_RUN_REGISTRY: dict[str, "BatchRunHandle"] = {}
_RUN_REGISTRY_LOCK = threading.Lock()


@dataclass
class BatchRunHandle:
    run_id: str
    total_jobs: int
    export_columns: list[str]
    output_path: Path
    extract_supplement_facts_details: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None
    scraper: AmazonScraper | None = None
    state: str = "queued"
    message: str = ""
    error: str = ""
    completed_jobs: int = 0
    current_asin: str = ""
    started_at: str = ""
    finished_at: str = ""
    rows: list[dict[str, str]] = field(default_factory=list)
    output_path_text: str = ""
    download_name: str = ""
    download_bytes: bytes = b""
    stop_requested: bool = False

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "run_id": self.run_id,
                "total_jobs": self.total_jobs,
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "completed_jobs": self.completed_jobs,
                "current_asin": self.current_asin,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "rows": list(self.rows),
                "output_path_text": self.output_path_text,
                "download_name": self.download_name,
                "download_bytes": self.download_bytes,
                "stop_requested": self.stop_requested,
                "extract_supplement_facts_details": self.extract_supplement_facts_details,
            }


def start_batch_run(
    *,
    jobs: Iterable,
    output_path: Path,
    export_columns: list[str],
    headless: bool,
    extract_supplement_facts_details: bool,
    debug_html_dir: str | None,
    delay_seconds: float,
    log_level: str,
) -> BatchRunHandle:
    job_list = list(jobs)
    resolved_output_path = _resolve_run_output_path(
        output_path,
        extract_supplement_facts_details=extract_supplement_facts_details,
    )
    handle = BatchRunHandle(
        run_id=uuid4().hex,
        total_jobs=len(job_list),
        export_columns=export_columns,
        output_path=resolved_output_path,
        extract_supplement_facts_details=extract_supplement_facts_details,
        state="queued",
        message=f"Queued {len(job_list)} ASIN job(s).",
    )
    worker = threading.Thread(
        target=_run_batch_worker,
        kwargs={
            "handle": handle,
            "jobs": job_list,
            "headless": headless,
            "extract_supplement_facts_details": extract_supplement_facts_details,
            "debug_html_dir": debug_html_dir,
            "delay_seconds": delay_seconds,
            "log_level": log_level,
        },
        daemon=True,
        name=f"streamlit-batch-{handle.run_id[:8]}",
    )
    handle.thread = worker
    _store_run_handle(handle)
    worker.start()
    return handle


def request_stop(run_id: str) -> None:
    handle = get_run_handle(run_id)
    if handle is None:
        return

    with handle.lock:
        handle.stop_requested = True
        handle.state = "stopping"
        handle.message = "Stop requested. Closing the active browser session."
        scraper = handle.scraper

    handle.cancel_event.set()
    if scraper is not None:
        scraper.cancel()


def get_run_handle(run_id: str) -> BatchRunHandle | None:
    with _RUN_REGISTRY_LOCK:
        return _RUN_REGISTRY.get(run_id)


def remove_run_handle(run_id: str) -> None:
    with _RUN_REGISTRY_LOCK:
        _RUN_REGISTRY.pop(run_id, None)


def _run_batch_worker(
    *,
    handle: BatchRunHandle,
    jobs,
    headless: bool,
    extract_supplement_facts_details: bool,
    debug_html_dir: str | None,
    delay_seconds: float,
    log_level: str,
) -> None:
    _update_run_handle(
        handle,
        state="running",
        started_at=_utc_now_iso(),
        message=f"Preparing browser session for {handle.total_jobs} ASIN job(s).",
        error="",
    )
    try:
        configure_logging(
            level=log_level,
            log_file="logs/streamlit_scraper.log",
            console=True,
        )
        openai_config = OpenAIConfig.from_env()
        scraper = AmazonScraper(
            headless=headless,
            openai_config=openai_config,
            debug_html_dir=debug_html_dir,
            extract_supplement_facts_details=extract_supplement_facts_details,
            cancel_event=handle.cancel_event,
            progress_callback=lambda event, completed_jobs, total_jobs, asin: _handle_progress_update(
                handle,
                event=event,
                completed_jobs=completed_jobs,
                total_jobs=total_jobs,
                asin=asin,
            ),
        )
        _update_run_handle(handle, scraper=scraper)
        results = scraper.run_batch(jobs, delay_seconds=delay_seconds)
        _persist_results_to_handle(handle, results)
        _update_run_handle(
            handle,
            state="completed",
            message=f"Batch finished. Results written to {handle.output_path}.",
            finished_at=_utc_now_iso(),
            current_asin="",
        )
    except ScrapeCancelledError as exc:
        _persist_results_to_handle(handle, getattr(exc, "partial_results", []) or [])
        completed_jobs = int(handle.snapshot()["completed_jobs"])
        _update_run_handle(
            handle,
            state="stopped",
            message=f"Batch stopped after {completed_jobs} of {handle.total_jobs} job(s).",
            finished_at=_utc_now_iso(),
            current_asin="",
        )
    except OpenAIBudgetExceededError as exc:
        _persist_results_to_handle(handle, getattr(exc, "partial_results", []) or [])
        _update_run_handle(
            handle,
            state="stopped",
            message=(
                "Batch stopped because the configured OpenAI spend limit was reached "
                f"({exc.estimated_cost_usd:.6f} >= {exc.max_cost_usd:.6f})."
            ),
            finished_at=_utc_now_iso(),
            current_asin="",
        )
    except DriverSessionLostError as exc:
        _persist_results_to_handle(handle, getattr(exc, "partial_results", []) or [])
        snapshot = handle.snapshot()
        if handle.cancel_event.is_set() or snapshot["stop_requested"]:
            _update_run_handle(
                handle,
                state="stopped",
                message=f"Batch stopped after {snapshot['completed_jobs']} of {handle.total_jobs} job(s).",
                finished_at=_utc_now_iso(),
                current_asin="",
            )
        else:
            _update_run_handle(
                handle,
                state="error",
                error=(
                    "The visible Chrome session was closed or crashed, so the batch was aborted early "
                    "to avoid continuing with a broken browser connection."
                ),
                message=str(exc),
                finished_at=_utc_now_iso(),
                current_asin="",
            )
    except OpenAIConfigError as exc:
        _update_run_handle(
            handle,
            state="error",
            error=f"OpenAI configuration error: {exc}",
            message="Batch could not start.",
            finished_at=_utc_now_iso(),
        )
    except Exception as exc:  # pragma: no cover - UI-only fallback
        _update_run_handle(
            handle,
            state="error",
            error=f"Batch run failed: {exc}",
            message="Batch run failed unexpectedly.",
            finished_at=_utc_now_iso(),
            current_asin="",
        )
    finally:
        _update_run_handle(handle, scraper=None)


def _handle_progress_update(
    handle: BatchRunHandle,
    *,
    event: str,
    completed_jobs: int,
    total_jobs: int,
    asin: str,
) -> None:
    if event == "starting":
        message = f"Preparing browser session for {total_jobs} ASIN job(s)."
    elif event == "job_started":
        message = f"Running ASIN {asin} ({completed_jobs + 1}/{total_jobs})."
    elif event == "job_finished":
        message = f"Finished ASIN {asin} ({completed_jobs}/{total_jobs})."
    elif event == "cancelled":
        message = f"Batch stopped after {completed_jobs} of {total_jobs} job(s)."
    elif event == "driver_lost":
        message = "Browser session lost. Ending the batch early."
    else:
        message = f"Processing {completed_jobs}/{total_jobs} job(s)."

    snapshot = handle.snapshot()
    state = "stopping" if handle.cancel_event.is_set() or snapshot["stop_requested"] else "running"
    current_asin = asin if event == "job_started" else ""
    _update_run_handle(
        handle,
        completed_jobs=completed_jobs,
        current_asin=current_asin,
        state=state,
        message=message,
    )


def _persist_results_to_handle(handle: BatchRunHandle, results) -> None:
    rows = _serialize_results_for_handle(handle, results)
    if results:
        written_output = write_results_csv(
            results=results,
            output_file=handle.output_path,
            export_columns=handle.export_columns,
            extract_supplement_facts_details=handle.extract_supplement_facts_details,
        )
        _update_run_handle(
            handle,
            rows=rows,
            output_path_text=str(written_output),
            download_name=written_output.name,
            download_bytes=written_output.read_bytes(),
        )
        return

    _update_run_handle(
        handle,
        rows=rows,
        output_path_text="",
        download_name="",
        download_bytes=b"",
    )


def _serialize_results_for_handle(handle: BatchRunHandle, results) -> list[dict[str, str]]:
    _, rows = build_result_rows(
        results,
        export_columns=handle.export_columns,
        extract_supplement_facts_details=handle.extract_supplement_facts_details,
    )
    return rows


def _store_run_handle(handle: BatchRunHandle) -> None:
    with _RUN_REGISTRY_LOCK:
        _RUN_REGISTRY[handle.run_id] = handle


def _update_run_handle(handle: BatchRunHandle, **changes) -> None:
    with handle.lock:
        for key, value in changes.items():
            setattr(handle, key, value)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _resolve_run_output_path(
    output_path: Path,
    *,
    extract_supplement_facts_details: bool,
) -> Path:
    if not extract_supplement_facts_details:
        return output_path

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return output_path.with_name(f"{output_path.stem}_{timestamp}{output_path.suffix}")
