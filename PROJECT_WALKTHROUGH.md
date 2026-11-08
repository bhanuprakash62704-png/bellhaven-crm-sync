# Bellhaven CRM Sync: Project and Code Walkthrough

This is a beginner-friendly guide for understanding, running, debugging, testing,
and presenting the assessment project. It follows the real program from its
command-line entry point through scraping, matching, review, and (only after
approval) a CRM API write.

> **Safety:** The CRM token belongs in your ignored local `.env` file only. Do
> not paste it into this document, a screenshot, a demo recording, source code,
> or a GitHub commit. The tests use fake clients and do not write to the live
> assessment CRM.

## 1. What the assignment asks for

The assessment is a small data-reconciliation project, not a timed quiz. The
system must:

1. Read the operator's public website and collect every community.
2. Read CRM accounts and compare names, locations, ownership, billing facts,
   and duplicates.
3. Show proposed changes and their evidence to a person.
4. Make a CRM change only after a person explicitly approves that proposal.
5. Remember decisions so a daily rerun does not put already-decided work back
   into the pending queue.
6. Follow the billing team's change-of-ownership (CHOW) rule.

### Plain-language glossary

| Term | Simple meaning in this project |
|---|---|
| Community | A location published on Bellhaven's website. |
| CRM account | A company or facility record in the sandbox CRM. |
| Parent account | The company account that owns a facility; linked by `parent_id`. |
| Proposal | A suggested action. It is not yet a CRM change. |
| Evidence | Website/CRM values and match details that support a proposal. |
| Review queue | Pending proposals displayed by the local web app. |
| CHOW | Change of ownership: preserve the old account and link it to a new one when both billing conditions are positive. |
| Idempotent rerun | Re-running refresh does not create another copy of a proposal or undo a person's decision. |

## 2. Quick map of the repository

