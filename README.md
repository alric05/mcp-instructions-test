# Trademark Knockout Report MCP

This package implements the UX-focused workflow boundary from the supplied
prompt:

- ChatGPT runs the trademark knockout workflow.
- The existing Clarivate / CompuMark MCP supplies trademark and litigation data.
- ChatGPT performs optional web search directly when the user agrees.
- This local MCP server only validates the final Markdown report and renders the
  verified Clarivate-style PDF.

It deliberately does not expose a tool that performs trademark searches, runs
litigation searches, performs web research, selects conflicts, assesses risk, or
drafts the report narrative.

## Tools

- `get_workflow_contract`
  - Read-only summary of the required workflow and tool boundaries.
- `get_report_template`
  - Read-only Markdown template for the final report.
- `validate_knockout_report`
  - Checks required numbered sections, required Top 5 row counts, allowed risk
    labels, unresolved placeholders, disclaimer presence, and domain-only link
    labels.
- `render_clarivate_knockout_pdf`
  - Renders finalized Markdown to PDF, updates the Clarivate cover subtitle,
    merges cover/body/closing pages, confirms the PDF exists, and returns a
    verified file reference.
- `healthcheck`
- `version`

## Local Codex registration

From this workspace, register the stdio server:

```bash
codex mcp add trademark-knockout-report-renderer \
  --env TRADEMARK_REPORT_OUTPUT_DIR="/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions 2/reports" \
  -- python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions 2/trademark-knockout-report-mcp/server.py"
```

Install dependencies if needed:

```bash
python3 -m pip install -r "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions 2/trademark-knockout-report-mcp/requirements.txt"
```

## Smoke tests

Validate the server and tool list:

```bash
python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions 2/trademark-knockout-report-mcp/server.py" --self-test
```

Validate and render a sample PDF:

```bash
python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions 2/trademark-knockout-report-mcp/server.py" --self-test-render
```

## HTTP wrapper

For ChatGPT web testing, run the HTTP wrapper locally and expose it through a
temporary HTTPS tunnel:

```bash
TRADEMARK_REPORT_OUTPUT_DIR="/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions 2/reports" \
python3 "/Users/alric.bouantoun/Library/CloudStorage/OneDrive-ClarivateAnalytics/Documents/Data Platform/AI Platform/tests/Plugins/MCP - instructions 2/trademark-knockout-report-mcp/http_server.py" \
  --host 127.0.0.1 \
  --port 8765
```

Health check:

```bash
curl http://127.0.0.1:8765/health
```

If deployed on Render, set:

```text
TRADEMARK_REPORT_OUTPUT_DIR=/tmp/reports
PUBLIC_BASE_URL=https://YOUR-RENDER-SERVICE.onrender.com
```

With `PUBLIC_BASE_URL`, generated report links are returned as:

```text
https://YOUR-RENDER-SERVICE.onrender.com/reports/report_name.pdf
```

## Workflow reminder

The report run itself must still be handled by ChatGPT:

1. Get search criteria.
2. Conduct trademark searches, litigation searches & optional web search.
3. Analyze trademark risk.
4. Draft report.
5. Validate and generate final PDF.

The local MCP server is called only at the validation and rendering gate.
