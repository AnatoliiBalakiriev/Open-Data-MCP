"""SYNTHETIC files with planted data-quality defects, for testing `profile_dataset`.

Nothing here is real data. Defects are planted at known positions, and `registry_truth` recomputes
the expected numbers from the generated rows with plain Python (csv, datetime, statistics), a code
path that shares nothing with the DuckDB/SQL profiler it is used to check.
"""

from __future__ import annotations

import csv
import io
import statistics
from dataclasses import dataclass
from datetime import datetime

NBSP = " "
REGISTRY_COLUMNS = ["id", "registered_at", "amount", "status", "note", "source"]
_PLACEHOLDERS = {"-", "null", "немає", "невідомо", "н/д"}  # the only ones planted in these files
_IMPOSSIBLE_DATES = [
    "31.02.2023",
    "29.02.2023",
    "15.13.2023",
    "00.05.2023",
    "32.01.2023",
    "31.04.2023",
]
_NOT_DATES = ["Подано: 05.03.2023", "Подано: 06.03.2023", "вчора", "минулого року"]


def _csv_bytes(columns: list[str], rows: list[dict[str, str]], *, bom: bool = True) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, columns, delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return (b"\xef\xbb\xbf" if bom else b"") + buffer.getvalue().encode("utf-8")


def registry_rows() -> list[dict[str, str]]:
    """214 rows: 200 base rows with planted defects, 4 exact copies, 3 reused ids, 7 empty rows."""
    rows = []
    for i in range(200):
        rows.append(
            {
                "id": str(i + 1),
                "registered_at": f"{1 + i % 28:02d}.{1 + i % 12:02d}.2023",
                "amount": f"{100 + (i * 7) % 90},{(i * 13) % 100:02d}",
                "status": ("new", "done", "in_progress")[i % 3],
                "note": "",
                "source": "registry",
            }
        )
    for i in range(0, 10):  # ISO layout mixed into a day-first column
        rows[i]["registered_at"] = f"2023-{1 + i % 12:02d}-{1 + i % 28:02d}"
    for i, text in enumerate(_IMPOSSIBLE_DATES, start=10):
        rows[i]["registered_at"] = text
    for i, text in enumerate(_NOT_DATES, start=16):
        rows[i]["registered_at"] = text
    for i in range(20, 23):
        rows[i]["registered_at"] = "01.01.1900"  # sentinel
    for i in range(23, 25):
        rows[i]["registered_at"] = "01.01.1850"  # out of range
    for i in range(25, 30):
        rows[i]["registered_at"] = ""
    for i in range(30, 33):
        rows[i]["registered_at"] = "-"
    rows[33]["registered_at"] = "невідомо"
    for i in range(40, 45):  # five extreme amounts
        rows[i]["amount"] = f"1{NBSP}000{NBSP}000,00"
    for i in range(45, 49):
        rows[i]["amount"] = "н/д"  # placeholder
    for i in range(49, 52):
        rows[i]["amount"] = "обмін"  # text in a numeric column
    for i in range(52, 58):
        rows[i]["amount"] = ""
    for i in range(60, 68):
        rows[i]["status"] = "-"
    for i in range(68, 72):
        rows[i]["status"] = "немає"
    for i in range(72, 75):
        rows[i]["status"] = "null"
    for i in range(75, 80):
        rows[i]["status"] = ""
    rows.extend(dict(rows[i]) for i in (100, 101, 102, 103))  # exact copies
    for i in (104, 105, 106):  # id reused for a different record
        rows.append({**rows[i], "amount": "999,99", "status": "done"})
    empty = {
        "id": "",
        "registered_at": "",
        "amount": "",
        "status": "",
        "note": "",
        "source": "registry",
    }
    rows.extend(dict(empty) for _ in range(7))
    return rows


def registry_q1_rows() -> list[dict[str, str]]:
    return [dict(r) for r in registry_rows()[:40]]


def registry_q3() -> tuple[list[str], list[dict[str, str]]]:
    """Next period's file: reordered, one column renamed in case, one dropped, one added."""
    columns = ["amount", "id", "registration_date", "Status", "region", "source"]
    rows = [
        {
            "amount": r["amount"],
            "id": f"A-{r['id']}",
            "registration_date": r["registered_at"],
            "Status": r["status"],
            "region": "Київ",
            "source": r["source"],
        }
        for r in registry_rows()[:30]
    ]
    return columns, rows


