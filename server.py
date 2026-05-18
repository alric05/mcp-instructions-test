#!/usr/bin/env python3
"""Small MCP server for a trademark knockout report POC.

This version uses MCP prompts and resources to put the workflow instructions in
context instead of asking the agent to call step-by-step instruction tools.

The server exposes:
1. One prompt: trademark_knockout_report
   - Starts the workflow and embeds the workflow + report-template resources.
2. Two resources:
   - workflow instructions
   - markdown report template
3. One tool:
   - generate_clarivate_report_pdf, the final action that renders the report.

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
SERVER_VERSION = "0.3.0-prompts-resources-poc"
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

WORKFLOW_INSTRUCTIONS_URI = "trademark-knockout://workflow/instructions.md"
REPORT_TEMPLATE_URI = "trademark-knockout://workflow/report-template.md"

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

WORKFLOW_INSTRUCTIONS = """# Trademark Knockout Report Workflow

Use this resource as the operating instructions for the trademark knockout report POC.
The old step-by-step instruction tools are intentionally not exposed in this version.
The workflow should be followed from context after this resource is attached or read.

## Goal

Create an AI Generated Trademark Knockout Search Report and render it as a Clarivate-template PDF.
Use CompuMark trademark and litigation evidence. Keep the report concise, source-backed, and visibly uncertain where the evidence is incomplete.

## Workflow order

1. Confirm search criteria.
2. Collect CompuMark trademark evidence.
3. Check relevant litigation.
4. Draft the report markdown using the report-template resource.
5. Call `generate_clarivate_report_pdf` and return the generated link or local path.

## 1. Confirm search criteria

Confirm only the essentials:

- Exact mark.
- Territories or registration offices.
- Nice classes.

If one essential item is missing, ask only for the first missing item. Do not run CompuMark searches before the essentials are known.
Optional details such as word/logo/both, match scope, and notes can be captured when available, but should not block the workflow unless they materially affect the search.

## 2. Collect CompuMark trademark evidence

Use the CompuMark trademark-search tool to find the most relevant records. Do not use the knockout-search tool.
Perform at most three trademark searches:

1. Exact search without phonetics.
2. Exact search with phonetics.
3. Contains search with phonetics.

Only proceed to the next search if you have fewer than five useful results. Start with the exact search in the requested offices and classes.
Only search using the verbal element provided by the user; do not invent spelling variants or alternate marks.

Select five records by:

- Exactness or similarity of the verbal element.
- Territory or registration office relevance.
- Nice class overlap.
- Active/live status.
- Owner relevance and apparent commercial significance.

For the selected IDs, fetch trademark-content and create full-text links labeled exactly `full-text`.
Do not fetch goods for this POC.

CompuMark search notes:

- Avoid country-code lookup tools if possible. Use common country/office codes from internal knowledge when possible.
- For `INT_CLASS_NUMBER`, use operator `EQUALS` with a comma-separated value such as `3,4,5` to search multiple Nice classes in one request.
- If a specific country is specified, also include `WO` with `limitWOresultsToDesignated: true` in search parameters.
- If an EU country is specified, also include `EM` and `WO` with `limitWOresultsToDesignated: true` in search parameters.

## 3. Check relevant litigation

Use the CompuMark litigation-search tool lightly for trademark disputes involving:

- The searched mark.
- The strongest matching verbal elements.
- Owners from the top trademark references.

Keep only cases that could affect risk. Summarize parties, jurisdiction, status, case type, and why each case matters. If there is no material litigation, say so plainly.

Litigation search constraints:

- Always use this condition on first action type in the first query: `{'field': 'FIRST_ACTION_TYPE', 'op': 'EQ', 'value': 'OPPOSITION'}`.
- When filtering on party name, also include: `{'field': 'PARTY_IS_EX_OFFICIO', 'op': 'EQ', 'value': False}`.
- Avoid queries with OR conditions. Split them into separate queries that use only AND conditions.
- When using `group_by`, only `order_by` fields that are also grouped or aggregated.
- The `order_by` clause is a dict. Valid example: `order_by: {'FIRST_ACTION_DATE': 'DESC'}`.

## 4. Draft the report markdown

Use the `trademark-knockout://workflow/report-template.md` resource.
Keep the section structure and numbering.
Use source-backed facts only and make uncertainty visible.
Keep the Top 5 trademark references table at exactly five data rows.
If fewer than five material references exist, fill remaining rows with a simple row such as `No further material source-backed finding`.
Use CompuMark links as `[full-text](url)`.
Use risk labels exactly as one of: `🟢 Low`, `🟠 Medium`, `🔴 High`.
Write the visible report in the user's language unless they asked otherwise.

