#!/usr/bin/env python3
"""MCP server for trademark knockout report validation and PDF rendering.

ChatGPT remains the workflow engine. This server intentionally does not search
trademark data, search litigation data, perform web research, select conflicts,
or write the legal narrative. It only exposes read-only workflow/template
references plus two quality-control tools:

- validate_knockout_report
- render_clarivate_knockout_pdf
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
from urllib.parse import quote, urlparse

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


SERVER_NAME = "trademark-knockout-report-renderer"
SERVER_VERSION = "1.0.0"
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

RISK_LABELS = {"🟢 Low", "🟠 Medium", "🔴 High"}
RISK_TOKEN_RE = re.compile(r"[🟢🟠🔴]\s*[A-Za-z]+")
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
PIPE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
PLACEHOLDER_RE = re.compile(
    r"\[(?=[^\]]*(?:"
    r"MARK|DATE|ROW|FIELD|DETAILS|NUMBER|APPROX|LIST|VERBAL ELEMENT|STATUS|OFFICE|"
    r"CLASS|OWNER|FULL_TEXT_URL|PARTY|COUNTRY|REGION|SUMMARY|NAME|WEBPAGE_URL|TYPE|"
    r"NOTES|RESULT|ITEM|ANY LIMITATIONS|WHY IT MATTERS|COMMENT|STATE WHETHER|"
    r"KEY TAKEAWAY|WORD\s*/\s*LOGO|EU\s*/\s*UK|EXACT ONLY|🟢|🟠|🔴"
    r"))[^\]]+\]",
    re.IGNORECASE,
)

WORKFLOW_CONTRACT = """# Trademark Knockout Report Workflow Contract

ChatGPT is the workflow engine. The local report MCP server is only an artifact
and quality-control server.

## Required sequence

1. Get search criteria.
2. Conduct trademark searches, litigation searches & optional web search.
3. Analyze trademark risk.
4. Draft report.
5. Generate final PDF.

## Tool boundaries

| Component | Role |
|---|---|
| Existing Clarivate / CompuMark MCP | Trademark search, trademark details, litigation data. |
| ChatGPT web search | Optional online-presence search when the user agrees. |
| Local report MCP | Report validation and Clarivate-style PDF rendering only. |

Do not use this local MCP server to run the workflow, perform web research,
analyze risk, select conflicts, decide broadening stages, or draft legal
narrative.

## Intake gate

Required minimum inputs are mark name, jurisdiction or registration office,
Nice class, and online-presence search preference. Ask only for missing required
information. Do not require optional matter details.

## Trademark data collection

Route A must use the identical knockout search. Route B must use staged custom
screening: B1 exact word specification contains the searched term with no class
filter; B2 word mark specification contains the searched term plus class filter
with phonetics false; B3 keeps class filtering and turns phonetics true only if
B2 is zero. Merge Route A and B IDs, de-duplicate, and retrieve details in
batches of no more than 100 IDs.

## Litigation data collection

Use search-litigation-cases for opposition signals. Always include
FIRST_ACTION_TYPE EQ OPPOSITION. When party-name filters are used, also include
PARTY_IS_EX_OFFICIO EQ false. Use AND-only queries and order_by as a dictionary.

## Online presence

Only perform ChatGPT web search when the user opts in. Do not use a web-search
MCP server. If the user opts out, keep Section 3 and state that online presence
search was not performed at the user's request.

## Risk and report gates

Use exactly one overall risk label: 🟢 Low, 🟠 Medium, or 🔴 High. Keep the
required report structure, keep every Top 5 table to exactly five rows, do not
invent source facts, and validate the final Markdown before rendering the PDF.
"""

REPORT_TEMPLATE = """# AI Generated Trademark Knockout Search Report (Demo only)

Mark searched: [MARK]
Date of report: [DATE]

---

## 1. Search Criteria

| Field | Details |
|---|---|
| Mark searched | [MARK] |
| Type | [Word / Logo / Both] |
| Territories covered | [EU / UK / US / WIPO designations / Other] |
| Nice classes | [CLASS NUMBERS] |
| Match scope | [Exact only / Contains / Phonetic / Plurals] |
| Notes / assumptions | [Any limitations, exclusions, or assumptions] |

---

## 2. CompuMark Search Results

### 2.1 Summary

