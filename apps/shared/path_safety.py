"""Small path-validation helpers shared by the local Flask services.

User-controlled identifiers must never be joined to a data directory directly.
These helpers deliberately validate both the textual value and the resolved path
so Windows backslashes, drive-qualified paths, junctions and symlinks cannot
escape the intended root.
"""

from __future__ import annotations

import re
from pathlib import Path


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_identifier(value: object, *, label: str = "identifier") -> str:
    """Return a conservative single path segment or raise ``ValueError``."""

    text = str(value or "").strip()
    if (
        not _SAFE_IDENTIFIER.fullmatch(text)
        or text.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(f"{label} 格式无效")
    return text


def confined_child(root: Path | str, relative: Path | str, *, label: str = "path") -> Path:
    """Resolve a child path and prove that it remains below ``root``.

    ``relative`` may contain nested forward-slash components (needed for image
    previews), but absolute paths, traversal components, Windows separators and
    alternate-data-stream syntax are rejected before resolution.
    """

    root_path = Path(root).resolve()
    raw = str(relative or "")
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValueError(f"{label} 格式无效")
    relative_path = Path(raw)
    if relative_path.is_absolute() or relative_path.drive:
        raise ValueError(f"{label} 格式无效")
    if any(
        part in {"", ".", ".."}
        or ":" in part
        or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        for part in relative_path.parts
    ):
        raise ValueError(f"{label} 格式无效")

    target = (root_path / relative_path).resolve()
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"{label} 超出允许目录") from exc
    if target == root_path:
        raise ValueError(f"{label} 格式无效")
    return target
