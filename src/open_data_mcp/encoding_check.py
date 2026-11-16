"""Byte-level encoding diagnostics for a downloaded file, run before any decoder is chosen.

Ukrainian portals still publish Windows-1251 files, UTF-8 files with a BOM, and files in which
both encodings were pasted together. The loader used by `query` makes one decision for the whole
file; this module looks at every line so the profile can say what is actually in the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CYRILLIC = re.compile("[Ѐ-ӿ]")
_CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPLACEMENT_UTF8 = "�".encode()
_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class EncodingReport:
    bom: bool
    detected: str  # ascii | utf-8 | windows-1251 | mixed | undecodable
    lines: int
    non_ascii_lines: int
    utf8_lines: int  # non-ASCII lines that are valid UTF-8 with Cyrillic text
    windows1251_lines: int  # non-ASCII lines that are not UTF-8 but decode as Windows-1251
    undecodable_lines: int
    replacement_characters: int  # U+FFFD already present in the decoded text
    control_characters: int
    note: str


def normalize_to_utf8(payload: bytes) -> tuple[bytes, EncodingReport]:
    """Return (UTF-8 bytes without BOM, report). Decodes line by line only when it must."""
    bom = payload.startswith(_BOM)
    body = payload[len(_BOM) :] if bom else payload
    lines = body.count(b"\n") + (0 if body.endswith(b"\n") or not body else 1)
    if body.isascii():
        return body, _report(body, bom, "ascii", lines, 0, 0, 0, 0)
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        non_ascii = sum(1 for line in body.split(b"\n") if not line.isascii())
        return body, _report(body, bom, "utf-8", lines, non_ascii, non_ascii, 0, 0)

    out: list[bytes] = []
    non_ascii = utf8 = legacy = undecodable = 0
    for line in body.split(b"\n"):
        if line.isascii():
            out.append(line)
            continue
        non_ascii += 1
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            try:
                out.append(line.decode("cp1251").encode("utf-8"))
                legacy += 1
            except UnicodeDecodeError:
                out.append(line.decode("cp1251", errors="replace").encode("utf-8"))
                undecodable += 1
        else:
            out.append(line)
            if _CYRILLIC.search(text):
                utf8 += 1
    normalized = b"\n".join(out)
    if legacy and utf8:
        detected = "mixed"
    elif legacy:
        detected = "windows-1251"
    elif undecodable and not utf8:
        detected = "undecodable"
    else:
        detected = "utf-8"
    report = _report(normalized, bom, detected, lines, non_ascii, utf8, legacy, undecodable)
    return normalized, report


def _report(
    normalized: bytes,
    bom: bool,
    detected: str,
    lines: int,
    non_ascii: int,
    utf8: int,
    legacy: int,
    undecodable: int,
) -> EncodingReport:
    return EncodingReport(
        bom=bom,
        detected=detected,
        lines=lines,
        non_ascii_lines=non_ascii,
        utf8_lines=utf8,
        windows1251_lines=legacy,
        undecodable_lines=undecodable,
        replacement_characters=normalized.count(_REPLACEMENT_UTF8),
        control_characters=len(_CONTROL.findall(normalized)),
        note=_NOTES[detected].format(utf8=utf8, legacy=legacy, undecodable=undecodable),
    )


_NOTES = {
    "ascii": "Pure ASCII: no encoding ambiguity.",
    "utf-8": "Valid UTF-8.",
    "windows-1251": (
        "Not UTF-8: every non-ASCII line decodes as Windows-1251. get_schema and query convert "
        "it automatically."
    ),
    "mixed": (
        "Mixed encodings in one file: {utf8} lines are UTF-8 and {legacy} are Windows-1251. This "
        "profile decodes line by line. get_schema and query decode the whole file as "
        "Windows-1251 instead, so the UTF-8 lines would look garbled there."
    ),
    "undecodable": "{undecodable} lines are neither UTF-8 nor Windows-1251.",
}
