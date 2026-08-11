from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .migrations import migrate_document


def load_project(path: str | Path) -> dict[str, Any]:
    project_path = Path(path)
    with project_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return migrate_document(raw)


def save_project(path: str | Path, data: dict[str, Any], create_backup: bool = False) -> None:
    project_path = Path(path)
    project_path.parent.mkdir(parents=True, exist_ok=True)

    if create_backup and project_path.exists():
        version = data.get("format", {}).get("version", "unknown")
        backup_path = project_path.with_suffix(project_path.suffix + f".backup-{version}")
        shutil.copy2(project_path, backup_path)

    fd, temp_name = tempfile.mkstemp(
        prefix=project_path.name + ".",
        suffix=".tmp",
        dir=str(project_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, project_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
