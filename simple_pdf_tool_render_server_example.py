#!/usr/bin/env python3
"""Minimal Render-ready Streamable HTTP MCP server for one PDF tool.

Deploy this with:
  - this file
  - requirements.txt
  - assets/Clarivate_template.pdf

Render start command:
  python3 simple_pdf_tool_render_server_example.py --host 0.0.0.0 --port $PORT

Environment variables:
  REPORT_OUTPUT_DIR=/tmp/reports
  PUBLIC_BASE_URL=https://your-render-service.onrender.com

Endpoints:
  /mcp          Streamable HTTP MCP endpoint, handled by the official MCP SDK
  /health       Health check
  /reports/...  Generated PDFs
"""

from __future__ import annotations

import argparse
import contextlib
import html
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import uvicorn
from mcp.server.fastmcp import FastMCP
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route


SERVER_NAME = "simple-clarivate-pdf-tool-render-example"
SERVER_VERSION = "0.2.0"

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "assets" / "Clarivate_template.pdf"

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

LINK_RE = re.compile(r"\[([^\]]+)]\((https?://[^)\s]+)\)")


def output_dir() -> Path:
    return Path(os.environ.get("REPORT_OUTPUT_DIR", "/tmp/reports")).expanduser().resolve()


def public_base_url() -> Optional[str]:
    value = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return value.rstrip("/") if value else None


def public_report_url(path: Path) -> Optional[str]:
    base_url = public_base_url()
    if not base_url:
        return None
    relative = path.resolve().relative_to(output_dir())
    return f"{base_url}/reports/{relative.as_posix()}"


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(value)).strip("._-")
    return name or "report"


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def inline_markup(text: str) -> str:
    parts: List[str] = []
    last = 0
    for match in LINK_RE.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        label = clean_text(match.group(1))
        url = match.group(2).strip()
        parts.append(
            f'<a href="{html.escape(url, quote=True)}">'
            f'<font color="#0563C1">{html.escape(label)}</font></a>'
        )
        last = match.end()
    parts.append(html.escape(text[last:]))
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", "".join(parts))


def build_styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#222222"),
            spaceAfter=8,
        ),
        "h1": ParagraphStyle(
            "ReportHeading1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#222222"),
            spaceBefore=8,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "ReportHeading2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=colors.HexColor("#222222"),
            spaceBefore=6,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "ReportBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#222222"),
            spaceAfter=4,
        ),
        "bullet": ParagraphStyle(
            "ReportBullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            leftIndent=12,
            firstLineIndent=-8,
            spaceAfter=3,
        ),
    }


def markdown_to_flowables(markdown_text: str) -> List[Any]:
    styles = build_styles()
    flowables: List[Any] = []
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in {"---", "***", "___"}:
            flowables.append(Spacer(1, 6))
        elif line.startswith("# "):
            flowables.append(Paragraph(inline_markup(line[2:].strip()), styles["title"]))
        elif line.startswith("## "):
            flowables.append(Paragraph(inline_markup(line[3:].strip()), styles["h1"]))
        elif line.startswith("### "):
            flowables.append(Paragraph(inline_markup(line[4:].strip()), styles["h2"]))
        elif line.startswith("- ") or line.startswith("* "):
            flowables.append(Paragraph("• " + inline_markup(line[2:].strip()), styles["bullet"]))
        else:
            flowables.append(Paragraph(inline_markup(line), styles["body"]))
    return flowables or [Paragraph("No report content provided.", styles["body"])]


def build_body_pdf(markdown_text: str, output_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Clarivate Report",
        author="Example MCP server",
    )
    doc.build(markdown_to_flowables(markdown_text))


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


mcp = FastMCP(SERVER_NAME, stateless_http=True, json_response=True)


@mcp.tool(
    name="generate_clarivate_report_pdf",
    description="Generate a Clarivate-template PDF from a subject, markdown report body, and filename.",
)
def generate_clarivate_report_pdf(subject: str, markdown: str, filename: str) -> Dict[str, Any]:
    """Generate a PDF and return a public link to the generated file."""
    subject = clean_text(subject)
    markdown = str(markdown or "").strip()
    filename = safe_filename(filename)

    if not subject:
        raise ValueError("subject is required.")
    if not markdown:
        raise ValueError("markdown is required.")
    if not filename:
        raise ValueError("filename is required.")

    output_path = output_dir() / f"{filename}_{timestamp()}.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        body_pdf = tmpdir / "body.pdf"
        overlay_pdf = tmpdir / "cover_overlay.pdf"
        build_body_pdf(markdown, body_pdf)
        build_cover_overlay(subject, overlay_pdf)
        merge_with_template(body_pdf, overlay_pdf, output_path)

    pdf_url = public_report_url(output_path)
    return {
        "pdf_url": pdf_url,
        "download_the_report": pdf_url,
        "pdf_path": str(output_path.resolve()),
        "pdf_exists": output_path.exists(),
        "pdf_size_bytes": output_path.stat().st_size if output_path.exists() else 0,
    }


async def root(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "mcp_endpoint": "/mcp",
            "reports_endpoint": "/reports",
        }
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "name": SERVER_NAME, "version": SERVER_VERSION})


async def report_file(request: Request) -> Response:
    relative = request.path_params["path"]
    if not relative or ".." in relative.split("/"):
        return JSONResponse({"error": "invalid report path"}, status_code=400)

    file_path = (output_dir() / relative).resolve()
    try:
        file_path.relative_to(output_dir())
    except ValueError:
        return JSONResponse({"error": "invalid report path"}, status_code=400)
    if not file_path.exists() or not file_path.is_file():
        return JSONResponse({"error": "report not found"}, status_code=404)

    media_type = mimetypes.guess_type(str(file_path))[0] or "application/pdf"
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=file_path.name,
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
    )


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        yield


starlette_app = Starlette(
    routes=[
        Route("/", root, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/reports/{path:path}", report_file, methods=["GET"]),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan,
)

app = CORSMiddleware(
    starlette_app,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a minimal Render-ready Streamable HTTP MCP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or [])
    output_dir().mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
