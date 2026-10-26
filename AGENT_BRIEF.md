# Agent brief: gmail-drafts

Setup notes from the conductor (read first):
- The files in this folder were unzipped here from ~/Downloads/gmail_drafts.zip on 2026-10-26. "This folder" in the brief below means ~/gmail-drafts/.
- The code has NEVER run against the live Gmail API. Treat all live behavior as unverified.
- This Mac only has system Python 3.9.6 (/usr/bin/python3). No Homebrew python, no uv. The tool needs 3.10+. Propose how to get a 3.10+ interpreter (e.g. installing uv or Homebrew python into a local venv) and ask Remington before installing anything system-wide.
- Remington's preferences: terse; no em-dashes in writing; end messages with "For you:" / "Need from you:" sections when applicable.

---

You're taking over a small, working-but-unproven tool and turning it into reusable infrastructure for an AI-assisted email-sending workflow. Read this whole brief, inspect the code, then start at step 1.

## Purpose
A human wants an agent (an AI assistant, or a script it drives) to prepare many personalized emails, each with the same file attached, and send them safely. Core constraints:
- The attachment's encoded bytes must never pass through the AI conversation. Local code handles the file.
- Drafts first; sending is a separate, explicit, confirmed action.
- Re-running must never duplicate drafts or sends.
- Uncertain outcomes are flagged for a human, never blindly retried.
- Credentials and tokens stay out of code, version control, logs, and AI context.

## What exists (in this folder)
- gmail_drafts.py: single-file CLI (Python 3.10+, official Gmail API).
  - Subcommands: auth, dry-run, draft, status, send, resolve.
  - Input: CSV with columns to, subject, body (one message per row) + one PDF path.
  - Reads the PDF once and reuses its bytes for every message. Builds MIME via email.message.EmailMessage with the PDF as a real attachment (original filename, application/pdf).
  - draft -> Gmail drafts.create. send -> drafts.send on drafts created earlier, with a typed "SEND <n>" confirmation (or --yes). Checks each draft still exists (drafts.get) before sending; deleted drafts are flagged, not sent.
  - Append-only JSONL ledger (state/ledger.jsonl, mode 0600). Row key = sha256(to, subject, body, attachment sha256). Statuses: drafted, draft_failed, draft_unknown, sending, sent, send_failed, send_unknown, draft_missing, cleared.
  - Error policy: HTTP 4xx = definite failure (retryable next run). 5xx, timeouts, connection errors, or a crash after the write-ahead "sending" marker = uncertain -> flagged for review, never auto-retried. All create/send calls use num_retries=0. A run stops after 3 consecutive failures.
  - `resolve --key <prefix> --as sent|drafted|cleared` lets a human settle flagged rows after checking Gmail.
  - dry-run validates CSV + PDF (email format, header-injection, empty fields, duplicates, PDF magic bytes, size) and previews recipients/subjects with no network and no writes.
  - Auth: OAuth installed-app flow, scope gmail.compose only. Client secret and token live in ~/.config/gmail-drafts/ (dir 700, files 600), overridable by env vars GMAIL_DRAFTS_HOME / GMAIL_CLIENT_SECRET / GMAIL_DRAFTS_STATE.
  - Logs contain addresses, statuses and Gmail IDs only; never the encoded message, PDF, or tokens.
- test_gmail_drafts.py: 24 offline unit tests with a fake Gmail service (MIME integrity incl. non-ASCII filename, validation, idempotent reruns, uncertain vs definite failures, crash mid-send, deleted drafts, dry-run side effects, no-leak checks). All pass.
- README.md (OAuth setup + commands), requirements.txt, recipients.example.csv (dummy recipients), test_recipient.csv (one self-addressed test row), .gitignore (secrets, state, PDFs, real recipient lists).

IMPORTANT: this has never run against the live Gmail API. The environment it was built in had no network access or credentials. Treat live behavior as unverified.

## Tasks, in order