| Item | Result |
|---|---|
| Total records reviewed | [NUMBER / APPROX.] |
| Most relevant jurisdictions | [LIST] |
| Most relevant classes | [LIST] |
| Overall initial risk impression | [🟢 Low / 🟠 Medium / 🔴 High] |

### 2.2 Most Relevant Trademark References (Top 5)

| Verbal Element | Status | Registration Office | Class(es) | Number | Date | Owner | Full Text URL |
|---|---|---|---|---|---|---|---|
| [ROW 1] |  |  |  |  |  |  |  |
| [ROW 2] |  |  |  |  |  |  |  |
| [ROW 3] |  |  |  |  |  |  |  |
| [ROW 4] |  |  |  |  |  |  |  |
| [ROW 5] |  |  |  |  |  |  |  |

### 2.3 Litigation Activity

| Parties | Case Type | Jurisdiction | Status | Key Details |
|---|---|---|---|---|
| [ROW 1] |  |  |  |  |
| [ROW 2] |  |  |  |  |

### 2.4 Trademark Assessment Comments

* [Comment on exact matches in the main class.]
* [Comment on similar or phonetic matches.]
* [Comment on exact matches outside the main class, if searched.]
* [Comment on the most material results and why they matter.]
* [Comment on litigation activity and whether it adds risk.]

---

## 3. Online Presence Search

### 3.1 Summary

| Item | Result |
|---|---|
| Exact same name found online | [Yes / No / Limited / Not performed (user opted out)] |
| Similar names found online | [Yes / No / Not performed (user opted out)] |
| Commercial use observed | [Yes / No / Limited / Not performed (user opted out)] |

### 3.2 Most Relevant Web Findings (Top 5)

| Name / Sign | Webpage URL / Source | Territory | Type of use | Notes |
|---|---|---|---|---|
| [ROW 1] |  |  |  |  |
| [ROW 2] |  |  |  |  |
| [ROW 3] |  |  |  |  |
| [ROW 4] |  |  |  |  |
| [ROW 5] |  |  |  |  |

### 3.3 Web Search Comments

* [State whether the searched name appears to be in active commercial use online.]
* [State whether similar names create practical marketplace overlap.]
* [State whether any domain or branding conflicts are notable.]
* [If web search was not run, state that online presence search was not performed at the user’s request.]

---

## 4. Key Takeaways

Overall clearance view: [🟢 Low / 🟠 Medium / 🔴 High concern]

* [Key takeaway 1: concise conclusion on trademark database results.]
* [Key takeaway 2: concise conclusion on online use / marketplace presence.]
* [Key takeaway 3: main legal or commercial risk.]
* [Key takeaway 4: practical next step.]

---

Disclaimer

