#!/usr/bin/env python3
"""Minimal Render-ready MCP HTTP server exposing one PDF tool.

This is a simplified version of the working server.py + http_server.py pattern:
- one flat tool schema;
- one JSON-RPC /mcp endpoint;
- one /reports endpoint that serves generated PDFs.

Deploy with:
  python3 simple_pdf_tool_render_server_example.py --host 0.0.0.0 --port $PORT

Required files:
  simple_pdf_tool_render_server_example.py
  requirements.txt
  assets/Clarivate_template.pdf

Recommended Render environment variables:
  REPORT_OUTPUT_DIR=/tmp/reports
  PUBLIC_BASE_URL=https://your-render-service.onrender.com
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


SERVER_NAME = "simple-clarivate-pdf-tool"
SERVER_VERSION = "0.1.0"
SESSION_ID = str(uuid.uuid4())
MCP_POST_PATHS = {"/", "/mcp", "/mcp/mcp"}

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "assets" / "Clarivate_template.pdf"

# Cover subtitle coordinates for the bundled Clarivate template.
PAGE_W = 595.32
PAGE_H = 841.92
SUBTITLE_COVER_X = 40
SUBTITLE_COVER_Y = 535
SUBTITLE_COVER_W = 270
SUBTITLE_COVER_H = 55
SUBTITLE_X = 48
SUBTITLE_BASELINE_Y = 564.91
SUBTITLE_FONT = "Helvetica-Bold"
SUBTITLE_FONT_SIZE = 22

ALLOWED_TAGS = {
    "h1",
    "h2",
    "h3",
    "p",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "strong",
    "em",
    "span",
    "section",
    "br",
    "a",
}
RISK_COLORS = {
    "low": "#1F6F43",
    "medium": "#8A5A00",
    "high": "#A61B1B",
}
SERVER_DISCLAIMER = (
    "This report is produced for informational purposes only and does not constitute legal advice. "
    "Trademark clearance searches are not exhaustive and do not guarantee the availability or registrability of a mark. "
    "Always consult a qualified trademark attorney before filing."
)
SERVER_REPORT_CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: Arial, Helvetica, sans-serif;
  color: #202124;
  background: #ffffff;
  font-size: 11px;
  line-height: 1.38;
  margin: 0;
}
main.report { width: 100%; }
h1 {
  font-size: 23px;
  line-height: 1.2;
  margin: 0 0 16px 0;
  font-weight: 700;
  color: #202124;
}
section {
  border-top: 1.5px solid #2f5597;
  padding-top: 10px;
  margin-top: 18px;
  break-inside: auto;
}
h2 {
  font-size: 14px;
  line-height: 1.25;
  margin: 0 0 8px 0;
  font-weight: 700;
  color: #003366;
}
h3 {
  font-size: 12px;
  line-height: 1.25;
  margin: 13px 0 6px 0;
  font-weight: 700;
  color: #202124;
}
p { margin: 0 0 8px 0; }
ul, ol { margin: 5px 0 10px 0; padding-left: 19px; }
li { margin: 0 0 4px 0; }
table {
  width: 100%;
  border-collapse: collapse;
  margin: 6px 0 12px 0;
  font-size: 10.5px;
}
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td {
  border: 1px solid #d9e2f3;
  padding: 6.5px 7px;
  vertical-align: top;
}
th { font-weight: 700; text-align: left; }
table[data-table="kv"] { break-inside: avoid; page-break-inside: avoid; }
table[data-table="kv"] th {
  width: 33%;
  background: #f3f6fb;
  color: #202124;
}
table[data-table="kv"] td { background: #ffffff; }
table[data-table="data"] thead th {
  background: #f3f6fb;
  color: #202124;
}
table[data-table="data"] tbody th { background: #f8f9fc; }
a { color: #003366; text-decoration: none; }
span[data-risk="low"] { font-weight: 700; color: #1f6f43; }
span[data-risk="medium"] { font-weight: 700; color: #8a5a00; }
span[data-risk="high"] { font-weight: 700; color: #a61b1b; }
.server-disclaimer {
  margin-top: 22px;
  padding-top: 8px;
  border-top: 1px solid #d0d0d0;
  font-size: 9px;
  color: #555555;
  line-height: 1.35;
}
""".strip()


def output_dir() -> Path:
    return Path(os.environ.get("REPORT_OUTPUT_DIR", "/tmp/reports")).expanduser().resolve()


