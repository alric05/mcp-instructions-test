#!/usr/bin/env python3
"""Standalone MCP server for trademark knockout report workflows.

This server intentionally does not depend on Codex plugin skills. It exposes the
workflow guidance and deterministic Clarivate-template PDF generation as MCP
tools, while live trademark and litigation data should still come from the
existing Clarivate CompuMark MCP connector.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

try:
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


SERVER_NAME = "trademark-knockout-report-workflow"
SERVER_VERSION = "0.1.0"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "assets" / "Clarivate_template.pdf"

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

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
PIPE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
RISK_LABELS = {"🟢 Low", "🟠 Medium", "🔴 High"}
SUPPORTED_RISK_LABELS = RISK_LABELS | {
    "🟢 Bajo",
    "🟠 Medio",
    "🔴 Alto",
}

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

WORKFLOW_ID = "trademark_knockout_report_v1"
WORKFLOW_NAME = "trademark_knockout_report"
WORKFLOW_GOAL = "Generate a source-backed trademark knockout report and Clarivate-template PDF."
WORKFLOW_STEPS = [
    {"step_id": "criteria", "title": "Confirm criteria"},
    {"step_id": "plan", "title": "Build call plan"},
    {"step_id": "collect", "title": "Collect evidence"},
    {"step_id": "report", "title": "Analyze and draft"},
    {"step_id": "deliver", "title": "Validate, render, respond"},
]
WORKFLOW_STEP_IDS = [step["step_id"] for step in WORKFLOW_STEPS]

REPORT_SECTION_PATTERNS = [
    (r"^##\s+1\.", "section 1 Search Criteria"),
    (r"^##\s+2\.", "section 2 CompuMark Search Results"),
    (r"^###\s+2\.1\b", "section 2.1 Summary"),
    (r"^###\s+2\.2\b", "section 2.2 Top 5 Trademark References"),
    (r"^###\s+2\.3\b", "section 2.3 Litigation Activity"),
    (r"^###\s+2\.4\b", "section 2.4 Trademark Assessment Comments"),
    (r"^##\s+3\.", "section 3 Online Presence Search"),
    (r"^###\s+3\.1\b", "section 3.1 Summary"),
    (r"^###\s+3\.2\b", "section 3.2 Top 5 Web Findings"),
    (r"^###\s+3\.3\b", "section 3.3 Web Search Comments"),
    (r"^##\s+4\.", "section 4 Key Takeaways"),
]

TOP_5_TABLE_PATTERNS = {
    "2.2": r"^###\s+2\.2\b",
    "3.2": r"^###\s+3\.2\b",
}

REPORT_TEMPLATE = """# AI Generated Trademark Knockout Search Report (Demo only)

Mark searched: [MARK]
Date of report: [DATE]

---

## 1. Search Criteria

| Field               | Details                                       |
| ------------------- | --------------------------------------------- |
| Mark searched       | [MARK]                                        |
| Type                | [Word / Logo / Both]                          |
| Territories covered | [EU / UK / US / WIPO designations / Other]    |
| Nice classes        | [CLASS NUMBERS, if known]                     |
| Match scope         | [Exact only / Contains / Phonetic / Plurals]  |
| Notes / assumptions | [Any limitations, exclusions, or assumptions] |

---

## 2. CompuMark Search Results

### 2.1 Summary

| Item                            | Result                |
| ------------------------------- | --------------------- |
| Total records reviewed          | [NUMBER / APPROX.]    |
| Most relevant jurisdictions     | [LIST]                |
| Most relevant classes           | [LIST]                |
| Overall initial risk impression | [🟢 Low / 🟠 Medium / 🔴 High] |

### 2.2 Most Relevant Trademark References (Top 5)

| Verbal Element     | Status   | Registration Office | Class(es) | Number   | Date   | Owner   | Full Text URL   |
| ------------------ | -------- | ------------------- | --------- | -------- | ------ | ------- | --------------- |
| [VERBAL ELEMENT 1] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [domain.tld](FULL_TEXT_URL) |
| [VERBAL ELEMENT 2] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [domain.tld](FULL_TEXT_URL) |
| [VERBAL ELEMENT 3] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [domain.tld](FULL_TEXT_URL) |
| [VERBAL ELEMENT 4] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [domain.tld](FULL_TEXT_URL) |
| [VERBAL ELEMENT 5] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [domain.tld](FULL_TEXT_URL) |