## 5. Generate the PDF

Call `generate_clarivate_report_pdf` with:

- `subject`: the searched mark.
- `markdown`: the completed report markdown.

Then answer the user with `download_the_report` or `pdf_url` if present. If no public URL is configured, return the local `pdf_path` and mention that no public URL is configured.
"""

REPORT_TEMPLATE_RESOURCE = """# Trademark Knockout Report Template

Rules:

- Keep the section structure and numbering.
- Use source-backed facts only; make uncertainty visible.
- Keep the Top 5 trademark references table at exactly five data rows.
- Use `full-text` as the label for CompuMark full-text links.
- Write the visible report in the user's language unless they asked otherwise.

---

""" + REPORT_TEMPLATE


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------


def text_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    """Return a normal MCP tool result.

    Prompts and resources do not use this wrapper; only tools/call does.
    """
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
# Prompt and resource helpers
# ---------------------------------------------------------------------------


RESOURCES: Dict[str, Dict[str, Any]] = {
    WORKFLOW_INSTRUCTIONS_URI: {
        "uri": WORKFLOW_INSTRUCTIONS_URI,
        "name": "workflow-instructions.md",
        "title": "Trademark knockout workflow instructions",
        "description": "End-to-end instructions for the trademark knockout report workflow.",
        "mimeType": "text/markdown",
        "text": WORKFLOW_INSTRUCTIONS,
        "annotations": {"audience": ["assistant"], "priority": 1.0},
    },
    REPORT_TEMPLATE_URI: {
        "uri": REPORT_TEMPLATE_URI,
        "name": "report-template.md",
        "title": "Trademark knockout markdown report template",
        "description": "Markdown template and rules for the final report body.",
        "mimeType": "text/markdown",
        "text": REPORT_TEMPLATE_RESOURCE,
        "annotations": {"audience": ["assistant"], "priority": 0.9},
    },
}

PROMPTS: Dict[str, Dict[str, Any]] = {
    WORKFLOW_NAME: {
        "name": WORKFLOW_NAME,
        "title": "Trademark knockout report",
        "description": "Run the trademark knockout report workflow using embedded MCP resources for instructions and report structure.",
        "arguments": [
            {"name": "mark", "description": "Exact word mark to search.", "required": False},
            {"name": "jurisdictions", "description": "Territories or offices, for example EU, UK, US, WO.", "required": False},
            {"name": "nice_classes", "description": "Nice class numbers, comma-separated if multiple.", "required": False},
            {"name": "language", "description": "Visible report language. Defaults to the user's language.", "required": False},
            {"name": "criteria_json", "description": "Optional JSON object with known criteria.", "required": False},
        ],
    }
}


def resource_metadata(resource: Dict[str, Any]) -> Dict[str, Any]:
    """Return the resource fields used by resources/list."""
    return {
        key: value
        for key, value in resource.items()
        if key in {"uri", "name", "title", "description", "mimeType", "annotations"}
    }


def resource_content(uri: str) -> Dict[str, Any]:
    resource = RESOURCES[uri]
    return {"uri": uri, "mimeType": resource["mimeType"], "text": resource["text"]}


def prompt_metadata(prompt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": prompt["name"],
        "title": prompt.get("title"),
        "description": prompt.get("description"),
        "arguments": prompt.get("arguments", []),
    }


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


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def known_criteria_from_prompt_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    criteria = parse_criteria_json(arguments.get("criteria_json"))
    for key in ["mark", "jurisdictions", "nice_classes", "language"]:
        value = arguments.get(key)
        if value not in (None, ""):
            criteria[key] = value
    return criteria


def criteria_markdown(criteria: Dict[str, Any]) -> str:
    if not criteria:
        return "No criteria were provided in the prompt arguments. Ask for the first missing essential item."

    rows = []
    for key in ["mark", "jurisdictions", "nice_classes", "language", "type", "match_scope", "notes"]:
        if key in criteria and criteria[key] not in (None, ""):
            rows.append(f"- {key}: {criteria[key]}")
    for key, value in criteria.items():
        if key not in {"mark", "jurisdictions", "nice_classes", "language", "type", "match_scope", "notes"}:
            rows.append(f"- {key}: {value}")
    return "\n".join(rows) if rows else "Criteria were provided, but no recognized fields were populated."


def build_workflow_prompt(arguments: Dict[str, Any]) -> Dict[str, Any]:
    criteria = known_criteria_from_prompt_args(arguments)
    mark = clean_text(criteria.get("mark")) or "[missing]"

    starter_text = f"""Run the trademark knockout report workflow.

Known criteria:
{criteria_markdown(criteria)}

Use the embedded MCP resources in this prompt as the workflow source of truth:

