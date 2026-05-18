#!/usr/bin/env python3
"""Small tools-only MCP server for a trademark knockout report POC.

This version is optimized for ChatGPT-style clients, where the model reliably
starts work by calling tools rather than by letting the user select MCP prompts.

The server exposes two tools only:

1. prepare_trademark_knockout_report
   - Mandatory kickoff/context tool.
   - Returns explicit workflow instructions and the exact report template.
   - Tells the agent what is missing, or that it is ready to proceed.

2. generate_clarivate_report_pdf
   - Mandatory final tool.
   - Validates that the report still follows the required template shape.
   - Renders the completed markdown into the Clarivate PDF template.

This is intentionally simple POC code, not a production-grade MCP framework.
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
from typing import Any, Callable, Dict, List, Optional, Sequence
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
except ImportError:  # pragma: no cover - runtime dependency guard
    PdfReader = PdfWriter = None  # type: ignore[assignment]
    colors = TA_LEFT = A4 = ParagraphStyle = getSampleStyleSheet = mm = None  # type: ignore[assignment]
    pdfmetrics = canvas = Paragraph = SimpleDocTemplate = Spacer = Table = TableStyle = None  # type: ignore[assignment]


SERVER_NAME = "trademark-knockout-report-workflow"
SERVER_VERSION = "0.5.0-tools-only-poc"
WORKFLOW_NAME = "trademark_knockout_report"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE_PATH = BASE_DIR / "assets" / "Clarivate_template.pdf"

# Clarivate template cover coordinates. Keep these unless the template changes.
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

LINK_RE = re.compile(r"\[([^\]]+)]\((https?://[^)\s]+)\)")
PIPE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")

REQUIRED_MARKDOWN_HEADINGS = [
    "# AI Generated Trademark Knockout Search Report (Demo only)",
    "## 1. Search Criteria",
    "## 2. CompuMark Search Results",
    "### 2.1 Summary",
    "### 2.2 Most Relevant Trademark References (Top 5)",
    "### 2.3 Litigation Activity",
    "### 2.4 Trademark Assessment Comments",
    "## 3. Key Takeaways",
    "Disclaimer",
]

COMMON_TEMPLATE_PLACEHOLDERS = [
    "[MARK]",
    "[DATE]",
    "[Word / Logo / Both]",
    "[EU / UK / US / WIPO designations / Other]",
    "[CLASS NUMBERS, if known]",
    "[Exact only / Contains / Phonetic / Plurals]",
    "[Any limitations, exclusions, or assumptions]",
    "[NUMBER / APPROX.]",
    "[LIST]",
    "[🟢 Low / 🟠 Medium / 🔴 High]",
    "[VERBAL ELEMENT 1]",
    "[VERBAL ELEMENT 2]",
    "[VERBAL ELEMENT 3]",
    "[VERBAL ELEMENT 4]",
    "[VERBAL ELEMENT 5]",
    "[Status]",
    "[OFFICE]",
    "[CLASS]",
    "[NUMBER]",
    "[OWNER]",
    "FULL_TEXT_URL",
    "[PARTY 1 vs PARTY 2]",
    "[Opposition / Infringement / Cancellation]",
    "[COUNTRY]",
    "[Active / Concluded]",
    "[Summary]",
    "[State whether exact matches were found in the main class.]",
    "[State whether similar or phonetic matches were found.]",
    "[State whether any exact matches were found outside the main class, if searched.]",
    "[State which results appear most material and why.]",
    "[State whether litigation activity was found, the type of case, and if it adds risk.]",
    "[🟢 Low / 🟠 Medium / 🔴 High concern]",
    "[Key takeaway 1: concise conclusion on trademark database results.]",
    "[Key takeaway 2: concise conclusion on relevant litigation, if any.]",
    "[Key takeaway 3: note on main legal or commercial risk.]",
    "[Key takeaway 4: optional recommendation, e.g. proceed / proceed with caution / consider narrowing / consider alternate mark.]",
]

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

| Item                            | Result                         |
| ------------------------------- | ------------------------------ |
| Total records reviewed          | [NUMBER / APPROX.]             |
| Most relevant jurisdictions     | [LIST]                         |
| Most relevant classes           | [LIST]                         |
| Overall initial risk impression | [🟢 Low / 🟠 Medium / 🔴 High] |

### 2.2 Most Relevant Trademark References (Top 5)

| Verbal Element     | Status   | Registration Office | Class(es) | Number   | Date   | Owner   | Full Text URL              |
| ------------------ | -------- | ------------------- | --------- | -------- | ------ | ------- | -------------------------- |
| [VERBAL ELEMENT 1] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [full-text](FULL_TEXT_URL) |
| [VERBAL ELEMENT 2] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [full-text](FULL_TEXT_URL) |
| [VERBAL ELEMENT 3] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [full-text](FULL_TEXT_URL) |
| [VERBAL ELEMENT 4] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [full-text](FULL_TEXT_URL) |
| [VERBAL ELEMENT 5] | [Status] | [OFFICE]            | [CLASS]   | [NUMBER] | [DATE] | [OWNER] | [full-text](FULL_TEXT_URL) |

### 2.3 Litigation Activity

| Parties              | Case Type                                  | Jurisdiction | Status               | Key Details |
| -------------------- | ------------------------------------------ | ------------ | -------------------- | ----------- |
| [PARTY 1 vs PARTY 2] | [Opposition / Infringement / Cancellation] | [COUNTRY]    | [Active / Concluded] | [Summary]   |
| [PARTY 1 vs PARTY 2] | [Opposition / Infringement / Cancellation] | [COUNTRY]    | [Active / Concluded] | [Summary]   |

### 2.4 Trademark Assessment Comments

- [State whether exact matches were found in the main class.]
- [State whether similar or phonetic matches were found.]
- [State whether any exact matches were found outside the main class, if searched.]
- [State which results appear most material and why.]
- [State whether litigation activity was found, the type of case, and if it adds risk.]

---

## 3. Key Takeaways

Overall clearance view: [🟢 Low / 🟠 Medium / 🔴 High concern]

- [Key takeaway 1: concise conclusion on trademark database results.]
- [Key takeaway 2: concise conclusion on relevant litigation, if any.]
- [Key takeaway 3: note on main legal or commercial risk.]
- [Key takeaway 4: optional recommendation, e.g. proceed / proceed with caution / consider narrowing / consider alternate mark.]

---

Disclaimer

This report is produced for informational purposes only and does not constitute legal advice. Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. Always consult a qualified trademark attorney before filing.
"""

