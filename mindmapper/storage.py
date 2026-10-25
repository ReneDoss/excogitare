from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .migrations import migrate_document
from .project_validation import InvalidProjectError, validate_project


def load_project(path: str | Path) -> dict[str, Any]:
    with Path(path).open('r', encoding='utf-8') as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise InvalidProjectError('Die Projektdatei muss ein JSON-Objekt enthalten.')
    try:
        data = migrate_document(raw)
    except (AttributeError, TypeError, KeyError, ValueError) as exc:
        raise InvalidProjectError(f'Ungültige Projektstruktur: {exc}') from exc
    validate_project(data)
    return data


def atomic_write(path: str | Path, payload: bytes) -> None:
    """Write and fsync a same-directory temporary file before replacing a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Directory sync adds durability on platforms supporting directory fds.
        if os.name == 'posix':
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_project(path: str | Path, data: dict[str, Any], create_backup: bool = False) -> None:
    path = Path(path)
    validate_project(data)
    # Serialize first: invalid data must not replace either the file or its backup.
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if create_backup and path.exists():
        atomic_write(path.with_suffix(path.suffix + '.bak'), path.read_bytes())
    atomic_write(path, payload)