def public_base_url() -> Optional[str]:
    value = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return value.rstrip("/") if value else None


def public_report_url(path: Path) -> str:
    relative = path.resolve().relative_to(output_dir())
    base_url = public_base_url()
    if base_url:
        return f"{base_url}/reports/{relative.as_posix()}"
    return f"/reports/{relative.as_posix()}"


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(value)).strip("._-")
    return name or "report"


def safe_report_basename(value: str) -> str:
    name = safe_filename(value)
    if name.lower().endswith(".pdf"):
        name = name[:-4].strip("._-")
    return name or "report"


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def is_safe_href(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("https://") or lowered.startswith("http://") or lowered.startswith("mailto:")


def valid_span(value: Optional[str], default: str = "1") -> str:
    try:
        number = int(str(value or default))
    except ValueError:
        number = int(default)
    return str(max(1, min(number, 20)))


def build_styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=23,
            leading=27,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#202124"),
            spaceAfter=16,
        ),
        "h1": ParagraphStyle(
            "ReportHeading1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=17.5,
            textColor=colors.HexColor("#003366"),
            spaceBefore=0,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "ReportHeading2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#202124"),
            spaceBefore=7,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "ReportBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=11,
            leading=15.2,
            textColor=colors.HexColor("#202124"),
            spaceAfter=8,
        ),
        "bullet": ParagraphStyle(
            "ReportBullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=11,
            leading=15.2,
            leftIndent=12,
            firstLineIndent=-8,
            spaceAfter=4,
        ),
        "cell": ParagraphStyle(
            "ReportCell",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14.5,
            textColor=colors.HexColor("#202124"),
            spaceAfter=0,
        ),
        "cell_label": ParagraphStyle(
            "ReportCellLabel",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=14.5,
            textColor=colors.HexColor("#202124"),
            spaceAfter=0,
        ),
        "disclaimer": ParagraphStyle(
            "ServerDisclaimer",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12.2,
            textColor=colors.HexColor("#555555"),
            spaceBefore=6,
        ),
    }

