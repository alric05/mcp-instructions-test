#!/usr/bin/env python3
"""MCP server for staged CompuMark trademark knockout report workflows.

Purpose
-------
This local server does *not* replace the Clarivate CompuMark MCP connector.
Instead it exposes a deterministic workflow controller, report drafter,
validator, and Clarivate-template PDF renderer. The agent should:

1. Start a workflow here and carry the returned run_id.
2. Ask this server for the next step.
3. Execute the requested CompuMark/web calls using the appropriate external
   tools available in the host.
4. Feed the results back through advance_trademark_knockout_workflow.
5. Draft/validate/render the final report with this server.

The design is intentionally explicit-state. MCP has no protocol-level session;
this server returns an opaque run_id and persists workflow state on disk so the
model can continue a staged workflow safely across tool calls.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

try:  # PDF generation is optional until generate_clarivate_report_pdf is called.
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
except ImportError:  # pragma: no cover - startup guard for MCP runtime
    PdfReader = PdfWriter = None  # type: ignore[assignment]
    colors = TA_LEFT = A4 = ParagraphStyle = getSampleStyleSheet = mm = None  # type: ignore[assignment]
    pdfmetrics = canvas = Paragraph = SimpleDocTemplate = Spacer = Table = TableStyle = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Server metadata and filesystem defaults
# ---------------------------------------------------------------------------

SERVER_NAME = "trademark-knockout-report-workflow"
SERVER_VERSION = "1.0.0"
WORKFLOW_VERSION = "trademark_knockout_report_v2"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "assets" / "Clarivate_template.pdf"

# Clarivate cover subtitle placement. These values are copied from the original
# implementation to keep the same template behavior.
PAGE_W = 595.32
PAGE_H = 841.92
SUBTITLE_COVER_X = 40
SUBTITLE_COVER_Y = 535
SUBTITLE_COVER_W = 270
SUBTITLE_COVER_H = 55
SUBTITLE_X = 48
SUBTITLE_BASELINE_Y = 564.91
SUBTITLE_FONT = "Carlito Bold"
SUBTITLE_FONT_FALLBACK = "Helvetica-Bold"
SUBTITLE_FONT_SIZE = 22


def default_output_dir() -> Path:
    configured = os.environ.get("TRADEMARK_REPORT_OUTPUT_DIR")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()
    # Use a directory beside this server rather than the process CWD. MCP hosts
    # often launch stdio servers from unpredictable working directories.
    return (BASE_DIR / "reports").resolve()


def workflow_state_dir() -> Path:
    configured = os.environ.get("TRADEMARK_WORKFLOW_STATE_DIR")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()
    return default_output_dir() / ".trademark_knockout_runs"


def public_base_url() -> Optional[str]:
    value = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return value.rstrip("/") if value else None


def public_file_url(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    base_url = public_base_url()
    if not base_url:
        return None
    try:
        relative = path.resolve().relative_to(default_output_dir())
    except ValueError:
        return None
    return f"{base_url}/reports/{relative.as_posix()}"


# ---------------------------------------------------------------------------
# Constants and report template
# ---------------------------------------------------------------------------

EU_COUNTRY_OFFICES = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
}

OFFICE_ALIASES = {
    "EU": "EM",
    "EUTM": "EM",
    "EUIPO": "EM",
    "EUROPEAN UNION": "EM",
    "EUROPEAN UNION INTELLECTUAL PROPERTY OFFICE": "EM",
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
    "BRITAIN": "GB",
    "US": "US",
    "USA": "US",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "WIPO": "WO",
    "WO": "WO",
    "MADRID": "WO",
    "INTERNATIONAL": "WO",
    "FRANCE": "FR",
    "SPAIN": "ES",
    "GERMANY": "DE",
    "ITALY": "IT",
    "CANADA": "CA",
    "AUSTRALIA": "AU",
    "CHINA": "CN",
    "JAPAN": "JP",
    "INDIA": "IN",
    "BRAZIL": "BR",
    "MEXICO": "MX",
}

RISK_LOW = "🟢 Low"
RISK_MEDIUM = "🟠 Medium"
RISK_HIGH = "🔴 High"
RISK_LABELS = {RISK_LOW, RISK_MEDIUM, RISK_HIGH}

EXTERNAL_COMPUMARK_TOOL_NAMES = {
    "knockout": "trademark-knockout-search",
    "search": "trademark-search",
    "content": "trademark-content",
    "goods": "trademark-goods",
    "fulltext": "trademark-fulltext",
    "litigation": "search-litigation-cases",
}

WORKFLOW_STEPS = [
    {
        "step_id": "criteria",
        "title": "Confirm normalized criteria",
        "goal": "Know the exact mark, territory/offices, Nice classes, and whether online search is enabled.",
    },
    {
        "step_id": "exact_search",
        "title": "Run exact/knockout CompuMark search",
        "goal": "Find identical marks in the requested classes and offices.",
    },
    {
        "step_id": "broad_search",
        "title": "Run broad CompuMark search if needed",
        "goal": "If exact results are limited, broaden to contains/plural/phonetic results by class.",
    },
    {
        "step_id": "trademark_details",
        "title": "Fetch Top 5 trademark details and full-text links",
        "goal": "Retrieve source-backed records for the five most material references.",
    },
    {
        "step_id": "litigation",
        "title": "Check litigation activity",
        "goal": "Look for trademark cases or oppositions involving the mark and material parties.",
    },
    {
        "step_id": "web_search",
        "title": "Check online presence",
        "goal": "Find material online commercial use or practical marketplace conflicts.",
    },
    {
        "step_id": "draft_report",
        "title": "Draft knockout report",
        "goal": "Produce the fixed-section markdown report with exactly five Top rows.",
    },
    {
        "step_id": "validate_report",
        "title": "Validate report",
        "goal": "Catch structural, placeholder, link-label, and risk-label problems before rendering.",
    },
    {
        "step_id": "generate_pdf",
        "title": "Generate Clarivate-template PDF",
        "goal": "Merge the report body into the existing Clarivate template.",
    },
    {
        "step_id": "complete",
        "title": "Complete",
        "goal": "Return the PDF URL/path and a concise final response.",
    },
]

REPORT_TEMPLATE = """# AI Generated Trademark Knockout Search Report (Demo only)

Mark searched: [MARK]
Date of report: [DATE]

---

## 1. Search Criteria

| Field               | Details                                    |
| ------------------- | ------------------------------------------ |
| Mark searched       | [MARK]                                     |
| Type                | Word                                       |
| Territories covered | [TERRITORIES]                              |
| Nice classes        | [CLASSES]                                  |
| Match scope         | Exact knockout; broad contains/phonetic only if exact results are limited |
| Notes / assumptions | [NOTES]                                    |

---

## 2. CompuMark Search Results

### 2.1 Summary

| Item                            | Result             |
| ------------------------------- | ------------------ |
| Total records reviewed          | [NUMBER]           |
| Most relevant jurisdictions     | [LIST]             |
| Most relevant classes           | [LIST]             |
| Overall initial risk impression | [RISK]             |

### 2.2 Most Relevant Trademark References (Top 5)

| Verbal Element | Status | Registration Office | Class(es) | Number | Date | Owner | Full Text URL |
| -------------- | ------ | ------------------- | --------- | ------ | ---- | ----- | ------------- |
| [ROW 1]        |        |                     |           |        |      |       |               |
| [ROW 2]        |        |                     |           |        |      |       |               |
| [ROW 3]        |        |                     |           |        |      |       |               |
| [ROW 4]        |        |                     |           |        |      |       |               |
| [ROW 5]        |        |                     |           |        |      |       |               |

### 2.3 Litigation Activity

| Parties | Case Type | Jurisdiction | Status | Key Details |
| ------- | --------- | ------------ | ------ | ----------- |
| [ROW 1] |           |              |        |             |
| [ROW 2] |           |              |        |             |

### 2.4 Trademark Assessment Comments

* [Comment on exact matches.]
* [Comment on broad/similar matches.]
* [Comment on material references and why they matter.]
* [Comment on litigation activity.]

---

## 3. Online Presence Search

### 3.1 Summary

| Item                         | Result |
| ---------------------------- | ------ |
| Exact same name found online | [Yes / No / Limited / Not performed] |
| Similar names found online   | [Yes / No / Limited / Not performed] |
| Commercial use observed      | [Yes / No / Limited / Not performed] |

### 3.2 Most Relevant Web Findings (Top 5)

| Name / Sign | Webpage URL / Source | Territory | Type of use | Notes |
| ----------- | -------------------- | --------- | ----------- | ----- |
| [ROW 1]     |                      |           |             |       |
| [ROW 2]     |                      |           |             |       |
| [ROW 3]     |                      |           |             |       |
| [ROW 4]     |                      |           |             |       |
| [ROW 5]     |                      |           |             |       |

### 3.3 Web Search Comments

* [Comment on active commercial use online.]
* [Comment on marketplace overlap.]
* [Comment on notable domain or branding conflicts.]

---

## 4. Key Takeaways

Overall clearance view: [RISK]

* [Key takeaway 1.]
* [Key takeaway 2.]
* [Key takeaway 3.]
* [Key takeaway 4.]

---

Disclaimer

This report is produced for informational purposes only and does not constitute legal advice. Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. Always consult a qualified trademark attorney before filing.
"""

REPORT_SECTION_PATTERNS = [
    (r"^##\s+1\.", "section 1 Search Criteria"),
    (r"^##\s+2\.", "section 2 CompuMark Search Results"),
    (r"^###\s+2\.1\b", "section 2.1 Summary"),
    (r"^###\s+2\.2\b", "section 2.2 Top 5 Trademark References"),
    (r"^###\s+2\.3\b", "section 2.3 Litigation Activity"),
    (r"^###\s+2\.4\b", "section 2.4 Trademark Assessment Comments"),
    (r"^##\s+3\.", "section 3 Online Presence Search"),
    (r"^###\s+3\.1\b", "section 3.1 Summary"),
    (r"^###\s+3\.2\b", "section 3.2 Web Findings"),
    (r"^###\s+3\.3\b", "section 3.3 Web Search Comments"),
    (r"^##\s+4\.", "section 4 Key Takeaways"),
]

TOP_5_TABLE_PATTERNS = {
    "2.2": r"^###\s+2\.2\b",
    "3.2": r"^###\s+3\.2\b",
}

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
URL_RE = re.compile(r"https?://[^\s)\]>'\"]+")
PIPE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
PLACEHOLDER_RE = re.compile(r"\[(?:[A-Z][A-Z0-9 _./-]{2,})\]")


# ---------------------------------------------------------------------------
# JSON-RPC / MCP helpers
# ---------------------------------------------------------------------------


def stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def text_tool_result(payload: Any, *, is_error: bool = False, resource_links: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Return an MCP CallToolResult.

    Structured results are returned both as structuredContent and serialized JSON
    text for compatibility with MCP clients that only read content blocks.
    """
    if isinstance(payload, str):
        text = payload
        structured = None
    else:
        text = stable_json(payload)
        structured = payload

    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for link in resource_links or []:
        uri = link.get("uri")
        name = link.get("name") or uri
        if not uri:
            continue
        content.append(
            {
                "type": "resource_link",
                "uri": uri,
                "name": name,
                "description": link.get("description", name),
                "mimeType": link.get("mimeType", "application/octet-stream"),
            }
        )

    result: Dict[str, Any] = {"content": content, "isError": is_error}
    if not is_error and structured is not None:
        result["structuredContent"] = structured
    return result


