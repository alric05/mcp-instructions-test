#!/usr/bin/env python3
"""Small MCP server for a trademark knockout report POC.

The server does two things only:
1. Gives concise staged instructions for an agent that will use the CompuMark MCP tools.
2. Generates the final PDF by merging a markdown report into the Clarivate PDF template.

It intentionally does not try to re-describe the CompuMark API or over-control the agent.
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
SERVER_VERSION = "0.2.0"
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

STEP_ORDER = [
    "criteria",
    "trademark_search",
    "litigation_search",
    "online_presence",
    "draft_report",
    "generate_pdf",
]

STEP_TITLES = {
    "criteria": "Confirm the search criteria",
    "trademark_search": "Collect CompuMark trademark evidence",
    "litigation_search": "Check relevant litigation",
    "online_presence": "Check online presence",
    "draft_report": "Draft the report markdown",
    "generate_pdf": "Generate the Clarivate-template PDF",
}

STEP_SUMMARY = [
    {"name": "criteria", "purpose": "Confirm mark, territories/offices, Nice classes, and whether web search is allowed."},
    {"name": "trademark_search", "purpose": "Use CompuMark trademark tools and keep the five most relevant records."},
    {"name": "litigation_search", "purpose": "Check whether the mark, top references, or owners appear in material trademark disputes."},
    {"name": "online_presence", "purpose": "Look for material web, company, product, domain, and social-use signals."},
    {"name": "draft_report", "purpose": "Fill the report template with source-backed evidence."},
    {"name": "generate_pdf", "purpose": "Render the report with the Clarivate PDF template and return the link/path."},
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

## 3. Online Presence Search

### 3.1 Summary

| Item                         | Result                                                |
| ---------------------------- | ----------------------------------------------------- |
| Exact same name found online | [Yes / No / Limited / Not performed (user opted out)] |
| Similar names found online   | [Yes / No / Not performed (user opted out)]           |
| Commercial use observed      | [Yes / No / Limited / Not performed (user opted out)] |

### 3.2 Most Relevant Web Findings (Top 5)

| Name / Sign | Webpage URL / Source      | Territory          | Type of use                                         | Notes            |
| ----------- | ------------------------- | ------------------ | --------------------------------------------------- | ---------------- |
| [NAME 1]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 2]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 3]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 4]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |
| [NAME 5]    | [domain.tld](WEBPAGE_URL) | [COUNTRY / REGION] | [Brand / Company / Product / Domain / Social media] | [Why it matters] |

### 3.3 Web Search Comments

- [State whether the searched name appears to be in active commercial use online.]
- [State whether similar names create practical marketplace overlap.]
- [State whether any domain or branding conflicts are notable.]
- [If web search was not run, state: "Online presence search not performed (user opted out)."]

---

## 4. Key Takeaways

Overall clearance view: [🟢 Low / 🟠 Medium / 🔴 High concern]

- [Key takeaway 1: concise conclusion on trademark database results.]
- [Key takeaway 2: concise conclusion on online use / marketplace presence.]
- [Key takeaway 3: note on main legal or commercial risk.]
- [Key takeaway 4: optional recommendation, e.g. proceed / proceed with caution / consider narrowing / consider alternate mark.]

---

Disclaimer

This report is produced for informational purposes only and does not constitute legal advice. Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. Always consult a qualified trademark attorney before filing.
"""


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
# Workflow instruction tools
# ---------------------------------------------------------------------------


def next_step_name(step_name: str) -> Optional[str]:
    if step_name not in STEP_ORDER:
        raise ValueError(f"Unknown step_name '{step_name}'. Expected one of: {', '.join(STEP_ORDER)}")
    index = STEP_ORDER.index(step_name)
    return STEP_ORDER[index + 1] if index + 1 < len(STEP_ORDER) else None


def next_call_text(next_step: Optional[str]) -> Optional[str]:
    if next_step is None:
        return None
    return f"Call get_trademark_knockout_step_instructions with step_name='{next_step}'."


