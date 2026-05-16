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

WORKFLOW_INSTRUCTIONS = """# Trademark Knockout Workflow Instructions

Use these instructions instead of Codex plugin skills. Live trademark and
litigation data must come from the connected Clarivate CompuMark MCP tools.

## Required visible behavior

1. Detect the user's language from the prompt and use it for visible headings,
   labels, table headers, status values, summaries, and final report text. Do
   not translate tool names, IDs, URLs, registration numbers, or mark verbal
   elements from source records.
2. Display a localized **Planned Steps** block exactly once before Step 1:
   1. Get search criteria
   2. Conduct trademark searches, litigation searches & optional web search
   3. Analyze trademark risk
   4. Draft report
   5. Generate final PDF
3. Ask only for missing required inputs, one at a time: mark name, jurisdiction
   or registration office, Nice class, and online-presence preference.
4. Use only the mark wording provided by the user. Do not add unrequested
   variations. Use exact matching unless the user explicitly asks for containing
   matches.
5. Query trademark data through both routes: identical knockout search and
   custom/screening trademark search.
6. Query litigation with the Clarivate litigation-search MCP tool.
7. Run online-presence search only when the user opted in. If no web-search MCP
   tool is connected, note the gap in the report rather than inventing facts.
8. Draft the report with the exact report structure returned by
   get_trademark_knockout_report_template. Top 5 tables must contain exactly
   five rows; use a localized equivalent of "No further material source-backed
   finding" for empty rows.
9. Generate the final PDF with generate_clarivate_report_pdf and confirm the
   output file exists before linking to it.

## Step summary behavior

After Steps 1, 2, 3, and 4, display exactly this localized three-bullet block:

**Step X Summary**
- **Completed:** ...
- **Key findings:** ...
- **Next:** ...

After Step 5, before the report, display a markdown blockquote:

> **Final Summary Before Report**
> - **Overall risk view:** exactly one of 🟢 Low / 🟠 Medium / 🔴 High
> - **Top conflicts:** concise source-backed list or none identified
> - **Deliverable status:** Report text complete; PDF generated: [filename].

If the PDF does not exist, state that PDF generation failed or was not completed.

## Data collection rules

- Use broad default settings unless the user asks otherwise: active_only false,
  plurals true, phonetics false for narrow stages, cross-references true, and
  supported regional phonetics true when controlled broadening calls for it.
- If a specific country is specified, include that office and WO with
  limitWOresultsToDesignated true.
- If an EU country is specified, include that office, EM, and WO with
  limitWOresultsToDesignated true.
- If the EU/EUIPO is specified, include EM and WO with
  limitWOresultsToDesignated true.
- Never query details for more than 100 trademark IDs per content call.
- Merge IDs from both trademark routes, de-duplicate, keep per-route counts, and
  record route-specific errors.

## Route A: identical knockout search

Call the CompuMark identical knockout search tool with the proposed mark,
registration office scope, Nice classes, and WO-designation flag.

## Route B: custom/screening trademark search

Run a progressive sequence and stop as soon as the result set is useful:

- B1: EXACT_WORD_MARK_SPECIFICATION CONTAINS searched mark. Do not add class
  filtering. If results are found, use this as the primary exact-match set.
- B2: if B1 is zero, WORD_MARK_SPECIFICATION CONTAINS searched mark AND
  INT_CLASS_NUMBER EQUALS requested class. Start with phonetics false.
- B3: if B2 is zero, keep the class filter and set phonetics true.

Stop broadening when any stage returns 5 to 100 results. If a stage returns more
than 100 results, do not run broader stages; trim by relevance during analysis.

## Litigation rules

- Always include FIRST_ACTION_TYPE EQ OPPOSITION.
- Prefer CASE_DOMAIN EQ TRADEMARK when available.
- Avoid OR conditions; split into separate AND-only calls.
- order_by is a dictionary/map, for example {"FIRST_ACTION_DATE": "DESC"}.
- For owner searches, use the most distinctive owner-name fragment with
  PARTY_OPTIMIZED_NAME LIKE %FRAGMENT%, PARTY_ROLE EQ PLAINTIFF, and
  PARTY_IS_EX_OFFICIO EQ false.
- For mark searches, query TRADEMARK_VERBAL_ELEMENT for the most relevant found
  names.

## Analysis rules

Assess overall clearance risk as exactly one of 🟢 Low, 🟠 Medium, or 🔴 High.
Consider exact/similar matches, class fit, jurisdiction fit, active status,
litigation, online commercial use, known reputation, and possible non-use risk
for records older than five years where no active use is visible.

Never invent registration numbers, owners, cases, dates, URLs, or legal
conclusions. If a URL is unavailable, use a localized equivalent of "link
unavailable". For visible web and trademark links, display the domain as link
text and embed the full absolute URL.
"""

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


