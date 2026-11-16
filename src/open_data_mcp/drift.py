"""Schema drift between sibling files of one dataset (e.g. one CSV per quarter).

Publishers append a new file each period and quietly rename, add, drop or reorder columns. The
portal API does not expose earlier contents of a replaced file, so "versions" here means the
other resources listed under the same dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .config import Settings
from .encoding_check import normalize_to_utf8
from .errors import UnsupportedResource
from .profiling import column_kinds
from .query_engine import TableSession

_SIMILAR_NAME = 0.85
_SAMPLE_ROWS = 1000


@dataclass(frozen=True)
class SchemaSnapshot:
    columns: list[str]
    kinds: dict[str, str]
    rows_sampled: int


def snapshot_from_prefix(payload: bytes, settings: Settings, *, truncated: bool) -> SchemaSnapshot:
    """Header plus the kinds of the first rows of a CSV head (cut at its last complete line)."""
    cut = payload.rfind(b"\n")
    if truncated and cut > 0:
        payload = payload[:cut]
    normalized, _ = normalize_to_utf8(payload)
    try:
        with TableSession(settings) as session:
            session.load_csv(normalized, lenient=True, max_rows=_SAMPLE_ROWS)
            return SchemaSnapshot(
                [c.name for c in session.columns],
                column_kinds(session, settings),
                session.row_count,
            )
    except UnsupportedResource:
        raise
    except Exception as exc:  # a sibling we cannot parse must not break the profile
        raise UnsupportedResource(f"Could not read the file head ({type(exc).__name__}).") from exc


def _norm(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.casefold())


def compare_schemas(reference: SchemaSnapshot, other: SchemaSnapshot) -> dict[str, Any]:
    """Describe how `other` differs from `reference` (the profiled resource)."""
    pairs: list[tuple[str, str, str | None]] = []  # (reference name, other name, rename reason)
    left = list(reference.columns)
    right = list(other.columns)
    for name in [c for c in left if c in right]:
        pairs.append((name, name, None))
        left.remove(name)
        right.remove(name)
    for name in list(left):
        twin = next((o for o in right if _norm(o) == _norm(name)), None)
        if twin is not None:
            pairs.append((name, twin, "differs only in case, spacing or punctuation"))
            left.remove(name)
            right.remove(twin)
    for name in list(left):
        scored = [(SequenceMatcher(None, _norm(name), _norm(o)).ratio(), o) for o in right]
        best = max(scored, default=(0.0, ""))
        if best[0] >= _SIMILAR_NAME:
            pairs.append((name, best[1], f"similar name (similarity {best[0]:.2f})"))
            left.remove(name)
            right.remove(best[1])
    renames = [{"from": a, "to": b, "reason": why} for a, b, why in pairs if why is not None]
    type_changes = [
        {"column": a, "reference_kind": reference.kinds.get(a), "other_kind": other.kinds.get(b)}
        for a, b, _ in pairs
        if reference.kinds.get(a) != other.kinds.get(b)
        and "empty" not in (reference.kinds.get(a), other.kinds.get(b))
    ]
    positions = [reference.columns.index(a) for b in other.columns for a, bb, _ in pairs if bb == b]
    order_changed = positions != sorted(positions)
    return {
        "columns_removed": left,
        "columns_added": right,
        "possible_renames": renames,
        "column_order_changed": order_changed,
        "type_changes": type_changes,
        "same_schema": not (left or right or renames or order_changed or type_changes),
    }
