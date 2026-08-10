from __future__ import annotations

import os

from .project_env import load_project_dotenv

_RUN_SCOPED_TEXT_MERGE_MODES = {"off", "shadow", "apply"}
_INGREDIENT_IDENTITY_GUARD_MODES = {"off", "shadow", "apply"}


def run_scoped_schema_enabled(*, default: bool = False) -> bool:
    load_project_dotenv()
    raw_value = os.getenv("RUN_SCOPED_SCHEMA_ENABLED", "").strip()
    if not raw_value:
        return default

    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def run_scoped_text_merge_mode(*, default: str = "off") -> str:
    load_project_dotenv()
    normalized_default = _normalize_run_scoped_text_merge_mode(default, fallback="off")
    raw_value = os.getenv("RUN_SCOPED_TEXT_MERGE_MODE", "").strip()
    if not raw_value:
        return normalized_default
    return _normalize_run_scoped_text_merge_mode(raw_value, fallback=normalized_default)


def _normalize_run_scoped_text_merge_mode(raw_value: str, *, fallback: str) -> str:
    normalized = str(raw_value or "").strip().lower()
    if normalized in _RUN_SCOPED_TEXT_MERGE_MODES:
        return normalized
    return fallback


def ingredient_identity_guard_mode(*, default: str = "off") -> str:
    """off | shadow | apply -- see ADR-004 phase 3.

    shadow records what the guards would have done and blocks those decisions from
    being persisted, without changing which column a row lands in. apply enforces.
    """
    load_project_dotenv()
    normalized_default = _normalize_identity_guard_mode(default, fallback="off")
    raw_value = os.getenv("INGREDIENT_IDENTITY_GUARD_MODE", "").strip()
    if not raw_value:
        return normalized_default
    return _normalize_identity_guard_mode(raw_value, fallback=normalized_default)


def _normalize_identity_guard_mode(raw_value: str, *, fallback: str) -> str:
    normalized = str(raw_value or "").strip().lower()
    if normalized in _INGREDIENT_IDENTITY_GUARD_MODES:
        return normalized
    return fallback