MUST_FOLLOW_RULES = [
    "This workflow is mandatory for trademark knockout, clearance, brand availability, or similar report requests.",
    "Call prepare_trademark_knockout_report first for those requests. Do not skip this kickoff tool.",
    "If prepare_trademark_knockout_report returns status='needs_clarification', ask only the clarifying_question and stop. Do not run searches yet.",
    "If status='ready', follow every workflow step in order before giving a final answer.",
    "Do not invent evidence. Use source-backed facts from CompuMark tools only, and make uncertainty visible.",
    "Do not use the CompuMark knockout-search tool. Use the trademark-search, trademark-content, and litigation-search tools as instructed.",
    "Do not change the report template headings, numbering, section order, or required Top 5 table shape.",
    "The Top 5 trademark references table must contain exactly five data rows. Use 'No further material source-backed finding' rows if fewer than five material records exist.",
    "The final user-facing reply must not be a loose text report. It must finish with the PDF generated by generate_clarivate_report_pdf.",
    "Call generate_clarivate_report_pdf as the final action after drafting the markdown. Return download_the_report or pdf_url if present; otherwise return pdf_path and say no public URL is configured.",
]

WORKFLOW_STEPS = [
    "1. Confirm exact mark, territories or registration offices, and Nice classes.",
    "2. Run limited CompuMark trademark searches and select the five most relevant records.",
    "3. Fetch trademark-content for selected records and create links labeled exactly 'full-text'.",
    "4. Run a light CompuMark litigation check for the searched mark, top references, or relevant owners.",
    "5. Fill the provided markdown template without changing its structure.",
    "6. Call generate_clarivate_report_pdf with subject set to the searched mark and markdown set to the completed report.",
    "7. Reply to the user with the generated PDF link/path, not with an unfinished draft.",
]

