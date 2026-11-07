# Bellhaven CRM Review

A local, human-in-the-loop workflow for comparing Bellhaven Senior Living's public community directory with its CRM sandbox. The daily refresh only creates review proposals; it never writes to the CRM. A reviewer must approve each proposal in the local web app before an API change is attempted.

## Setup

The project uses Python 3.11+ and the `certifi` CA bundle for verified HTTPS requests.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Edit `.env` locally and add a freshly rotated assessment token:

```text
BELLHAVEN_API_TOKEN=put-your-new-token-here
BELLHAVEN_API_BASE=https://analyst-assessment-production.up.railway.app/api/v1
```

`.env` is ignored by Git. Never commit or paste the token into source code.

## Run

First, scrape the website without contacting the CRM:

```bash
python -m bellhaven_crm_sync.cli scrape
```

Then fetch the CRM accounts and create proposals:

```bash
python -m bellhaven_crm_sync.cli refresh
```

Start the reviewer at <http://127.0.0.1:8000>:

```bash
python -m bellhaven_crm_sync.cli serve
```

Or refresh once and start the app in one command:

```bash
python -m bellhaven_crm_sync.cli run
```

Review the evidence and proposed payload before choosing **Approve and apply** or **Reject**. Rejections never write to the CRM. Approved proposals are recorded before the API call; if an API call fails, the proposal is marked failed and must be reconciled manually rather than blindly retried.

## Matching and safety

- Scrapes all directory pages and each community detail page, including name, address, city, state, postal code, and care offerings.
- Scores name and location evidence. Exact full-address matches are the strongest match. Ambiguous candidates are sent to review rather than silently updated.
- Proposes new accounts, field corrections, ownership corrections, stale-account review, and possible duplicate outcomes.
- Requires a unique top-level `Bellhaven Senior Living` CRM account before generating writes.
- Verifies the configured token with the CRM's `/me` endpoint before reading account data.
- For a parent change, a positive lifetime revenue **and** positive outstanding AR cause a billing-preserving proposal: create a replacement under Bellhaven and link the old account through `chow_current_account`. The old account is re-fetched and both billing conditions are checked again at approval time.
- Other parent corrections update the existing account.
- Possible duplicates are proposed with `duplicate_of_account` and `Inactive`, and still require explicit approval.
- Persists proposals and decisions in `data/review.sqlite3`. Deterministic proposal IDs update evidence on still-pending items, supersede items that no longer apply, and never re-open rejected, approved, or applied decisions.
- The web server binds only to localhost. A CSRF token is required for review submissions.

The CRM API docs currently do not publish request-body schemas. Verify the account fields and approved write behavior against the sandbox before approving any proposal.

## Daily schedule

`cron.example` contains a local cron template. It runs only the read/scrape-and-propose refresh and reuses the same SQLite database, so review decisions persist. The web app remains a separate local process.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use local fixtures and mocks; they do not call the live CRM or write any data.
