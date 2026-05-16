#!/usr/bin/env python3
"""HTTP wrapper for the local trademark knockout report MCP server."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, Optional, Sequence

from server import SERVER_NAME, SERVER_VERSION, TOOLS, default_output_dir, handle_request


SESSION_ID = str(uuid.uuid4())


class MCPHttpHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_common_headers(self, status: int, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
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

    def _auth_ok(self) -> bool:
        token = os.environ.get("MCP_BEARER_TOKEN")
        if not token:
            return True
        return self.headers.get("Authorization") == f"Bearer {token}"

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

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._write_json(404, {"error": "not found"})
            return
        if not self._auth_ok():
            self._write_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            self._write_json(
                400,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error", "data": str(exc)}},
            )
            return
        response = self._handle_payload(payload)
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
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

    def _serve_report_file(self, include_body: bool) -> None:
        if not self._auth_ok():
            self._write_json(401, {"error": "unauthorized"})
            return

        relative_url_path = self.path.split("?", 1)[0][len("/reports/") :]
        if not relative_url_path or ".." in relative_url_path.split("/"):
            self._write_json(400, {"error": "invalid report path"})
            return

        output_dir = default_output_dir()
        file_path = (output_dir / relative_url_path).resolve()
        try:
            file_path.relative_to(output_dir)
        except ValueError:
            self._write_json(400, {"error": "invalid report path"})
            return
        if not file_path.exists() or not file_path.is_file():
            self._write_json(404, {"error": "report not found"})
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
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


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the report renderer MCP server over HTTP.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Use 127.0.0.1 when tunneling.")
    parser.add_argument("--port", default=8765, type=int, help="Bind port.")
    parser.add_argument("--bearer-token", help="Optional bearer token required in Authorization header.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.bearer_token:
        os.environ["MCP_BEARER_TOKEN"] = args.bearer_token
    httpd = ThreadingHTTPServer((args.host, args.port), MCPHttpHandler)
    print(f"{SERVER_NAME} HTTP MCP listening on http://{args.host}:{args.port}/mcp", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopping HTTP MCP server", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