def json_rpc_result(message_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def json_rpc_error(message_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


# ---------------------------------------------------------------------------
# Generic data utilities
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_mark(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_mark(value)).strip("._")
    return name or "trademark_report"


def ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        return text if text else default
    if isinstance(value, list):
        parts = [as_text(item) for item in value]
        return ", ".join(part for part in parts if part) or default
    return default


def truncate(text: str, limit: int = 180) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def deep_get_case_insensitive(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    lowered = {str(key).lower(): key for key in mapping.keys()}
    for name in names:
        key = lowered.get(str(name).lower())
        if key is not None:
            return mapping.get(key)
    # Fallback: normalize out punctuation/underscores.
    normalized = {re.sub(r"[^a-z0-9]", "", str(key).lower()): key for key in mapping.keys()}
    for name in names:
        key = normalized.get(re.sub(r"[^a-z0-9]", "", str(name).lower()))
        if key is not None:
            return mapping.get(key)
    return None


def unique_strings(values: Iterable[Any], *, limit: Optional[int] = None) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values:
        text = as_text(value)
        if not text:
            continue
        key = text.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
        if limit is not None and len(output) >= limit:
            break
    return output


def domain_for(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "") or url
    except Exception:
        return url


def markdown_link(label: str, url: str) -> str:
    if not url:
        return ""
    safe_label = label or domain_for(url)
    safe_label = safe_label.replace("|", " ").strip() or domain_for(url)
    return f"[{safe_label}]({url})"


def escape_md_cell(value: Any) -> str:
    text = as_text(value, "-")
    text = text.replace("\n", " ").replace("\r", " ").strip()
    text = text.replace("|", "/")
    return text or "-"


def today_display() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Criteria normalization
# ---------------------------------------------------------------------------


def normalize_classes(classes: Any) -> List[str]:
    if isinstance(classes, str):
        parts = re.split(r"[,;\s]+", classes)
    else:
        parts = list(classes or [])
    output: List[str] = []
    for item in parts:
        text = str(item).strip()
        if not text:
            continue
        if text.lower().startswith("class"):
            text = re.sub(r"(?i)^class\s*", "", text).strip()
        if not text.isdigit():
            raise ValueError(f"Nice class must be a number from 1 to 45: {text}")
        number = int(text)
        if number < 1 or number > 45:
            raise ValueError(f"Nice class out of range 1 to 45: {text}")
        normalized = str(number)
        if normalized not in output:
            output.append(normalized)
    return output


def normalize_jurisdictions(jurisdictions: Any) -> Tuple[List[str], List[str], bool]:
    if isinstance(jurisdictions, str):
        raw_parts = [part.strip() for part in re.split(r"[,;/]+", jurisdictions) if part.strip()]
    else:
        raw_parts = [str(part).strip() for part in jurisdictions or [] if str(part).strip()]

    offices: List[str] = []
    notes: List[str] = []
    saw_specific_country = False
    saw_eu_scope = False

    for raw in raw_parts:
        key = re.sub(r"\s+", " ", raw.strip().upper())
        code = OFFICE_ALIASES.get(key, key if re.fullmatch(r"[A-Z]{2}", key) else None)
        if not code:
            notes.append(f"Could not confidently map jurisdiction '{raw}'. Use CompuMark office-code lookup if needed.")
            continue
        if code not in offices:
            offices.append(code)
        if code == "EM":
            saw_eu_scope = True
        elif code != "WO":
            saw_specific_country = True
            if code in EU_COUNTRY_OFFICES:
                saw_eu_scope = True

    # In an EU country-level search, include EUTM and WIPO designations because
    # they may be relevant to clearance in that country. This is a default for a
    # knockout report and can be overridden by passing exact office codes only.
    if saw_eu_scope and "EM" not in offices:
        offices.append("EM")
    if (saw_specific_country or saw_eu_scope) and "WO" not in offices:
        offices.append("WO")

    limit_wo = "WO" in offices and len([code for code in offices if code != "WO"]) > 0
    return offices, notes, limit_wo


def bool_from_user(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"no", "false", "0", "n", "disabled", "skip", "off"}:
        return False
    if text in {"yes", "true", "1", "y", "enabled", "on"}:
        return True
    return default


def normalize_criteria(raw: Mapping[str, Any], *, allow_missing: bool = True) -> Dict[str, Any]:
    mark = clean_mark(raw.get("mark") or raw.get("trademark") or raw.get("subject") or "")
    raw_classes = raw.get("nice_classes") or raw.get("classes") or raw.get("nice_class")
    raw_jurisdictions = raw.get("jurisdictions") or raw.get("jurisdiction") or raw.get("registrationOfficeCodes") or raw.get("offices")

    nice_classes = normalize_classes(raw_classes) if raw_classes else []
    if raw_jurisdictions:
        offices, mapping_notes, limit_wo = normalize_jurisdictions(raw_jurisdictions)
    else:
        offices, mapping_notes, limit_wo = [], [], False

    match_scope = str(raw.get("match_scope") or "exact_then_broad_if_limited").strip().lower()
    if match_scope in {"exact", "knockout"}:
        match_scope = "exact_then_broad_if_limited"
    elif match_scope not in {"exact_then_broad_if_limited", "contains", "broad"}:
        match_scope = "exact_then_broad_if_limited"

    criteria = {
        "mark": mark,
        "type": str(raw.get("type") or "Word").strip() or "Word",
        "jurisdictions_requested": ensure_list(raw_jurisdictions) if raw_jurisdictions is not None else [],
        "registrationOfficeCodes": offices,
        "limitWOresultsToDesignated": limit_wo,
        "nice_classes": nice_classes,
        "match_scope": match_scope,
        "web_search_enabled": bool_from_user(raw.get("web_search_enabled"), True),
        "include_goods": bool_from_user(raw.get("include_goods"), False),
        "mapping_notes": mapping_notes,
        "notes": as_text(raw.get("notes") or raw.get("assumptions")),
    }
    missing = missing_criteria(criteria)
    if missing and not allow_missing:
        raise ValueError("Missing required search criteria: " + ", ".join(missing))
    return criteria


def missing_criteria(criteria: Mapping[str, Any]) -> List[str]:
    missing: List[str] = []
    if not clean_mark(criteria.get("mark")):
        missing.append("mark")
    if not criteria.get("registrationOfficeCodes"):
        missing.append("jurisdictions/offices")
    if not criteria.get("nice_classes"):
        missing.append("nice_classes")
    return missing


# ---------------------------------------------------------------------------
# Workflow state persistence
# ---------------------------------------------------------------------------


def state_path_for(run_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(run_id or ""))
    if not safe:
        raise ValueError("run_id is required")
    return workflow_state_dir() / f"{safe}.json"


def save_state(state: Mapping[str, Any]) -> None:
    run_id = as_text(state.get("run_id"))
    if not run_id:
        raise ValueError("Cannot save workflow state without run_id")
    path = state_path_for(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = copy.deepcopy(dict(state))
    payload["updated_at"] = now_iso()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_state(run_id: str) -> Dict[str, Any]:
    path = state_path_for(run_id)
    if not path.exists():
        raise ValueError(f"Unknown or expired run_id: {run_id}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def new_state(criteria: Dict[str, Any], user_goal: str = "", language: str = "en") -> Dict[str, Any]:
    slug = safe_filename(criteria.get("mark") or "workflow")[:36]
    run_id = f"tko_{slug}_{uuid.uuid4().hex[:12]}"
    return {
        "schema_version": WORKFLOW_VERSION,
        "run_id": run_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "user_goal": user_goal,
        "language": language or "en",
        "criteria": criteria,
        "evidence": {
            "exact_search_result": None,
            "broad_search_results": None,
            "selected_ids": [],
            "trademark_content_result": None,
            "fulltext_result": None,
            "goods_results": None,
            "litigation_results": None,
            "web_findings": None,
        },
        "report_markdown": None,
        "validation_result": None,
        "pdf_result": None,
        "history": [],
    }


def append_history(state: MutableMapping[str, Any], event: str, details: Optional[Mapping[str, Any]] = None) -> None:
    history = state.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        state["history"] = history
    history.append({"at": now_iso(), "event": event, "details": dict(details or {})})
    # Keep state files compact.
    state["history"] = history[-80:]


def summarize_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    criteria = state.get("criteria") or {}
    evidence = state.get("evidence") or {}
    exact_count = result_count(evidence.get("exact_search_result"))
    broad_count = result_count(evidence.get("broad_search_results"))
    lit_count = result_count(evidence.get("litigation_results"))
    web_count = result_count(evidence.get("web_findings"))
    selected_ids = get_selected_ids(state)
    return {
        "run_id": state.get("run_id"),
        "mark": criteria.get("mark"),
        "offices": criteria.get("registrationOfficeCodes", []),
        "nice_classes": criteria.get("nice_classes", []),
        "missing_criteria": missing_criteria(criteria),
        "exact_result_count": exact_count,
        "broad_result_count": broad_count,
        "selected_ids": selected_ids,
        "trademark_details_loaded": bool(evidence.get("trademark_content_result")),
        "fulltext_loaded": bool(evidence.get("fulltext_result")),
        "litigation_result_count": lit_count,
        "web_result_count": web_count,
        "report_present": bool(state.get("report_markdown")),
        "validation_present": bool(state.get("validation_result")),
        "validation_valid": bool((state.get("validation_result") or {}).get("valid")),
        "pdf_present": bool(state.get("pdf_result")),
    }


# ---------------------------------------------------------------------------
# Evidence extraction and ranking helpers
# ---------------------------------------------------------------------------


def looks_like_record(mapping: Mapping[str, Any]) -> bool:
    keys = {str(key).lower() for key in mapping.keys()}
    signal_fragments = [
        "id",
        "trademark",
        "mark",
        "word",
        "verbal",
        "registration",
        "application",
        "applicant",
        "owner",
        "case_",
        "case",
        "docket",
        "party",
        "document",
        "url",
    ]
    return any(any(fragment in key for fragment in signal_fragments) for key in keys)


def flatten_records(value: Any, *, max_records: int = 250) -> List[Dict[str, Any]]:
    """Extract likely row dictionaries from varied MCP result shapes."""
    output: List[Dict[str, Any]] = []

    def visit(node: Any, depth: int = 0) -> None:
        if len(output) >= max_records or depth > 8:
            return
        if isinstance(node, list):
            for item in node:
                visit(item, depth + 1)
            return
        if not isinstance(node, dict):
            return

        if looks_like_record(node):
            # Avoid adding a pure wrapper that only contains result lists.
            listish_keys = {"results", "records", "items", "data", "trademarks", "cases", "content"}
            if not (set(str(key).lower() for key in node.keys()) <= listish_keys):
                output.append(node)

        preferred_keys = [
            "results",
            "records",
            "items",
            "data",
            "trademarks",
            "trademarkResults",
            "cases",
            "caseResults",
            "content",
            "rows",
        ]
        for key in preferred_keys:
            if key in node:
                visit(node[key], depth + 1)
        # As a fallback, inspect nested containers but avoid exploding every scalar.
        for child in node.values():
            if isinstance(child, (dict, list)):
                visit(child, depth + 1)

    visit(value)

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for record in output:
        fingerprint = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)[:2000]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(record)
    return deduped[:max_records]


def extract_ids(value: Any, *, max_ids: int = 100) -> List[str]:
    """Extract trademark IDs from common CompuMark MCP result shapes.

    The extractor is deliberately conservative: it favors keys that are clearly
    IDs or an `ids` array and avoids arbitrary registration/application numbers.
    """
    ids: List[str] = []
    seen = set()

    def add(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, (int, float)):
            text = str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw)
        else:
            text = str(raw).strip()
        if not text or len(text) > 160:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        ids.append(text)

    def visit(node: Any, parent_key: str = "", depth: int = 0) -> None:
        if len(ids) >= max_ids or depth > 8:
            return
        if isinstance(node, list):
            if parent_key.lower() in {"ids", "idlist", "trademarkids", "trademark_ids"}:
                for item in node:
                    add(item)
                return
            for item in node:
                visit(item, parent_key, depth + 1)
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key)
                key_norm = re.sub(r"[^a-z0-9]", "", key_text.lower())
                if key_norm in {"id", "ids", "trademarkid", "trademarkrecordid", "recordid", "guid"}:
                    if isinstance(value, list):
                        for item in value:
                            add(item)
                    else:
                        add(value)
                elif key_norm in {"results", "records", "items", "data", "trademarks", "content"}:
                    visit(value, key_text, depth + 1)
                elif isinstance(value, (dict, list)):
                    visit(value, key_text, depth + 1)

    visit(value)
    return ids[:max_ids]


def result_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ["count", "total", "totalCount", "resultCount", "numberOfResults", "hits"]:
            raw = deep_get_case_insensitive(value, [key])
            if isinstance(raw, (int, float)):
                return int(raw)
            if isinstance(raw, str) and raw.strip().isdigit():
                return int(raw.strip())
        ids = extract_ids(value)
        if ids:
            return len(ids)
        records = flatten_records(value)
        if records:
            return len(records)
        # A non-empty dict result may represent one object.
        return 1 if value else 0
    return 0


def extract_urls(value: Any) -> List[str]:
    if value is None:
        return []
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return unique_strings(URL_RE.findall(text))


def extract_fulltext_url(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        direct = deep_get_case_insensitive(value, ["url", "fullTextUrl", "fulltextUrl", "fulltext", "href", "link"])
        if isinstance(direct, str) and direct.startswith("http"):
            return direct
    urls = extract_urls(value)
    return urls[0] if urls else ""


def get_selected_ids(state: Mapping[str, Any]) -> List[str]:
    evidence = state.get("evidence") or {}
    existing = evidence.get("selected_ids") or []
    if existing:
        return unique_strings(existing, limit=5)
    exact_ids = extract_ids(evidence.get("exact_search_result"), max_ids=100)
    broad_ids = extract_ids(evidence.get("broad_search_results"), max_ids=100)
    return unique_strings([*exact_ids, *broad_ids], limit=5)


def update_selected_ids(state: MutableMapping[str, Any]) -> List[str]:
    evidence = state.setdefault("evidence", {})
    selected = get_selected_ids(state)
    evidence["selected_ids"] = selected
    return selected


def record_id(record: Mapping[str, Any]) -> str:
    return as_text(
        deep_get_case_insensitive(
            record,
            [
                "id",
                "ID",
                "trademarkId",
                "trademark_id",
                "recordId",
                "GUID",
            ],
        )
    )


def value_from_record(record: Mapping[str, Any], names: Sequence[str], default: str = "") -> str:
    value = deep_get_case_insensitive(record, names)
    return as_text(value, default)


def content_records_by_id(content_result: Any) -> Dict[str, Dict[str, Any]]:
    records = flatten_records(content_result)
    by_id: Dict[str, Dict[str, Any]] = {}
    for record in records:
        rid = record_id(record)
        if rid and rid not in by_id:
            by_id[rid] = record
    return by_id


def normalize_trademark_row(record: Optional[Mapping[str, Any]], fallback_id: str, fulltext_url: str) -> Dict[str, str]:
    record = record or {}
    verbal = value_from_record(
        record,
        [
            "wordMarkSpecification",
            "WORD_MARK_SPECIFICATION",
            "exactWordMarkSpecification",
            "TRADEMARK_VERBAL_ELEMENT",
            "verbalElement",
            "mark",
            "name",
        ],
        fallback_id or "Trademark reference",
    )
    status = value_from_record(record, ["status", "STATUS", "currentStatus", "TRADEMARK_STATUS"], "Not specified")
    office = value_from_record(
        record,
        [
            "registrationOfficeCode",
            "REGISTRATION_OFFICE_CODE",
            "registrationOffice",
            "office",
            "countryCode",
            "DOCKET_COURT_COUNTRY",
        ],
        "Not specified",
    )
    classes = value_from_record(
        record,
        [
            "classes",
            "niceClasses",
            "niceClass",
            "INT_CLASS_NUMBER",
            "TRADEMARK_NICE_CLASS",
            "classNumber",
        ],
        "Not specified",
    )
    number = value_from_record(
        record,
        [
            "registrationNumber",
            "REGISTRATION_NUMBER",
            "TRADEMARK_REGISTRATION_NUMBER",
            "applicationNumber",
            "APPLICATION_NUMBER",
        ],
        fallback_id or "Not specified",
    )
    date = value_from_record(
        record,
        [
            "registrationDate",
            "REGISTRATION_DATE",
            "applicationDate",
            "APPLICATION_DATE",
            "date",
        ],
        "Not specified",
    )
    owner = value_from_record(
        record,
        [
            "owner",
            "OWNER",
            "applicantName",
            "APPLICANT_NAME",
            "applicant",
            "holder",
            "PARTY_OPTIMIZED_NAME",
        ],
        "Not specified",
    )
    return {
        "verbal": verbal,
        "status": status,
        "office": office,
        "classes": classes,
        "number": number,
        "date": date,
        "owner": owner,
        "fulltext": markdown_link("full-text", fulltext_url) if fulltext_url else "Not available",
    }


def extract_owner_names(content_result: Any, limit: int = 5) -> List[str]:
    owners: List[str] = []
    for record in flatten_records(content_result):
        owner = value_from_record(
            record,
            ["owner", "OWNER", "applicantName", "APPLICANT_NAME", "applicant", "holder", "PARTY_OPTIMIZED_NAME"],
        )
        if owner:
            owners.append(owner)
    return unique_strings(owners, limit=limit)


def normalize_litigation_row(record: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    if not record:
        return {
            "parties": "No material source-backed litigation found",
            "case_type": "-",
            "jurisdiction": "-",
            "status": "-",
            "details": "No litigation result was supplied for this row.",
        }
    case_name = value_from_record(record, ["CASE_NAME", "caseName", "name"], "Unspecified parties")
    first_type = value_from_record(record, ["FIRST_ACTION_TYPE", "firstActionType", "CASE_DOMAIN", "caseDomain"], "Trademark matter")
    jurisdiction = value_from_record(
        record,
        ["DOCKET_COURT_COUNTRY", "docketCourtCountry", "DOCKET_COURT_AREA", "jurisdiction", "country"],
        "Not specified",
    )
    status = value_from_record(record, ["CASE_STATUS", "caseStatus", "status"], "Not specified")
    resolution = value_from_record(record, ["CASE_RESOLUTION", "caseResolution"], "")
    date = value_from_record(record, ["FIRST_ACTION_DATE", "firstActionDate", "DOCUMENT_DATE", "documentDate"], "")
    citation = value_from_record(record, ["DOCUMENT_CITATION_REFERENCE", "documentCitationReference", "citation"], "")
    details = "; ".join(part for part in [date, resolution, citation] if part) or "See CompuMark litigation record."
    return {
        "parties": case_name,
        "case_type": first_type,
        "jurisdiction": jurisdiction,
        "status": status,
        "details": details,
    }


def normalize_web_finding(value: Any) -> Dict[str, str]:
    if isinstance(value, Mapping):
        url = as_text(deep_get_case_insensitive(value, ["url", "link", "href", "source_url", "sourceUrl"]))
        source = as_text(deep_get_case_insensitive(value, ["source", "domain", "site", "title", "name"]))
        name = as_text(deep_get_case_insensitive(value, ["name", "sign", "title", "brand"]), source or "Web finding")
        territory = as_text(deep_get_case_insensitive(value, ["territory", "country", "region", "location"]), "Not specified")
        use_type = as_text(deep_get_case_insensitive(value, ["type", "type_of_use", "useType", "category"]), "Online use")
        notes = as_text(deep_get_case_insensitive(value, ["notes", "snippet", "summary", "description"]), "Potential online relevance.")
    else:
        text = as_text(value)
        urls = extract_urls(text)
        url = urls[0] if urls else ""
        source = domain_for(url) if url else "Search result"
        name = truncate(text.replace(url, ""), 60) or source
        territory = "Not specified"
        use_type = "Online use"
        notes = truncate(text, 140) or "Potential online relevance."
    label = domain_for(url) if url else source
    return {
        "name": name or "Web finding",
        "source": markdown_link(label, url) if url else source or "Not available",
        "territory": territory,
        "type_of_use": use_type,
        "notes": notes,
    }


def normalize_web_findings(value: Any, limit: int = 5) -> List[Dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ["findings", "results", "items", "data"]:
            nested = deep_get_case_insensitive(value, [key])
            if isinstance(nested, list):
                return [normalize_web_finding(item) for item in nested[:limit]]
        return [normalize_web_finding(value)]
    if isinstance(value, list):
        return [normalize_web_finding(item) for item in value[:limit]]
    text = as_text(value)
    if not text:
        return []
    return [normalize_web_finding(text)]


# ---------------------------------------------------------------------------
# External call builders
# ---------------------------------------------------------------------------


def external_call(tool: str, arguments: Dict[str, Any], *, save_as: str, why: str, required: bool = True, when: str = "now") -> Dict[str, Any]:
    return {
        "mcp_server_hint": "CompuMark TM & Litigation MCP" if tool in EXTERNAL_COMPUMARK_TOOL_NAMES.values() else "Host/browser tool",
        "tool_name": tool,
        "arguments": arguments,
        "save_result_as": save_as,
        "required": required,
        "when": when,
        "why": why,
    }


def exact_knockout_call(criteria: Mapping[str, Any]) -> Dict[str, Any]:
    return external_call(
        EXTERNAL_COMPUMARK_TOOL_NAMES["knockout"],
        {
            "registrationOfficeCodes": criteria.get("registrationOfficeCodes", []),
            "limitWOresultsToDesignated": bool(criteria.get("limitWOresultsToDesignated")),
            "classes": criteria.get("nice_classes", []),
            "trademarkName": criteria.get("mark", ""),
        },
        save_as="updates.exact_search_result",
        why="Identical knockout search in the requested offices/classes.",
    )


def broad_search_calls(criteria: Mapping[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for nice_class in criteria.get("nice_classes", []):
        calls.append(
            external_call(
                EXTERNAL_COMPUMARK_TOOL_NAMES["search"],
                {
                    "registrationOfficeCodes": criteria.get("registrationOfficeCodes", []),
                    "limitWOresultsToDesignated": bool(criteria.get("limitWOresultsToDesignated")),
                    "searchFields": [
                        {
                            "name": "WORD_MARK_SPECIFICATION",
                            "operator": "CONTAINS",
                            "value": criteria.get("mark", ""),
                        },
                        {
                            "name": "INT_CLASS_NUMBER",
                            "operator": "EQUALS",
                            "value": str(nice_class),
                        },
                    ],
                    "plurals": True,
                    "activeOnly": False,
                    "crossReferences": True,
                    "japanesePhonetics": True,
                    "centralEuropeanPhonetics": True,
                    "phonetics": True,
                },
                save_as="updates.broad_search_results[]",
                why=f"Broad contains/plural/phonetic search for Nice class {nice_class}; used only when exact results are five or fewer.",
            )
        )
    return calls


def detail_calls(criteria: Mapping[str, Any], ids: Sequence[str]) -> List[Dict[str, Any]]:
    calls = [
        external_call(
            EXTERNAL_COMPUMARK_TOOL_NAMES["content"],
            {"ids": list(ids)},
            save_as="updates.trademark_content_result",
            why="Fetch record details for the selected Top 5 trademark IDs.",
        ),
        external_call(
            EXTERNAL_COMPUMARK_TOOL_NAMES["fulltext"],
            {"ids": list(ids)},
            save_as="updates.fulltext_result",
            why="Create the HTML full-text link. Use the visible label 'full-text' in the report.",
        ),
    ]
    if criteria.get("include_goods"):
        for trademark_id in ids:
            calls.append(
                external_call(
                    EXTERNAL_COMPUMARK_TOOL_NAMES["goods"],
                    {"id": trademark_id},
                    save_as=f"updates.goods_results.{trademark_id}",
                    why="Fetch goods/services text because include_goods is enabled.",
                    required=False,
                )
            )
    return calls


def litigation_calls(criteria: Mapping[str, Any], owners: Sequence[str]) -> List[Dict[str, Any]]:
    mark = as_text(criteria.get("mark")).upper()
    base_fields = [
        "GUID",
        "CASE_NAME",
        "FIRST_ACTION_DATE",
        "FIRST_ACTION_TYPE",
        "CASE_DOMAIN",
        "CASE_STATUS",
        "CASE_RESOLUTION",
        "DOCKET_COURT_NAME",
        "DOCKET_COURT_AREA",
        "DOCKET_COURT_COUNTRY",
        "PARTY_OPTIMIZED_NAME",
        "PARTY_ROLE",
        "TRADEMARK_VERBAL_ELEMENT",
        "DOCUMENT_CITATION_REFERENCE",
    ]
    calls = [
        external_call(
            EXTERNAL_COMPUMARK_TOOL_NAMES["litigation"],
            {
                "request": {
                    "conditions": [
                        {"field": "CASE_DOMAIN", "op": "EQ", "value": "TRADEMARK", "logical_connector_to_next": "AND"},
                        {"field": "TRADEMARK_VERBAL_ELEMENT", "op": "LIKE", "value": mark, "logical_connector_to_next": "AND"},
                    ],
                    "fields": base_fields,
                    "group_by": ["GUID"],
                    "aggregate": [],
                    "order_by": [{"field": "FIRST_ACTION_DATE", "direction": "DESC"}],
                    "limit": 10,
                    "offset": 0,
                }
            },
            save_as="updates.litigation_results.mark_search",
            why="Search trademark litigation/opposition activity that mentions the searched verbal element. FIRST_ACTION_TYPE is returned as a field, not pre-filtered, so the agent can classify opposition/cancellation/infringement after retrieval.",
        )
    ]
    for owner in unique_strings(owners, limit=2):
        calls.append(
            external_call(
                EXTERNAL_COMPUMARK_TOOL_NAMES["litigation"],
                {
                    "request": {
                        "conditions": [
                            {"field": "CASE_DOMAIN", "op": "EQ", "value": "TRADEMARK", "logical_connector_to_next": "AND"},
                            {"field": "PARTY_OPTIMIZED_NAME", "op": "LIKE", "value": owner.upper(), "logical_connector_to_next": "AND"},
                            {"field": "TRADEMARK_VERBAL_ELEMENT", "op": "LIKE", "value": mark, "logical_connector_to_next": "AND"},
                        ],
                        "fields": base_fields,
                        "group_by": ["GUID"],
                        "aggregate": [],
                        "order_by": [{"field": "FIRST_ACTION_DATE", "direction": "DESC"}],
                        "limit": 5,
                        "offset": 0,
                    }
                },
                save_as="updates.litigation_results.owner_searches[]",
                why=f"Optional owner/mark litigation cross-check for {owner}.",
                required=False,
                when="after the mark litigation search, if time permits",
            )
        )
    return calls


def web_search_instruction(criteria: Mapping[str, Any]) -> Dict[str, Any]:
    mark = criteria.get("mark", "")
    territories = ", ".join(criteria.get("registrationOfficeCodes", [])) or "requested territory"
    return {
        "tool_name": "web_search_or_browser",
        "save_result_as": "updates.web_findings",
        "required": bool(criteria.get("web_search_enabled", True)),
        "query_guidance": [
            f'Find the 5 most relevant online commercial-use results for the exact name "{mark}" in or affecting {territories}.',
            f'Also check obvious close variants only if exact results are limited: "{mark}" brand, company, product, software, app, domain.',
            "Return each finding as {name, url, territory, type_of_use, notes}. Use source-backed facts only.",
        ],
    }


# ---------------------------------------------------------------------------
# Step selection
# ---------------------------------------------------------------------------


def evidence(state: Mapping[str, Any]) -> Mapping[str, Any]:
    return state.get("evidence") or {}


def next_step_id(state: Mapping[str, Any]) -> str:
    criteria = state.get("criteria") or {}
    ev = evidence(state)
    if missing_criteria(criteria):
        return "criteria"
    if ev.get("exact_search_result") is None:
        return "exact_search"
    exact_count = result_count(ev.get("exact_search_result"))
    if exact_count <= 5 and ev.get("broad_search_results") is None:
        return "broad_search"
    selected_ids = get_selected_ids(state)
    if selected_ids and (ev.get("trademark_content_result") is None or ev.get("fulltext_result") is None):
        return "trademark_details"
    if ev.get("litigation_results") is None:
        return "litigation"
    if criteria.get("web_search_enabled", True) and ev.get("web_findings") is None:
        return "web_search"
    if not state.get("report_markdown"):
        return "draft_report"
    validation = state.get("validation_result")
    if not validation or not validation.get("valid"):
        return "validate_report"
    if not state.get("pdf_result"):
        return "generate_pdf"
    return "complete"


def step_definition(step_id: str) -> Dict[str, Any]:
    for step in WORKFLOW_STEPS:
        if step["step_id"] == step_id:
            return step
    raise ValueError(f"Unknown step_id: {step_id}")


def step_payload(state: MutableMapping[str, Any]) -> Dict[str, Any]:
    step_id = next_step_id(state)
    criteria = state.get("criteria") or {}
    ev = state.get("evidence") or {}
    update_selected_ids(state)
    summary = summarize_state(state)

    base = {
        "workflow_id": WORKFLOW_VERSION,
        "run_id": state.get("run_id"),
        "next_step_id": step_id,
        "step_title": step_definition(step_id)["title"],
        "workflow_state_summary": summary,
        "done": step_id == "complete",
    }

    if step_id == "criteria":
        missing = missing_criteria(criteria)
        return {
            **base,
            "instructions": [
                "Ask the user for the first missing criterion only; do not run CompuMark yet.",
                "Required criteria are: exact mark, jurisdiction/offices, and Nice class numbers.",
                "When the user replies, call advance_trademark_knockout_workflow with updates.criteria.",
            ],
            "missing": missing,
            "expected_update_keys": ["updates.criteria"],
        }

    if step_id == "exact_search":
        return {
            **base,
            "instructions": [
                "Run the exact knockout search exactly as specified.",
                "Feed the raw result back as updates.exact_search_result.",
                "Do not fetch content/full-text yet; first determine whether a broad search is needed.",
            ],
            "external_tool_calls": [exact_knockout_call(criteria)],
            "expected_update_keys": ["updates.exact_search_result"],
        }

    if step_id == "broad_search":
        return {
            **base,
            "instructions": [
                "Exact results are five or fewer, so run the broad searches below.",
                "Run one broad search per Nice class to avoid accidental ANDing across classes.",
                "Feed the raw results back as updates.broad_search_results; preserve each class result.",
            ],
            "external_tool_calls": broad_search_calls(criteria),
            "expected_update_keys": ["updates.broad_search_results"],
        }

    if step_id == "trademark_details":
        selected_ids = get_selected_ids(state)
        return {
            **base,
            "instructions": [
                "Fetch content and create the full-text HTML link for the selected Top 5 IDs only.",
                "The full-text link must be shown in the report with visible label 'full-text'.",
                "Feed the raw content result as updates.trademark_content_result and the full-text result as updates.fulltext_result.",
            ],
            "selected_ids": selected_ids,
            "external_tool_calls": detail_calls(criteria, selected_ids),
            "expected_update_keys": ["updates.trademark_content_result", "updates.fulltext_result"],
        }

    if step_id == "litigation":
        owners = extract_owner_names(ev.get("trademark_content_result"))
        return {
            **base,
            "instructions": [
                "Run the mark litigation search; optional owner searches may help if material owners are known.",
                "Keep fields to 14 or fewer per CompuMark litigation API constraints.",
                "Feed all raw litigation results back as updates.litigation_results.",
            ],
            "external_tool_calls": litigation_calls(criteria, owners),
            "expected_update_keys": ["updates.litigation_results"],
        }

    if step_id == "web_search":
        return {
            **base,
            "instructions": [
                "Run an online presence search and capture up to five source-backed findings.",
                "Prioritize commercial brand/company/product/domain use over dictionary or irrelevant hits.",
                "Feed findings back as updates.web_findings with name, url, territory, type_of_use, and notes.",
            ],
            "web_search": web_search_instruction(criteria),
            "expected_update_keys": ["updates.web_findings"],
        }

    if step_id == "draft_report":
        return {
            **base,
            "instructions": [
                "Call draft_trademark_knockout_report with this run_id, or draft manually using get_trademark_knockout_report_template.",
                "The report must preserve the numbered structure and exactly five data rows in sections 2.2 and 3.2.",
                "Use only source-backed details supplied in workflow evidence; use neutral empty-row wording for missing findings.",
            ],
            "recommended_local_tool_call": {
                "tool_name": "draft_trademark_knockout_report",
                "arguments": {"run_id": state.get("run_id")},
                "save_result_as": "state.report_markdown",
            },
            "expected_update_keys": ["updates.report_markdown"],
        }

    if step_id == "validate_report":
        return {
            **base,
            "instructions": [
                "Call validate_trademark_knockout_report with the current report markdown.",
                "If validation fails, fix the report and validate again before rendering.",
            ],
            "recommended_local_tool_call": {
                "tool_name": "validate_trademark_knockout_report",
                "arguments": {"run_id": state.get("run_id")},
                "save_result_as": "state.validation_result",
            },
            "expected_update_keys": ["updates.validation_result"],
        }

    if step_id == "generate_pdf":
        return {
            **base,
            "instructions": [
                "Call generate_clarivate_report_pdf with this run_id.",
                "Return the generated pdf_url if configured; otherwise return the local pdf_path and explain that PUBLIC_BASE_URL is not configured.",
                "Do not fetch or inspect the generated PDF URL after this tool returns it.",
            ],
            "recommended_local_tool_call": {
                "tool_name": "generate_clarivate_report_pdf",
                "arguments": {"run_id": state.get("run_id")},
                "save_result_as": "state.pdf_result",
            },
            "expected_update_keys": ["updates.pdf_result"],
        }

    return {
        **base,
        "instructions": [
            "Workflow complete. Respond to the user with the report link/path and a concise caveat that this is not legal advice.",
        ],
        "final_response_guidance": final_response_guidance(state),
    }


# ---------------------------------------------------------------------------
# Tool implementations: workflow control
# ---------------------------------------------------------------------------


def start_workflow(arguments: Dict[str, Any]) -> Dict[str, Any]:
    raw_criteria = arguments.get("search_criteria") or arguments.get("criteria") or {}
    if not isinstance(raw_criteria, Mapping):
        raise ValueError("search_criteria must be an object")
    # Also accept top-level criteria for convenience.
    top_level = {key: arguments.get(key) for key in ["mark", "trademark", "subject", "jurisdictions", "offices", "registrationOfficeCodes", "nice_classes", "classes", "nice_class", "match_scope", "web_search_enabled", "include_goods", "notes"] if key in arguments}
    merged = {**dict(raw_criteria), **{key: value for key, value in top_level.items() if value is not None}}
    criteria = normalize_criteria(merged, allow_missing=True)
    state = new_state(criteria, user_goal=as_text(arguments.get("user_goal")), language=as_text(arguments.get("language"), "en"))
    append_history(state, "workflow_started", {"criteria": criteria})
    save_state(state)
    payload = step_payload(state)
    save_state(state)
    return {
        "workflow_id": WORKFLOW_VERSION,
        "run_id": state["run_id"],
        "state_file": str(state_path_for(state["run_id"])),
        "state_retention": "State is stored on disk under TRADEMARK_WORKFLOW_STATE_DIR or the output directory until deleted.",
        "workflow_steps": WORKFLOW_STEPS,
        "normalized_criteria": criteria,
        "next_step": payload,
    }


def apply_updates(state: MutableMapping[str, Any], updates: Mapping[str, Any]) -> None:
    if not updates:
        return
    evidence_state = state.setdefault("evidence", {})

    if "criteria" in updates and isinstance(updates["criteria"], Mapping):
        merged = {**dict(state.get("criteria") or {}), **dict(updates["criteria"])}
        state["criteria"] = normalize_criteria(merged, allow_missing=True)
        append_history(state, "criteria_updated", {"criteria": state["criteria"]})

    evidence_keys = [
        "exact_search_result",
        "broad_search_results",
        "trademark_content_result",
        "fulltext_result",
        "goods_results",
        "litigation_results",
        "web_findings",
    ]
    synonyms = {
        "exact_search": "exact_search_result",
        "exact_results": "exact_search_result",
        "broad_search": "broad_search_results",
        "broad_results": "broad_search_results",
        "trademark_content": "trademark_content_result",
        "content_result": "trademark_content_result",
        "fulltext": "fulltext_result",
        "full_text": "fulltext_result",
        "litigation": "litigation_results",
        "web": "web_findings",
        "web_results": "web_findings",
    }
    for raw_key, raw_value in updates.items():
        key = synonyms.get(raw_key, raw_key)
        if key in evidence_keys:
            # Append broad and owner-search arrays if caller sends incremental results.
            if key == "broad_search_results" and evidence_state.get(key) is not None:
                existing = ensure_list(evidence_state.get(key))
                evidence_state[key] = [*existing, raw_value]
            elif key == "litigation_results" and evidence_state.get(key) is not None and isinstance(evidence_state.get(key), Mapping) and isinstance(raw_value, Mapping):
                merged = dict(evidence_state.get(key) or {})
                merged.update(raw_value)
                evidence_state[key] = merged
            else:
                evidence_state[key] = raw_value
            append_history(state, f"{key}_updated", {"count": result_count(raw_value)})

    if "report_markdown" in updates:
        state["report_markdown"] = as_text(updates["report_markdown"])
        state["validation_result"] = None  # force revalidation after edits
        append_history(state, "report_markdown_updated", {"length": len(state["report_markdown"] or "")})
    if "validation_result" in updates and isinstance(updates["validation_result"], Mapping):
        state["validation_result"] = dict(updates["validation_result"])
        append_history(state, "validation_result_updated", {"valid": bool(state["validation_result"].get("valid"))})
    if "pdf_result" in updates and isinstance(updates["pdf_result"], Mapping):
        state["pdf_result"] = dict(updates["pdf_result"])
        append_history(state, "pdf_result_updated", {"pdf_path": state["pdf_result"].get("pdf_path")})

    update_selected_ids(state)


def advance_workflow(arguments: Dict[str, Any]) -> Dict[str, Any]:
    run_id = as_text(arguments.get("run_id"))
    if not run_id:
        raise ValueError("run_id is required. Call start_trademark_knockout_workflow first.")
    state = load_state(run_id)
    updates = arguments.get("updates") or {}
    if updates and not isinstance(updates, Mapping):
        raise ValueError("updates must be an object")
    apply_updates(state, updates)
    payload = step_payload(state)
    append_history(state, "advanced", {"next_step_id": payload.get("next_step_id")})
    save_state(state)
    return payload


def get_workflow_status(arguments: Dict[str, Any]) -> Dict[str, Any]:
    run_id = as_text(arguments.get("run_id"))
    state = load_state(run_id)
    payload = step_payload(state)
    save_state(state)
    return {
        "workflow_id": WORKFLOW_VERSION,
        "run_id": run_id,
        "state_file": str(state_path_for(run_id)),
        "summary": summarize_state(state),
        "next_step": payload,
    }


def get_report_template(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "template_markdown": REPORT_TEMPLATE,
        "rules": [
            "Keep numbered sections 1 through 4 exactly in this order.",
            "Sections 2.2 and 3.2 must each contain exactly five data rows, even when empty rows are needed.",
            "Use only 🟢 Low, 🟠 Medium, or 🔴 High for risk labels.",
            "CompuMark full-text links must use visible label full-text.",
            "Ordinary web links should use a domain label, not a raw URL label.",
            "Do not leave bracketed placeholders such as [MARK], [ROW 1], or [RISK].",
        ],
    }


# ---------------------------------------------------------------------------
# Tool implementation: drafting
# ---------------------------------------------------------------------------


def combined_litigation_records(litigation_result: Any) -> List[Dict[str, Any]]:
    return flatten_records(litigation_result, max_records=50)


def web_summary_from_findings(criteria: Mapping[str, Any], web_value: Any) -> Tuple[str, str, str, List[Dict[str, str]]]:
    if not criteria.get("web_search_enabled", True):
        return "Not performed (user opted out)", "Not performed (user opted out)", "Not performed (user opted out)", []
    findings = normalize_web_findings(web_value, limit=5)
    if not findings:
        return "No source-backed finding supplied", "No source-backed finding supplied", "No source-backed finding supplied", []
    mark = as_text(criteria.get("mark")).lower()
    exact = any(mark and mark in finding.get("name", "").lower() for finding in findings)
    commercial_terms = {"brand", "company", "product", "domain", "software", "app", "service", "online use"}
    commercial = any(any(term in finding.get("type_of_use", "").lower() for term in commercial_terms) for finding in findings)
    return ("Yes" if exact else "Limited"), "Limited", ("Yes" if commercial else "Limited"), findings


def infer_risk(state: Mapping[str, Any]) -> Tuple[str, List[str]]:
    ev = evidence(state)
    criteria = state.get("criteria") or {}
    exact_count = result_count(ev.get("exact_search_result"))
    broad_count = result_count(ev.get("broad_search_results"))
    litigation_records = combined_litigation_records(ev.get("litigation_results"))
    web_findings = normalize_web_findings(ev.get("web_findings"), limit=5)
    reasons: List[str] = []

    if exact_count > 0:
        risk = RISK_HIGH
        reasons.append(f"{exact_count} identical/knockout CompuMark result(s) were returned in the searched class scope.")
    elif broad_count > 0:
        risk = RISK_MEDIUM
        reasons.append(f"{broad_count} broad contains/phonetic/plural CompuMark result(s) were returned.")
    else:
        risk = RISK_LOW
        reasons.append("No CompuMark exact or broad result was supplied for the searched scope.")

    open_lit = 0
    for record in litigation_records:
        status = value_from_record(record, ["CASE_STATUS", "caseStatus", "status"]).upper()
        if "OPEN" in status or "ACTIVE" in status:
            open_lit += 1
    if litigation_records:
        reasons.append(f"{len(litigation_records)} litigation/opposition record(s) were supplied; {open_lit} appear open/active based on status fields.")
        if risk == RISK_LOW:
            risk = RISK_MEDIUM
    else:
        reasons.append("No source-backed litigation record was supplied.")

    if web_findings:
        reasons.append(f"{len(web_findings)} online finding(s) were supplied for marketplace context.")
        if risk == RISK_LOW:
            risk = RISK_MEDIUM
    elif criteria.get("web_search_enabled", True):
        reasons.append("No source-backed online finding was supplied.")
    else:
        reasons.append("Online search was not performed because it was disabled.")

    return risk, reasons


def build_top_trademark_rows(state: Mapping[str, Any]) -> List[Dict[str, str]]:
    ev = evidence(state)
    selected_ids = get_selected_ids(state)
    content_by_id = content_records_by_id(ev.get("trademark_content_result"))
    fulltext_url = extract_fulltext_url(ev.get("fulltext_result"))
    rows: List[Dict[str, str]] = []
    for trademark_id in selected_ids[:5]:
        rows.append(normalize_trademark_row(content_by_id.get(trademark_id), trademark_id, fulltext_url))

    # If content records exist but IDs did not match, use the first records as a fallback.
    if len(rows) < 5:
        used_ids = {record_id(content_by_id.get(trademark_id, {})) for trademark_id in selected_ids}
        for record in flatten_records(ev.get("trademark_content_result")):
            rid = record_id(record)
            if rid and rid in used_ids:
                continue
            rows.append(normalize_trademark_row(record, rid, fulltext_url))
            if len(rows) >= 5:
                break

    while len(rows) < 5:
        rows.append(
            {
                "verbal": "No further material source-backed finding",
                "status": "-",
                "office": "-",
                "classes": "-",
                "number": "-",
                "date": "-",
                "owner": "-",
                "fulltext": "-",
            }
        )
    return rows[:5]


def build_litigation_rows(state: Mapping[str, Any]) -> List[Dict[str, str]]:
    records = combined_litigation_records(evidence(state).get("litigation_results"))
    rows = [normalize_litigation_row(record) for record in records[:2]]
    while len(rows) < 2:
        rows.append(normalize_litigation_row(None))
    return rows[:2]


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("-" * max(3, len(header)) for header in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(escape_md_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def draft_report_from_state(state: Mapping[str, Any], *, extra_notes: str = "") -> str:
    criteria = state.get("criteria") or {}
    ev = evidence(state)
    risk, risk_reasons = infer_risk(state)
    exact_count = result_count(ev.get("exact_search_result"))
    broad_count = result_count(ev.get("broad_search_results"))
    total_reviewed = exact_count + broad_count
    selected_ids = get_selected_ids(state)
    top_rows = build_top_trademark_rows(state)
    litigation_rows = build_litigation_rows(state)
    web_exact, web_similar, web_commercial, web_rows = web_summary_from_findings(criteria, ev.get("web_findings"))
    while len(web_rows) < 5:
        web_rows.append(
            {
                "name": "No further material source-backed finding",
                "source": "-",
                "territory": "-",
                "type_of_use": "-",
                "notes": "-",
            }
        )
    web_rows = web_rows[:5]

    territories = ", ".join(criteria.get("registrationOfficeCodes", [])) or "Not specified"
    classes = ", ".join(criteria.get("nice_classes", [])) or "Not specified"
    notes = "; ".join(
        part
        for part in [
            criteria.get("notes"),
            "WIPO results limited to designated offices" if criteria.get("limitWOresultsToDesignated") else "",
            "; ".join(criteria.get("mapping_notes", [])),
            extra_notes,
        ]
        if part
    ) or "Knockout-stage report based only on supplied CompuMark, litigation, and web evidence."

    exact_comment = (
        f"Exact knockout search returned {exact_count} record(s) in the searched class/office scope."
        if exact_count
        else "Exact knockout search did not return a supplied record in the searched class/office scope."
    )
    broad_comment = (
        f"Broad contains/plural/phonetic search returned {broad_count} supplied record(s)."
        if broad_count
        else "Broad search did not return supplied records or was not required because exact results exceeded the broad-search threshold."
    )
    material_comment = (
        f"The Top 5 table prioritizes selected CompuMark IDs: {', '.join(selected_ids)}."
        if selected_ids
        else "No CompuMark IDs were selected for Top 5 review, so the table uses empty source-backed rows."
    )
    lit_count = len(combined_litigation_records(ev.get("litigation_results")))
    litigation_comment = (
        f"Litigation search supplied {lit_count} record(s); review the litigation table for status and jurisdiction details."
        if lit_count
        else "No source-backed litigation activity was supplied for inclusion."
    )
    web_comment = (
        "Online presence search was not performed because the workflow criteria disabled it."
        if not criteria.get("web_search_enabled", True)
        else ("Online presence findings were supplied and are summarized in the web findings table." if normalize_web_findings(ev.get("web_findings")) else "No source-backed online presence finding was supplied.")
    )

    report_parts = [
        "# AI Generated Trademark Knockout Search Report (Demo only)",
        "",
        f"Mark searched: {escape_md_cell(criteria.get('mark') or 'Not specified')}",
        f"Date of report: {today_display()}",
        "",
        "---",
        "",
        "## 1. Search Criteria",
        "",
        markdown_table(
            ["Field", "Details"],
            [
                ["Mark searched", criteria.get("mark") or "Not specified"],
                ["Type", criteria.get("type") or "Word"],
                ["Territories covered", territories],
                ["Nice classes", classes],
                ["Match scope", "Exact knockout; broad contains/plural/phonetic search only when exact results are five or fewer"],
                ["Notes / assumptions", notes],
            ],
        ),
        "",
        "---",
        "",
        "## 2. CompuMark Search Results",
        "",
        "### 2.1 Summary",
        "",
        markdown_table(
            ["Item", "Result"],
            [
                ["Total records reviewed", str(total_reviewed)],
                ["Most relevant jurisdictions", territories],
                ["Most relevant classes", classes],
                ["Overall initial risk impression", risk],
            ],
        ),
        "",
        "### 2.2 Most Relevant Trademark References (Top 5)",
        "",
        markdown_table(
            ["Verbal Element", "Status", "Registration Office", "Class(es)", "Number", "Date", "Owner", "Full Text URL"],
            [[row["verbal"], row["status"], row["office"], row["classes"], row["number"], row["date"], row["owner"], row["fulltext"]] for row in top_rows],
        ),
        "",
        "### 2.3 Litigation Activity",
        "",
        markdown_table(
            ["Parties", "Case Type", "Jurisdiction", "Status", "Key Details"],
            [[row["parties"], row["case_type"], row["jurisdiction"], row["status"], row["details"]] for row in litigation_rows],
        ),
        "",
        "### 2.4 Trademark Assessment Comments",
        "",
        f"* {exact_comment}",
        f"* {broad_comment}",
        f"* {material_comment}",
        f"* {litigation_comment}",
        "",
        "---",
        "",
        "## 3. Online Presence Search",
        "",
        "### 3.1 Summary",
        "",
        markdown_table(
            ["Item", "Result"],
            [
                ["Exact same name found online", web_exact],
                ["Similar names found online", web_similar],
                ["Commercial use observed", web_commercial],
            ],
        ),
        "",
        "### 3.2 Most Relevant Web Findings (Top 5)",
        "",
        markdown_table(
            ["Name / Sign", "Webpage URL / Source", "Territory", "Type of use", "Notes"],
            [[row["name"], row["source"], row["territory"], row["type_of_use"], row["notes"]] for row in web_rows],
        ),
        "",
        "### 3.3 Web Search Comments",
        "",
        f"* {web_comment}",
        "* Marketplace overlap should be reassessed by counsel if any web finding is in the same goods/services space as the proposed filing.",
        "* Domain or branding conflicts should be reviewed separately because online use evidence is not a substitute for trademark register evidence.",
        "",
        "---",
        "",
        "## 4. Key Takeaways",
        "",
        f"Overall clearance view: {risk}",
        "",
        f"* {risk_reasons[0] if risk_reasons else 'Risk is based on supplied knockout evidence.'}",
        f"* {risk_reasons[1] if len(risk_reasons) > 1 else 'Litigation activity was assessed only from supplied results.'}",
        f"* {risk_reasons[2] if len(risk_reasons) > 2 else 'Online use was assessed only from supplied results.'}",
        "* Treat this as a knockout-stage screen only; consider a full clearance search and attorney review before filing or launch.",
        "",
        "---",
        "",
        "Disclaimer",
        "",
        "This report is produced for informational purposes only and does not constitute legal advice. Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. Always consult a qualified trademark attorney before filing.",
    ]
    return "\n".join(report_parts).strip() + "\n"


def draft_report(arguments: Dict[str, Any]) -> Dict[str, Any]:
    run_id = as_text(arguments.get("run_id"))
    state = load_state(run_id)
    extra_notes = as_text(arguments.get("extra_notes"))
    markdown = draft_report_from_state(state, extra_notes=extra_notes)
    state["report_markdown"] = markdown
    state["validation_result"] = None
    append_history(state, "report_drafted", {"length": len(markdown)})
    payload = step_payload(state)
    save_state(state)
    risk, reasons = infer_risk(state)
    return {
        "run_id": run_id,
        "risk": risk,
        "risk_reasons": reasons,
        "markdown": markdown,
        "next_step": payload,
    }


# ---------------------------------------------------------------------------
# Tool implementation: validation
# ---------------------------------------------------------------------------


def table_lines_after_heading_pattern(markdown_text: str, heading_pattern: str) -> List[str]:
    lines = markdown_text.splitlines()
    pattern = re.compile(heading_pattern, re.IGNORECASE)
    try:
        start = next(idx for idx, line in enumerate(lines) if pattern.search(line.strip()))
    except StopIteration:
        return []
    table: List[str] = []
    found = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if stripped.startswith("|") and stripped.endswith("|"):
            table.append(stripped)
            found = True
        elif found and stripped:
            break
    return table


def count_markdown_table_data_rows(table_lines: List[str]) -> int:
    if not table_lines:
        return 0
    rows = [line for line in table_lines if not PIPE_SEPARATOR_RE.match(line)]
    return max(0, len(rows) - 1)


def validate_report_text(markdown_text: str) -> Dict[str, Any]:
    if not markdown_text or not markdown_text.strip():
        raise ValueError("markdown is required")
    issues: List[str] = []
    warnings: List[str] = []
    lines = markdown_text.splitlines()

    for pattern, label in REPORT_SECTION_PATTERNS:
        if not any(re.search(pattern, line.strip(), flags=re.IGNORECASE) for line in lines):
            issues.append(f"Missing required numbered {label}.")

    for section, pattern in TOP_5_TABLE_PATTERNS.items():
        rows = count_markdown_table_data_rows(table_lines_after_heading_pattern(markdown_text, pattern))
        if rows != 5:
            issues.append(f"Section {section} Top 5 table has {rows} data rows; expected exactly 5.")

    risk_tokens = set(re.findall(r"[🟢🟠🔴]\s*[A-Za-z]+", markdown_text))
    unsupported = sorted(token for token in risk_tokens if token not in RISK_LABELS)
    if unsupported:
        issues.append(f"Unsupported risk labels found: {', '.join(unsupported)}")
    if not any(label in markdown_text for label in RISK_LABELS):
        issues.append("No supported risk label found. Use 🟢 Low, 🟠 Medium, or 🔴 High.")

    placeholders = sorted(set(PLACEHOLDER_RE.findall(markdown_text)))
    # Do not treat link labels containing uppercase domains as placeholders.
    placeholders = [ph for ph in placeholders if not re.match(r"\[[A-Z0-9.-]+\]", ph)]
    if placeholders:
        issues.append("Unresolved placeholders remain: " + ", ".join(placeholders[:15]))

    for label, url in LINK_RE.findall(markdown_text):
        expected = domain_for(url)
        if label.lower().startswith("http"):
            warnings.append(f"Visible link text should not be a raw URL: {label}")
        if "fulltext" in url.lower() or "full-text" in url.lower():
            if label != "full-text":
                issues.append(f"CompuMark full-text link must use visible label 'full-text', found '{label}'.")
        elif label != expected and label != "full-text" and expected:
            warnings.append(f"Check link label '{label}' for URL {url}; preferred visible text is '{expected}'.")

    if "FULL_TEXT_URL" in markdown_text or "WEBPAGE_URL" in markdown_text:
        issues.append("Raw template URL placeholders remain.")

    return {"valid": not issues, "issues": issues, "warnings": warnings}


def validate_report(arguments: Dict[str, Any]) -> Dict[str, Any]:
    run_id = as_text(arguments.get("run_id"))
    markdown = as_text(arguments.get("markdown") or arguments.get("markdown_text"))
    state: Optional[Dict[str, Any]] = None
    if run_id:
        state = load_state(run_id)
        if not markdown:
            markdown = as_text(state.get("report_markdown"))
    result = validate_report_text(markdown)
    if state is not None:
        state["validation_result"] = result
        append_history(state, "report_validated", {"valid": result["valid"], "issue_count": len(result["issues"])})
        save_state(state)
    return result


# ---------------------------------------------------------------------------
# PDF generation helpers
# ---------------------------------------------------------------------------


def require_pdf_dependencies() -> None:
    missing = []
    if PdfReader is None:
        missing.append("pypdf")
    if canvas is None:
        missing.append("reportlab")
    if missing:
        raise RuntimeError(
            "Missing PDF dependencies: "
            + ", ".join(missing)
            + ". Install with: python3 -m pip install pypdf reportlab"
        )


def inline_markup(text: str) -> str:
    parts: List[str] = []
    last = 0
    for match in LINK_RE.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        label = match.group(1).strip() or domain_for(match.group(2))
        url = match.group(2).strip()
        parts.append('<link href="{}">{}</link>'.format(html.escape(url, quote=True), html.escape(label)))
        last = match.end()
    parts.append(html.escape(text[last:]))
    marked = "".join(parts)

    replacements = {
        RISK_LOW: '<font color="#188038">Low</font>',
        RISK_MEDIUM: '<font color="#b06000">Medium</font>',
        RISK_HIGH: '<font color="#b00020">High</font>',
    }
    for needle, replacement in replacements.items():
        marked = marked.replace(html.escape(needle), replacement)
        marked = marked.replace(needle, replacement)

    # Minimal markdown bold support after escaping.
    marked = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", marked)
    return marked


def build_styles() -> Dict[str, Any]:
    base = getSampleStyleSheet()
    styles: Dict[str, Any] = {}
    styles["title"] = ParagraphStyle(
        "TitleCustom",
        parent=base["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#222222"),
        spaceAfter=8,
    )
    styles["h1"] = ParagraphStyle(
        "Heading1Custom",
        parent=base["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#222222"),
        spaceBefore=8,
        spaceAfter=6,
    )
    styles["h2"] = ParagraphStyle(
        "Heading2Custom",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#222222"),
        spaceBefore=6,
        spaceAfter=4,
    )
    styles["h3"] = ParagraphStyle(
        "Heading3Custom",
        parent=base["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=13,
        textColor=colors.HexColor("#222222"),
        spaceBefore=5,
        spaceAfter=3,
    )
    styles["body"] = ParagraphStyle(
        "BodyCustom",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#222222"),
        spaceAfter=4,
    )
    styles["bullet"] = ParagraphStyle(
        "BulletCustom",
        parent=styles["body"],
        leftIndent=12,
        firstLineIndent=-8,
        bulletIndent=0,
    )
    styles["cell"] = ParagraphStyle(
        "CellCustom",
        parent=styles["body"],
        fontSize=7.0,
        leading=8.4,
        spaceAfter=0,
    )
    styles["cell_header"] = ParagraphStyle("CellHeaderCustom", parent=styles["cell"], fontName="Helvetica-Bold")
    return styles


def is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def parse_table_row(line: str) -> List[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def normalize_table(lines: Sequence[str]) -> List[List[str]]:
    rows = []
    for line in lines:
        if PIPE_SEPARATOR_RE.match(line):
            continue
        row = parse_table_row(line)
        if row:
            rows.append(row)
    if not rows:
        return []
    max_cols = max(len(row) for row in rows)
    return [row + [""] * (max_cols - len(row)) for row in rows]


def column_widths_for(headers: Sequence[str], available_width: float) -> List[float]:
    col_count = max(1, len(headers))
    normalized = [h.strip().lower() for h in headers]
    if col_count == 2:
        return [available_width * 0.28, available_width * 0.72]
    if "full text url" in normalized and col_count == 8:
        weights = [1.25, 0.9, 0.9, 0.65, 0.8, 0.75, 1.15, 0.85]
    elif "key details" in normalized and col_count == 5:
        weights = [1.35, 0.85, 0.85, 0.65, 1.6]
    elif "webpage url / source" in normalized and col_count == 5:
        weights = [1.1, 1.1, 0.75, 0.9, 1.7]
    else:
        weights = [1.0] * col_count
    total = sum(weights)
    return [available_width * weight / total for weight in weights]


def table_flowable(table_lines: Sequence[str], styles: Dict[str, Any]) -> Any:
    rows = normalize_table(table_lines)
    if not rows:
        return Spacer(1, 2)
    data = []
    for row_index, row in enumerate(rows):
        style = styles["cell_header"] if row_index == 0 else styles["cell"]
        data.append([Paragraph(inline_markup(cell), style) for cell in row])

    available_width = A4[0] - 36 * mm
    col_widths = column_widths_for(rows[0], available_width)
    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BDBDBD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
    ]
    for index in range(1, len(rows)):
        if index % 2 == 0:
            commands.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F7F7F7")))
    table.setStyle(TableStyle(commands))
    return table


def markdown_to_flowables(markdown_text: str, styles: Dict[str, Any]) -> List[Any]:
    flowables: List[Any] = []
    lines = markdown_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped in {"---", "***", "___"}:
            flowables.append(Spacer(1, 6))
            index += 1
            continue
        if is_table_line(stripped):
            table_lines = [stripped]
            index += 1
            while index < len(lines) and is_table_line(lines[index].strip()):
                table_lines.append(lines[index].strip())
                index += 1
            flowables.append(table_flowable(table_lines, styles))
            flowables.append(Spacer(1, 6))
            continue
        if stripped.startswith("# "):
            flowables.append(Paragraph(inline_markup(stripped[2:].strip()), styles["title"]))
        elif stripped.startswith("## "):
            flowables.append(Paragraph(inline_markup(stripped[3:].strip()), styles["h1"]))
        elif stripped.startswith("### "):
            flowables.append(Paragraph(inline_markup(stripped[4:].strip()), styles["h2"]))
        elif stripped.startswith("#### "):
            flowables.append(Paragraph(inline_markup(stripped[5:].strip()), styles["h3"]))
        elif stripped.startswith("* ") or stripped.startswith("- "):
            flowables.append(Paragraph("• " + inline_markup(stripped[2:].strip()), styles["bullet"]))
        else:
            flowables.append(Paragraph(inline_markup(stripped), styles["body"]))
        index += 1
    return flowables


def build_body_pdf(markdown_text: str, output_path: Path) -> None:
    styles = build_styles()
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Trademark Knockout Search Report",
        author="Clarivate workflow MCP",
    )
    doc.build(markdown_to_flowables(markdown_text, styles))


def subtitle_font_name() -> str:
    try:
        pdfmetrics.getFont(SUBTITLE_FONT)
        return SUBTITLE_FONT
    except Exception:
        return SUBTITLE_FONT_FALLBACK


def build_overlay_pdf(subject: str, output_path: Path) -> None:
    pdf_canvas = canvas.Canvas(str(output_path), pagesize=(PAGE_W, PAGE_H))
    pdf_canvas.setFillColor(colors.white)
    pdf_canvas.rect(SUBTITLE_COVER_X, SUBTITLE_COVER_Y, SUBTITLE_COVER_W, SUBTITLE_COVER_H, stroke=0, fill=1)
    pdf_canvas.setFillColor(colors.HexColor("#222222"))
    pdf_canvas.setFont(subtitle_font_name(), SUBTITLE_FONT_SIZE)
    pdf_canvas.drawString(SUBTITLE_X, SUBTITLE_BASELINE_Y, clean_mark(subject) or "Trademark Report")
    pdf_canvas.save()


def merge_template(template_path: Path, body_path: Path, overlay_path: Path, output_path: Path) -> None:
    template_reader = PdfReader(str(template_path))
    if len(template_reader.pages) < 2:
        raise ValueError("Template PDF must contain at least two pages: cover and closing/about page.")
    overlay_reader = PdfReader(str(overlay_path))
    body_reader = PdfReader(str(body_path))
    writer = PdfWriter()

    cover_page = template_reader.pages[0]
    cover_page.merge_page(overlay_reader.pages[0])
    writer.add_page(cover_page)
    for page in body_reader.pages:
        writer.add_page(page)
    writer.add_page(template_reader.pages[1])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        writer.write(handle)


def resolve_output_path(output_path: Optional[str], subject: str) -> Path:
    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = default_output_dir() / path
    else:
        path = default_output_dir() / f"trademark_report_{safe_filename(subject)}.pdf"
    if path.suffix.lower() != ".pdf":
        raise ValueError("Output filename must end with .pdf")
    return path.resolve()


def generate_pdf_core(
    *,
    subject: str,
    markdown_text: str,
    output_path: Optional[str] = None,
    template_path: Optional[str] = None,
    save_markdown: bool = True,
    markdown_output_path: Optional[str] = None,
) -> Dict[str, Any]:
    require_pdf_dependencies()
    subject = clean_mark(subject)
    if not subject:
        raise ValueError("subject is required")
    if not markdown_text or not markdown_text.strip():
        raise ValueError("markdown is required")

    template = Path(template_path or DEFAULT_TEMPLATE_PATH).expanduser()
    if not template.is_absolute():
        template = BASE_DIR / template
    if not template.exists():
        raise FileNotFoundError(f"Template PDF not found: {template}")

    pdf_path = resolve_output_path(output_path, subject)
    md_path: Optional[Path] = None
    if save_markdown:
        if markdown_output_path:
            md_path = Path(markdown_output_path).expanduser()
            if not md_path.is_absolute():
                md_path = default_output_dir() / md_path
        else:
            md_path = pdf_path.with_suffix(".md")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown_text, encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        body_pdf = tmpdir / "report_body.pdf"
        overlay_pdf = tmpdir / "cover_overlay.pdf"
        build_body_pdf(markdown_text, body_pdf)
        build_overlay_pdf(subject, overlay_pdf)
        merge_template(template, body_pdf, overlay_pdf, pdf_path)

    if not pdf_path.exists() or pdf_path.stat().st_size <= 0:
        raise RuntimeError(f"PDF generation failed: {pdf_path}")

    pdf_url = public_file_url(pdf_path)
    md_url = public_file_url(md_path)
    return {
        "pdf_path": str(pdf_path),
        "pdf_file_uri": pdf_path.as_uri(),
        "pdf_url": pdf_url,
        "download_the_report": pdf_url,
        "pdf_exists": True,
        "pdf_size_bytes": pdf_path.stat().st_size,
        "markdown_path": str(md_path) if md_path else None,
        "markdown_url": md_url,
        "template_path": str(template.resolve()),
        "pdf_generation_workflow": "Clarivate template merge: template cover + generated report body + template closing/about page",
        "final_response_instruction": (
            "Use pdf_url/download_the_report if present. If pdf_url is null, use the local pdf_path/pdf_file_uri and state that PUBLIC_BASE_URL is not configured."
        ),
    }


def generate_pdf(arguments: Dict[str, Any]) -> Dict[str, Any]:
    run_id = as_text(arguments.get("run_id"))
    state: Optional[Dict[str, Any]] = None
    markdown = as_text(arguments.get("markdown") or arguments.get("markdown_text"))
    subject = clean_mark(arguments.get("subject") or arguments.get("mark") or "")

    if run_id:
        state = load_state(run_id)
        criteria = state.get("criteria") or {}
        if not markdown:
            markdown = as_text(state.get("report_markdown"))
        if not subject:
            subject = as_text(criteria.get("mark"))
        validation = state.get("validation_result")
        if not validation or not validation.get("valid"):
            validation = validate_report_text(markdown)
            state["validation_result"] = validation
            if not validation.get("valid"):
                save_state(state)
                raise ValueError("Report validation failed; fix issues before PDF generation: " + "; ".join(validation.get("issues", [])))

    result = generate_pdf_core(
        subject=subject,
        markdown_text=markdown,
        output_path=arguments.get("output_path"),
        template_path=arguments.get("template_path"),
        save_markdown=bool_from_user(arguments.get("save_markdown"), True),
        markdown_output_path=arguments.get("markdown_output_path"),
    )
    if state is not None:
        state["pdf_result"] = result
        append_history(state, "pdf_generated", {"pdf_path": result.get("pdf_path"), "pdf_url": result.get("pdf_url")})
        save_state(state)
    return result


def final_response_guidance(state: Mapping[str, Any]) -> Dict[str, Any]:
    pdf = state.get("pdf_result") or {}
    if pdf.get("pdf_url"):
        link = pdf.get("pdf_url")
        instruction = "Return this as a hyperlink labeled download the report."
    else:
        link = pdf.get("pdf_path") or pdf.get("pdf_file_uri")
        instruction = "PUBLIC_BASE_URL is not configured, so return the local path/file URI instead of a public hyperlink."
    return {
        "report_link": link,
        "instruction": instruction,
        "disclaimer": "This knockout report is informational only and not legal advice.",
    }


# ---------------------------------------------------------------------------
# Schemas and tool registry
# ---------------------------------------------------------------------------


OBJECT_SCHEMA = {"type": "object", "additionalProperties": True}
STRING_ARRAY_SCHEMA = {"type": "array", "items": {"type": "string"}}
NULLABLE_STRING_SCHEMA = {"type": ["string", "null"]}

START_WORKFLOW_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["workflow_id", "run_id", "normalized_criteria", "next_step"],
    "properties": {
        "workflow_id": {"type": "string"},
        "run_id": {"type": "string"},
        "state_file": {"type": "string"},
        "state_retention": {"type": "string"},
        "workflow_steps": {"type": "array", "items": OBJECT_SCHEMA},
        "normalized_criteria": OBJECT_SCHEMA,
        "next_step": OBJECT_SCHEMA,
    },
    "additionalProperties": True,
}

ADVANCE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["workflow_id", "run_id", "next_step_id", "step_title", "workflow_state_summary", "done"],
    "properties": {
        "workflow_id": {"type": "string"},
        "run_id": {"type": "string"},
        "next_step_id": {"type": "string"},
        "step_title": {"type": "string"},
        "workflow_state_summary": OBJECT_SCHEMA,
        "instructions": STRING_ARRAY_SCHEMA,
        "external_tool_calls": {"type": "array", "items": OBJECT_SCHEMA},
        "web_search": OBJECT_SCHEMA,
        "recommended_local_tool_call": OBJECT_SCHEMA,
        "expected_update_keys": STRING_ARRAY_SCHEMA,
        "done": {"type": "boolean"},
    },
    "additionalProperties": True,
}

VALIDATION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["valid", "issues", "warnings"],
    "properties": {"valid": {"type": "boolean"}, "issues": STRING_ARRAY_SCHEMA, "warnings": STRING_ARRAY_SCHEMA},
    "additionalProperties": False,
}

PDF_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["pdf_path", "pdf_exists", "pdf_size_bytes"],
    "properties": {
        "pdf_path": {"type": "string"},
        "pdf_file_uri": {"type": "string"},
        "pdf_url": NULLABLE_STRING_SCHEMA,
        "download_the_report": NULLABLE_STRING_SCHEMA,
        "pdf_exists": {"type": "boolean"},
        "pdf_size_bytes": {"type": "integer"},
        "markdown_path": NULLABLE_STRING_SCHEMA,
        "markdown_url": NULLABLE_STRING_SCHEMA,
        "template_path": {"type": "string"},
        "pdf_generation_workflow": {"type": "string"},
        "final_response_instruction": {"type": "string"},
    },
    "additionalProperties": True,
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]
    annotations: Optional[Dict[str, Any]] = None


def tool_specs() -> List[ToolSpec]:
    return [
        ToolSpec(
            name="start_trademark_knockout_workflow",
            title="Start trademark knockout workflow",
            description=(
                "Start a staged trademark knockout report workflow. Returns an explicit run_id handle; "
                "carry it into later workflow calls. This tool does not call CompuMark itself."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_goal": {"type": "string"},
                    "language": {"type": "string", "default": "en"},
                    "search_criteria": OBJECT_SCHEMA,
                    "mark": {"type": "string"},
                    "jurisdictions": {"type": "array", "items": {"type": "string"}},
                    "nice_classes": {"type": "array", "items": {"type": "string"}},
                    "web_search_enabled": {"type": "boolean", "default": True},
                    "include_goods": {"type": "boolean", "default": False},
                },
                "additionalProperties": True,
            },
            output_schema=START_WORKFLOW_OUTPUT_SCHEMA,
            handler=start_workflow,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        ),
        ToolSpec(
            name="advance_trademark_knockout_workflow",
            title="Advance trademark knockout workflow",
            description=(
                "Apply completed-step updates and return the next actionable step. Use this after each external "
                "CompuMark/web call and after local drafting/validation/PDF generation."
            ),
            input_schema={
                "type": "object",
                "required": ["run_id"],
                "properties": {
                    "run_id": {"type": "string"},
                    "updates": {
                        "type": "object",
                        "description": "Use keys such as criteria, exact_search_result, broad_search_results, trademark_content_result, fulltext_result, litigation_results, web_findings, report_markdown, validation_result, pdf_result.",
                        "additionalProperties": True,
                    },
                },
                "additionalProperties": False,
            },
            output_schema=ADVANCE_OUTPUT_SCHEMA,
            handler=advance_workflow,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        ),
        ToolSpec(
            name="get_trademark_knockout_workflow_status",
            title="Get workflow status",
            description="Return the current workflow state summary and next step for a run_id.",
            input_schema={
                "type": "object",
                "required": ["run_id"],
                "properties": {"run_id": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["workflow_id", "run_id", "summary", "next_step"], "properties": {"workflow_id": {"type": "string"}, "run_id": {"type": "string"}, "summary": OBJECT_SCHEMA, "next_step": OBJECT_SCHEMA}, "additionalProperties": True},
            handler=get_workflow_status,
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        ),
        ToolSpec(
            name="get_trademark_knockout_report_template",
            title="Get report template",
            description="Return the fixed markdown report template and drafting rules.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object", "required": ["template_markdown", "rules"], "properties": {"template_markdown": {"type": "string"}, "rules": STRING_ARRAY_SCHEMA}, "additionalProperties": False},
            handler=get_report_template,
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        ),
        ToolSpec(
            name="draft_trademark_knockout_report",
            title="Draft trademark knockout report",
            description=(
                "Draft and persist the markdown report from evidence already stored under run_id. "
                "The draft is conservative and source-backed; the agent may edit and revalidate."
            ),
            input_schema={
                "type": "object",
                "required": ["run_id"],
                "properties": {"run_id": {"type": "string"}, "extra_notes": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["run_id", "risk", "risk_reasons", "markdown", "next_step"], "properties": {"run_id": {"type": "string"}, "risk": {"type": "string"}, "risk_reasons": STRING_ARRAY_SCHEMA, "markdown": {"type": "string"}, "next_step": OBJECT_SCHEMA}, "additionalProperties": True},
            handler=draft_report,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        ),
        ToolSpec(
            name="validate_trademark_knockout_report",
            title="Validate knockout report",
            description="Validate markdown structure, Top 5 row counts, risk labels, placeholders, and link labels. Accepts run_id or raw markdown.",
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}, "markdown": {"type": "string"}, "markdown_text": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema=VALIDATION_OUTPUT_SCHEMA,
            handler=validate_report,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        ),
        ToolSpec(
            name="generate_clarivate_report_pdf",
            title="Generate Clarivate report PDF",
            description=(
                "Generate the final PDF using the same Clarivate template merge: template cover, generated body, template closing page. "
                "Requires a valid report. Accepts run_id or explicit subject+markdown."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "mark": {"type": "string"},
                    "markdown": {"type": "string"},
                    "markdown_text": {"type": "string"},
                    "output_path": {"type": "string"},
                    "template_path": {"type": "string"},
                    "save_markdown": {"type": "boolean", "default": True},
                    "markdown_output_path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            output_schema=PDF_OUTPUT_SCHEMA,
            handler=generate_pdf,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        ),
    ]


TOOLS: Dict[str, ToolSpec] = {spec.name: spec for spec in tool_specs()}


# ---------------------------------------------------------------------------
# JSON-RPC request handling
# ---------------------------------------------------------------------------


def handle_initialize(message_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    protocol = params.get("protocolVersion") or "2025-06-18"
    return json_rpc_result(
        message_id,
        {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def handle_tools_list(message_id: Any, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tools = []
    for spec in TOOLS.values():
        item = {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "inputSchema": spec.input_schema,
            "outputSchema": spec.output_schema,
        }
        if spec.annotations:
            item["annotations"] = spec.annotations
        tools.append(item)
    return json_rpc_result(message_id, {"tools": tools})


def handle_tools_call(message_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name not in TOOLS:
        return json_rpc_error(message_id, -32602, f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return json_rpc_result(message_id, text_tool_result({"error": "arguments must be an object", "tool": name}, is_error=True))
    spec = TOOLS[name]
    try:
        payload = spec.handler(arguments)
        resource_links: List[Dict[str, str]] = []
        if name == "generate_clarivate_report_pdf" and isinstance(payload, Mapping) and payload.get("pdf_file_uri"):
            resource_links.append(
                {
                    "uri": str(payload["pdf_file_uri"]),
                    "name": Path(str(payload.get("pdf_path") or "report.pdf")).name,
                    "description": "Generated Clarivate-template trademark knockout report PDF",
                    "mimeType": "application/pdf",
                }
            )
        return json_rpc_result(message_id, text_tool_result(payload, resource_links=resource_links))
    except Exception as exc:
        return json_rpc_result(message_id, text_tool_result({"error": str(exc), "tool": name}, is_error=True))


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if message_id is None and str(method).startswith("notifications/"):
        return None
    if method == "initialize":
        return handle_initialize(message_id, params)
    if method == "ping":
        return json_rpc_result(message_id, {})
    if method == "tools/list":
        return handle_tools_list(message_id, params)
    if method == "tools/call":
        return handle_tools_call(message_id, params)
    if method == "resources/list":
        return json_rpc_result(message_id, {"resources": []})
    if method == "prompts/list":
        return json_rpc_result(message_id, {"prompts": []})
    return json_rpc_error(message_id, -32601, f"Method not found: {method}")


def run_stdio() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle_request(message)
        except Exception as exc:
            response = json_rpc_error(None, -32700, "Parse error", str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------


def self_test() -> int:
    criteria = {
        "mark": "NOVALYTIC",
        "jurisdictions": ["EU", "UK"],
        "nice_classes": ["9", "42"],
    }
    start = start_workflow({"user_goal": "Generate a report", "search_criteria": criteria})
    run_id = start["run_id"]
    # Simulate a no-hit exact and broad search to exercise drafting and validation.
    advance_workflow({"run_id": run_id, "updates": {"exact_search_result": {"ids": []}}})
    advance_workflow({"run_id": run_id, "updates": {"broad_search_results": [{"ids": []}]}})
    advance_workflow({"run_id": run_id, "updates": {"litigation_results": {"results": []}}})
    advance_workflow({"run_id": run_id, "updates": {"web_findings": []}})
    draft = draft_report({"run_id": run_id})
    validation = validate_report({"run_id": run_id})
    status = get_workflow_status({"run_id": run_id})
    print(
        stable_json(
            {
                "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "tools": list(TOOLS.keys()),
                "sample_run_id": run_id,
                "start_next_step": start["next_step"]["next_step_id"],
                "draft_length": len(draft["markdown"]),
                "validation": validation,
                "current_next_step": status["next_step"]["next_step_id"],
                "state_file": str(state_path_for(run_id)),
            }
        )
    )
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the trademark knockout report MCP server.")
    parser.add_argument("--self-test", action="store_true", help="Run a local workflow smoke test and exit.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        return self_test()
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