TRADEMARK_SEARCH_RULES = [
    "Perform at most three trademark searches: exact without phonetics, exact with phonetics, contains with phonetics.",
    "Only proceed to the next search if fewer than five useful results have been found.",
    "Start with the exact search in the requested offices and Nice classes.",
    "Only search using the verbal element given by the user. Do not invent spelling variants or alternate marks.",
    "Select records by exactness, territory/office relevance, class overlap, active/live status, owner relevance, and apparent commercial significance.",
    "For selected IDs, fetch trademark-content and create CompuMark full-text links labeled exactly 'full-text'.",
    "Do not fetch goods for this POC.",
    "Avoid country-code lookup tools if possible. Use common office/country codes from internal knowledge where practical.",
    "For INT_CLASS_NUMBER, use operator 'EQUALS' with a comma-separated value such as '3,4,5' to search multiple Nice classes in one request.",
    "If a specific country is specified, also include 'WO' with limitWOresultsToDesignated=true in search parameters.",
    "If an EU country is specified, also include 'EM' and 'WO' with limitWOresultsToDesignated=true in search parameters.",
]

LITIGATION_SEARCH_RULES = [
    "Use the CompuMark litigation-search tool lightly; do not over-search.",
    "Search material disputes involving the searched mark, strongest matching verbal elements, or owners from the top references.",
    "Keep only cases that could affect risk. Summarize parties, jurisdiction, status, case type, and why each case matters.",
    "If there is no material litigation, say so plainly in the report.",
    "Always use this first-action condition in the first query: {'field': 'FIRST_ACTION_TYPE', 'op': 'EQ', 'value': 'OPPOSITION'}.",
    "When filtering on party name, also include: {'field': 'PARTY_IS_EX_OFFICIO', 'op': 'EQ', 'value': False}.",
    "Avoid OR conditions. Split into multiple separate queries that use only AND conditions.",
    "When using group_by, only order_by fields that are also grouped or aggregated.",
    "The order_by clause must be a dict, for example: {'FIRST_ACTION_DATE': 'DESC'}.",
]

REPORT_DRAFTING_RULES = [
    "Use the report_template returned by prepare_trademark_knockout_report.",
    "Keep every heading, section number, section order, table header, and disclaimer structure intact.",
    "Fill placeholders with source-backed evidence. Do not leave obvious placeholders such as [MARK] or [DATE] in the final markdown.",
    "Use one of these exact risk labels: 🟢 Low, 🟠 Medium, or 🔴 High.",
    "Keep the Top 5 table at exactly five data rows. If fewer than five records exist, fill remaining rows with 'No further material source-backed finding'.",
    "Use CompuMark links as [full-text](url). The visible link label must be exactly 'full-text'.",
    "Keep the report concise. Do not add unrequested sections or change the template into a narrative memo.",
]

FINAL_RESPONSE_RULES = [
    "After report drafting, call generate_clarivate_report_pdf. This is mandatory.",
    "Do not present the final report to the user as plain markdown instead of generating the PDF.",
    "If the PDF tool returns download_the_report or pdf_url, give that to the user.",
    "If there is no public URL, give pdf_path and mention that no public URL is configured.",
    "Keep the final response concise and centered on the generated PDF.",
]


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------


def text_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if not is_error and isinstance(payload, (dict, list)):
        result["structuredContent"] = payload
    return result