def append_next_instruction(body: str, next_step: Optional[str]) -> str:
    if next_step is None:
        return body + "\n\nThis is the final step; do not call another instruction step."
    return body + f"\n\nWhen you are done with this, call get_trademark_knockout_step_instructions with step_name='{next_step}'."


def start_trademark_knockout_report(arguments: Dict[str, Any]) -> Dict[str, Any]:
    first_step = STEP_ORDER[0]
    return {
        "workflow_name": WORKFLOW_NAME,
        "steps": STEP_SUMMARY,
        "first_step_name": first_step,
        "next_instruction_tool": "get_trademark_knockout_step_instructions",
        "next_instruction_call": next_call_text(first_step),
        "known_criteria": arguments.get("search_criteria") or arguments.get("criteria") or {},
        "note": "Follow the steps in order. After each step, use next_step_name to fetch the next instructions.",
    }


def get_step_instructions(arguments: Dict[str, Any]) -> Dict[str, Any]:
    step = str(arguments.get("step_name") or "").strip()
    if not step:
        raise ValueError("step_name is required.")
    next_step = next_step_name(step)

    if step == "criteria":
        instructions = append_next_instruction(
            "Confirm only the essentials: exact mark, territories or registration offices, and Nice classes. "
            "If one is missing, ask only for the first missing item. Assume web search is allowed unless the user opted out. "
            "Do not run CompuMark searches in this step.",
            next_step,
        )
        expected_output = "A small criteria object: mark, jurisdictions/offices, nice_classes, and optional web_search_enabled."

    elif step == "trademark_search":
        instructions = append_next_instruction(
            "Use the CompuMark trademark-search tool to find the most relevant records. Do not use the knockout-search tool. "
            "Perform at most three trademark searches. An exact search without phonetics, an exact search with phonetics, and a contain search with phonetics. " 
            "Only proceed to next search if you have less than five results. Start with the exact search "
            "in the requested offices and classes. "
            "Select five records by exactness, territory, class overlap, active status, and owner relevance. For those IDs, fetch trademark-content "
            "and create full-text links. Do not fetch goods. "
            "only do trademark searches using the verbal element given by the user, no variations. "
            "* Avoid calling country code lookup tools if possible. Use your internal knowledge of country codes when possible."
            "* For INT_CLASS_NUMBER, use operator: 'EQUALS' with a comma-separated value such as '3,4,5' to search multiple Nice classes in one request. "
            "* If a specific country is specified, also include 'WO' with 'limitWOresultsToDesignated': true in search parameters."
            "* If an EU country is specified, also include 'EM' and 'WO' with 'limitWOresultsToDesignated': true in search parameters.",
            next_step,
        )
        expected_output = "Top trademark references, content for selected IDs, and full-text links labeled 'full-text'."

    elif step == "litigation_search":
        instructions = append_next_instruction(
            "Use the CompuMark litigation-search tool lightly for trademark disputes involving the searched mark, the strongest matching verbal elements, "
            "or owners from the top references. Keep only cases that could affect risk. Summarize parties, jurisdiction, status, case type, "
            "and why each case matters. If there is no material litigation, say so plainly. "
            "* always use this condition on first action type: {'field': 'FIRST_ACTION_TYPE', 'op': 'EQ', 'value': 'OPPOSITION'}. "
            "* When using a filter on party name, also include this filter: {'field': 'PARTY_IS_EX_OFFICIO', 'op': 'EQ', 'value': False}. "
            "* avoid queries with OR conditions. Instead, split them into multiple separate queries that use only AND conditions. "
            "* when using group_by, only order_by fields that are also grouped or aggregated. "
            "* the format of the order_by clause is a dict. A valid example: order_by: { 'FIRST_ACTION_DATE':  'DESC' }.",
            next_step,
        )
        expected_output = "Material litigation findings or a clear no-material-litigation note."

    elif step == "online_presence":
        instructions = append_next_instruction(
            "Use the SerpApi Google Search tool to perform an online presence search, unless the user explicitly opted out. "
            "Determine whether the searched name has meaningful real-world use in domains, software/apps, products, marketplaces, or branding. "
            "Do not limit the search to any region. "
            "Keep only the five most relevant findings, prioritizing companies, products, domains, social profiles, app/software listings, and marketplace conflicts. "
            "Cite each finding with a clear domain label.",
            next_step,
        )        
        expected_output = "Top web findings and a brief view on whether online use increases practical risk."

    elif step == "draft_report":
        instructions = append_next_instruction(
            "You must use the report template available and follow it exactly. "
            "Call get_trademark_knockout_report_template to get the report template, then fill the same structure with the evidence collected. Keep the two Top 5 tables at exactly "
            "five data rows each; use a simple 'No further material source-backed finding' row only when needed. Use 🟢 Low, 🟠 Medium, or 🔴 High risk labels. "
            "Use CompuMark links as [full-text](url) and ordinary web links with domain labels. "
            "Once you got the template, always move on to the next step to generate the PDF using the available tool. The user always wants the PDF even if not explicitly said.",
            next_step,
        )
        expected_output = "Completed markdown report ready for PDF generation."

    elif step == "generate_pdf":
        instructions = append_next_instruction(
            "Call generate_clarivate_report_pdf with subject set to the mark and markdown set to the completed report. Then answer the user with "
            "download_the_report or pdf_url if present. If no public URL is configured, return the local pdf_path and mention that no public URL is configured.",
            next_step,
        )
        expected_output = "Final user response with the generated report link or local path."

    else:  # next_step_name already validates; this is only for type-checkers.
        raise ValueError(f"Unknown step_name '{step}'.")

    return {
        "step_name": step,
        "title": STEP_TITLES[step],
        "instructions": instructions,
        "expected_output": expected_output,
        "next_step_name": next_step,
        "next_instruction_tool": "get_trademark_knockout_step_instructions" if next_step else None,
        "next_instruction_call": next_call_text(next_step),
        "done": next_step is None,
    }


