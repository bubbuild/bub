"""Per-session runtime state persistence.

A minimal JSON-file store keyed by session id, used to carry small pieces of
runtime state (currently the per-session model choice set via the ``set_model``
tool) across process restarts. Each session is one file under
``bub.home/sessions/<sanitised-id>.json``.

The store deliberately persists *only* the small, user-chosen keys it is told to
(see :class:`BuiltinImpl` usage) and never touches the process environment, so
concurrent sessions never overwrite one another.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Characters that are unsafe in a filename; replaced with ``_``.
_INVALID_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9_.-]")


def sanitise_session_id(session_id: str) -> str:
    """Return a filesystem-safe representation of *session_id*."""
    return _INVALID_FILENAME_CHARS.sub("_", session_id)


class SessionStateStore:
    """Tiny JSON store for per-session metadata, one file per session id.

    ``base_dir`` is resolved lazily (defaulting to ``bub.home / "sessions"``) so
    that merely constructing a store — e.g. eagerly at framework init — never
    touches the filesystem. The directory is created on the first write only.

    Writes are synchronous and atomic (write to a ``.tmp`` sibling then rename),
    which is fine for the low-frequency, small-payload use case of persisting a
    model choice once per turn.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir

    @property
    def base(self) -> Path:
        if self._base is None:
            import bub

            self._base = (bub.home / "sessions").resolve()
        return self._base

    def load(self, session_id: str) -> dict:
        """Return the stored mapping for *session_id*, or an empty dict.

        Missing files, unreadable files, and corrupt JSON all collapse to an
        empty dict rather than raising, so a bad session file can never block a
        turn.
        """
        path = self._path(session_id)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, session_id: str, data: dict) -> None:
        """Atomically persist *data* for *session_id*."""
        path = self._path(session_id)
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def delete(self, session_id: str) -> None:
        """Remove any persisted mapping for *session_id* (no error if absent)."""
        self._path(session_id).unlink(missing_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.base / f"{sanitise_session_id(session_id)}.json"