def json_rpc_result(message_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def json_rpc_error(message_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


# ---------------------------------------------------------------------------
# Kickoff/context tool
# ---------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def parse_criteria_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {"criteria_json_parse_error": "criteria_json was provided but was not valid JSON."}


def known_criteria_from_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Merge criteria from loose tool arguments.

    The function is permissive on purpose because different clients may infer
    slightly different argument shapes from natural language.
    """
    criteria: Dict[str, Any] = {}

    for key in ["criteria", "search_criteria"]:
        nested = arguments.get(key)
        if isinstance(nested, dict):
            criteria.update(nested)

    criteria.update(parse_criteria_json(arguments.get("criteria_json")))

    for key in [
        "mark",
        "jurisdictions",
        "territories",
        "offices",
        "registration_offices",
        "nice_classes",
        "classes",
        "nice_class_numbers",
        "int_class_numbers",
        "language",
        "type",
        "match_scope",
        "notes",
    ]:
        value = arguments.get(key)
        if value not in (None, ""):
            criteria[key] = value

    return criteria


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def criteria_value(criteria: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = criteria.get(key)
        if has_value(value):
            return value
    return None


def first_missing_essential(criteria: Dict[str, Any]) -> Optional[str]:
    if not has_value(criteria_value(criteria, "mark")):
        return "mark"
    if not has_value(criteria_value(criteria, "jurisdictions", "territories", "offices", "registration_offices")):
        return "jurisdictions"
    if not has_value(criteria_value(criteria, "nice_classes", "classes", "nice_class_numbers", "int_class_numbers")):
        return "nice_classes"
    return None


def clarifying_question(missing: Optional[str]) -> Optional[str]:
    if missing == "mark":
        return "What exact word mark should I search?"
    if missing == "jurisdictions":
        return "Which territories or registration offices should I cover, for example EU, UK, US, or WO?"
    if missing == "nice_classes":
        return "Which Nice classes should I cover?"
    return None


def prepare_trademark_knockout_report(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Mandatory kickoff tool for trademark knockout report requests."""
    criteria = known_criteria_from_args(arguments)
    missing = first_missing_essential(criteria)
    ready = missing is None

    if ready:
        next_action = (
            "Proceed with the workflow now. Use the separate CompuMark tools to collect trademark and litigation evidence, "
            "draft the report in the exact provided template, then call generate_clarivate_report_pdf before replying to the user."
        )
    else:
        next_action = (
            "Ask the user only the clarifying_question and stop. Do not run CompuMark searches and do not draft or generate a report yet."
        )

    return {
        "workflow_name": WORKFLOW_NAME,
        "status": "ready" if ready else "needs_clarification",
        "known_criteria": criteria,
        "missing_required_field": missing,
        "clarifying_question": clarifying_question(missing),
        "next_action": next_action,
        "must_follow_rules": MUST_FOLLOW_RULES,
        "workflow_steps": WORKFLOW_STEPS,
        "compumark_trademark_search_rules": TRADEMARK_SEARCH_RULES,
        "compumark_litigation_search_rules": LITIGATION_SEARCH_RULES,
        "report_drafting_rules": REPORT_DRAFTING_RULES,
        "final_response_rules": FINAL_RESPONSE_RULES,
        "report_template": REPORT_TEMPLATE,
        "final_pdf_tool": "generate_clarivate_report_pdf",
        "final_response_instruction": (
            "The final answer to the end user must include the PDF produced by generate_clarivate_report_pdf. "
            "Use download_the_report or pdf_url when available; otherwise use pdf_path and mention that no public URL is configured."
        ),
    }


# ---------------------------------------------------------------------------
# Markdown validation guardrails
# ---------------------------------------------------------------------------


def top5_trademark_table_rows(markdown_text: str) -> Optional[List[List[str]]]:
    heading = "### 2.2 Most Relevant Trademark References (Top 5)"
    if heading not in markdown_text:
        return None

    after_heading = markdown_text.split(heading, 1)[1]
    section_lines: List[str] = []
    for raw_line in after_heading.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### ") or line.startswith("## "):
            break
        section_lines.append(line)

    table_lines = [line.strip() for line in section_lines if is_table_line(line.strip())]
    rows = normalize_table(table_lines)
    return rows or None


def validate_report_markdown(markdown_text: str) -> Dict[str, Any]:
    """Small, opinionated POC validation to stop obvious agent drift.

    This intentionally checks only the structural requirements that matter most:
    required headings, the exact Top 5 table shape, and unresolved template
    placeholders. It is not a full markdown parser.
    """
    errors: List[str] = []
    warnings: List[str] = []

    for heading in REQUIRED_MARKDOWN_HEADINGS:
        if heading not in markdown_text:
            errors.append(f"Missing required template heading or marker: {heading}")

    top5_rows = top5_trademark_table_rows(markdown_text)
    top5_count: Optional[int] = None
    if top5_rows is None:
        errors.append("Could not find the Top 5 trademark references table.")
    else:
        expected_header = [
            "Verbal Element",
            "Status",
            "Registration Office",
            "Class(es)",
            "Number",
            "Date",
            "Owner",
            "Full Text URL",
        ]
        header = top5_rows[0] if top5_rows else []
        if header != expected_header:
            errors.append("The Top 5 trademark references table header was changed. Keep the exact template columns.")
        top5_count = max(0, len(top5_rows) - 1)
        if top5_count != 5:
            errors.append(f"The Top 5 trademark references table must contain exactly 5 data rows; found {top5_count}.")

    unresolved = [placeholder for placeholder in COMMON_TEMPLATE_PLACEHOLDERS if placeholder in markdown_text]
    if unresolved:
        errors.append(
            "The markdown still contains template placeholders. Replace them before generating the PDF: "
            + ", ".join(unresolved[:12])
            + (" ..." if len(unresolved) > 12 else "")
        )

    if "FULL_TEXT_URL" in markdown_text:
        errors.append("The markdown still contains FULL_TEXT_URL placeholders. Replace each with a real CompuMark full-text URL or a clear no-finding row.")

    if not any(label in markdown_text for label in ["🟢 Low", "🟠 Medium", "🔴 High"]):
        errors.append("The report must include one of the exact risk labels: 🟢 Low, 🟠 Medium, or 🔴 High.")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "top5_trademark_reference_rows": top5_count,
    }


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------


def require_pdf_dependencies() -> None:
    missing = []
    if PdfReader is None:
        missing.append("pypdf")
    if canvas is None:
        missing.append("reportlab")
    if missing:
        raise RuntimeError("Missing PDF dependencies: " + ", ".join(missing))


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(value)).strip("._-")
    return name or "trademark_report"