1. Live verification (do this first, with me in the loop).
   - Walk me through the one-time Google OAuth setup in README.md (Desktop client, gmail.compose). Then run against test_recipient.csv with --state-dir state_test: auth -> dry-run -> draft -> I confirm in Gmail that the attachment is present, correctly named, and opens -> send (typed confirmation) -> status -> rerun draft (must skip, no duplicate).
   - Confirm real-API behavior the tests only simulate: drafts.create/get/send responses and field names, error shapes (googleapiclient HttpError .resp.status), whether a deleted draft returns 404 on drafts.get, and whether a 4xx on send truly guarantees nothing was sent. Fix any discrepancies and add tests that capture them.

2. Harden for real use.
   - Rate limiting/backoff for 429 and per-user quotas (backoff applies only to requests that are definitely safe to repeat; never to create/send with an uncertain outcome).
   - Large batches: confirm behavior and message-size limits (25 MB total incl. encoding); consider a configurable per-run cap.
   - Concurrency safety for the ledger (file lock) so two runs can't double-process.
   - Better recovery for uncertain draft creates: investigate whether a draft can be reliably looked up afterward (e.g. via a unique marker header/token and drafts.list/messages search under the current scope) to auto-resolve draft_unknown. If it needs a broader OAuth scope, do NOT widen the scope without asking me; document the tradeoff.
   - Clear, actionable errors for: expired/revoked token (including 7-day expiry for External apps in Testing mode), missing client file, admin-blocked scope.

3. Generalize it.
   - Support multiple attachments and per-row attachment overrides (e.g. an optional `attachments` CSV column), still reading each file once and reusing its bytes.
   - Optional per-row cc/bcc/reply-to/from-alias columns, with the same validation rigor (no header injection).
   - Template mode: a body template with {{placeholders}} filled from extra CSV columns, plus a clear error for missing/unused fields. Keep plain-text default; add optional HTML body alongside text (multipart/alternative) behind an explicit flag.
   - Pluggable recipient source: keep CSV as the contract, but add a small, documented interface so another process (e.g. a CRM export or an AI agent) can emit that CSV. Do not couple the tool to any specific CRM or business use case.
   - Optional "--from-ledger" report (CSV/JSON export of sent/drafted/flagged rows with Gmail IDs) so results can be written back to other systems.

4. Make it agent-friendly without weakening safety.
   - Add machine-readable output (--json) for dry-run/draft/status/send summaries so an AI agent can read results without seeing message bodies or attachment bytes.
   - Document an "agent workflow" in the README: agent writes the CSV -> dry-run -> human reviews drafts in Gmail -> human (or explicit approval step) runs send. The agent must never be able to send without the typed confirmation; keep --yes behind a clearly documented flag and consider disabling it by default via an env var.
   - Keep the agent unable to read credentials, tokens, or attachment contents through any of the tool's output.

5. Quality and packaging.
   - Keep the offline test suite green; add tests for every new feature and every bug found in step 1. Add a CI-friendly command (python -m unittest) and a simple linter/type-check config (ruff/mypy) if it doesn't bloat the project.
   - Consider packaging as an installable CLI (pyproject.toml) with a console entry point; keep single-file simplicity if packaging adds little value.
   - Keep README.md accurate: setup, commands, safety model, troubleshooting.

## Rules
- Never send an email, or run `send`, without my explicit approval of the exact recipient list in this session. Live tests go only to addresses I name.
- Never print, log, commit, or paste into any chat: client_secret.json, token.json, OAuth codes, the encoded message, or the attachment's base64.
- Don't widen the OAuth scope beyond gmail.compose without asking me first and explaining why.
- Don't substitute a hosted link for the attachment feature.
- Calendar invites, CRM updates, and anything outside email drafting/sending are out of scope unless I ask.
- If anything is ambiguous or risky (uncertain send outcomes, quota problems, scope questions), stop and ask rather than guessing.

Start by reading gmail_drafts.py and README.md, run the existing tests, then propose a short plan for step 1 and wait for my go-ahead before touching any live Gmail account.
