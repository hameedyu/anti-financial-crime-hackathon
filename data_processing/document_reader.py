"""Reading evidence documents.

The supplied dataset stores every document as Markdown. This module parses
that Markdown into a generic structure (title, ``**Key:** value`` fields,
tables, narrative lines) without interpreting what the document means; type-
specific interpretation lives in :mod:`evidence_intelligence.documents`.

Optional formats (kept off the critical path):
* ``.pdf``  - text is extracted only if the third-party ``pypdf`` package is
  installed; otherwise the document is returned with ``read_status
  "unsupported_format"``. A Markdown file is never reported as a PDF.
* ``.csv``  - :func:`read_csv_records` turns a CSV file into dictionaries
  shaped like the JSONL records, for future tabular inputs.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .models import EvidenceDocument, EvidenceIndexEntry, MarkdownTable, ParsedMarkdown
from .textutil import parse_text_date

_FIELD = re.compile(r"^\*\*(?P<key>[^*]+?):\*\*\s*(?P<val>.*)$")
_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MEDIA = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf"}


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_markdown(text: str) -> ParsedMarkdown:
    lines = text.splitlines()
    title = None
    header_doc_id = None
    header_dated = None
    fields: list[tuple[str, str]] = []
    tables: list[MarkdownTable] = []
    paragraphs: list[str] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
            i += 1
            continue
        if line.startswith("|"):
            block = []
            start = i
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            headers = _split_row(block[0])
            # make header names unique
            seen: dict[str, int] = {}
            uniq = []
            for h in headers:
                seen[h] = seen.get(h, 0) + 1
                uniq.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
            body = block[1:]
            if body and _SEPARATOR.match(body[0].strip()):
                body = body[1:]
            rows = []
            for b in body:
                cells = _split_row(b)
                cells += [""] * (len(uniq) - len(cells))
                rows.append({h: cells[k] for k, h in enumerate(uniq)})
            tables.append(MarkdownTable(tuple(uniq), tuple(rows), start + 1))
            continue
        m = _FIELD.match(line)
        if m:
            key, val = m.group("key").strip(), m.group("val").strip()
            if key.lower() == "document id" and header_doc_id is None:
                header_doc_id = val
            elif key.lower() == "dated" and header_dated is None:
                header_dated = parse_text_date(val)
            else:
                fields.append((key, val))
            i += 1
            continue
        paragraphs.append(line)
        i += 1
    return ParsedMarkdown(title, header_doc_id, header_dated, tuple(fields), tuple(tables), tuple(paragraphs))


def _read_text(path: Path) -> tuple[str | None, str]:
    try:
        return path.read_text(encoding="utf-8-sig"), "ok"
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="cp1252"), "ok_non_utf8"
        except (OSError, UnicodeDecodeError):
            return None, "unreadable"
    except OSError:
        return None, "unreadable"


def _read_pdf(path: Path) -> tuple[str | None, str]:
    try:
        import pypdf  # type: ignore  # optional, not required for the supplied dataset
    except ImportError:
        return None, "unsupported_format"
    try:
        reader = pypdf.PdfReader(str(path))
        return "\n".join((p.extract_text() or "") for p in reader.pages), "ok"
    except Exception:  # pragma: no cover - depends on the optional library
        return None, "unreadable"


def load_evidence_document(institution_path: str | Path, entry: EvidenceIndexEntry) -> EvidenceDocument:
    """Read and parse the file an evidence-index entry points to.

    Paths are resolved relative to the institution folder, never the working
    directory, and may not escape it.
    """
    root = Path(institution_path).resolve()
    target = (root / entry.file).resolve()
    media = _MEDIA.get(target.suffix.lower(), "application/octet-stream")
    if root not in target.parents:
        return EvidenceDocument(entry.doc_id, entry.customer_id, entry.type, entry.title, entry.dated,
                                entry.file, media, None, None, "path_outside_institution", entry)
    if not target.is_file():
        return EvidenceDocument(entry.doc_id, entry.customer_id, entry.type, entry.title, entry.dated,
                                entry.file, media, None, None, "missing_file", entry)
    if media == "application/pdf":
        text, status = _read_pdf(target)
    elif media.startswith("text/"):
        text, status = _read_text(target)
    else:
        text, status = None, "unsupported_format"
    parsed = parse_markdown(text) if text is not None and media != "application/pdf" else None
    return EvidenceDocument(entry.doc_id, entry.customer_id, entry.type, entry.title, entry.dated,
                            entry.file, media, text, parsed, status, entry)


def read_csv_records(path: str | Path) -> list[dict]:
    """Optional: read a CSV file into dicts (empty cells become ``None``).

    Numeric columns stay strings; the JSONL builders accept numeric strings
    and convert them to ``Decimal``. Not used for the supplied JSONL data.
    """
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(fh)]