class HTMLFragmentSanitizer(HTMLParser):
    """Sanitize the agent's body fragment to a strict, report-safe subset."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: List[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"html", "body"}:
            return
        if tag not in ALLOWED_TAGS:
            return
        if tag == "br":
            self.output.append("<br/>")
            return

        clean_attrs = self._clean_attrs(tag, attrs)
        attr_text = "".join(f' {name}="{html.escape(value, quote=True)}"' for name, value in clean_attrs)
        self.output.append(f"<{tag}{attr_text}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in {"html", "body", "br"}:
            return
        if tag in ALLOWED_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.output.append(html.escape(data))

    def _clean_attrs(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> List[tuple[str, str]]:
        raw = {name.lower(): value for name, value in attrs if name and value is not None}
        cleaned: List[tuple[str, str]] = []
        if tag == "table" and raw.get("data-table") in {"kv", "data"}:
            cleaned.append(("data-table", raw["data-table"]))
        elif tag == "span" and raw.get("data-risk") in RISK_COLORS:
            cleaned.append(("data-risk", raw["data-risk"]))
        elif tag in {"th", "td"}:
            if "colspan" in raw:
                cleaned.append(("colspan", valid_span(raw["colspan"])))
            if "rowspan" in raw:
                cleaned.append(("rowspan", valid_span(raw["rowspan"])))
            if tag == "th" and raw.get("scope") in {"row", "col", "rowgroup", "colgroup"}:
                cleaned.append(("scope", raw["scope"]))
        elif tag == "a" and raw.get("href") and is_safe_href(raw["href"]):
            cleaned.append(("href", raw["href"].strip()))
        return cleaned

    def sanitized(self) -> str:
        return "".join(self.output)


class ReportHTMLParser(HTMLParser):
    """Extract sanitized report HTML into ReportLab-friendly render nodes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: List[Any] = []
        self.current_block: Optional[Dict[str, Any]] = None
        self.current_table: Optional[Dict[str, Any]] = None
        self.current_row: Optional[Dict[str, Any]] = None
        self.current_cell: Optional[Dict[str, Any]] = None
        self.in_thead = False
        self.list_stack: List[Dict[str, Any]] = []
        self.span_risk_stack: List[bool] = []
        self.link_stack: List[bool] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attr = {name.lower(): value for name, value in attrs if name and value is not None}
        if tag == "section":
            self._finish_block()
            self.nodes.append(("section_start",))
        elif tag in {"h1", "h2", "h3"}:
            self._finish_block()
            self.current_block = {"kind": "heading", "level": int(tag[1]), "parts": []}
        elif tag == "p":
            self._finish_block()
            self.current_block = {"kind": "paragraph", "parts": []}
        elif tag in {"ul", "ol"}:
            self.list_stack.append({"tag": tag, "count": 0})
        elif tag == "li":
            self._finish_block()
            prefix = "-"
            if self.list_stack and self.list_stack[-1]["tag"] == "ol":
                self.list_stack[-1]["count"] += 1
                prefix = f"{self.list_stack[-1]['count']}."
            self.current_block = {"kind": "bullet", "prefix": prefix, "parts": []}
        elif tag == "table":
            self._finish_block()
            self.current_table = {"type": attr.get("data-table") or "data", "rows": []}
        elif tag == "thead":
            self.in_thead = True
        elif tag == "tbody":
            self.in_thead = False
        elif tag == "tr" and self.current_table is not None:
            self.current_row = {"is_header": self.in_thead, "cells": []}
        elif tag in {"th", "td"} and self.current_row is not None:
            self.current_cell = {
                "tag": tag,
                "parts": [],
                "colspan": int(attr.get("colspan", "1")),
                "rowspan": int(attr.get("rowspan", "1")),
            }
        elif tag == "strong":
            self._append_markup("<b>")
        elif tag == "em":
            self._append_markup("<i>")
        elif tag == "span":
            risk = attr.get("data-risk")
            if risk in RISK_COLORS:
                self._append_markup(f'<font color="{RISK_COLORS[risk]}"><b>')
                self.span_risk_stack.append(True)
            else:
                self.span_risk_stack.append(False)
        elif tag == "a":
            href = attr.get("href")
            if href and is_safe_href(href):
                escaped = html.escape(href, quote=True)
                self._append_markup(f'<a href="{escaped}"><font color="#003366">')
                self.link_stack.append(True)
            else:
                self.link_stack.append(False)
        elif tag == "br":
            self._append_markup("<br/>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "p", "li"}:
            self._finish_block()
        elif tag in {"ul", "ol"} and self.list_stack:
            self.list_stack.pop()
        elif tag == "thead":
            self.in_thead = False
        elif tag in {"th", "td"} and self.current_cell is not None and self.current_row is not None:
            self.current_row["cells"].append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None and self.current_table is not None:
            if any("".join(cell["parts"]).strip() for cell in self.current_row["cells"]):
                if all(cell["tag"] == "th" for cell in self.current_row["cells"]):
                    self.current_row["is_header"] = True
                self.current_table["rows"].append(self.current_row)
            self.current_row = None
        elif tag == "table" and self.current_table is not None:
            if self.current_table["rows"]:
                self.nodes.append(("table", self.current_table))
            self.current_table = None
        elif tag == "strong":
            self._append_markup("</b>")
        elif tag == "em":
            self._append_markup("</i>")
        elif tag == "span":
            if self.span_risk_stack and self.span_risk_stack.pop():
                self._append_markup("</b></font>")
        elif tag == "a":
            if self.link_stack and self.link_stack.pop():
                self._append_markup("</font></a>")

    def handle_data(self, data: str) -> None:
        self._append_markup(html.escape(data))

    def close(self) -> None:
        self._finish_block()
        super().close()

    def _append_markup(self, value: str) -> None:
        if self.current_cell is not None:
            self.current_cell["parts"].append(value)
        elif self.current_block is not None:
            self.current_block["parts"].append(value)

    def _finish_block(self) -> None:
        if not self.current_block:
            return
        markup = "".join(self.current_block["parts"]).strip()
        if markup:
            if self.current_block["kind"] == "heading":
                self.nodes.append(("heading", self.current_block["level"], markup))
            elif self.current_block["kind"] == "bullet":
                self.nodes.append(("bullet", self.current_block["prefix"], markup))
            else:
                self.nodes.append(("paragraph", markup))
        self.current_block = None


def sanitize_html_fragment(fragment: str) -> str:
    parser = HTMLFragmentSanitizer()
    parser.feed(fragment)
    parser.close()
    return parser.sanitized()


def wrap_html_document(document_title: str, sanitized_fragment: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(document_title)}</title>
  <style>
{SERVER_REPORT_CSS}
  </style>
</head>
<body>
  <main class="report">
    {sanitized_fragment}
  </main>

  <div class="server-disclaimer">
    {html.escape(SERVER_DISCLAIMER)}
  </div>
</body>
</html>"""


def html_table_flowable(table_node: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> Table:
    rows = table_node["rows"]
    table_type = table_node["type"] if table_node["type"] in {"kv", "data"} else "data"
    max_cols = max(sum(cell["colspan"] for cell in row["cells"]) for row in rows)
    data = []
    span_commands = []

    for row_index, row in enumerate(rows):
        rendered_row = []
        col_index = 0
        for cell in row["cells"]:
            style_name = "cell_label" if cell["tag"] == "th" or (table_type == "kv" and col_index == 0) else "cell"
            rendered_row.append(Paragraph("".join(cell["parts"]).strip(), styles[style_name]))
            colspan = max(1, int(cell["colspan"]))
            if colspan > 1:
                span_commands.append(("SPAN", (col_index, row_index), (col_index + colspan - 1, row_index)))
                for _ in range(colspan - 1):
                    rendered_row.append("")
            col_index += colspan
        rendered_row.extend([""] * (max_cols - len(rendered_row)))
        data.append(rendered_row)

    available_width = A4[0] - 32 * mm
    if table_type == "kv" and max_cols == 2:
        col_widths = [available_width * 0.33, available_width * 0.67]
    else:
        col_widths = [available_width / max_cols] * max_cols

    table = Table(data, colWidths=col_widths, repeatRows=1 if rows and rows[0]["is_header"] else 0, hAlign="LEFT")
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D9E2F3")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6.5),
    ]
    if table_type == "kv":
        commands.append(("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F6FB")))
    else:
        for row_index, row in enumerate(rows):
            if row["is_header"]:
                commands.append(("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#F3F6FB")))
            for col_index, cell in enumerate(row["cells"]):
                if cell["tag"] == "th" and not row["is_header"]:
                    commands.append(("BACKGROUND", (col_index, row_index), (col_index, row_index), colors.HexColor("#F8F9FC")))
    table.setStyle(TableStyle(commands + span_commands))
    return table


def html_fragment_to_flowables(html_fragment: str, document_title: str) -> List[Any]:
    sanitized_fragment = sanitize_html_fragment(html_fragment)
    # Keep the complete server-side document available as the canonical render input.
    # The ReportLab renderer consumes the sanitized semantic fragment and mirrors the fixed CSS below.
    _server_owned_html_document = wrap_html_document(document_title, sanitized_fragment)

    parser = ReportHTMLParser()
    parser.feed(sanitized_fragment)
    parser.close()

    styles = build_styles()
    flowables: List[Any] = []
    for node in parser.nodes:
        kind = node[0]
        if kind == "section_start":
            flowables.append(Spacer(1, 10))
            flowables.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2F5597"), spaceAfter=8))
        elif kind == "heading":
            _, level, markup = node
            style_name = "title" if level == 1 else "h1" if level == 2 else "h2"
            flowables.append(Paragraph(markup, styles[style_name]))
        elif kind == "paragraph":
            _, markup = node
            flowables.append(Paragraph(markup, styles["body"]))
        elif kind == "bullet":
            _, prefix, markup = node
            flowables.append(Paragraph(f"{prefix} {markup}", styles["bullet"]))
        elif kind == "table":
            _, table_node = node
            flowables.append(html_table_flowable(table_node, styles))
            flowables.append(Spacer(1, 12))

    flowables.append(Spacer(1, 22))
    flowables.append(HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#D0D0D0"), spaceAfter=8))
    flowables.append(Paragraph(html.escape(SERVER_DISCLAIMER), styles["disclaimer"]))
    return flowables or [Paragraph("No report content provided.", styles["body"])]


def build_html_body_pdf(html_fragment: str, document_title: str, output_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=clean_text(document_title) or "Clarivate Report",
        author="Example MCP server",
    )
    doc.build(html_fragment_to_flowables(html_fragment, document_title))


def build_cover_overlay(subject: str, output_path: Path) -> None:
    pdf_canvas = canvas.Canvas(str(output_path), pagesize=(PAGE_W, PAGE_H))
    pdf_canvas.setFillColor(colors.white)
    pdf_canvas.rect(SUBTITLE_COVER_X, SUBTITLE_COVER_Y, SUBTITLE_COVER_W, SUBTITLE_COVER_H, stroke=0, fill=1)
    pdf_canvas.setFillColor(colors.HexColor("#222222"))
    pdf_canvas.setFont(SUBTITLE_FONT, SUBTITLE_FONT_SIZE)
    pdf_canvas.drawString(SUBTITLE_X, SUBTITLE_BASELINE_Y, clean_text(subject) or "Report")
    pdf_canvas.save()


def merge_with_template(body_path: Path, overlay_path: Path, output_path: Path) -> None:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Template PDF not found: {TEMPLATE_PATH}")

    template_reader = PdfReader(str(TEMPLATE_PATH))
    if len(template_reader.pages) < 2:
        raise ValueError("Template PDF must contain at least two pages.")

    body_reader = PdfReader(str(body_path))
    overlay_reader = PdfReader(str(overlay_path))
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


def build_report_pdf(
    document_title: str,
    file_name: str,
    html_fragment: str,
) -> Dict[str, Any]:
    output_path = output_dir() / f"{file_name}_{timestamp()}.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        body_pdf = tmpdir / "body.pdf"
        overlay_pdf = tmpdir / "cover_overlay.pdf"
        build_html_body_pdf(html_fragment, document_title, body_pdf)
        build_cover_overlay(document_title, overlay_pdf)
        merge_with_template(body_pdf, overlay_pdf, output_path)

    return {
        "pdf_url": public_report_url(output_path),
        "download_the_report": public_report_url(output_path),
        "pdf_path": str(output_path.resolve()),
        "pdf_exists": output_path.exists(),
        "pdf_size_bytes": output_path.stat().st_size if output_path.exists() else 0,
        "template_path": str(TEMPLATE_PATH.resolve()),
    }


def generate_clarivate_report_pdf(arguments: Dict[str, Any]) -> Dict[str, Any]:
    html_fragment = str(arguments.get("htmlFragment") or "").strip()
    file_name = safe_report_basename(str(arguments.get("fileName") or "report"))
    document_title = clean_text(arguments.get("documentTitle") or "Clarivate Report")

    if not html_fragment:
        raise ValueError("htmlFragment is required.")
    if not file_name:
        raise ValueError("fileName is required.")
    if not document_title:
        raise ValueError("documentTitle is required.")

    return build_report_pdf(document_title, file_name, html_fragment)


def text_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if not is_error and isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


def json_rpc_result(message_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def json_rpc_error(message_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


PDF_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["pdf_url", "download_the_report", "pdf_path", "pdf_exists", "pdf_size_bytes", "template_path"],
    "properties": {
        "pdf_url": {"type": "string"},
        "download_the_report": {"type": "string"},
        "pdf_path": {"type": "string"},
        "pdf_exists": {"type": "boolean"},
        "pdf_size_bytes": {"type": "integer"},
        "template_path": {"type": "string"},
    },
    "additionalProperties": False,
}

TOOLS: Dict[str, Dict[str, Any]] = {
    "generate_clarivate_report_pdf": {
        "description": (
            "Generate a Clarivate-template PDF from a compact, semantic HTML body fragment. "
            "Send report content only: no full HTML document, CSS, inline styles, scripts, or Markdown tables. "
            "The server sanitizes the fragment, applies fixed sober business-report styling, adds the disclaimer, "
            "wraps the body, creates the PDF, and returns a downloadable PDF link."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["htmlFragment", "fileName", "documentTitle"],
            "properties": {
                "htmlFragment": {
                    "type": "string",
                    "description": (
                        "Report body fragment only; the server sanitizes it before PDF generation. Allowed tags include h1, h2, h3, p, ul, ol, li, "
                        "section, table, thead, tbody, tr, th, td, strong, em, span, br, and a. "
                        "Use table data-table='kv' for key/value tables, table data-table='data' for data tables, "
                        "span data-risk='low|medium|high' for risk labels, th/td colspan or rowspan for simple spans, "
                        "optional th scope, and a href with http, https, or mailto links only. Do not include html/head/body/style/script, CSS, "
                        "inline style attributes, event handlers, or Markdown tables."
                    ),
                },
                "fileName": {
                    "type": "string",
                    "description": "Base PDF filename, for example trademark-knockout-report.pdf. Directories are ignored and a timestamp is appended.",
                },
                "documentTitle": {
                    "type": "string",
                    "description": "Document title used for the PDF metadata and Clarivate cover subtitle.",
                },
            },
            "additionalProperties": False,
        },
        "outputSchema": PDF_OUTPUT_SCHEMA,
        "handler": generate_clarivate_report_pdf,
    }
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
        handler: Callable[[Dict[str, Any]], Dict[str, Any]] = TOOLS[name]["handler"]
        return json_rpc_result(message_id, text_result(handler(arguments)))
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


class MCPHttpHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_common_headers(self, status: int, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "authorization, content-type, mcp-protocol-version, mcp-session-id",
        )
        self.send_header("Access-Control-Expose-Headers", "mcp-session-id")
        self.send_header("Mcp-Session-Id", SESSION_ID)
        self.end_headers()

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_common_headers(status)
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_common_headers(204)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/"}:
            self._write_json(
                200,
                {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "mcp_endpoint": "/mcp",
                    "reports_endpoint": "/reports",
                    "tools": list(TOOLS.keys()),
                },
            )
            return
        if self.path == "/health":
            self._write_json(200, {"ok": True, "name": SERVER_NAME, "version": SERVER_VERSION})
            return
        if self.path.startswith("/reports/"):
            self._serve_report_file(include_body=True)
            return
        if self.path == "/mcp":
            self._write_json(405, {"error": "Use POST /mcp with JSON-RPC messages."})
            return
        self._write_json(404, {"error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path.startswith("/reports/"):
            self._serve_report_file(include_body=False)
            return
        self._send_common_headers(404)

    def _serve_report_file(self, include_body: bool) -> None:
        relative_url_path = self.path.split("?", 1)[0][len("/reports/") :]
        if not relative_url_path or ".." in relative_url_path.split("/"):
            self._write_json(400, {"error": "invalid report path"})
            return

        file_path = (output_dir() / relative_url_path).resolve()
        try:
            file_path.relative_to(output_dir())
        except ValueError:
            self._write_json(400, {"error": "invalid report path"})
            return
        if not file_path.exists() or not file_path.is_file():
            self._write_json(404, {"error": "report not found"})
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/pdf"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Disposition", f'inline; filename="{file_path.name}"')
        self.end_headers()
        if not include_body:
            return
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 64)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self) -> None:  # noqa: N802
        request_path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if request_path not in MCP_POST_PATHS:
            self._write_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            self._write_json(400, json_rpc_error(None, -32700, "Parse error", str(exc)))
            return

        try:
            response = self._handle_payload(payload)
        except Exception as exc:
            self._write_json(500, json_rpc_error(None, -32603, "Internal error", str(exc)))
            return

        if response is None:
            self._send_common_headers(202)
            return
        self._write_json(200, response)

    def _handle_payload(self, payload: Any) -> Optional[Any]:
        if isinstance(payload, list):
            responses: List[Any] = []
            for message in payload:
                response = handle_request(message)
                if response is not None:
                    responses.append(response)
            return responses or None
        if isinstance(payload, dict):
            return handle_request(payload)
        return json_rpc_error(None, -32600, "Invalid Request")


def self_test() -> int:
    result = generate_clarivate_report_pdf(
        {
            "documentTitle": "Trademark Knockout Search Report",
            "fileName": "trademark-knockout-report.pdf",
            "htmlFragment": (
                "<h1>Trademark Knockout Search Report</h1>"
                "<section><h2>1. Search Criteria</h2><table data-table='kv'><tbody>"
                "<tr><th>Mark searched</th><td>POWER BULA</td></tr>"
                "<tr><th>Type</th><td>Word</td></tr>"
                "<tr><th>Territories covered</th><td>Philippines (PH), WIPO designations in PH</td></tr>"
                "<tr><th>Nice classes</th><td>3</td></tr>"
                "</tbody></table></section>"
                "<section><h2>2. Risk Summary</h2><table data-table='kv'><tbody>"
                "<tr><th>Exact match found</th><td>Yes</td></tr>"
                "<tr><th>Initial risk</th><td><span data-risk='high'>High</span></td></tr>"
                "</tbody></table></section>"
                "<section><h2>3. Key Takeaways</h2><ul>"
                "<li>Exact or near-identical use was identified in the searched class.</li>"
                "<li>Proceed with caution and obtain legal review before filing.</li>"
                "</ul></section>"
            ),
        }
    )
    print(json.dumps({"tools": list(TOOLS.keys()), "result": result}, indent=2))
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a minimal one-tool MCP HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--self-test", action="store_true", help="Generate one example PDF and exit.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_dir().mkdir(parents=True, exist_ok=True)
    if args.self_test:
        return self_test()

    httpd = ThreadingHTTPServer((args.host, args.port), MCPHttpHandler)
    print(f"{SERVER_NAME} listening on http://{args.host}:{args.port}/mcp", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