### 2.3 Litigation Activity

| Parties              | Case Type                                  | Jurisdiction | Status               | Key Details |
| -------------------- | ------------------------------------------ | ------------ | -------------------- | ----------- |
| [PARTY 1 vs PARTY 2] | [Opposition / Infringement / Cancellation] | [COUNTRY]    | [Active / Concluded] | [Summary]   |
| [PARTY 1 vs PARTY 2] | [Opposition / Infringement / Cancellation] | [COUNTRY]    | [Active / Concluded] | [Summary]   |

### 2.4 Trademark Assessment Comments

* [State whether exact matches were found in the main class.]
* [State whether similar or phonetic matches were found.]
* [State whether any exact matches were found outside the main class, if searched.]
* [State which results appear most material and why.]
* [State whether litigation activity was found, the type of case, and if it adds risk.]

---

## 3. Online Presence Search

### 3.1 Summary

| Item                         | Result               |
| ---------------------------- | -------------------- |
| Exact same name found online | [Yes / No / Limited / Not performed (user opted out)] |
| Similar names found online   | [Yes / No / Not performed (user opted out)]           |
| Commercial use observed      | [Yes / No / Limited / Not performed (user opted out)] |

### 3.2 Most Relevant Web Findings (Top 5)

| Name / Sign | Webpage URL / Source | Territory          | Type of use                                         | Notes            |
| ----------- | -------------------- | ------------------ | --------------------------------------------------- | ---------------- |
| [NAME 1]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 2]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 3]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 4]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 5]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |

### 3.3 Web Search Comments

* [State whether the searched name appears to be in active commercial use online.]
* [State whether similar names create practical marketplace overlap.]
* [State whether any domain or branding conflicts are notable.]
* [If web search was not run, state: "Online presence search not performed (user opted out)."]

---

## 4. Key Takeaways

Overall clearance view: [🟢 Low / 🟠 Medium / 🔴 High concern]

* [Key takeaway 1: concise conclusion on trademark database results.]
* [Key takeaway 2: concise conclusion on online use / marketplace presence.]
* [Key takeaway 3: note on main legal or commercial risk.]
* [Key takeaway 4: optional recommendation, e.g. proceed / proceed with caution / consider narrowing / consider alternate mark.]

---

Disclaimer

