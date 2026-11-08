"""Local-only review UI; an explicit approval is the sole path to CRM writes."""

from __future__ import annotations

import html
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .actions import apply_action
from .api_client import CRMClient, load_local_env
from .store import ReviewStore


def _format_json(value: object) -> str:
    """Escape evidence and action JSON before embedding it in HTML."""
    return html.escape(json.dumps(value, indent=2, ensure_ascii=False))


def _page(
    store: ReviewStore,
    csrf_token: str,
    message: str = "",
) -> str:
    """Render a review queue with evidence, proposed payloads, and decision history."""
    pending = store.list_proposals(("pending",))
    # Keep finalized outcomes visible so the reviewer can verify what happened.
    history = store.list_proposals(("applied", "rejected", "failed", "approved", "superseded"))
    latest_run = store.latest_run()
    summary_html = ""
    if latest_run:
        # Before the first refresh, the page remains usable with no summary block.
        summary = latest_run["summary"]
        summary_html = (
            f"<p>Last refresh: {html.escape(latest_run['created_at'])}. "
            f"Website locations: {summary.get('community_count', 0)}; "
            f"CRM accounts: {summary.get('crm_account_count', 0)}; "
            f"proposals generated: {summary.get('proposal_count', 0)}.</p>"
        )
    pending_html = []
    for proposal in pending:
        # Escape all stored evidence before inserting it into HTML.
        evidence = "".join(
            f"<li><strong>{html.escape(str(item.get('type', 'Evidence')).replace('_', ' ').title())}</strong>"
            f"<pre>{_format_json(item)}</pre></li>"
            for item in proposal["evidence"]
        )
        action_html = _format_json(proposal["action"])
        website_url = proposal.get("community_url")
        source = (
            f'<p><a href="{html.escape(website_url, quote=True)}" target="_blank" rel="noopener">View website source</a></p>'
            if website_url
            else ""
        )
        # CRM-only proposals intentionally have no website link.
        proposal_id = html.escape(proposal["proposal_id"], quote=True)
        pending_html.append(
            f"""
            <article class="proposal">
              <p class="category">{html.escape(proposal["category"].replace("_", " ").title())}</p>
              <h2>{html.escape(proposal["title"])}</h2>
              {source}
              <details><summary>Evidence</summary><ul>{evidence}</ul></details>
              <details><summary>Proposed API change</summary><pre>{action_html}</pre></details>
              <form method="post" action="/review/{proposal_id}" onsubmit="return confirm('This action will be recorded. Approving may write to the CRM. Continue?')">
                <input type="hidden" name="csrf" value="{html.escape(csrf_token, quote=True)}">
                <button class="approve" name="decision" value="approved">Approve and apply</button>
                <button class="reject" name="decision" value="rejected">Reject</button>
              </form>
            </article>
            """
        )
    if not pending_html:
        # Show a useful empty state rather than leaving the reviewer unsure.
        pending_html.append('<p class="empty">The review queue is empty. Refresh the pipeline to check for new changes.</p>')

    history_html = "".join(
        f"<tr><td>{html.escape(item['title'])}</td><td>{html.escape(item['status'])}</td>"
        f"<td>{html.escape(item.get('error') or (json.dumps(item.get('result')) if item.get('result') else ''))}</td></tr>"
        for item in history
    )
    status_message = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bellhaven CRM Review</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#242820;background:#faf9f5}}