- {WORKFLOW_INSTRUCTIONS_URI}
- {REPORT_TEMPLATE_URI}

Start by checking whether the essentials are present: exact mark, territories/offices, and Nice classes.
If any essential item is missing, ask only for the first missing item.
If all essentials are present, use the available CompuMark tools to collect evidence, draft the markdown report, then call `generate_clarivate_report_pdf` with `subject` set to `{mark}`.
"""

    return {
        "description": "Trademark knockout report prompt with embedded workflow and report-template resources.",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": starter_text}},
            {"role": "user", "content": {"type": "resource", "resource": resource_content(WORKFLOW_INSTRUCTIONS_URI)}},
            {"role": "user", "content": {"type": "resource", "resource": resource_content(REPORT_TEMPLATE_URI)}},
        ],
    }


def get_prompt(params: Dict[str, Any]) -> Dict[str, Any]:
    name = str(params.get("name") or "").strip()
    if not name:
        raise ValueError("Prompt name is required.")
    if name != WORKFLOW_NAME:
        raise ValueError(f"Unknown prompt: {name}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("Prompt arguments must be an object.")
    return build_workflow_prompt(arguments)


def read_resource(params: Dict[str, Any]) -> Dict[str, Any]:
    uri = str(params.get("uri") or "").strip()
    if not uri:
        raise ValueError("Resource uri is required.")
    if uri not in RESOURCES:
        raise ValueError(f"Unknown resource: {uri}")
    return {"contents": [resource_content(uri)]}


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


NULLABLE_STRING = {"type": ["string", "null"]}

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
    "generate_clarivate_report_pdf": {
        "description": "Generate the final PDF using the Clarivate template: template cover, generated report body, template closing page.",
        "inputSchema": {
            "type": "object",
            "required": ["subject"],
            "anyOf": [{"required": ["markdown"]}, {"required": ["markdown_path"]}],
            "properties": {
                "subject": {"type": "string", "description": "Cover subtitle, normally the searched mark."},
                "markdown": {"type": "string", "description": "Completed report markdown."},
                "markdown_path": {"type": "string", "description": "Markdown file path if markdown is omitted."},
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
    protocol = params.get("protocolVersion") or "2025-06-18"
    return json_rpc_result(
        message_id,
        {
            "protocolVersion": protocol,
            "capabilities": {
                "tools": {"listChanged": False},
                "prompts": {"listChanged": False},
                "resources": {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Use the MCP prompt 'trademark_knockout_report' to start the workflow. "
                "The prompt embeds the workflow and report-template resources. "
                "Only call the tool 'generate_clarivate_report_pdf' after drafting the completed markdown report."
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


def handle_prompts_list(message_id: Any) -> Dict[str, Any]:
    return json_rpc_result(message_id, {"prompts": [prompt_metadata(prompt) for prompt in PROMPTS.values()]})


def handle_prompts_get(message_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json_rpc_result(message_id, get_prompt(params))
    except Exception as exc:
        return json_rpc_error(message_id, -32602, str(exc))


def handle_resources_list(message_id: Any) -> Dict[str, Any]:
    return json_rpc_result(message_id, {"resources": [resource_metadata(resource) for resource in RESOURCES.values()]})


def handle_resources_read(message_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json_rpc_result(message_id, read_resource(params))
    except Exception as exc:
        return json_rpc_error(message_id, -32602, str(exc))


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
    if method == "prompts/list":
        return handle_prompts_list(message_id)
    if method == "prompts/get":
        return handle_prompts_get(message_id, params)
    if method == "resources/list":
        return handle_resources_list(message_id)
    if method == "resources/read":
        return handle_resources_read(message_id, params)
    if method == "resources/templates/list":
        return json_rpc_result(message_id, {"resourceTemplates": []})
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
    prompt = get_prompt(
        {
            "name": WORKFLOW_NAME,
            "arguments": {
                "mark": "NOVALYTIC",
                "jurisdictions": "EU, UK",
                "nice_classes": "9, 42",
                "language": "English",
            },
        }
    )
    print(
        json.dumps(
            {
                "tools": list(TOOLS.keys()),
                "prompts": [prompt_metadata(item) for item in PROMPTS.values()],
                "resources": [resource_metadata(item) for item in RESOURCES.values()],
                "prompt_message_summary": [
                    {
                        "role": message["role"],
                        "content_type": message["content"]["type"],
                        "uri": message["content"].get("resource", {}).get("uri"),
                    }
                    for message in prompt["messages"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the trademark knockout report MCP server.")
    parser.add_argument("--self-test", action="store_true", help="Print sample prompt/resource/tool output and exit.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        return self_test()
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
