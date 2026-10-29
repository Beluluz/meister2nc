"""Small helpers shared by all stages: JSON I/O, time conversion, text limits."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("meister2nc")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --- JSON files -----------------------------------------------------------

def read_json(path: Path, default: Any = None) -> Any:
    if not Path(path).exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any, mode: int | None = None) -> None:
    """Atomic write: tmp file in the same directory, then rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- time -----------------------------------------------------------------

def parse_iso(value: str | None) -> datetime | None:
    """Parse Meister timestamps such as ``2019-05-09T14:49:18.303930Z``."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_to_epoch(value: str | None, default: int | None = None) -> int | None:
    dt = parse_iso(value)
    if dt is None:
        return default
    return int(dt.timestamp())


def iso_to_atom(value: str | None) -> str | None:
    """PHP DateTime::ATOM / ``Y-m-d\\TH:i:sP`` without microseconds, UTC."""
    dt = parse_iso(value)
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat()


def now_epoch() -> int:
    return int(time.time())


def epoch_to_display(epoch: int | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- text -----------------------------------------------------------------

def truncate(text: str, limit: int, marker: str = "…") -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most ``limit`` characters, preferring
    paragraph, line and word boundaries."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > limit // 2:
                break
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, fallback: str = "unbenannt", limit: int = 150) -> str:
    cleaned = _FILENAME_BAD.sub("_", (name or "").strip()).strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    return truncate(cleaned, limit, marker="")


def format_duration(seconds: int | float | None) -> str:
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m"


# --- compact table format used by the Meister MCP server ------------------

_COMPACT_HEADER = re.compile(r"^\[(\d+)\]\{([^}]*)\}:\s*$")


def parse_compact_table(text: str) -> tuple[int | None, list[dict]]:
    """Parse the ``[N]{col1,col2,...}:`` + CSV rows format the Meister MCP
    server uses for large results.  Returns (claimed_total, rows).  Rows cut
    off by the server's size limit (column count mismatch) are dropped."""
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if _COMPACT_HEADER.match(line.strip()):
            header_idx = i
            break
    if header_idx is None:
        return None, []
    m = _COMPACT_HEADER.match(lines[header_idx].strip())
    claimed = int(m.group(1))
    columns = [c.strip() for c in m.group(2).split(",")]
    rows: list[dict] = []
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("Response truncated"):
            continue
        fields = _split_row(stripped, len(columns))
        if fields is None:
            continue
        row = {}
        for col, val in zip(columns, fields):
            row[col] = _coerce(val)
        rows.append(row)
    return claimed, rows


def _split_row(line: str, ncols: int) -> list[str] | None:
    """Quoted fields may contain ``\\"`` (JSON-style) or ``""`` escapes; try both."""
    for kwargs in ({"escapechar": "\\", "doublequote": False}, {}):
        try:
            fields = next(csv.reader(io.StringIO(line), skipinitialspace=True, **kwargs))
        except csv.Error:
            continue
        if len(fields) == ncols:
            return fields
    return None


def _coerce(value: str) -> Any:
    if value == "":
        return None
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def parse_tool_payload(result: dict) -> Any:
    """Extract structured data from an MCP tools/call result.

    Preference: ``structuredContent`` → first JSON text block → compact table.
    Returns dict/list, or for compact tables ``{"__claimed__": N, "rows": [...]}``.
    """
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    for text in texts:
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            if _COMPACT_HEADER.match(stripped.splitlines()[0].strip()):
                claimed, rows = parse_compact_table(stripped)
                return {"__claimed__": claimed, "rows": rows}
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        # JSON may follow a preface sentence
        start = min([p for p in (stripped.find("{"), stripped.find("[")) if p >= 0], default=-1)
        if start > 0:
            try:
                return json.loads(stripped[start:])
            except json.JSONDecodeError:
                pass
    for text in texts:
        claimed, rows = parse_compact_table(text)
        if claimed is not None:
            return {"__claimed__": claimed, "rows": rows}
    return None


def tool_texts(result: dict) -> list[str]:
    return [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]


def unique(seq: Iterable[Any]) -> list[Any]:
    seen = set()
    out = []
    for item in seq:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