This report is produced for informational purposes only and does not constitute legal advice. Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. Always consult a qualified trademark attorney before filing.
"""


def text_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
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
    return name or "trademark_knockout_report"


def default_output_dir() -> Path:
    configured = os.environ.get("TRADEMARK_REPORT_OUTPUT_DIR")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()
    return (BASE_DIR.parent / "reports").resolve()


def public_base_url() -> Optional[str]:
    value = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return value.rstrip("/") if value else None


def public_file_url(path: Path) -> Optional[str]:
    base = public_base_url()
    if not base:
        return None
    try:
        relative = path.resolve().relative_to(default_output_dir())
    except ValueError:
        return None
    return f"{base}/reports/{quote(relative.as_posix())}"


def domain_for(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc or url).removeprefix("www.")


def is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def table_after_heading(markdown_text: str, heading_number: str) -> List[str]:
    lines = markdown_text.splitlines()
    heading_re = re.compile(rf"^\s*###\s+{re.escape(heading_number)}\b")
    start_index = None
    for index, line in enumerate(lines):
        if heading_re.search(line):
            start_index = index
            break
    if start_index is None:
        return []

    table_lines: List[str] = []
    found = False
    for line in lines[start_index + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#") and found:
            break
        if is_table_line(stripped):
            table_lines.append(stripped)
            found = True
            continue
        if found and stripped:
            break
    return table_lines


def count_markdown_table_data_rows(table_lines: Sequence[str]) -> int:
    if not table_lines:
        return 0
    rows = [line for line in table_lines if not PIPE_SEPARATOR_RE.match(line)]
    return max(0, len(rows) - 1)


def required_section_missing(markdown_text: str, heading_regex: str) -> bool:
    return re.search(heading_regex, markdown_text, flags=re.IGNORECASE | re.MULTILINE) is None


def validate_markdown_report(markdown_text: str) -> Dict[str, Any]:
    issues: List[str] = []
    warnings: List[str] = []
    text = markdown_text or ""

    if not text.strip():
        issues.append("Report Markdown is empty.")
        return {"valid": False, "issues": issues, "warnings": warnings}

    section_checks = [
        ("title", r"^#\s+\S"),
        ("Section 1", r"^##\s+1\.\s+"),
        ("Section 2", r"^##\s+2\.\s+"),
        ("Section 2.1", r"^###\s+2\.1\b"),
        ("Section 2.2", r"^###\s+2\.2\b"),
        ("Section 2.3", r"^###\s+2\.3\b"),
        ("Section 2.4", r"^###\s+2\.4\b"),
        ("Section 3", r"^##\s+3\.\s+"),
        ("Section 3.1", r"^###\s+3\.1\b"),
        ("Section 3.2", r"^###\s+3\.2\b"),
        ("Section 3.3", r"^###\s+3\.3\b"),
        ("Section 4", r"^##\s+4\.\s+"),
    ]
    for label, pattern in section_checks:
        if required_section_missing(text, pattern):
            issues.append(f"Missing required report structure element: {label}.")

    if count_markdown_table_data_rows(table_after_heading(text, "2.2")) != 5:
        found = count_markdown_table_data_rows(table_after_heading(text, "2.2"))
        issues.append(f"Section 2.2 Top 5 table has {found} data rows; expected exactly 5.")

    if count_markdown_table_data_rows(table_after_heading(text, "3.2")) != 5:
        found = count_markdown_table_data_rows(table_after_heading(text, "3.2"))
        issues.append(f"Section 3.2 Top 5 table has {found} data rows; expected exactly 5.")

    risk_tokens = {match.group(0).strip() for match in RISK_TOKEN_RE.finditer(text)}
    unsupported_risks = sorted(token for token in risk_tokens if token not in RISK_LABELS)
    if unsupported_risks:
        issues.append("Unsupported risk labels found: " + ", ".join(unsupported_risks))
    if not risk_tokens:
        issues.append("No required risk label found; use exactly one of 🟢 Low, 🟠 Medium, or 🔴 High.")

    placeholder_matches = sorted(set(match.group(0) for match in PLACEHOLDER_RE.finditer(text)))
    if placeholder_matches:
        issues.append("Unresolved template placeholders remain: " + ", ".join(placeholder_matches[:12]))

    disclaimer_present = re.search(r"(?im)^Disclaimer\s*$", text) is not None
    legal_advice_phrase = re.search(
        r"(?i)(does not constitute legal advice|no constituye asesoramiento legal|ne constitue pas un conseil juridique|keine rechtsberatung|non costituisce consulenza legale)",
        text,
    )
    if not disclaimer_present and not legal_advice_phrase:
        issues.append("Disclaimer section or localized legal-advice disclaimer could not be found.")

    for label, url in LINK_RE.findall(text):
        expected = domain_for(url)
        normalized_label = label.strip().removeprefix("www.")
        if normalized_label.lower() != expected.lower():
            issues.append(
                f"Visible link text '{label}' should be the source domain '{expected}' for URL {url}."
            )

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
            + ". Install them with: python3 -m pip install -r requirements.txt"
        )


def inline_markup(text: str) -> str:
    parts: List[str] = []
    last = 0
    for match in LINK_RE.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        label = match.group(1).strip() or domain_for(match.group(2))
        url = match.group(2).strip()
        parts.append(f'<link href="{html.escape(url, quote=True)}">{html.escape(label)}</link>')
        last = match.end()
    parts.append(html.escape(text[last:]))
    marked = "".join(parts)

    risk_markup = {
        "🟢 Low": '<font color="#188038">Low</font>',
        "🟠 Medium": '<font color="#b06000">Medium</font>',
        "🔴 High": '<font color="#b00020">High</font>',
    }
    for needle, replacement in risk_markup.items():
        marked = marked.replace(html.escape(needle), replacement)
        marked = marked.replace(needle, replacement)
    marked = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", marked)
    return marked


def build_styles() -> Dict[str, Any]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "TitleCustom",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#222222"),
            spaceAfter=8,
        ),
        "h1": ParagraphStyle(
            "Heading1Custom",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#222222"),
            spaceBefore=8,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "Heading2Custom",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#222222"),
            spaceBefore=6,
            spaceAfter=4,
        ),
        "h3": ParagraphStyle(
            "Heading3Custom",
            parent=base["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=13,
            textColor=colors.HexColor("#222222"),
            spaceBefore=5,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "BodyCustom",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#222222"),
            spaceAfter=4,
        ),
        "bullet": ParagraphStyle(
            "BulletCustom",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            leftIndent=12,
            firstLineIndent=-8,
            bulletIndent=0,
            textColor=colors.HexColor("#222222"),
            spaceAfter=3,
        ),
        "cell": ParagraphStyle(
            "CellCustom",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7.2,
            leading=8.7,
            textColor=colors.HexColor("#222222"),
            spaceAfter=0,
        ),
        "cell_header": ParagraphStyle(
            "CellHeaderCustom",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.7,
            textColor=colors.HexColor("#222222"),
            spaceAfter=0,
        ),
    }


def parse_table_row(line: str) -> List[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def normalize_table(lines: Sequence[str]) -> List[List[str]]:
    rows = [parse_table_row(line) for line in lines if not PIPE_SEPARATOR_RE.match(line)]
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
        author="ChatGPT",
    )
    doc.build(markdown_to_flowables(markdown_text, styles))


def subtitle_font_name() -> str:
    try:
        pdfmetrics.getFont(SUBTITLE_FONT)
        return SUBTITLE_FONT
    except Exception:
        return SUBTITLE_FONT_FALLBACK


def fit_subtitle(subject: str, font_name: str, font_size: int, max_width: float) -> str:
    text = clean_mark(subject) or "Trademark Knockout Report"
    if pdfmetrics.stringWidth(text, font_name, font_size) <= max_width:
        return text
    ellipsis = "..."
    while text and pdfmetrics.stringWidth(text + ellipsis, font_name, font_size) > max_width:
        text = text[:-1]
    return (text.rstrip() + ellipsis) if text else "..."


def build_overlay_pdf(subject: str, output_path: Path) -> None:
    pdf_canvas = canvas.Canvas(str(output_path), pagesize=(PAGE_W, PAGE_H))
    pdf_canvas.setFillColor(colors.white)
    pdf_canvas.rect(SUBTITLE_COVER_X, SUBTITLE_COVER_Y, SUBTITLE_COVER_W, SUBTITLE_COVER_H, stroke=0, fill=1)
    pdf_canvas.setFillColor(colors.HexColor("#222222"))
    font_name = subtitle_font_name()
    pdf_canvas.setFont(font_name, SUBTITLE_FONT_SIZE)
    pdf_canvas.drawString(
        SUBTITLE_X,
        SUBTITLE_BASELINE_Y,
        fit_subtitle(subject, font_name, SUBTITLE_FONT_SIZE, SUBTITLE_COVER_W),
    )
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


def resolve_output_path(arguments: Dict[str, Any], searched_mark: str) -> Path:
    output_value = arguments.get("output_filename") or arguments.get("output_path")
    if output_value:
        path = Path(str(output_value)).expanduser()
        if not path.is_absolute():
            path = default_output_dir() / path
    else:
        path = default_output_dir() / f"{safe_filename(searched_mark)}_knockout_report.pdf"
    if path.suffix.lower() != ".pdf":
        raise ValueError("Output filename must end in .pdf.")
    return path.resolve()


def get_workflow_contract(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "server_role": "artifact_and_quality_control_only",
        "workflow_engine": "ChatGPT",
        "contract_markdown": WORKFLOW_CONTRACT,
        "action_tools": ["validate_knockout_report", "render_clarivate_knockout_pdf"],
    }


def get_report_template(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "template_markdown": REPORT_TEMPLATE,
        "notes": [
            "Localize visible report text as needed.",
            "Do not translate source IDs, URLs, registration numbers, tool names, or trademark verbal elements.",
            "Every Top 5 table must contain exactly five data rows.",
            "Use only source-backed facts.",
        ],
    }


def validate_knockout_report(arguments: Dict[str, Any]) -> Dict[str, Any]:
    markdown_text = arguments.get("markdown") or arguments.get("markdown_text") or ""
    result = validate_markdown_report(str(markdown_text))
    return {
        "success": result["valid"],
        "valid": result["valid"],
        "issues": result["issues"],
        "warnings": result["warnings"],
    }


def render_clarivate_knockout_pdf(arguments: Dict[str, Any]) -> Dict[str, Any]:
    require_pdf_dependencies()
    markdown_text = arguments.get("markdown") or arguments.get("markdown_text")
    if not markdown_text or not str(markdown_text).strip():
        raise ValueError("markdown is required.")

    searched_mark = clean_mark(str(arguments.get("searched_mark") or arguments.get("mark") or ""))
    if not searched_mark:
        raise ValueError("searched_mark is required.")

    validation = validate_markdown_report(str(markdown_text))
    if arguments.get("require_valid", True) and not validation["valid"]:
        return {
            "success": False,
            "pdf_exists": False,
            "validation": validation,
            "error": "Report validation failed; fix the report and call render again.",
        }

    template_path = Path(str(arguments.get("template_path") or DEFAULT_TEMPLATE_PATH)).expanduser()
    if not template_path.is_absolute():
        template_path = BASE_DIR / template_path
    template_path = template_path.resolve()
    if not template_path.exists():
        raise FileNotFoundError(f"Clarivate template PDF not found: {template_path}")

    output_path = resolve_output_path(arguments, searched_mark)
    save_markdown = bool(arguments.get("save_markdown", True))
    markdown_path: Optional[Path] = None
    if save_markdown:
        markdown_path = output_path.with_suffix(".md")
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(str(markdown_text), encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        body_pdf = tmpdir / "report_body.pdf"
        overlay_pdf = tmpdir / "cover_overlay.pdf"
        build_body_pdf(str(markdown_text), body_pdf)
        build_overlay_pdf(str(arguments.get("cover_subtitle") or searched_mark), overlay_pdf)
        merge_template(template_path, body_pdf, overlay_pdf, output_path)

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"PDF generation failed: {output_path}")

    pdf_url = public_file_url(output_path)
    return {
        "success": True,
        "pdf_exists": True,
        "filename": output_path.name,
        "file_reference": str(output_path),
        "artifact_link": pdf_url or str(output_path),
        "pdf_url": pdf_url,
        "pdf_size_bytes": output_path.stat().st_size,
        "markdown_file_reference": str(markdown_path) if markdown_path else None,
        "template_path": str(template_path),
        "validation": validation,
    }


def healthcheck(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "template_exists": DEFAULT_TEMPLATE_PATH.exists(),
        "default_output_dir": str(default_output_dir()),
        "role": "validate_and_render_only",
    }


def version(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


TOOLS: Dict[str, Dict[str, Any]] = {
    "get_workflow_contract": {
        "description": "Return the workflow/tool-boundary contract. This is read-only guidance; ChatGPT still runs the workflow.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": get_workflow_contract,
    },
    "get_report_template": {
        "description": "Return the required Markdown report structure.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": get_report_template,
    },
    "validate_knockout_report": {
        "description": "Validate finalized knockout report Markdown before PDF rendering.",
        "inputSchema": {
            "type": "object",
            "required": ["markdown"],
            "properties": {"markdown": {"type": "string", "description": "Final report Markdown."}},
            "additionalProperties": False,
        },
        "handler": validate_knockout_report,
    },
    "render_clarivate_knockout_pdf": {
        "description": "Render a validated Markdown report into a Clarivate-style PDF and confirm the file exists.",
        "inputSchema": {
            "type": "object",
            "required": ["markdown", "searched_mark"],
            "properties": {
                "markdown": {"type": "string", "description": "Final report Markdown."},
                "searched_mark": {"type": "string", "description": "Searched mark for the cover subtitle."},
                "cover_subtitle": {"type": "string", "description": "Optional cover subtitle override."},
                "output_filename": {
                    "type": "string",
                    "description": "Output filename ending in .pdf. Relative paths resolve under the report output directory.",
                },
                "output_path": {"type": "string", "description": "Alias for output_filename."},
                "template_path": {"type": "string", "description": "Optional Clarivate cover/closing PDF template path."},
                "save_markdown": {"type": "boolean", "default": True},
                "require_valid": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": render_clarivate_knockout_pdf,
    },
    "healthcheck": {
        "description": "Check local renderer health and configured paths.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": healthcheck,
    },
    "version": {
        "description": "Return the local report MCP server version.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": version,
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
                {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
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
        return json_rpc_result(message_id, text_result({"success": False, "tool": name, "error": str(exc)}, is_error=True))


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


def sample_report() -> str:
    no_finding = "No further material source-backed finding"
    return f"""# AI Generated Trademark Knockout Search Report (Demo only)