def get_report_template(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "template_markdown": REPORT_TEMPLATE,
        "rules": [
            "Keep the section structure and numbering.",
            "Use source-backed facts only; make uncertainty visible.",
            "Keep each Top 5 table at exactly five data rows.",
            "Use 'full-text' as the label for CompuMark full-text links and domain labels for web links.",
            "Write the visible report in the user's language unless they asked otherwise.",
        ],
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


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


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
    """Convert tiny markdown subset to ReportLab paragraph markup."""
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
        raise ValueError("subject is required.")

    markdown_text = arguments.get("markdown") or arguments.get("markdown_text")
    markdown_path_arg = arguments.get("markdown_path")
    if not markdown_text and markdown_path_arg:
        markdown_path = resolve_path(str(markdown_path_arg), "")
        if not markdown_path.exists():
            raise FileNotFoundError(f"Markdown report not found: {markdown_path}")
        markdown_text = markdown_path.read_text(encoding="utf-8")
    if not markdown_text or not str(markdown_text).strip():
        raise ValueError("markdown or markdown_path is required.")

    template_path = Path(arguments.get("template_path") or DEFAULT_TEMPLATE_PATH).expanduser()
    if not template_path.is_absolute():
        template_path = BASE_DIR / template_path
    if not template_path.exists():
        raise FileNotFoundError(f"Template PDF not found: {template_path}")

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
        "final_response_instruction": "Give the user pdf_url/download_the_report when present. If it is null, give pdf_path and mention that no public URL is configured.",
    }


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
OBJECT = {"type": "object", "additionalProperties": True}
NULLABLE_STRING = {"type": ["string", "null"]}

START_OUTPUT_SCHEMA = {
    "type": "object",
    "description": "First workflow response. Use first_step_name to fetch the first instructions.",
    "required": ["workflow_name", "steps", "first_step_name", "next_instruction_tool", "next_instruction_call"],
    "properties": {
        "workflow_name": {"type": "string"},
        "steps": {"type": "array", "items": OBJECT},
        "first_step_name": {"type": "string"},
        "next_instruction_tool": {"type": "string"},
        "next_instruction_call": {"type": "string"},
        "known_criteria": OBJECT,
        "note": {"type": "string"},
    },
    "additionalProperties": False,
}