def domain_for(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc[4:] if netloc.startswith("www.") else (netloc or url)
    except Exception:
        return url


def inline_markup(text: str) -> str:
    """Convert a tiny markdown subset to ReportLab paragraph markup."""
    parts: List[str] = []
    last = 0
    for match in LINK_RE.finditer(text):
        parts.append(html.escape(text[last:match.start()]))
        label = clean_text(match.group(1)) or domain_for(match.group(2))
        url = match.group(2).strip()
        parts.append(
            f'<a href="{html.escape(url, quote=True)}"><font color="#0563C1">{html.escape(label)}</font></a>'
        )
        last = match.end()
    parts.append(html.escape(text[last:]))
    marked = "".join(parts)

    risk_replacements = {
        "🟢 Low": '<font color="#188038">Low</font>',
        "🟠 Medium": '<font color="#b06000">Medium</font>',
        "🔴 High": '<font color="#b00020">High</font>',
        "🟢 Bajo": '<font color="#188038">Bajo</font>',
        "🟠 Medio": '<font color="#b06000">Medio</font>',
        "🔴 Alto": '<font color="#b00020">Alto</font>',
    }
    for needle, replacement in risk_replacements.items():
        marked = marked.replace(html.escape(needle), replacement)
        marked = marked.replace(needle, replacement)

    marked = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", marked)
    return marked


def build_styles() -> Dict[str, Any]:
    base = getSampleStyleSheet()
    styles: Dict[str, Any] = {}
    styles["title"] = ParagraphStyle(
        "ReportTitle",
        parent=base["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#222222"),
        spaceAfter=8,
    )
    styles["h1"] = ParagraphStyle(
        "ReportHeading1",
        parent=base["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#222222"),
        spaceBefore=8,
        spaceAfter=6,
    )
    styles["h2"] = ParagraphStyle(
        "ReportHeading2",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#222222"),
        spaceBefore=6,
        spaceAfter=4,
    )
    styles["h3"] = ParagraphStyle(
        "ReportHeading3",
        parent=base["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=13,
        textColor=colors.HexColor("#222222"),
        spaceBefore=5,
        spaceAfter=3,
    )
    styles["body"] = ParagraphStyle(
        "ReportBody",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#222222"),
        spaceAfter=4,
    )
    styles["bullet"] = ParagraphStyle(
        "ReportBullet",
        parent=styles["body"],
        leftIndent=12,
        firstLineIndent=-8,
        bulletIndent=0,
    )
    styles["cell"] = ParagraphStyle(
        "ReportCell",
        parent=styles["body"],
        fontSize=7.2,
        leading=8.7,
        spaceAfter=0,
    )
    styles["cell_header"] = ParagraphStyle("ReportCellHeader", parent=styles["cell"], fontName="Helvetica-Bold")
    return styles


def is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def parse_table_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def normalize_table(lines: Sequence[str]) -> List[List[str]]:
    rows: List[List[str]] = []
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
    col_widths = [available_width / max(1, len(rows[0]))] * max(1, len(rows[0]))
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
        elif stripped.startswith("- ") or stripped.startswith("* "):
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
    pdf_canvas.drawString(SUBTITLE_X, SUBTITLE_BASELINE_Y, clean_text(subject) or "Trademark Report")
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
        return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    return Path.cwd().resolve()


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


def resolve_path(value: Optional[str], fallback_name: str) -> Path:
    if value:
        path = Path(value).expanduser()
        return path if path.is_absolute() else default_output_dir() / path
    return default_output_dir() / fallback_name


def generate_clarivate_report_pdf(arguments: Dict[str, Any]) -> Dict[str, Any]:
    require_pdf_dependencies()

    subject = clean_text(arguments.get("subject") or arguments.get("mark"))
    if not subject:
        raise ValueError("subject is required. It must be the searched mark.")

    markdown_text = arguments.get("markdown") or arguments.get("markdown_text")
    markdown_path_arg = arguments.get("markdown_path")
    if not markdown_text and markdown_path_arg:
        markdown_path = resolve_path(str(markdown_path_arg), "")
        if not markdown_path.exists():
            raise FileNotFoundError(f"Markdown report not found: {markdown_path}")
        markdown_text = markdown_path.read_text(encoding="utf-8")
    if not markdown_text or not str(markdown_text).strip():
        raise ValueError("markdown or markdown_path is required. The completed report markdown must be provided.")

    template_validation = validate_report_markdown(str(markdown_text))
    if not template_validation["valid"]:
        raise ValueError(
            "Report markdown does not follow the required template. Fix these issues before generating the PDF: "
            + "; ".join(template_validation["errors"])
        )

    if arguments.get("template_path"):
        raise ValueError("template_path is not accepted in this POC. The tool must use assets/Clarivate_template.pdf.")

    template_path = DEFAULT_TEMPLATE_PATH
    if not template_path.exists():
        raise FileNotFoundError(f"Clarivate template PDF not found: {template_path}")

    output_path = resolve_path(
        arguments.get("output_path"),
        f"trademark_report_{safe_filename(subject)}.pdf",
    )
    if output_path.suffix.lower() != ".pdf":
        raise ValueError("output_path must end with .pdf")

    save_markdown = bool(arguments.get("save_markdown", True))
    markdown_output_path: Optional[Path] = None
    if save_markdown:
        markdown_output_path = resolve_path(
            arguments.get("markdown_output_path"),
            f"trademark_report_{safe_filename(subject)}.md",
        )
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
    markdown_url = public_file_url(markdown_output_path)
    return {
        "pdf_path": str(output_path.resolve()),
        "pdf_url": pdf_url,
        "download_the_report": pdf_url,
        "pdf_exists": True,
        "pdf_size_bytes": output_path.stat().st_size,
        "markdown_path": str(markdown_output_path.resolve()) if markdown_output_path else None,
        "markdown_url": markdown_url,
        "template_path": str(template_path.resolve()),
        "template_validation": template_validation,
        "final_response_instruction": (
            "Give the user pdf_url/download_the_report when present. If it is null, give pdf_path and mention that no public URL is configured."
        ),
    }


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
OBJECT = {"type": "object", "additionalProperties": True}
NULLABLE_STRING = {"type": ["string", "null"]}

PREPARE_OUTPUT_SCHEMA = {
    "type": "object",
    "description": "Mandatory kickoff response containing the full workflow guardrails and exact markdown template.",
    "required": [
        "workflow_name",
        "status",
        "known_criteria",
        "missing_required_field",
        "clarifying_question",
        "next_action",
        "must_follow_rules",
        "workflow_steps",
        "compumark_trademark_search_rules",
        "compumark_litigation_search_rules",
        "report_drafting_rules",
        "final_response_rules",
        "report_template",
        "final_pdf_tool",
        "final_response_instruction",
    ],
    "properties": {
        "workflow_name": {"type": "string"},
        "status": {"type": "string", "enum": ["ready", "needs_clarification"]},
        "known_criteria": OBJECT,
        "missing_required_field": NULLABLE_STRING,
        "clarifying_question": NULLABLE_STRING,
        "next_action": {"type": "string"},
        "must_follow_rules": STRING_ARRAY,
        "workflow_steps": STRING_ARRAY,
        "compumark_trademark_search_rules": STRING_ARRAY,
        "compumark_litigation_search_rules": STRING_ARRAY,
        "report_drafting_rules": STRING_ARRAY,
        "final_response_rules": STRING_ARRAY,
        "report_template": {"type": "string"},
        "final_pdf_tool": {"type": "string"},
        "final_response_instruction": {"type": "string"},
    },
    "additionalProperties": False,
}

PDF_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["pdf_path", "pdf_exists", "pdf_size_bytes", "template_validation"],
    "properties": {
        "pdf_path": {"type": "string"},
        "pdf_url": NULLABLE_STRING,
        "download_the_report": NULLABLE_STRING,
        "pdf_exists": {"type": "boolean"},
        "pdf_size_bytes": {"type": "integer"},
        "markdown_path": NULLABLE_STRING,
        "markdown_url": NULLABLE_STRING,
        "template_path": {"type": "string"},
        "template_validation": OBJECT,
        "final_response_instruction": {"type": "string"},
    },
    "additionalProperties": False,
}

TOOLS: Dict[str, Dict[str, Any]] = {
    "prepare_trademark_knockout_report": {
        "description": (
            "MANDATORY FIRST TOOL for any user request to run, start, prepare, create, or generate a trademark knockout report, "
            "brand clearance report, trademark clearance search, or brand availability report. Call this before CompuMark searches. "
            "It returns strict workflow instructions, missing-criteria handling, the exact markdown template, and the requirement to finish by calling "
            "generate_clarivate_report_pdf. If it returns needs_clarification, ask only the clarifying question and stop. If it returns ready, "
            "follow all steps and do not give a final user answer until the PDF has been generated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_criteria": {
                    "type": "object",
                    "description": "Known values such as mark, jurisdictions/offices, nice_classes, language, type, match_scope, and notes.",
                    "additionalProperties": True,
                },
                "criteria": {
                    "type": "object",
                    "description": "Alias for search_criteria.",
                    "additionalProperties": True,
                },
                "criteria_json": {"type": "string", "description": "Optional JSON object string with known criteria."},
                "mark": {"type": "string", "description": "Exact word mark to search."},
                "jurisdictions": {"type": "string", "description": "Territories or offices, for example EU, UK, US, WO."},
                "territories": {"type": "string", "description": "Alias for jurisdictions."},
                "offices": {"type": "string", "description": "Alias for jurisdictions or registration offices."},
                "registration_offices": {"type": "string", "description": "Alias for jurisdictions or offices."},
                "nice_classes": {"type": "string", "description": "Nice class numbers, comma-separated if multiple."},
                "classes": {"type": "string", "description": "Alias for nice_classes."},
                "nice_class_numbers": {"type": "string", "description": "Alias for nice_classes."},
                "int_class_numbers": {"type": "string", "description": "Alias for nice_classes."},
                "language": {"type": "string", "description": "Visible report language. Defaults to the user's language."},
                "type": {"type": "string", "description": "Word, logo, or both, if known."},
                "match_scope": {"type": "string", "description": "Exact, contains, phonetic, plurals, etc., if known."},
                "notes": {"type": "string", "description": "Any limitations, exclusions, or assumptions."},
            },
            "additionalProperties": False,
        },
        "outputSchema": PREPARE_OUTPUT_SCHEMA,
        "handler": prepare_trademark_knockout_report,
    },
    "generate_clarivate_report_pdf": {
        "description": (
            "MANDATORY FINAL TOOL for the trademark knockout workflow. Call this only after the report markdown has been completed using the exact "
            "template returned by prepare_trademark_knockout_report. This tool validates required headings, rejects unresolved template placeholders, "
            "checks the exact five-row Top 5 table, and generates the Clarivate-template PDF using assets/Clarivate_template.pdf. The agent must "
            "finish the user-facing reply with download_the_report or pdf_url when present, or with pdf_path if no public URL is configured. "
            "Do not provide the final report as plain markdown instead of using this tool, and do not try to change the PDF template."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["subject"],
            "anyOf": [{"required": ["markdown"]}, {"required": ["markdown_path"]}],
            "properties": {
                "subject": {"type": "string", "description": "Cover subtitle and searched mark. Required."},
                "markdown": {"type": "string", "description": "Completed report markdown using the exact required template."},
                "markdown_path": {"type": "string", "description": "Markdown file path if markdown is omitted."},
                "output_path": {"type": "string", "description": "Optional PDF output path. Defaults to TRADEMARK_REPORT_OUTPUT_DIR or current directory."},
                "save_markdown": {"type": "boolean", "default": True},
                "markdown_output_path": {"type": "string", "description": "Optional markdown copy output path."},
            },
            "additionalProperties": False,
        },
        "outputSchema": PDF_OUTPUT_SCHEMA,
        "handler": generate_clarivate_report_pdf,
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC stdio server
# ---------------------------------------------------------------------------


def handle_initialize(message_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    protocol = params.get("protocolVersion") or "2024-11-05"
    return json_rpc_result(
        message_id,
        {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Tools-only POC. For any trademark knockout, clearance, brand availability, or similar report request, "
                "call prepare_trademark_knockout_report first. Follow all returned rules and workflow steps. If clarification "
                "is needed, ask only the returned clarifying_question and stop. If ready, use the separate CompuMark tools, "
                "fill the exact template, then call generate_clarivate_report_pdf. The final user-facing answer must include "
                "the generated PDF URL/path. Do not use prompts/resources and do not finish with only markdown."
            ),
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
                    "outputSchema": spec["outputSchema"],
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
    if not isinstance(arguments, dict):
        return json_rpc_error(message_id, -32602, "Tool arguments must be an object.")

    try:
        handler: Callable[[Dict[str, Any]], Any] = TOOLS[name]["handler"]
        payload = handler(arguments)
        return json_rpc_result(message_id, text_result(payload))
    except Exception as exc:
        return json_rpc_result(message_id, text_result({"tool": name, "error": str(exc)}, is_error=True))


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

    # No prompts or resources are exposed in this tools-only POC.
    # Return empty lists for compatibility with clients that probe these methods.
    if method == "prompts/list":
        return json_rpc_result(message_id, {"prompts": []})
    if method == "resources/list":
        return json_rpc_result(message_id, {"resources": []})
    if method == "resources/templates/list":
        return json_rpc_result(message_id, {"resourceTemplates": []})
    if method == "prompts/get":
        return json_rpc_error(message_id, -32602, "This tools-only POC does not expose MCP prompts. Use prepare_trademark_knockout_report.")
    if method == "resources/read":
        return json_rpc_error(message_id, -32602, "This tools-only POC does not expose MCP resources. Use prepare_trademark_knockout_report.")

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
    ready = prepare_trademark_knockout_report(
        {"mark": "NOVALYTIC", "jurisdictions": "EU, UK", "nice_classes": "9, 42", "language": "English"}
    )
    missing = prepare_trademark_knockout_report({"mark": "NOVALYTIC", "jurisdictions": "EU, UK"})
    template_validation = validate_report_markdown(REPORT_TEMPLATE)

    print(
        json.dumps(
            {
                "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "tools": list(TOOLS.keys()),
                "ready_status": ready["status"],
                "ready_next_action": ready["next_action"],
                "missing_status": missing["status"],
                "missing_required_field": missing["missing_required_field"],
                "missing_clarifying_question": missing["clarifying_question"],
                "template_validation_on_blank_template_should_be_invalid": template_validation,
                "note": "PDF generation is not run in self-test because it requires assets/Clarivate_template.pdf.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the trademark knockout report MCP server.")
    parser.add_argument("--self-test", action="store_true", help="Print sample tool output and exit.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        return self_test()
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