h1{{color:#245b4d}}h2{{margin:.25rem 0 1rem}}.proposal{{background:white;border:1px solid #d6d3c7;border-radius:8px;padding:1.2rem;margin:1rem 0}}
.category{{text-transform:uppercase;letter-spacing:.08em;font-size:.8rem;color:#6b5b23}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f1eb;padding:.8rem;border-radius:4px}}
details{{margin:.8rem 0}}button{{padding:.65rem 1rem;border:0;border-radius:4px;color:white;font-weight:600;cursor:pointer;margin-right:.5rem}}
.approve{{background:#276b4f}}.reject{{background:#8c3d32}}.notice{{padding:1rem;background:#fff2cc}}.empty{{padding:1rem;background:#eef5ef}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;border-bottom:1px solid #ddd;padding:.6rem}}a{{color:#245b4d}}
</style></head>
<body><h1>Bellhaven CRM Review Queue</h1>
<p>Local reviewer interface. CRM updates happen only after you click <strong>Approve and apply</strong>.</p>
{summary_html}{status_message}<h2>Pending review ({len(pending)})</h2>
{"".join(pending_html)}
<h2>Decision history</h2><table><thead><tr><th>Proposal</th><th>Status</th><th>Result / error</th></tr></thead>
<tbody>{history_html or '<tr><td colspan="3">No decisions yet.</td></tr>'}</tbody></table>
</body></html>"""


def create_review_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    database_path: str = "data/review.sqlite3",
    *,
    client: CRMClient | None = None,
) -> ThreadingHTTPServer:
    """Construct the local review server, allowing a fake client in integration tests.

    The app intentionally refuses non-local bind addresses; it has no user-auth
    layer and is designed for the assessment on the candidate's own machine. This
    factory separates server construction from serving so tests can exercise real
    HTTP requests without opening the real CRM or blocking a test process.
    """
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The review app must bind to localhost only.")
    load_local_env()
    crm_client = client or CRMClient()
    store = ReviewStore(database_path)
    # A per-process token helps prevent cross-site form submissions against the local app.
    csrf_token = secrets.token_urlsafe(32)

    class ReviewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Render the queue; GET requests never make CRM changes."""
            parsed = urlparse(self.path)
            if parsed.path != "/":
                # Restrict the local server to the single review page.
                self.send_error(404)
                return
            message = parse_qs(parsed.query).get("message", [""])[0]
            body = _page(store, csrf_token, message).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            """Validate a reviewer decision and, for approval only, execute its action."""
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "review":
                # Reject unexpected write routes instead of dispatching arbitrary actions.
                self.send_error(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 10_000:
                    # Cap the form body to avoid accepting empty or oversized submissions.
                    raise ValueError("Invalid review form submission.")
                form = parse_qs(self.rfile.read(content_length).decode("utf-8"))
                if not secrets.compare_digest(form.get("csrf", [""])[0], csrf_token):
                    # Constant-time comparison avoids leaking how much of a token matched.
                    raise PermissionError("Invalid review form token. Reload the page and try again.")
                decision = form.get("decision", [""])[0]
                proposal_id = parts[1]
                if decision == "rejected":
                    # Rejection is persisted locally and deliberately has no API call.
                    store.decide(proposal_id, "rejected")
                    message = "Proposal rejected. No CRM changes were made."
                elif decision == "approved":
                    # Persist approval before the request so an interrupted write is not replayed.
                    proposal = store.decide(proposal_id, "approved")
                    try:
                        result = apply_action(crm_client, proposal["action"])
                        store.record_result(proposal_id, result)
                    except Exception as error:
                        store.record_error(proposal_id, str(error))
                        raise
                    message = "Approved proposal applied successfully."
                else:
                    # Only the two rendered form decisions are valid transitions.
                    raise ValueError("Choose approve or reject.")
                self.send_response(303)
                self.send_header("Location", f"/?message={quote(message)}")
                self.end_headers()
            except Exception as error:
                # Report validation, persistence, and CRM errors in the UI. Do not
                # convert a failed approval into a success-shaped response.
                message = f"Review failed: {error}"
                self.send_response(303)
                self.send_header(
                    "Location",
                    f"/?message={quote(message)}",
                )
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            print(f"[review-app] {format % args}")

    server = ThreadingHTTPServer((host, port), ReviewHandler)
    return server


def serve(host: str = "127.0.0.1", port: int = 8000, database_path: str = "data/review.sqlite3") -> None:
    """Serve the review app on loopback until interrupted by the operator."""
    server = create_review_server(host, port, database_path)
    print(f"Review app listening at http://{host}:{port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    finally:
        # Close the listening socket on Ctrl-C or server failure.
        server.server_close()
