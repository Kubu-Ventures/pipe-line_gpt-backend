from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from pathlib import Path

from app.ingest.chunker import chunk_text

# PHMSA field → normalised name mapping
PHMSA_FIELD_MAP = {
    "IYEAR": "year",
    "OPERATOR_ID": "operator_id",
    "OPERATOR_NAME": "operator_name",
    "CAUSE": "cause",
    "SUBCAUSE": "subcause",
    "LOCATION_CITY": "city",
    "LOCATION_STATE": "state",
    "FATAL": "fatalities",
    "INJURE": "injuries",
    "COST_CURRENT": "cost_usd",
    "COMMODITY": "commodity",
    "PIPELINE_TYPE": "pipeline_type",
    "PIPELINE_SYSTEM": "pipeline_system",
}


def _incident_date(local_datetime: str) -> str:
    """'6/22/2024 10:27' → '22 Jun 2024'; anything else is returned as recorded."""
    value = local_datetime.strip()
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return f"{parsed.day} {parsed:%b %Y}"
    return value


def _incident_label(row: dict) -> str:
    """
    A label that tells one incident from another, e.g.
    "22 Jun 2024 · Pawnee County, OK · Excavation damage · Report 20240093".
    Every chunk of the incident carries it, including narrative continuations.
    """
    date = _incident_date(row.get("LOCAL_DATETIME") or "") or (row.get("IYEAR") or "").strip()
    city = (row.get("LOCATION_CITY") or "").strip()
    county = (row.get("ONSHORE_COUNTY_NAME") or row.get("OFFSHORE_COUNTY_NAME") or "").strip()
    if city and "MUNICIPALITY" not in city.upper():
        place = city.title()
    elif county:
        place = county.title() if county.upper().endswith(("COUNTY", "PARISH")) else f"{county.title()} County"
    else:
        place = ""
    place = ", ".join(filter(None, [place, (row.get("LOCATION_STATE") or "").strip().upper()]))
    cause = (row.get("CAUSE") or "").strip().capitalize()
    report = (row.get("REPORT_NUMBER") or "").strip()
    return " · ".join(filter(None, [date, place, cause, f"Report {report}" if report else ""])) or "Incident"


def _row_to_text(row: dict) -> str:
    """Serialise a PHMSA incident row to a human-readable chunk text."""
    parts = []
    mapped = {PHMSA_FIELD_MAP.get(k, k.lower()): v for k, v in row.items() if v and v.strip()}

    year = mapped.pop("year", "")
    city = mapped.pop("city", "")
    state = mapped.pop("state", "")
    cause = mapped.pop("cause", "")
    subcause = mapped.pop("subcause", "")
    fatalities = mapped.pop("fatalities", "0")
    injuries = mapped.pop("injuries", "0")
    cost = mapped.pop("cost_usd", "")
    operator = mapped.pop("operator_name", mapped.pop("operator_id", ""))

    location = ", ".join(filter(None, [city, state]))
    incident_summary = f"PHMSA Incident [{year}]: Operator: {operator}. Location: {location}. Cause: {cause}"
    if subcause:
        incident_summary += f" ({subcause})"
    incident_summary += f". Fatalities: {fatalities}. Injuries: {injuries}."
    if cost:
        incident_summary += f" Estimated cost: ${cost}."

    parts.append(incident_summary)
    for k, v in mapped.items():
        if v and str(v).strip():
            parts.append(f"{k.replace('_', ' ').title()}: {v}")

    return " ".join(parts)


def load_phmsa_tsv(content: bytes, filename: str) -> tuple[list[dict], dict]:
    """Parse a PHMSA incident TSV file into normalised chunks."""
    text_io = io.StringIO(content.decode("utf-8", errors="replace"))

    # PHMSA files use tab or comma separation
    sample = text_io.read(2048)
    text_io.seek(0)
    delimiter = "\t" if "\t" in sample else ","

    reader = csv.DictReader(text_io, delimiter=delimiter)
    rows = list(reader)

    metadata: dict = {
        "filename": filename,
        "source_type": "phmsa",
        "row_count": len(rows),
    }

    # Extract year range and commodity
    years = [int(r.get("IYEAR", 0)) for r in rows if r.get("IYEAR", "").isdigit()]
    if years:
        metadata["year_from"] = min(years)
        metadata["year_to"] = max(years)

    commodities = list({r.get("COMMODITY", "") for r in rows if r.get("COMMODITY")})
    if len(commodities) == 1:
        metadata["commodity"] = commodities[0]

    all_chunks: list[dict] = []

    for row in rows:
        text = _row_to_text(row)
        fatal = row.get("FATAL", "0")
        injure = row.get("INJURE", "0")

        has_fatality = str(fatal).strip() not in ("", "0") or str(injure).strip() not in ("", "0")
        section = _incident_label(row)
        if has_fatality:
            section += " · Fatality or injury"

        chunks = chunk_text(text, section_label=section)
        for chunk in chunks:
            chunk["chunk_index"] = len(all_chunks) + chunk["chunk_index"]
        all_chunks.extend(chunks)

    # Re-index
    for idx, chunk in enumerate(all_chunks):
        chunk["chunk_index"] = idx

    return all_chunks, metadata


def load_phmsa_zip(content: bytes) -> list[tuple[list[dict], dict, str]]:
    """
    Extract all TSV/CSV files from a PHMSA ZIP download.
    Returns list of (chunks, metadata, filename) per file found.
    """
    results = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.lower().endswith((".csv", ".tsv", ".txt")):
                file_content = zf.read(name)
                chunks, meta = load_phmsa_tsv(file_content, Path(name).name)
                results.append((chunks, meta, Path(name).name))
    return results