def build_trademark_search_args(
    offices: List[str],
    limit_wo: bool,
    search_fields: List[Dict[str, str]],
    phonetics: bool,
) -> Dict[str, Any]:
    return {
        "activeOnly": False,
        "centralEuropeanPhonetics": phonetics,
        "crossReferences": True,
        "japanesePhonetics": phonetics,
        "limitWOresultsToDesignated": limit_wo,
        "phonetics": phonetics,
        "plurals": True,
        "registrationOfficeCodes": offices,
        "searchFields": search_fields,
    }


def get_workflow(arguments: Dict[str, Any]) -> Dict[str, Any]:
    language = arguments.get("language") or "detected from user prompt"
    return {
        "language": language,
        "instructions": WORKFLOW_INSTRUCTIONS,
        "required_compumark_tool_purposes": [
            "identical knockout trademark search",
            "custom/screening trademark search",
            "trademark content/details for up to 100 IDs",
            "full-text URL creation for trademark IDs",
            "trademark litigation/caselaw search",
        ],
        "optional_tool_purposes": [
            "web/online presence search MCP tool, only when the user opts in",
        ],
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
            "Display only domains as link text while embedding full absolute URLs.",
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
    web_pref = arguments.get("web_search_enabled")
    if isinstance(web_pref, str):
        web_enabled = web_pref.strip().lower() in {"yes", "true", "1", "y", "enabled"}
    else:
        web_enabled = bool(web_pref)

    route_a = {
        "tool_purpose": "CompuMark identical knockout search",
        "arguments": {
            "trademarkName": mark,
            "registrationOfficeCodes": offices,
            "classes": nice_classes,
            "limitWOresultsToDesignated": limit_wo,
        },
    }

    route_b_b1 = {
        "stage": "B1",
        "stop_rule": "If this returns any usable exact-match results, use it as the primary Route B set and do not broaden.",
        "tool_purpose": "CompuMark custom/screening trademark search",
        "arguments": build_trademark_search_args(
            offices,
            limit_wo,
            [
                {
                    "name": "EXACT_WORD_MARK_SPECIFICATION",
                    "operator": "CONTAINS",
                    "value": mark,
                }
            ],
            phonetics=False,
        ),
    }
    route_b_b2 = []
    route_b_b3 = []
    for nice_class in nice_classes:
        base_fields = [
            {"name": "WORD_MARK_SPECIFICATION", "operator": "CONTAINS", "value": mark},
            {"name": "INT_CLASS_NUMBER", "operator": "EQUALS", "value": nice_class},
        ]
        route_b_b2.append(
            {
                "stage": "B2",
                "nice_class": nice_class,
                "run_only_if": "B1 returned 0 usable results.",
                "tool_purpose": "CompuMark custom/screening trademark search",
                "arguments": build_trademark_search_args(offices, limit_wo, base_fields, phonetics=False),
            }
        )
        route_b_b3.append(
            {
                "stage": "B3",
                "nice_class": nice_class,
                "run_only_if": f"B2 for class {nice_class} returned 0 usable results.",
                "tool_purpose": "CompuMark custom/screening trademark search",
                "arguments": build_trademark_search_args(offices, limit_wo, base_fields, phonetics=True),
            }
        )

    online_queries = []
    if web_enabled:
        online_queries = [
            f'"{mark}"',
            f'"{mark}" ("app" OR "software" OR "SaaS" OR "API" OR "extension" OR "plugin" OR "platform")',
            f'"{mark}" (Amazon OR Walmart)',
        ]

    return {
        "planned_steps_block": [
            "Get search criteria",
            "Conduct trademark searches, litigation searches & optional web search",
            "Analyze trademark risk",
            "Draft report",
            "Generate final PDF",
        ],
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
            "stages": [route_b_b1] + route_b_b2 + route_b_b3,
            "reporting_requirements": [
                "Record which stage produced the working set.",
                "Record counts for every attempted stage.",
                "Highlight exact matches in the requested class and outside the requested class.",
            ],
        },
        "litigation_search_templates": [
            {
                "purpose": "Owner-related opposition search, repeated for distinctive owner fragments from the top trademarks.",
                "request": {
                    "conditions": [
                        {"field": "CASE_DOMAIN", "op": "EQ", "value": "TRADEMARK", "logical_connector_to_next": "AND"},
                        {"field": "FIRST_ACTION_TYPE", "op": "EQ", "value": "OPPOSITION", "logical_connector_to_next": "AND"},
                        {"field": "PARTY_OPTIMIZED_NAME", "op": "LIKE", "value": "%OWNER_FRAGMENT%", "logical_connector_to_next": "AND"},
                        {"field": "PARTY_ROLE", "op": "EQ", "value": "PLAINTIFF", "logical_connector_to_next": "AND"},
                        {"field": "PARTY_IS_EX_OFFICIO", "op": "EQ", "value": "false"},
                    ],
                    "fields": [
                        "GUID",
                        "CASE_NAME",
                        "FIRST_ACTION_DATE",
                        "FIRST_ACTION_TYPE",
                        "CASE_STATUS",
                        "CASE_RESOLUTION",
                        "DOCKET_COURT_COUNTRY",
                        "PARTY_PLAINTIFF_NAME",
                        "PARTY_DEFENDANT_NAME",
                    ],
                    "limit": 10,
                    "order_by": {"FIRST_ACTION_DATE": "DESC"},
                },
            },
            {
                "purpose": "Mark-related opposition search, repeated for the most relevant trademark names.",
                "request": {
                    "conditions": [
                        {"field": "CASE_DOMAIN", "op": "EQ", "value": "TRADEMARK", "logical_connector_to_next": "AND"},
                        {"field": "FIRST_ACTION_TYPE", "op": "EQ", "value": "OPPOSITION", "logical_connector_to_next": "AND"},
                        {"field": "TRADEMARK_VERBAL_ELEMENT", "op": "EQ", "value": mark},
                    ],
                    "fields": [
                        "GUID",
                        "CASE_NAME",
                        "FIRST_ACTION_DATE",
                        "FIRST_ACTION_TYPE",
                        "CASE_STATUS",
                        "CASE_RESOLUTION",
                        "DOCKET_COURT_COUNTRY",
                        "TRADEMARK_VERBAL_ELEMENT",
                    ],
                    "limit": 10,
                    "order_by": {"FIRST_ACTION_DATE": "DESC"},
                },
            },
        ],
        "online_presence": {
            "enabled": web_enabled,
            "queries": online_queries,
            "note": (
                "Run these through a connected web-search MCP tool. If no such MCP tool is available, record the gap in Section 3."
                if web_enabled
                else "Online presence search skipped because the user opted out."
            ),
        },
        "post_collection": [
            "Merge Route A and Route B IDs and de-duplicate.",
            "Retrieve trademark details for no more than 100 IDs per content call.",
            "Create full-text URLs for selected top IDs.",
            "Select five most relevant trademarks by name similarity, active status, class fit, and jurisdiction fit.",
            "Analyze risk before drafting.",
        ],
    }