Mark searched: TESTMARK
Date of report: 2026-05-17

---

## 1. Search Criteria

| Field | Details |
|---|---|
| Mark searched | TESTMARK |
| Type | Word |
| Territories covered | EU |
| Nice classes | 9 |
| Match scope | Exact only; plurals where supported |
| Notes / assumptions | Smoke-test report using synthetic data. |

---

## 2. CompuMark Search Results

### 2.1 Summary

| Item | Result |
|---|---|
| Total records reviewed | 0 |
| Most relevant jurisdictions | EU |
| Most relevant classes | 9 |
| Overall initial risk impression | 🟢 Low |

### 2.2 Most Relevant Trademark References (Top 5)

| Verbal Element | Status | Registration Office | Class(es) | Number | Date | Owner | Full Text URL |
|---|---|---|---|---|---|---|---|
| {no_finding} |  |  |  |  |  |  | link unavailable |
| {no_finding} |  |  |  |  |  |  | link unavailable |
| {no_finding} |  |  |  |  |  |  | link unavailable |
| {no_finding} |  |  |  |  |  |  | link unavailable |
| {no_finding} |  |  |  |  |  |  | link unavailable |

### 2.3 Litigation Activity

| Parties | Case Type | Jurisdiction | Status | Key Details |
|---|---|---|---|---|
| {no_finding} |  |  |  | No source-backed litigation finding was identified. |
| {no_finding} |  |  |  | No source-backed litigation finding was identified. |