def build_files() -> dict[str, bytes]:
    q3_columns, q3_rows = registry_q3()
    cp1251_rows = [{"id": str(i), "name": f"Місто {i} Київ"} for i in range(20)]
    mixed_lines = [f"{i};Запис {i} Львів" for i in range(30)]
    legacy_lines = [f"{100 + i};Одеса {i}" for i in range(4)]
    moji = "Привіт світе".encode().decode("cp1251")
    moji_rows = [{"id": str(i), "name": "Київ"} for i in range(12)]
    for i in range(6):
        moji_rows[i]["name"] = moji
    for i in (6, 7):
        moji_rows[i]["name"] = "Ки�в"
    ragged = b"a;b;c\n" + b"".join(f"{i};x{i};y{i}\n".encode() for i in range(10))
    ragged += b"11;only-two\n12;also\n13;a;b;c;d\n"
    big = "ts;level;msg\n" + "".join(
        f"2024-01-01 00:{i % 60:02d};INFO;message number {i}\n" for i in range(3000)
    )
    return {
        "registry_2024q1.csv": _csv_bytes(REGISTRY_COLUMNS, registry_q1_rows()),
        "registry_2024q2.csv": _csv_bytes(REGISTRY_COLUMNS, registry_rows()),
        "registry_2024q3.csv": _csv_bytes(q3_columns, q3_rows),
        "enc_cp1251.csv": _csv_bytes(["id", "name"], cp1251_rows, bom=False)
        .decode("utf-8")
        .encode("cp1251"),
        "enc_mixed.csv": ("id;name\n" + "\n".join(mixed_lines) + "\n").encode("utf-8")
        + ("\n".join(legacy_lines) + "\n").encode("cp1251"),
        "enc_mojibake.csv": _csv_bytes(["id", "name"], moji_rows, bom=False),
        "ragged.csv": ragged,
        "big_log.csv": big.encode("utf-8"),
    }


# --------------------------------------------------------------------------- expected values


@dataclass(frozen=True)
class RegistryTruth:
    rows: int
    empty: dict[str, int]
    placeholders: dict[str, int]
    exact_duplicate_extra_rows: int
    id_rows_beyond_first: int
    sparse_rows: int
    date_valid: int
    date_impossible: int
    date_not_a_date: int
    date_sentinel: int
    date_out_of_range: int
    date_layout_count: int
    amount_numeric: int
    amount_non_numeric: int
    amount_mild_outliers: int
    amount_extreme_outliers: int
    amount_q1: float
    amount_q3: float


def _parse_amount(text: str) -> float | None:
    cleaned = text.strip().replace(NBSP, "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _judge_date(text: str) -> str:
    text = text.strip()
    for layout in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            when = datetime.strptime(text, layout)
        except ValueError:
            continue
        if not 1900 <= when.year <= 2100:
            return "out_of_range"
        return "sentinel" if (when.year, when.month, when.day) == (1900, 1, 1) else "valid"
    parts = text.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4:
        return "impossible"
    return "not_a_date"


def registry_truth(rows: list[dict[str, str]] | None = None) -> RegistryTruth:
    rows = rows if rows is not None else registry_rows()
    empty = {c: sum(1 for r in rows if r[c].strip() == "") for c in REGISTRY_COLUMNS}
    placeholders = {
        c: sum(1 for r in rows if r[c].strip().lower() in _PLACEHOLDERS) for c in REGISTRY_COLUMNS
    }
    counts: dict[tuple[str, ...], int] = {}
    for r in rows:
        key = tuple(r[c] for c in REGISTRY_COLUMNS)
        counts[key] = counts.get(key, 0) + 1
    ids = [r["id"] for r in rows if r["id"].strip()]
    sparse = sum(
        1
        for r in rows
        if sum(
            1
            for c in REGISTRY_COLUMNS
            if r[c].strip() == "" or r[c].strip().lower() in _PLACEHOLDERS
        )
        >= 0.8 * len(REGISTRY_COLUMNS)
    )
    verdicts = [
        _judge_date(r["registered_at"])
        for r in rows
        if r["registered_at"].strip() and r["registered_at"].strip().lower() not in _PLACEHOLDERS
    ]
    amounts = [
        v
        for r in rows
        if r["amount"].strip() and r["amount"].strip().lower() not in _PLACEHOLDERS
        for v in [_parse_amount(r["amount"])]
        if v is not None
    ]
    present_amounts = sum(
        1 for r in rows if r["amount"].strip() and r["amount"].strip().lower() not in _PLACEHOLDERS
    )
    q1, _median, q3 = statistics.quantiles(amounts, n=4, method="inclusive")
    iqr = q3 - q1
    return RegistryTruth(
        rows=len(rows),
        empty=empty,
        placeholders=placeholders,
        exact_duplicate_extra_rows=sum(n - 1 for n in counts.values()),
        id_rows_beyond_first=len(ids) - len(set(ids)),
        sparse_rows=sparse,
        date_valid=sum(v in ("valid", "sentinel") for v in verdicts),
        date_impossible=verdicts.count("impossible"),
        date_not_a_date=verdicts.count("not_a_date"),
        date_sentinel=verdicts.count("sentinel"),
        date_out_of_range=verdicts.count("out_of_range"),
        date_layout_count=2,
        amount_numeric=len(amounts),
        amount_non_numeric=present_amounts - len(amounts),
        amount_mild_outliers=sum(1 for a in amounts if a < q1 - 1.5 * iqr or a > q3 + 1.5 * iqr),
        amount_extreme_outliers=sum(1 for a in amounts if a < q1 - 3 * iqr or a > q3 + 3 * iqr),
        amount_q1=q1,
        amount_q3=q3,
    )