STEP_OUTPUT_SCHEMA = {
    "type": "object",
    "description": "Concise instructions for one step, plus the exact next_step_name to request next.",
    "required": ["step_name", "title", "instructions", "expected_output", "next_step_name", "next_instruction_call", "done"],
    "properties": {
        "step_name": {"type": "string"},
        "title": {"type": "string"},
        "instructions": {"type": "string"},
        "expected_output": {"type": "string"},
        "next_step_name": NULLABLE_STRING,
        "next_instruction_tool": NULLABLE_STRING,
        "next_instruction_call": NULLABLE_STRING,
        "done": {"type": "boolean"},
    },
    "additionalProperties": False,
}

TEMPLATE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["template_markdown", "rules"],
    "properties": {"template_markdown": {"type": "string"}, "rules": STRING_ARRAY},
    "additionalProperties": False,
}

PDF_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["pdf_path", "pdf_exists", "pdf_size_bytes"],
    "properties": {
        "pdf_path": {"type": "string"},
        "pdf_url": NULLABLE_STRING,
        "download_the_report": NULLABLE_STRING,
        "pdf_exists": {"type": "boolean"},
        "pdf_size_bytes": {"type": "integer"},
        "markdown_path": NULLABLE_STRING,
        "markdown_url": NULLABLE_STRING,
        "template_path": {"type": "string"},
        "final_response_instruction": {"type": "string"},
    },
    "additionalProperties": False,
}

TOOLS: Dict[str, Dict[str, Any]] = {
    "start_trademark_knockout_report": {
        "description": "Mandatory entrypoint for trademark knockout, brand clearance, clearance search. Returns the first step name and how to fetch its instructions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_criteria": {
                    "type": "object",
                    "description": "Known values such as mark, jurisdictions/offices, nice_classes, and web_search_enabled.",
                    "additionalProperties": True,
                },
                "language": {"type": "string", "description": "Optional visible report language."},
            },
            "additionalProperties": False,
        },
        "outputSchema": START_OUTPUT_SCHEMA,
        "handler": start_trademark_knockout_report,
    },
    "get_trademark_knockout_step_instructions": {
        "description": "Get concise instructions for one workflow step. After completing it, call this tool again with next_step_name.",
        "inputSchema": {
            "type": "object",
            "required": ["step_name"],
            "properties": {
                "step_name": {
                    "type": "string",
                    "enum": STEP_ORDER,
                    "description": "The step whose instructions are needed.",
                },
                "context": {
                    "type": "object",
                    "description": "Optional current criteria or evidence. The instructions stay lightweight.",
                    "additionalProperties": True,
                },
            },
            "additionalProperties": False,
        },
        "outputSchema": STEP_OUTPUT_SCHEMA,
        "handler": get_step_instructions,
    },
    "get_trademark_knockout_report_template": {
        "description": "Return the markdown report template to fill before generating the PDF.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": TEMPLATE_OUTPUT_SCHEMA,
        "handler": get_report_template,
    },
    "generate_clarivate_report_pdf": {
        "description": "Generate the final PDF using the Clarivate template: template cover, generated report body, template closing page.",
        "inputSchema": {
            "type": "object",
            "required": ["subject", "markdown"],
            "properties": {
                "subject": {"type": "string", "description": "Cover subtitle, normally the searched mark."},
                "markdown": {"type": "string", "description": "Completed report markdown. Required for normal agent use."},
                "markdown_path": {"type": "string", "description": "Optional markdown file path for direct local/server-side use."},
                "output_path": {"type": "string", "description": "Optional PDF output path. Defaults to TRADEMARK_REPORT_OUTPUT_DIR or current directory."},
                "template_path": {"type": "string", "description": "Optional Clarivate template path."},
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


def self_test() -> int:
    start = start_trademark_knockout_report({"search_criteria": {"mark": "NOVALYTIC", "jurisdictions": ["EU", "UK"], "nice_classes": ["9", "42"]}})
    first = get_step_instructions({"step_name": start["first_step_name"]})
    second = get_step_instructions({"step_name": first["next_step_name"]})
    print(
        json.dumps(
            {
                "tools": list(TOOLS.keys()),
                "start": start,
                "first_step": first,
                "second_step": second,
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