This report is produced for informational purposes only and does not constitute legal advice. Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. Always consult a qualified trademark attorney before filing.
"""


def text_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def json_rpc_result(message_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def json_rpc_error(message_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def clean_mark(value: str) -> str:
    return " ".join((value or "").strip().split())


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_mark(value)).strip("._")
    return name or "trademark_report"


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
        if not text.isdigit():
            raise ValueError(f"Nice class must be a number from 1 to 45: {text}")
        number = int(text)
        if number < 1 or number > 45:
            raise ValueError(f"Nice class out of range 1 to 45: {text}")
        normalized = str(number)
        if normalized not in output:
            output.append(normalized)
    if not output:
        raise ValueError("At least one Nice class is required.")
    return output


def normalize_jurisdictions(jurisdictions: Any) -> Tuple[List[str], List[str], bool]:
    if isinstance(jurisdictions, str):
        raw_parts = [part.strip() for part in re.split(r"[,;/]+", jurisdictions) if part.strip()]
    else:
        raw_parts = [str(part).strip() for part in jurisdictions or [] if str(part).strip()]
    if not raw_parts:
        raise ValueError("At least one jurisdiction or registration office is required.")

    offices: List[str] = []
    notes: List[str] = []
    saw_specific_country = False
    saw_eu_scope = False

    for raw in raw_parts:
        key = re.sub(r"\s+", " ", raw.strip().upper())
        code = OFFICE_ALIASES.get(key, key if re.fullmatch(r"[A-Z]{2}", key) else None)
        if not code:
            notes.append(
                f"Could not confidently map jurisdiction '{raw}'. Use the CompuMark office-code lookup if needed."
            )
            continue
        if code not in offices:
            offices.append(code)
        if code == "EM":
            saw_eu_scope = True
        elif code != "WO":
            saw_specific_country = True
            if code in EU_COUNTRY_OFFICES:
                saw_eu_scope = True

    if saw_eu_scope and "EM" not in offices:
        offices.append("EM")
    if (saw_specific_country or saw_eu_scope) and "WO" not in offices:
        offices.append("WO")
    if not offices:
        raise ValueError("No usable registration office code could be derived.")
    limit_wo = "WO" in offices and len([code for code in offices if code != "WO"]) > 0
    return offices, notes, limit_wo


def phonetic_args(phonetics: bool) -> Dict[str, bool]:
    return {
        "centralEuropeanPhonetics": phonetics,
        "japanesePhonetics": phonetics,
        "phonetics": phonetics,
    }


def build_trademark_search_base_args(offices: List[str], limit_wo: bool) -> Dict[str, Any]:
    return {
        "activeOnly": False,
        "crossReferences": True,
        "limitWOresultsToDesignated": limit_wo,
        "plurals": True,
        "registrationOfficeCodes": offices,
        **phonetic_args(False),
    }


def start_workflow(arguments: Dict[str, Any]) -> Dict[str, Any]:
    criteria = arguments.get("search_criteria") or arguments.get("criteria") or {}
    if criteria is None:
        criteria = {}
    if not isinstance(criteria, dict):
        raise ValueError("search_criteria must be an object when provided.")
    return {
        "workflow_id": WORKFLOW_ID,
        "goal": WORKFLOW_GOAL,
        "high_level_plan": WORKFLOW_STEPS,
        "criteria_status": {"missing": missing_required_criteria(criteria)},
        "first_step_id": WORKFLOW_STEPS[0]["step_id"],
        "next_tool_to_call": "continue_workflow",
        "agent_contract": [
            "Follow steps in order using next_step_id.",
            "Do not skip deliver.",
            "Use specialist tools for details; this workflow only coordinates.",
        ],
    }


def workflow_step_title(step_id: str) -> str:
    for step in WORKFLOW_STEPS:
        if step["step_id"] == step_id:
            return step["title"]
    raise ValueError(f"Unknown workflow step_id: {step_id}")


def next_workflow_step_id(step_id: str) -> Optional[str]:
    try:
        index = WORKFLOW_STEP_IDS.index(step_id)
    except ValueError:
        raise ValueError(f"Unknown workflow step_id: {step_id}")
    if index + 1 >= len(WORKFLOW_STEP_IDS):
        return None
    return WORKFLOW_STEP_IDS[index + 1]


def normalized_criteria_snapshot(criteria: Dict[str, Any]) -> Dict[str, Any]:
    mark = clean_mark(str(criteria.get("mark") or criteria.get("trademark") or criteria.get("subject") or ""))
    jurisdictions = criteria.get("jurisdictions") or criteria.get("jurisdiction") or criteria.get("offices")
    nice_classes = criteria.get("nice_classes") or criteria.get("classes") or criteria.get("nice_class")
    return {
        "mark": mark,
        "jurisdictions": jurisdictions,
        "nice_classes": nice_classes,
        "match_scope": criteria.get("match_scope") or "exact",
        "web_search_enabled": criteria.get("web_search_enabled", True),
    }


def missing_required_criteria(criteria: Dict[str, Any]) -> List[str]:
    snapshot = normalized_criteria_snapshot(criteria)
    missing = []
    if not snapshot["mark"]:
        missing.append("mark")
    if not snapshot["jurisdictions"]:
        missing.append("jurisdictions")
    if not snapshot["nice_classes"]:
        missing.append("nice_classes")
    return missing


def workflow_step_payload(step_id: str, criteria: Dict[str, Any], previous_step_result: Any) -> Dict[str, Any]:
    next_step = next_workflow_step_id(step_id)
    if step_id == "criteria":
        missing = missing_required_criteria(criteria)
        if missing:
            return {
                "instructions": [
                    "Ask for the first missing required input only.",
                    "Required: mark, jurisdiction/office, Nice class.",
                    "Defaults: exact match; web search on unless opted out.",
                ],
                "missing": missing,
                "success_criteria": "One missing input is requested from the user; no search is run yet.",
                "next_step_id": step_id,
                "next_tool_to_call": "continue_workflow",
                "done": False,
            }
        return {
            "instructions": [
                "Keep the mark wording exact.",
                "Move on once mark, jurisdiction/office, and Nice class are known.",
            ],
            "criteria": normalized_criteria_snapshot(criteria),
            "success_criteria": "Required criteria are complete and normalized enough to build the plan.",
            "next_step_id": next_step,
            "next_tool_to_call": "continue_workflow",
            "done": False,
        }

    if step_id == "plan":
        return {
            "instructions": [
                "Call build_trademark_knockout_execution_plan.",
                "Use its normalized offices, routes, litigation guidance, and web-search guidance.",
            ],
            "output": "execution_plan",
            "success_criteria": "Execution plan is available; do not start drafting yet.",
            "next_step_id": next_step,
            "next_tool_to_call": "continue_workflow",
            "done": False,
        }

    if step_id == "collect":
        return {
            "instructions": [
                "Run the planned CompuMark trademark and litigation searches.",
                "De-duplicate IDs; keep route counts and errors.",
                "Fetch details and full-text URLs for likely top records.",
                "Run the planned web search unless opted out.",
            ],
            "output": "evidence",
            "success_criteria": "Evidence set includes route counts, source records, litigation results, web findings or opt-out note, and errors if any.",
            "next_step_id": next_step,
            "next_tool_to_call": "continue_workflow",
            "done": False,
        }

    if step_id == "report":
        return {
            "instructions": [
                "Assess risk as 🟢 Low, 🟠 Medium, or 🔴 High using source-backed evidence.",
                "Call get_trademark_knockout_report_template and draft to that structure.",
                "Keep Top 5 tables at exactly five rows.",
                "Use full-text labels for CompuMark links and domains for web links.",
            ],
            "output": "report_markdown",
            "success_criteria": "Markdown report is source-backed, complete, and ready for validation.",
            "next_step_id": next_step,
            "next_tool_to_call": "continue_workflow",
            "done": False,
        }

    if step_id == "deliver":
        return {
            "instructions": [
                "Validate the markdown; fix issues before rendering.",
                "Generate the Clarivate-template PDF.",
                "Return a concise final answer with the generated pdf_url/download_the_report link.",
            ],
            "output": "final response",
            "success_criteria": "Validation passes, PDF tool is called, final answer uses the returned PDF URL or states no public URL is configured.",
            "next_step_id": None,
            "next_tool_to_call": None,
            "done": True,
        }

    raise ValueError(f"Unknown workflow step_id: {step_id}")


def continue_workflow(arguments: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = arguments.get("workflow_id")
    if workflow_id != WORKFLOW_ID:
        raise ValueError(f"Unknown workflow_id: {workflow_id}. Expected {WORKFLOW_ID}.")
    step_id = arguments.get("step_id")
    if not step_id:
        raise ValueError("step_id is required.")
    criteria = arguments.get("search_criteria") or arguments.get("criteria") or {}
    if criteria is None:
        criteria = {}
    if not isinstance(criteria, dict):
        raise ValueError("search_criteria must be an object when provided.")
    previous_step_result = arguments.get("previous_step_result")
    payload = workflow_step_payload(str(step_id), criteria, previous_step_result)
    done = bool(payload.get("done"))
    return {
        "workflow_id": WORKFLOW_ID,
        "current_step_id": step_id,
        "step_title": workflow_step_title(str(step_id)),
        "next_action": (
            "Respond to the user and stop this workflow."
            if done
            else "Complete this step, then call continue_workflow with next_step_id."
        ),
        **payload,
    }


def get_report_template(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "template_markdown": REPORT_TEMPLATE,
        "drafting_rules": [
            "Keep the section numbering and structure unchanged.",
            "Localize visible headings, metadata labels, table headers, enum values, and disclaimer text.",
            "Top 5 tables must contain exactly five data rows.",
            "Use only source-backed facts. Fill empty Top 5 rows with a localized equivalent of 'No further material source-backed finding'.",
            "Use only 🟢 Low, 🟠 Medium, or 🔴 High for risk statements.",
            "Use 'full-text' as the label for CompuMark full-text links; use domain labels for ordinary web links.",
        ],
    }


def build_execution_plan(arguments: Dict[str, Any]) -> Dict[str, Any]:
    mark = clean_mark(arguments.get("mark", ""))
    if not mark:
        raise ValueError("mark is required.")
    nice_classes = normalize_classes(arguments.get("nice_classes") or arguments.get("classes"))
    offices, mapping_notes, limit_wo = normalize_jurisdictions(arguments.get("jurisdictions"))
    match_scope = (arguments.get("match_scope") or "exact").strip().lower()
    if match_scope not in {"exact", "contains"}:
        match_scope = "exact"
    web_pref = arguments.get("web_search_enabled", True)
    if isinstance(web_pref, str):
        web_enabled = web_pref.strip().lower() not in {"no", "false", "0", "n", "disabled", "skip", "off"}
    elif web_pref is None:
        web_enabled = True
    else:
        web_enabled = bool(web_pref)

    route_a = {
        "tool_purpose": "identical knockout trademark search",
        "arguments": {
            "trademarkName": mark,
            "registrationOfficeCodes": offices,
            "classes": nice_classes,
            "limitWOresultsToDesignated": limit_wo,
        },
    }

    route_b_base_args = build_trademark_search_base_args(offices, limit_wo)
    class_search_field_template = [
        {"name": "WORD_MARK_SPECIFICATION", "operator": "CONTAINS", "value": mark},
        {"name": "INT_CLASS_NUMBER", "operator": "EQUALS", "value": "{nice_class}"},
    ]
    route_b_stages = [
        {
            "stage": "B1",
            "run_when": "always",
            "stop_if_usable": True,
            "argument_delta": {
                "searchFields": [
                    {
                        "name": "EXACT_WORD_MARK_SPECIFICATION",
                        "operator": "CONTAINS",
                        "value": mark,
                    }
                ]
            },
        },
        {
            "stage": "B2",
            "run_when": "B1 returned 0 usable results",
            "repeat_for_nice_classes": nice_classes,
            "argument_delta_template": {"searchFields": class_search_field_template},
        },
        {
            "stage": "B3",
            "run_when": "B2 for that Nice class returned 0 usable results",
            "repeat_for_nice_classes": nice_classes,
            "argument_delta_template": {
                **phonetic_args(True),
                "searchFields": class_search_field_template,
            },
        },
    ]

    return {
        "search_statement": (
            f"Search {mark} in {', '.join(offices)} for Nice class(es) "
            f"{', '.join(nice_classes)}, using {match_scope} matching, reviewing active and inactive records "
            "with default broad screening settings."
        ),
        "normalized_inputs": {
            "mark": mark,
            "nice_classes": nice_classes,
            "registrationOfficeCodes": offices,
            "limitWOresultsToDesignated": limit_wo,
            "match_scope": match_scope,
            "web_search_enabled": web_enabled,
            "mapping_notes": mapping_notes,
        },
        "trademark_route_a": route_a,
        "trademark_route_b": {
            "tool_purpose": "custom/screening trademark search",
            "base_arguments": route_b_base_args,
            "stages": route_b_stages,
            "call_instruction": "Merge base_arguments with stage delta/template; replace {nice_class}.",
            "stop_rule": "Stop after useful stage or >100 results.",
            "reporting": "Record counts, errors, and working stage.",
        },
        "litigation": {
            "goal": "opposition checks for top owners and marks",
            "defaults": {"case_domain": "TRADEMARK", "first_action_type": "OPPOSITION", "limit": 10},
            "rules": ["use AND-only calls", "for owners, prefer distinctive owner fragments as plaintiff"],
        },
        "online_presence": {
            "enabled": web_enabled,
            "search_guidance": (
                f'What do you find online related to "{mark}"? Return the 5 most relevant results.'
                if web_enabled
                else "Online presence search skipped because the user opted out."
            ),
            "source": "agent browsing/web search, not CompuMark",
        },
        "post_collection": "De-duplicate IDs; fetch details in batches <=100; create full-text URLs for top records.",
    }


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


def validate_report(arguments: Dict[str, Any]) -> Dict[str, Any]:
    text = arguments.get("markdown") or arguments.get("markdown_text") or ""
    if not text.strip():
        raise ValueError("markdown is required.")
    issues: List[str] = []
    warnings: List[str] = []
    lines = text.splitlines()
    for pattern, label in REPORT_SECTION_PATTERNS:
        if not any(re.search(pattern, line.strip(), flags=re.IGNORECASE) for line in lines):
            issues.append(f"Missing required numbered {label}.")

    top_tm_rows = count_markdown_table_data_rows(
        table_lines_after_heading_pattern(text, TOP_5_TABLE_PATTERNS["2.2"])
    )
    if top_tm_rows != 5:
        issues.append(f"Section 2.2 Top 5 table has {top_tm_rows} data rows; expected exactly 5.")

    top_web_rows = count_markdown_table_data_rows(
        table_lines_after_heading_pattern(text, TOP_5_TABLE_PATTERNS["3.2"])
    )
    if top_web_rows != 5:
        issues.append(f"Section 3.2 Top 5 table has {top_web_rows} data rows; expected exactly 5.")

    emoji_risks = set(re.findall(r"[🟢🟠🔴]\s*[^\s|,\]/]+", text))
    unsupported = sorted(label for label in emoji_risks if label not in SUPPORTED_RISK_LABELS)
    if unsupported:
        issues.append(f"Unsupported risk labels found: {', '.join(unsupported)}")

    if "http://" in text or "https://" in text:
        for label, url in LINK_RE.findall(text):
            expected_domain = domain_for(url)
            if label.startswith("http"):
                warnings.append(f"Visible link text should be a domain, not a full URL: {label}")
            elif expected_domain and label not in {expected_domain, "full-text"} and "." in expected_domain:
                warnings.append(f"Check link label '{label}' for URL {url}; preferred visible text is '{expected_domain}'.")

    return {"valid": not issues, "issues": issues, "warnings": warnings}


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
            + ". Install with: python3 -m pip install -r requirements.txt"
        )


def domain_for(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "") or url
    except Exception:
        return url


def inline_markup(text: str) -> str:
    parts: List[str] = []
    last = 0
    for match in LINK_RE.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        label = match.group(1).strip() or domain_for(match.group(2))
        url = match.group(2).strip()
        parts.append(
            '<link href="{}">{}</link>'.format(
                html.escape(url, quote=True),
                html.escape(label),
            )
        )
        last = match.end()
    parts.append(html.escape(text[last:]))
    marked = "".join(parts)

    replacements = {
        "🟢 Low": '<font color="#188038">Low</font>',
        "🟠 Medium": '<font color="#b06000">Medium</font>',
        "🔴 High": '<font color="#b00020">High</font>',
        "🟢 Bajo": '<font color="#188038">Bajo</font>',
        "🟠 Medio": '<font color="#b06000">Medio</font>',
        "🔴 Alto": '<font color="#b00020">Alto</font>',
    }
    for needle, replacement in replacements.items():
        marked = marked.replace(html.escape(needle), replacement)
        marked = marked.replace(needle, replacement)
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
        fontSize=7.2,
        leading=8.7,
        spaceAfter=0,
    )
    styles["cell_header"] = ParagraphStyle("CellHeaderCustom", parent=styles["cell"], fontName="Helvetica-Bold")
    return styles


def is_table_line(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|")


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


def table_flowable(table_lines: Sequence[str], styles: Dict[str, Any]) -> Any:
    rows = normalize_table(table_lines)
    if not rows:
        return Spacer(1, 2)
    data = []
    for row_index, row in enumerate(rows):
        style = styles["cell_header"] if row_index == 0 else styles["cell"]
        data.append([Paragraph(inline_markup(cell), style) for cell in row])

    available_width = A4[0] - 36 * mm
    col_count = max(1, len(rows[0]))
    col_widths = [available_width / col_count] * col_count
    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BDBDBD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
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
        author="Codex",
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


def default_output_dir() -> Path:
    configured = os.environ.get("TRADEMARK_REPORT_OUTPUT_DIR")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()
    return Path.cwd().resolve()


def public_base_url() -> Optional[str]:
    value = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not value:
        return None
    return value.rstrip("/")


def public_file_url(path: Path) -> Optional[str]:
    base_url = public_base_url()
    if not base_url:
        return None
    try:
        resolved = path.resolve()
        output_dir = default_output_dir()
        relative = resolved.relative_to(output_dir)
    except ValueError:
        return None
    return f"{base_url}/reports/{relative.as_posix()}"


def resolve_output_path(output_path: Optional[str], subject: str) -> Path:
    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = default_output_dir() / path
    else:
        path = default_output_dir() / f"trademark_report_{safe_filename(subject)}.pdf"
    if path.suffix.lower() != ".pdf":
        raise ValueError("Output filename must end with .pdf")
    return path


def generate_pdf(arguments: Dict[str, Any]) -> Dict[str, Any]:
    require_pdf_dependencies()
    subject = clean_mark(arguments.get("subject") or arguments.get("mark") or "")
    if not subject:
        raise ValueError("subject is required.")

    markdown_text = arguments.get("markdown") or arguments.get("markdown_text")
    markdown_path_arg = arguments.get("markdown_path")
    if markdown_text is None and markdown_path_arg:
        markdown_path = Path(markdown_path_arg).expanduser()
        if not markdown_path.is_absolute():
            markdown_path = default_output_dir() / markdown_path
        if not markdown_path.exists():
            raise FileNotFoundError(f"Markdown report not found: {markdown_path}")
        markdown_text = markdown_path.read_text(encoding="utf-8")
    if markdown_text is None or not str(markdown_text).strip():
        raise ValueError("markdown or markdown_path is required.")

    template_path = Path(arguments.get("template_path") or DEFAULT_TEMPLATE_PATH).expanduser()
    if not template_path.is_absolute():
        template_path = BASE_DIR / template_path
    if not template_path.exists():
        raise FileNotFoundError(f"Template PDF not found: {template_path}")

    output_path = resolve_output_path(arguments.get("output_path"), subject)
    save_markdown = bool(arguments.get("save_markdown", True))
    markdown_output_path = None
    if save_markdown:
        if arguments.get("markdown_output_path"):
            markdown_output_path = Path(arguments["markdown_output_path"]).expanduser()
            if not markdown_output_path.is_absolute():
                markdown_output_path = default_output_dir() / markdown_output_path
        else:
            markdown_output_path = output_path.with_suffix(".md")
        markdown_output_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_output_path.write_text(str(markdown_text), encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        body_pdf = tmpdir / "report_body.pdf"
        overlay_pdf = tmpdir / "cover_overlay.pdf"
        build_body_pdf(str(markdown_text), body_pdf)
        build_overlay_pdf(subject, overlay_pdf)
        merge_template(template_path, body_pdf, overlay_pdf, output_path)

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"PDF generation failed: {output_path}")

    pdf_url = public_file_url(output_path)
    markdown_url = public_file_url(markdown_output_path) if markdown_output_path else None
    return {
        "pdf_path": str(output_path.resolve()),
        "pdf_url": pdf_url,
        "download_the_report": pdf_url,
        "pdf_exists": True,
        "pdf_size_bytes": output_path.stat().st_size,
        "markdown_path": str(markdown_output_path.resolve()) if markdown_output_path else None,
        "markdown_url": markdown_url,
        "template_path": str(template_path.resolve()),
        "pdf_generation_workflow": "Clarivate template merge: template cover + generated report body + template closing page",
        "chatgpt_final_response_instruction": (
            "Use download_the_report/pdf_url as the only PDF link. Do not create or link a ChatGPT-generated PDF artifact. "
            "Do not fetch, open, download, inspect, or review the PDF URL; give the link directly to the user. "
            "If pdf_url is null, tell the user that PUBLIC_BASE_URL is not configured on the MCP server."
        ),
    }


TOOLS: Dict[str, Dict[str, Any]] = {
    "start_workflow": {
        "description": "Start the report workflow; returns a short plan and first step.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Optional; defaults to trademark_knockout_report.",
                },
                "user_goal": {
                    "type": "string",
                    "description": "User request.",
                },
                "search_criteria": {
                    "type": "object",
                    "description": "Known criteria: mark, jurisdictions, nice_classes, match_scope, web_search_enabled.",
                    "additionalProperties": True,
                },
                "language": {
                    "type": "string",
                    "description": "Optional visible language.",
                },
            },
            "additionalProperties": False,
        },
        "handler": start_workflow,
    },
    "continue_workflow": {
        "description": "Return step instructions, success criteria, and next action.",
        "inputSchema": {
            "type": "object",
            "required": ["workflow_id", "step_id"],
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "description": f"Expected {WORKFLOW_ID}.",
                },
                "step_id": {
                    "type": "string",
                    "description": "Current step id.",
                },
                "search_criteria": {
                    "type": "object",
                    "description": "Original or normalized criteria.",
                    "additionalProperties": True,
                },
                "previous_step_result": {
                    "type": "object",
                    "description": "Optional previous result.",
                    "additionalProperties": True,
                },
            },
            "additionalProperties": False,
        },
        "handler": continue_workflow,
    },
    "get_trademark_knockout_report_template": {
        "description": "Return report markdown template.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": get_report_template,
    },
    "build_trademark_knockout_execution_plan": {
        "description": "Normalize inputs and return compact CompuMark/web search plan.",
        "inputSchema": {
            "type": "object",
            "required": ["mark", "jurisdictions", "nice_classes"],
            "properties": {
                "mark": {"type": "string", "description": "Proposed mark exactly as provided."},
                "jurisdictions": {
                    "description": "Jurisdictions or office codes.",
                    "type": "array",
                    "items": {"type": "string"},
                },
                "nice_classes": {
                    "description": "Nice classes as numbers from 1 to 45.",
                    "type": "array",
                    "items": {"type": "string"},
                },
                "match_scope": {
                    "type": "string",
                    "description": "exact unless user asked for contains.",
                    "default": "exact",
                },
                "web_search_enabled": {
                    "description": "Defaults true; false only if opted out.",
                    "type": "boolean",
                    "default": True,
                },
            },
            "additionalProperties": False,
        },
        "handler": build_execution_plan,
    },
    "validate_trademark_knockout_report": {
        "description": "Validate report markdown before PDF generation.",
        "inputSchema": {
            "type": "object",
            "required": ["markdown"],
            "properties": {"markdown": {"type": "string", "description": "Report markdown."}},
            "additionalProperties": False,
        },
        "handler": validate_report,
    },
    "generate_clarivate_report_pdf": {
        "description": "Generate Clarivate-template PDF.",
        "inputSchema": {
            "type": "object",
            "required": ["subject"],
            "properties": {
                "subject": {"type": "string", "description": "Cover subtitle / mark."},
                "markdown": {"type": "string", "description": "Report markdown."},
                "markdown_path": {"type": "string", "description": "Markdown path if markdown omitted."},
                "output_path": {
                    "type": "string",
                    "description": "PDF output path.",
                },
                "template_path": {
                    "type": "string",
                    "description": "Optional template path.",
                },
                "save_markdown": {
                    "type": "boolean",
                    "default": True,
                    "description": "Save markdown copy.",
                },
                "markdown_output_path": {"type": "string", "description": "Optional markdown output path."},
            },
            "additionalProperties": False,
        },
        "handler": generate_pdf,
    },
}


def handle_initialize(message_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    protocol = params.get("protocolVersion") or "2024-11-05"
    return json_rpc_result(
        message_id,
        {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def handle_tools_list(message_id: Any) -> Dict[str, Any]:
    return json_rpc_result(
        message_id,
        {
            "tools": [
                {
                    "name": name,
                    "description": spec["description"],
                    "inputSchema": spec["inputSchema"],
                }
                for name, spec in TOOLS.items()
            ]
        },
    )


def handle_tools_call(message_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name not in TOOLS:
        return json_rpc_error(message_id, -32602, f"Unknown tool: {name}")
    try:
        handler: Callable[[Dict[str, Any]], Any] = TOOLS[name]["handler"]
        payload = handler(arguments)
        return json_rpc_result(message_id, text_result(payload))
    except Exception as exc:
        return json_rpc_result(message_id, text_result({"error": str(exc), "tool": name}, is_error=True))


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
        return handle_tools_list(message_id)
    if method == "tools/call":
        return handle_tools_call(message_id, params)
    if method in {"resources/list", "prompts/list"}:
        return json_rpc_result(message_id, {"resources": []} if method == "resources/list" else {"prompts": []})
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


def self_test() -> int:
    tools = handle_tools_list(1)["result"]["tools"]
    criteria = {
        "mark": "NOVALYTIC",
        "jurisdictions": ["EU", "UK"],
        "nice_classes": ["9", "42"],
    }
    workflow_start = start_workflow(
        {
            "user_goal": "Generate a trademark knockout report for NOVALYTIC in the EU and UK.",
            "search_criteria": criteria,
        }
    )
    first_step = continue_workflow(
        {
            "workflow_id": workflow_start["workflow_id"],
            "step_id": workflow_start["first_step_id"],
            "search_criteria": criteria,
        }
    )
    plan = build_execution_plan(
        {
            **criteria,
        }
    )
    print(
        json.dumps(
            {
                "tools": [tool["name"] for tool in tools],
                "workflow_start": workflow_start,
                "first_step": first_step,
                "sample_execution_plan": plan,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the trademark knockout report MCP server.")
    parser.add_argument("--self-test", action="store_true", help="Print a sample tool list and execution plan, then exit.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        return self_test()
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
