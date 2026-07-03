from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_project_dotenv(*, require_package: bool = False) -> bool:
    try:
        from dotenv import load_dotenv
    except ImportError:
        if require_package:
            raise
        return False

    dotenv_path = project_root() / ".env"
    if not dotenv_path.is_file():
        return False

    load_dotenv(dotenv_path=dotenv_path, override=False)
    return True