def table_lines_after_heading(markdown_text: str, heading: str) -> List[str]:
    lines = markdown_text.splitlines()
    try:
        start = next(idx for idx, line in enumerate(lines) if line.strip().lower() == heading.lower())
    except StopIteration:
        return []
    table: List[str] = []
    found = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#") and found:
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
    required_sections = [
        "## 1. Search Criteria",
        "## 2. CompuMark Search Results",
        "### 2.1 Summary",
        "### 2.2 Most Relevant Trademark References (Top 5)",
        "### 2.3 Litigation Activity",
        "### 2.4 Trademark Assessment Comments",
        "## 3. Online Presence Search",
        "### 3.1 Summary",
        "### 3.2 Most Relevant Web Findings (Top 5)",
        "### 3.3 Web Search Comments",
        "## 4. Key Takeaways",
    ]
    lowered = text.lower()
    for section in required_sections:
        if section.lower() not in lowered:
            issues.append(f"Missing required section: {section}")

    top_tm_rows = count_markdown_table_data_rows(
        table_lines_after_heading(text, "### 2.2 Most Relevant Trademark References (Top 5)")
    )
    if top_tm_rows != 5:
        issues.append(f"Section 2.2 Top 5 table has {top_tm_rows} data rows; expected exactly 5.")

    top_web_rows = count_markdown_table_data_rows(
        table_lines_after_heading(text, "### 3.2 Most Relevant Web Findings (Top 5)")
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

    return {
        "pdf_path": str(output_path.resolve()),
        "pdf_exists": True,
        "pdf_size_bytes": output_path.stat().st_size,
        "markdown_path": str(markdown_output_path.resolve()) if markdown_output_path else None,
        "template_path": str(template_path.resolve()),
    }


TOOLS: Dict[str, Dict[str, Any]] = {
    "get_trademark_knockout_workflow": {
        "description": "Return the MCP-only trademark knockout workflow instructions that replace the Codex plugin skill files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "Optional user-visible language name, otherwise detect from the user's prompt.",
                }
            },
            "additionalProperties": False,
        },
        "handler": get_workflow,
    },
    "get_trademark_knockout_report_template": {
        "description": "Return the exact report template and drafting rules for the trademark knockout report.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": get_report_template,
    },
    "build_trademark_knockout_execution_plan": {
        "description": "Normalize search inputs and return a concrete MCP tool-call plan for trademark, litigation, web, analysis, and PDF steps.",
        "inputSchema": {
            "type": "object",
            "required": ["mark", "jurisdictions", "nice_classes", "web_search_enabled"],
            "properties": {
                "mark": {"type": "string", "description": "Proposed word mark exactly as provided by the user."},
                "jurisdictions": {
                    "description": "Jurisdictions or registration offices, such as EU, UK, US, WIPO, France, or office codes.",
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
                    "description": "Use 'exact' unless the user explicitly asked for containing matches.",
                    "default": "exact",
                },
                "web_search_enabled": {
                    "description": "Whether the user opted into online-presence search.",
                    "type": "boolean",
                },
            },
            "additionalProperties": False,
        },
        "handler": build_execution_plan,
    },
    "validate_trademark_knockout_report": {
        "description": "Check the drafted report against the required section, Top 5 row, link-label, and risk-label gates before PDF generation.",
        "inputSchema": {
            "type": "object",
            "required": ["markdown"],
            "properties": {"markdown": {"type": "string", "description": "Final report markdown to validate."}},
            "additionalProperties": False,
        },
        "handler": validate_report,
    },
    "generate_clarivate_report_pdf": {
        "description": "Generate the final Clarivate-template PDF from finalized report markdown using the bundled template asset.",
        "inputSchema": {
            "type": "object",
            "required": ["subject"],
            "properties": {
                "subject": {"type": "string", "description": "Cover subtitle, usually the searched mark."},
                "markdown": {"type": "string", "description": "Finalized report markdown text."},
                "markdown_path": {"type": "string", "description": "Path to finalized report markdown, used if markdown is not supplied."},
                "output_path": {
                    "type": "string",
                    "description": "PDF output path. Relative paths resolve under TRADEMARK_REPORT_OUTPUT_DIR or the server working directory.",
                },
                "template_path": {
                    "type": "string",
                    "description": "Optional alternative Clarivate template path. Defaults to the MCP server bundled template.",
                },
                "save_markdown": {
                    "type": "boolean",
                    "default": True,
                    "description": "Save the markdown next to the generated PDF.",
                },
                "markdown_output_path": {"type": "string", "description": "Optional path for the saved markdown copy."},
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
    plan = build_execution_plan(
        {
            "mark": "NOVALYTIC",
            "jurisdictions": ["EU", "UK"],
            "nice_classes": ["9", "42"],
            "web_search_enabled": False,
        }
    )
    print(json.dumps({"tools": [tool["name"] for tool in tools], "sample_plan": plan}, ensure_ascii=False, indent=2))
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
