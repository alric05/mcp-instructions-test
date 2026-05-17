# Trademark Knockout Report MCP

This is a standalone MCP-only replacement for the Codex plugin skill layer. It
does not call the CompuMark data APIs itself; instead, it gives Codex MCP tools
for the workflow, search plan, report template, validation gates, and final PDF
generation. Live trademark and litigation records still come from the existing
Clarivate CompuMark MCP connector.

## What This Server Provides

- `get_trademark_knockout_workflow`
  - Returns the full workflow and output rules that were previously stored in
    plugin skill files.
- `build_trademark_knockout_execution_plan`
  - Normalizes mark, jurisdiction, Nice class, match scope, and online-presence
    default into a concrete plan for the CompuMark MCP tools and a goal-oriented
    web-search brief.
- `get_trademark_knockout_report_template`
  - Returns the required report structure.
- `validate_trademark_knockout_report`
  - Checks required sections, Top 5 table row counts, risk labels, and link
    label warnings before PDF generation.
- `generate_clarivate_report_pdf`
  - Generates the final PDF with the bundled Clarivate template asset.

## Install In Codex

From this workspace, register the local server:

```bash
codex mcp add trademark-knockout-report-workflow \
  --env TRADEMARK_REPORT_OUTPUT_DIR="/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions/reports" \
  -- python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions/trademark-knockout-report-mcp/server.py"
```

Then start a new Codex session so the new MCP tools can be discovered.

If `reportlab` or `pypdf` are missing in a different environment:

```bash
python3 -m pip install -r "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions/trademark-knockout-report-mcp/requirements.txt"
```

## Expected Workflow In A Report Run

1. Call `get_trademark_knockout_workflow`.
2. Ask the user only for missing inputs: mark, jurisdiction/office, and Nice
   class. Do not ask about online presence unless the user raises it; online
   presence is enabled by default.
3. Call `build_trademark_knockout_execution_plan`.
4. Use the existing CompuMark MCP connector tools by purpose:
   - identical knockout trademark search;
   - custom/screening trademark search;
   - trademark content/details lookup;
   - full-text URL creation;
   - litigation/caselaw search.
5. Run the online-presence check by default using ChatGPT's or Claude's own
   browsing/web-search capability. Use the plain instruction: `What do you find
   online related to "<MARK>"? Return the 5 most relevant results.` Skip it only
   if the user explicitly opts out.
6. Draft the report with `get_trademark_knockout_report_template`.
7. Call `validate_trademark_knockout_report`.
8. Call `generate_clarivate_report_pdf`.
9. Give the user the `download_the_report`/`pdf_url` returned by
   `generate_clarivate_report_pdf`. Do not fetch, open, download, inspect, or
   review the final PDF URL.

## Test From ChatGPT Web

ChatGPT web cannot connect directly to this stdio MCP server. To test there, run
the HTTP wrapper locally and expose it through a temporary HTTPS tunnel.

Start the local HTTP MCP endpoint:

```bash
TRADEMARK_REPORT_OUTPUT_DIR="/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions/reports" \
python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions/trademark-knockout-report-mcp/http_server.py" \
  --host 127.0.0.1 \
  --port 8765
```

Verify locally:

```bash
curl http://127.0.0.1:8765/health
```

Expose it with a tunnel. Examples:

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

or:

```bash
ngrok http 8765
```

In ChatGPT web, create a custom MCP app/connector and use the HTTPS tunnel URL
with `/mcp` appended, for example:

```text
https://example-tunnel.trycloudflare.com/mcp
```

For a first smoke test, use a short-lived private tunnel and no auth. Before
sharing or publishing, put the server behind an authentication mechanism that
your ChatGPT workspace supports and review the tool actions carefully.

## Deploy On Render

Upload these files with the same relative paths:

```text
server.py
http_server.py
requirements.txt
assets/Clarivate_template.pdf
```

Use:

```text
Build command: pip install -r requirements.txt
Start command: python3 http_server.py --host 0.0.0.0 --port $PORT
```

Set environment variables:

```text
TRADEMARK_REPORT_OUTPUT_DIR=/tmp/reports
PUBLIC_BASE_URL=https://YOUR-RENDER-SERVICE.onrender.com
```

`PUBLIC_BASE_URL` is important for ChatGPT web. Without it, the PDF generator can
create `/tmp/reports/...pdf` inside the Render container, but ChatGPT only sees a
private container path. With it, the tool response includes:

```text
https://YOUR-RENDER-SERVICE.onrender.com/reports/report_name.pdf
```

and `http_server.py` serves that file back over HTTPS.

When testing in ChatGPT, the final PDF link must look like:

```text
https://YOUR-RENDER-SERVICE.onrender.com/reports/report_name.pdf
```

A link beginning with `https://chatgpt.com/c/...` is only a conversation anchor
or ChatGPT-created artifact reference. It is not proof that this MCP server
generated the Clarivate-template PDF.

When ChatGPT or Claude receives the `pdf_url`, it should return that link to the
user directly. It should not download the PDF, review the generated PDF, or
attempt to inspect the generated PDF.

## Local Smoke Test

```bash
python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions/trademark-knockout-report-mcp/server.py" --self-test
```

HTTP wrapper smoke test:

```bash
python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions/trademark-knockout-report-mcp/http_server.py" --host 127.0.0.1 --port 8765
```