| Path | Responsibility |
|---|---|
| [`src/bellhaven_crm_sync/cli.py`](./src/bellhaven_crm_sync/cli.py#L15) | Defines commands and starts the chosen workflow. |
| [`src/bellhaven_crm_sync/scraper.py`](./src/bellhaven_crm_sync/scraper.py#L19) | Fetches and parses the homepage, directory pages, and community pages. |
| [`src/bellhaven_crm_sync/api_client.py`](./src/bellhaven_crm_sync/api_client.py#L16) | Loads local configuration and sends authenticated CRM requests. |
| [`src/bellhaven_crm_sync/models.py`](./src/bellhaven_crm_sync/models.py#L11) | Defines the normalized data objects shared by the app. |
| [`src/bellhaven_crm_sync/matching.py`](./src/bellhaven_crm_sync/matching.py#L15) | Scores matches and builds review proposals without writing to the CRM. |
| [`src/bellhaven_crm_sync/pipeline.py`](./src/bellhaven_crm_sync/pipeline.py#L16) | Orchestrates scrape + CRM reads + matching + local persistence. |
| [`src/bellhaven_crm_sync/store.py`](./src/bellhaven_crm_sync/store.py#L13) | Stores proposals, decisions, and run summaries in SQLite. |
| [`src/bellhaven_crm_sync/webapp.py`](./src/bellhaven_crm_sync/webapp.py#L21) | Displays proposals and processes reviewer decisions. |
| [`src/bellhaven_crm_sync/actions.py`](./src/bellhaven_crm_sync/actions.py#L23) | Applies an approved action and rechecks important safety conditions. |
| [`src/bellhaven_crm_sync/http_utils.py`](./src/bellhaven_crm_sync/http_utils.py#L1) | Configures verified HTTPS for website and CRM connections. |
| [`tests/`](./tests) | Offline tests for parsing, matching, persistence, approval, and failure cases. |
| [`cron.example`](./cron.example) | Example daily schedule; it runs refresh only, not approvals. |
| [`pyproject.toml`](./pyproject.toml) | Package metadata, Python requirement, dependency, and executable entry point. |
| [`README.md`](./README.md) | Setup and short run instructions. |

The app uses Python's standard library for HTTP, HTML parsing, SQLite, and the
local web server. The external runtime dependency is `certifi`, which supplies
a maintained certificate-authority bundle for HTTPS.

## 3. The end-to-end picture

```text
Command typed in terminal
       |
       v
cli.main() -- chooses scrape / refresh / serve / run
       |
       +-- scrape: website pages --> Community objects --> local JSON --> exit
       |
       +-- refresh: website pages + CRM GETs
       |                --> normalized objects
       |                --> matching/proposals
       |                --> SQLite review queue + JSON snapshot --> exit
       |
       +-- serve: local browser GET --> HTML review page (no CRM write)
       |                |
       |                +-- Reject POST --> save rejection locally (no CRM write)
       |                |
       |                +-- Approve POST --> recheck safety --> CRM API write
       |                                           --> save result or failure
       |
       +-- run: refresh first, then serve as above
```

The important design boundary is between **proposing** and **applying**. The
refresh pipeline has no write call. CRM write methods are reached only from the
approved POST route in the review app.

## 4. Start here: setup and commands

Use the project's virtual environment so the Python and installed packages are
the ones belonging to this project:

```bash
cd /path/to/bellhaven-crm-sync
source .venv/bin/activate
python --version
python -m bellhaven_crm_sync.cli --help
```

Configure `.env` locally with `BELLHAVEN_API_TOKEN` and, if needed,
`BELLHAVEN_API_BASE`. `.env.example` shows the variable names but has no token.
The actual `.env` is ignored by Git.

### Commands and what each one does

```bash
# Fetch website data only; save a local JSON file. No CRM access.
python -m bellhaven_crm_sync.cli scrape

# Fetch website + CRM data, make/update local proposals, and exit. No CRM writes.
python -m bellhaven_crm_sync.cli refresh

# Open the reviewer UI at http://127.0.0.1:8000.
python -m bellhaven_crm_sync.cli serve

# Run refresh, then open the reviewer UI.
python -m bellhaven_crm_sync.cli run

# Run the offline regression tests.
python -m unittest discover -s tests -v
```

The default SQLite file is `data/review.sqlite3`; the default website snapshot
is written next to it as `data/latest_communities.json`. The `scrape` command
defaults to `data/communities.json`.

## 5. Entry point: how Python starts the program

The package executable is also registered in [`pyproject.toml`](./pyproject.toml#L20):
`bellhaven-crm-sync` points to `bellhaven_crm_sync.cli:main`. The same function
is reached by `python -m bellhaven_crm_sync.cli`.

The real command routing is in [`cli.py`](./src/bellhaven_crm_sync/cli.py#L43):

```python
def main() -> None:
    parser = build_parser()         # Define the accepted subcommands and flags.
    args = parser.parse_args()      # Read what the user typed in the terminal.

    if args.command == "scrape":    # Public-website-only path.
        communities = scrape_communities()
        # Serialize those records to a local JSON file.
        ...
        return                      # Return from main; Python then exits normally.

    if args.command == "refresh":   # Read-only CRM reconciliation path.
        print(json.dumps(refresh_review_queue(args.db), indent=2))
        return                      # No server stays running for refresh.

    if args.command == "run":
        print(json.dumps(refresh_review_queue(args.db), indent=2))
        # Deliberately continue into the same server branch as "serve".

    if args.command in {"serve", "run"}:
        load_local_env()
        serve(host=args.host, port=args.port, database_path=args.db)
```

**How it ends:** `scrape` and `refresh` return from `main()` and the Python
process exits. `serve` blocks inside the HTTP server until interrupted, usually
with Ctrl+C; `serve()` closes the server socket in a `finally` block. `run`
finishes its refresh first and then behaves like `serve`. An uncaught network,
validation, or programming error stops the command with an error instead of
pretending it succeeded. Invalid command-line arguments are handled by
`argparse` (normally exit status 2); successful help output exits normally.

The parser requires a subcommand, which helps avoid a blank command being
mistaken for a successful run. `serve` and `run` restrict the bind address to
`127.0.0.1` or `localhost`; they are not intended as public hosted services.

## 6. Website scraper: how public records become Python data

The scraper has two jobs:

1. Discover detail-page URLs from the homepage and all numbered directory
   pages, including featured homepage items.
2. Parse each detail page into one `Community` object.

[`scraper.py`](./src/bellhaven_crm_sync/scraper.py#L19) fetches a page over
verified HTTPS. The HTML parsers in [`scraper.py`](./src/bellhaven_crm_sync/scraper.py#L33)
and [`scraper.py`](./src/bellhaven_crm_sync/scraper.py#L74) collect links,
labels, address text, and care badges. The detail parser validates the title
and address instead of guessing when a page is incomplete:
[`parse_community_detail`](./src/bellhaven_crm_sync/scraper.py#L135).
[`scrape_communities`](./src/bellhaven_crm_sync/scraper.py#L161) follows
forward pagination, deduplicates URLs, and fails if the directory unexpectedly
returns no community links.

An example normalized object looks like this (illustrative data):

```json
{
  "url": "https://example.test/communities/maplewood",
  "name": "Bellhaven of Maplewood",
  "address": "210 Orchard Lane",
  "city": "Maplewood",
  "state": "OH",
  "postal_code": "44280",
  "care_offerings": ["Assisted Living", "Memory Support"]
}
```

The internal type is [`Community`](./src/bellhaven_crm_sync/models.py#L11).
Postal codes stay strings so leading zeroes and ZIP+4 values are preserved.
The object uses a tuple for care offerings internally; `to_dict()` converts
that tuple into a JSON array when writing a snapshot.

## 7. CRM client: configuration, reads, and writes

[`load_local_env`](./src/bellhaven_crm_sync/api_client.py#L16) reads simple
`KEY=VALUE` lines from `.env` without overwriting environment variables already
set by the shell. [`CRMClient`](./src/bellhaven_crm_sync/api_client.py#L61)
requires a token before making a request.

All HTTP details are centralized in
[`CRMClient._request`](./src/bellhaven_crm_sync/api_client.py#L83):

```python
headers = {
    "Accept": "application/json",                     # We expect JSON responses.
    "Authorization": f"Bearer {self.token}",          # Send the secret from config.
    "User-Agent": "BellhavenCRMReview/1.0 (assessment project)",
}
if data is not None:
    headers["Content-Type"] = "application/json"      # Only write requests have a body.
```

The request method encodes JSON, uses verified TLS, and distinguishes HTTP
errors, connection errors, and invalid JSON responses. It never prints the
token. The API methods build on it:

- [`get_identity`](./src/bellhaven_crm_sync/api_client.py#L169) checks the token
  using `/me` before refresh reads account data.
- [`get_all_accounts`](./src/bellhaven_crm_sync/api_client.py#L134) reads every
  page, rejects invalid page sizes, repeated pages, and excessive pagination.
- [`get_account`](./src/bellhaven_crm_sync/api_client.py#L176) fetches the latest
  state immediately before an approved change.
- [`create_account`](./src/bellhaven_crm_sync/api_client.py#L186) and
  [`update_account`](./src/bellhaven_crm_sync/api_client.py#L194) are write
  primitives. Their presence does not mean refresh uses them; only the approved
  action path invokes them.

`http_utils.py` creates the TLS context with `certifi`. Certificate checking is
not disabled as a shortcut if an HTTPS connection fails.

## 8. Normalized models: common shapes for messy inputs

The CRM may return aliases for the same field, such as `id` / `account_id`, or
`billing_street` / `street`. [`CRMAccount.from_mapping`](./src/bellhaven_crm_sync/models.py#L59)
chooses the first populated supported name and keeps a copy of the original
record in `raw` for evidence.

It also normalizes care offerings to a tuple and billing values to finite
numbers. Missing amounts mean zero; malformed, NaN, or infinite billing values
raise an error rather than silently making a possibly unsafe account appear
eligible for direct re-parenting. This is a fail-closed choice.

[`Proposal`](./src/bellhaven_crm_sync/models.py#L131) is the central review
record: a stable ID, category/title, optional CRM account ID, optional website
URL, action payload, and evidence. Proposals describe intended work; they do
not execute it.

## 9. Matching: how the code decides what to propose

The matcher first normalizes text. [`normalize`](./src/bellhaven_crm_sync/matching.py#L15)
removes punctuation and case differences; `normalize_address()` also standardizes
common street suffixes and compass directions. Candidate scoring is in
[`_candidate_score`](./src/bellhaven_crm_sync/matching.py#L59), with readable
reasons retained for the reviewer.

[`find_parent_account`](./src/bellhaven_crm_sync/matching.py#L162) requires
exactly one top-level Bellhaven parent. If there are zero or multiple possible
parents, matching stops rather than guessing which company to link to.

[`build_proposals`](./src/bellhaven_crm_sync/matching.py#L179) does the main
comparison. In simple terms:

1. Normalize the CRM accounts and validate that every account has an ID.
2. Score each website community against candidate CRM facilities.
3. Give strong exact-address and matching-location evidence more weight than a
   similar name by itself.
4. Put plausible but uncertain candidates into an `ambiguous_match` proposal.
5. For a clear match, compare fields and parent ownership.
6. Look for same-location Bellhaven duplicates with one clear website-matched
   survivor.
7. Propose review of Bellhaven child accounts not represented on the website.
8. Return proposals plus a run summary; make no API changes.

Categories a reviewer may see:

| Category | Meaning |
|---|---|
| `confident_match` | Good match and no material discrepancy; recorded in the summary, so no action is needed. |
| `field_correction` | Matched account has differing name, address, locality, or status. |
| `wrong_parent` | Matched account is under a different parent and direct re-parenting is permitted by the billing rule. |
| `billing_chow` | Both billing values are positive; preserve the old account and create/link a replacement. |
| `missing_crm_account` | Website community has no sufficiently plausible CRM match; propose creating an account. |
| `ambiguous_match` | Candidate is plausible but not decisive; ask a human to review. |
| `duplicate_account` | Same-location duplicate with exactly one website-matched survivor; propose linking/inactivating the losing copy. |
| `not_on_website` | Bellhaven child account did not match a current website community; mark `Needs Review`, not deleted. |

The score and candidate-selection rules are intentionally conservative. They
are a decision aid, not a claim that fuzzy matching proves ownership. Every
write remains a reviewer decision.

### Billing rule in the proposal generator

The assignment's billing rule is **revenue greater than zero AND outstanding AR
greater than zero**. When both are true, the matcher creates a `billing_chow`
action with a new-account payload and a link patch for the old account; it does
not put a new `parent_id` on the old account. If either is zero, a parent change
can be proposed as a direct update.

### Stable IDs and safe daily reruns

The matcher hashes the meaningful identity of each proposal (category, account,
community URL, and action). Evidence is excluded, so refreshed evidence updates
the existing pending item rather than creating a duplicate. The SQLite upsert
preserves rejected, approved, applied, and failed decisions.

## 10. Refresh pipeline: from snapshots to the pending queue

The orchestration function is
[`refresh_review_queue`](./src/bellhaven_crm_sync/pipeline.py#L16). It:

1. Loads local environment settings.
2. Uses the provided CRM client or constructs the real authenticated client.
3. Scrapes the live website unless test fixtures were injected.
4. Validates the token, then fetches all CRM accounts.
5. Calls `build_proposals()`.
6. Saves proposal and run data to SQLite.
7. Saves `latest_communities.json` beside the database.
8. Prints/returns summary data to the CLI.

There is deliberately no `create_account()` or `update_account()` call in this
function. If auth, scraping, parent identification, or validation fails, it
raises before replacing the review queue with incomplete results.

The heart of the refresh can be summarized like this (see
[`refresh_review_queue()`](./src/bellhaven_crm_sync/pipeline.py#L16)):

```python
crm_client.get_identity()                       # Stop early if the token is invalid.
crm_accounts = crm_client.get_all_accounts()     # Read every CRM account page.
proposals, summary = build_proposals(             # Compare snapshots; this is proposal-only.
    website_communities,
    crm_accounts,
)
inserted_count = ReviewStore(database_path).add_proposals(proposals, summary)
# Persist evidence and decisions locally; this is not a CRM API write.
```

In the debugger, step into `build_proposals()` to see matching, and step into
`add_proposals()` to see how stable IDs and prior reviewer decisions are
preserved.

An illustrative output shape (sample IDs/counts only; actual results vary):

```json
{
  "community_count": 2,
  "crm_account_count": 3,
  "parent_account_id": "parent-1",
  "proposal_count": 1,
  "classifications": [
    {
      "community": "Bellhaven of Maplewood",
      "classification": "confident_match",
      "account_id": "facility-1",
      "score": 108
    },
    {
      "community": "Bellhaven Meadows of Findlay",
      "classification": "missing_crm_account",
      "account_id": null,
      "score": 0
    }
  ],
  "new_proposals_added": 1,
  "already_seen_or_decided": 0
}
```

The counts can differ on later runs. `new_proposals_added` counts new or
re-opened/superseded items that became pending; `already_seen_or_decided`
counts findings that already had a current or finalized ID.

## 11. SQLite review store: proposal lifecycle and history

[`ReviewStore`](./src/bellhaven_crm_sync/store.py#L13) creates two tables:

- `proposals`: action/evidence JSON, state, decision time, success result or
  error, and timestamps.
- `pipeline_runs`: JSON summary for each successful refresh.

The proposal state lifecycle is:

```text
new finding --> pending --> rejected
                       \--> approved --> applied
                                    \--> failed

pending finding that disappears from a later successful refresh --> superseded
superseded finding that returns later --> pending again
```

[`add_proposals`](./src/bellhaven_crm_sync/store.py#L55) refreshes evidence on
pending items, marks disappeared pending items `superseded`, and never deletes
history. [`decide`](./src/bellhaven_crm_sync/store.py#L154) allows only
`approved` or `rejected` and only from `pending`, which prevents two stale browser
tabs from deciding the same proposal twice. [`record_result`](./src/bellhaven_crm_sync/store.py#L186)
marks a successful action `applied`; [`record_error`](./src/bellhaven_crm_sync/store.py#L201)
stores failure information as `failed`.

An approved write is not blindly retried when it errors. In particular, if a
CHOW replacement was created but the old account could not be linked, the
operator must reconcile the two IDs manually before taking further action.

## 12. Review app: the human approval gate

The review page is rendered by `_page()` in
[`webapp.py`](./src/bellhaven_crm_sync/webapp.py#L21). It displays pending items,
evidence, source links, proposed API payloads, and decision history. Dynamic
values are HTML-escaped before rendering.

[`create_review_server`](./src/bellhaven_crm_sync/webapp.py#L104) builds a
localhost-only server. It also creates a per-process CSRF token. The routes:

- `GET /`: show the review page. A GET never calls the CRM.
- `POST /review/<proposal-id>` with `decision=rejected`: persist rejection;
  no CRM call.
- `POST /review/<proposal-id>` with `decision=approved`: record approval first,
  execute the approved action, then record `applied` or `failed`.
- Other routes or invalid decisions are rejected.

The submission validates the CSRF token, checks the request body size, and
returns a message in the UI when an operation fails. The server is intentionally
local-only and is not a multi-user production web service.

## 13. Applying actions: exact CRM write behavior

[`apply_action`](./src/bellhaven_crm_sync/actions.py#L23) is the only layer
that dispatches approved proposal action types:

| Action kind | What happens after approval |
|---|---|
| `create_account` | POST the reviewed payload; require the response to include the new ID. |
| `update_account` | Re-fetch the account; check its parent is unchanged; re-check the billing guard for a parent move; then PATCH. |
| `set_status` | Re-fetch and verify parent; PATCH the reviewed status and optional note. |
| `billing_chow` | Re-fetch the old account and verify billing + parent; POST a new account; PATCH `chow_current_account` on the preserved old account to the new ID. |

The billing guard is checked twice: when the proposal is generated and again
right before the write. This protects against CRM data changing while a proposal
waits in the queue. If the CHOW create succeeds but its follow-up link fails,
the code surfaces both account IDs and says to reconcile manually; the API does
not provide a transaction that could safely roll back the created account.

The critical CHOW sequence in
[`apply_action()`](./src/bellhaven_crm_sync/actions.py#L23) is:

```python
old = CRMAccount.from_mapping(client.get_account(old_id))   # Read current billing/parent.
if old.lifetime_revenue <= 0 or old.outstanding_ar <= 0:   # Both must still be positive.
    raise RuntimeError("Billing condition changed; refresh and review again.")
created = client.create_account(create_payload)             # Create replacement first.
new_id = _account_id(created)                                # Require an ID for the link.
client.update_account(old_id, {"chow_current_account": new_id})  # Preserve old account.
```

This order matters: the old account is never directly re-parented by the CHOW
action, and the final link cannot be made until the new account ID exists.

The API has no merge or delete operation in this solution. Duplicate handling
is a proposal to set `duplicate_of_account`, mark the losing copy `Inactive`,
and explain the reason in a note.

## 14. Running and debugging step by step

### Beginner-friendly way: VS Code breakpoints

1. Open the repository folder in VS Code.
2. Select the project's `.venv` Python interpreter.
3. Open `src/bellhaven_crm_sync/cli.py`.
4. Click left of a line number to add a breakpoint. For the refresh route, use
   `main()` at [`cli.py`](./src/bellhaven_crm_sync/cli.py#L43); for the data
   pipeline, use [`pipeline.py`](./src/bellhaven_crm_sync/pipeline.py#L16);
   for matching, use [`matching.py`](./src/bellhaven_crm_sync/matching.py#L179).
5. Open **Run and Debug**, choose a Python debugger configuration to launch the
   current module, and use module `bellhaven_crm_sync.cli` with argument
   `refresh`. Set the working directory to the project root.
6. The `.env` loader reads local settings when the command starts; do not put
   the token in the debug configuration or share screenshots of it.
7. Press **Continue** to advance. Inspect the Variables panel for `args`,
   `website_communities`, `crm_accounts`, `proposals`, and `summary` as execution
   reaches those scopes. Step Over runs the current line; Step Into enters the
   called function; Step Out finishes the current function.
8. Stop at a breakpoint before any reviewer approval if you are only learning
   the code. A normal `refresh` execution does not write CRM records.

### Terminal debugger option

With the virtual environment active and the repository root as the working
directory, Python's built-in debugger can launch the module:

```bash
PYTHONPATH=src python -m pdb -m bellhaven_crm_sync.cli refresh
```

At the `(Pdb)` prompt, useful commands include `n` (next line), `s` (step into
function), `r` (run until the current function returns), `p variable_name`
(print a value), `where` (show the call stack), `c` (continue), and `q` (quit).
`refresh` still needs valid local API configuration because it performs real
read-only website/CRM requests. For safe offline debugging, start from a test
or use the fake clients already used by the tests.

### Trace one request in the review app

1. Start `python -m bellhaven_crm_sync.cli serve`.
2. Open `http://127.0.0.1:8000`; the browser requests `GET /`.
3. `do_GET()` loads pending proposals from SQLite and renders the page.
4. Click **Reject** to see a local state transition with no CRM request.
5. For an approval demo, pick a proposal only after verifying its evidence and
   exact payload. The browser sends the CSRF token and decision to
   `do_POST()`.
6. The store records `approved`; `apply_action()` re-reads required CRM state
   and only then calls the API.
7. A successful response becomes `applied`; an exception becomes `failed` and
   appears in decision history. Refresh the CRM browser separately to verify
   an approved change landed.
8. Ctrl+C stops the local server and closes its listening socket.

The integration tests exercise these real HTTP routes against a fake CRM, so
they validate the browser-to-store-to-action path without modifying the live
sandbox.

## 15. Test suite: what is covered

Run all tests from the project root:

```bash
python -m unittest discover -s tests -v
```

The current suite contains **97 tests**. It uses standard-library
`unittest`, mocks, fixtures, temporary directories/SQLite files, and a
localhost test server. It does not require the real API token and does not
perform live CRM writes.

| Test file | Areas and representative cases |
|---|---|
| [`test_api_client.py`](./tests/test_api_client.py#L18) | Pagination and page bounds; wrapped response shapes; token/env behavior; JSON request/response; HTTP, transport, and malformed JSON errors; account endpoint responses. |
| [`test_scraper.py`](./tests/test_scraper.py#L24) | HTML entities; community and pagination links; detail fields; ZIP+4; duplicate badges; malformed/missing values; empty directory; fetch failure. |
| [`test_models.py`](./tests/test_models.py#L8) | Canonical/alias field precedence; defaults; care labels; finite billing parsing; serialization. |
| [`test_matching.py`](./tests/test_matching.py#L53) | Correct parent; exact and fuzzy match evidence; ambiguous and cross-state cases; missing/stale records; duplicate logic; status; direct parent move vs CHOW; stable IDs. |
| [`test_pipeline.py`](./tests/test_pipeline.py#L31) | Read-only refresh; stable second run; auth failure leaves the queue intact; ambiguous parent fails before snapshot write. |
| [`test_review.py`](./tests/test_review.py#L37) | CHOW order and billing guard; SQLite transitions; repeated decisions; superseded and failed proposals; review-page evidence and HTML escaping. |
| [`test_actions.py`](./tests/test_actions.py#L36) | Each action kind; stale parent; billing re-checks; missing create ID; partial CHOW failure; unsupported action. |
| [`test_cli.py`](./tests/test_cli.py#L16) | Required command; scrape JSON; refresh/serve/run routing without making network calls. |
| [`test_webapp_http.py`](./tests/test_webapp_http.py#L36) | Real local GET/reject/approve requests; CSRF rejection; invalid decisions; unknown routes; localhost-only binding. |

Example successful test-run ending:

```text
Ran 97 tests in 3.6s

OK
```

Exact runtime varies by computer. `compileall` and `git diff --check` are also
useful checks after code changes:

```bash
python -m compileall -q src tests
git diff --check
```

No finite test suite can prove correctness for every possible input. These
tests cover the major normal paths, boundary cases, important safety rules, and
representative failures. They are intentionally offline; they cannot prove the
assessment server is currently available or that every live CRM field is
accepted by its undocumented write schema.

### What the important exception blocks do

| Where | Why the code catches or raises |
|---|---|
| [`scraper.fetch_page()`](./src/bellhaven_crm_sync/scraper.py#L19) | Converts a URL/timeout error into an error that names the page; it does not disable TLS or pretend the page was empty. |
| [`CRMClient._request()`](./src/bellhaven_crm_sync/api_client.py#L83) | Separates HTTP responses, connectivity problems, and invalid JSON so operators can diagnose the failure. |
| [`ReviewHandler.do_POST()`](./src/bellhaven_crm_sync/webapp.py#L142) | Shows a validation or API error back in the local UI; failed approval is not reported as success. |
| CHOW linking in [`apply_action()`](./src/bellhaven_crm_sync/actions.py#L23) | If the new account was created but the old-account link fails, reports both IDs for manual reconciliation; no automatic retry can safely infer whether the POST succeeded. |
| [`serve()`](./src/bellhaven_crm_sync/webapp.py#L199) | Uses `finally` to close the listening socket if the operator interrupts the server or serving raises. |

## 16. Daily scheduling

[`cron.example`](./cron.example#L1) shows a daily `refresh` command. It only
scrapes, reads CRM data, and updates the local proposal queue. It does not
approve proposals or write CRM records. The reviewer app remains a separate
process that a person starts when reviewing.

The example path must be changed to the real project directory on the computer
running cron. Scheduled jobs also need access to the `.env` file and the same
SQLite database, with suitable file permissions and a reliable machine/network.
Do not schedule unattended approvals.

## 17. What to demonstrate in the interview

The interviewer is looking for a working system and how you reason about
production risks. A simple walkthrough:

1. Explain the problem: website locations are the source to compare; CRM account
   ownership is what must be kept accurate.
2. Show the terminal commands and explain that `refresh` proposes only.
3. Show a confident match and a discrepancy. Expand evidence and show the
   action payload before making a decision.
4. Reject one proposal and explain that rejection is persisted locally and
   makes no CRM call.
5. Approve one safe proposal; explain the billing guard if ownership changes.
6. Show its `applied` result in the queue and verify the corresponding account
   in the read-only CRM browser.
7. Explain rerun safety: stable proposal IDs and preserved decisions prevent
   already-decided work from reappearing as new pending work.
8. Show the tests and explain that they use fake CRM clients, not the live
   sandbox.
9. Be candid about what is not automated: ambiguous matches remain for review;
   failed writes need operator reconciliation; the API schema should be verified.

Possible short explanation to say aloud:

> “I scrape the public locations, fetch the CRM records, normalize the fields,
> and score candidate matches. The pipeline only writes evidence-backed
> proposals to a local SQLite queue. A reviewer can inspect the website evidence
> and exact API payload, then approve or reject. Before an approved ownership
> change, the action layer re-fetches the account and checks the billing rule
> again. A daily refresh updates pending evidence without reopening finalized
> decisions.”

## 18. Known limits and honest caveats

- Matching is heuristic. A score supports a decision but does not prove legal
  ownership; ambiguous matches are for a human.
- Care offerings are scraped, shown as website evidence, and included when
  proposing a new account. Existing-account matching does **not** currently
  compare or update the CRM `care_type` field.
- The CRM API does not publish full write schemas. Confirm accepted fields and
  verify changes in the sandbox during the assessment.
- The local review app has no user login and is deliberately loopback-only.
  Do not expose it on a public network.
- A CRM may accept a write and then the connection may fail before the client
  receives the response. The app marks that attempt failed and avoids an
  automatic retry; an operator must check CRM state before proceeding.
- The app does not merge or delete records. Duplicate proposals link and
  inactivate the losing record as the assessment API allows.
- The test suite is broad, not mathematically exhaustive. Live integration
  behavior must still be checked in the isolated assessment CRM.

## 19. Final checklist before submission

- [ ] `.env` has a valid token locally and is not staged or committed.
- [ ] `scrape` and `refresh` run successfully.
- [ ] Review each proposal's source evidence, target account, payload, and
      billing implications.
- [ ] Approve only changes you believe are correct; reject incorrect ones.
- [ ] Verify approved CRM changes in the CRM browser.
- [ ] Run the test suite and keep its output available for the walkthrough.
- [ ] Record the requested short demo, set link sharing appropriately, and add
      the link to the assessment form.
- [ ] Explain honestly which decisions were human-reviewed and any unresolved
      proposals/failures.