### 2.4 Trademark Assessment Comments

* No exact matches are included in this smoke-test report.
* No similar or phonetic matches are included in this smoke-test report.
* No exact matches outside the main class are included in this smoke-test report.
* No material source-backed trademark result is included in this smoke-test report.
* No litigation activity is included in this smoke-test report.

---

## 3. Online Presence Search

### 3.1 Summary

| Item | Result |
|---|---|
| Exact same name found online | Not performed (user opted out) |
| Similar names found online | Not performed (user opted out) |
| Commercial use observed | Not performed (user opted out) |

### 3.2 Most Relevant Web Findings (Top 5)

| Name / Sign | Webpage URL / Source | Territory | Type of use | Notes |
|---|---|---|---|---|
| {no_finding} | link unavailable | unknown |  | Online presence search was not performed. |
| {no_finding} | link unavailable | unknown |  | Online presence search was not performed. |
| {no_finding} | link unavailable | unknown |  | Online presence search was not performed. |
| {no_finding} | link unavailable | unknown |  | Online presence search was not performed. |
| {no_finding} | link unavailable | unknown |  | Online presence search was not performed. |

### 3.3 Web Search Comments

* Online presence search was not performed at the user's request.
* Similar online names were not searched.
* Domains and branding conflicts were not searched.

---

## 4. Key Takeaways

Overall clearance view: 🟢 Low

* This smoke-test report contains no source-backed trademark conflicts.
* Online use was not searched in this smoke test.
* No legal or commercial risk conclusion is drawn from synthetic data.
* Use this output only to verify validation and PDF rendering.

---

Disclaimer

This report is produced for informational purposes only and does not constitute legal advice. Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. Always consult a qualified trademark attorney before filing.
"""


def self_test(render: bool = False) -> int:
    payload: Dict[str, Any] = {
        "tools": list(TOOLS.keys()),
        "healthcheck": healthcheck({}),
        "validation": validate_markdown_report(sample_report()),
    }
    if render:
        payload["render"] = render_clarivate_knockout_pdf(
            {
                "markdown": sample_report(),
                "searched_mark": "TESTMARK",
                "output_filename": "smoke_test_TESTMARK.pdf",
            }
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["validation"]["valid"] else 1


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the trademark knockout report rendering MCP server.")
    parser.add_argument("--self-test", action="store_true", help="Validate a synthetic report and print tool metadata.")
    parser.add_argument("--self-test-render", action="store_true", help="Validate and render a synthetic PDF.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test or args.self_test_render:
        return self_test(render=args.self_test_render)
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
