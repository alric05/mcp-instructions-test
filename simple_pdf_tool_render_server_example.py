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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


SERVER_NAME = "simple-clarivate-pdf-tool"
SERVER_VERSION = "0.1.0"
SESSION_ID = str(uuid.uuid4())

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

LINK_RE = re.compile(r"\[([^\]]+)]\((https?://[^)\s]+)\)")


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


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def inline_markup(text: str) -> str:
    """Convert a small markdown subset to ReportLab paragraph markup."""
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
            flowables.append(Paragraph("- " + inline_markup(line[2:].strip()), styles["bullet"]))
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


def generate_clarivate_report_pdf(arguments: Dict[str, Any]) -> Dict[str, Any]:
    subject = clean_text(arguments.get("subject"))
    markdown = str(arguments.get("markdown") or "").strip()
    filename = safe_filename(str(arguments.get("filename") or "report"))

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

    return {
        "pdf_url": public_report_url(output_path),
        "download_the_report": public_report_url(output_path),
        "pdf_path": str(output_path.resolve()),
        "pdf_exists": output_path.exists(),
        "pdf_size_bytes": output_path.stat().st_size if output_path.exists() else 0,
        "template_path": str(TEMPLATE_PATH.resolve()),
    }


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
        "description": "Generate a Clarivate-template PDF from a subject, markdown report body, and filename.",
        "inputSchema": {
            "type": "object",
            "required": ["subject", "markdown", "filename"],
            "properties": {
                "subject": {"type": "string", "description": "Cover subtitle, usually the searched mark or report subject."},
                "markdown": {"type": "string", "description": "Completed markdown report body."},
                "filename": {"type": "string", "description": "Base report filename without directories. A timestamp is appended."},
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
        if self.path != "/mcp":
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
            "subject": "EXAMPLE",
            "markdown": "# Example Report\n\nThis is a simplified PDF tool test.",
            "filename": "example_report",
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
