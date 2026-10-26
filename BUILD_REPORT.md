# gmail-drafts: Build Report

Oct 26, 2026 · Remington Gack
Live copy: https://claude.ai/artifact/XPra2jXqaaokdHvi7Crjov

## Summary

gmail-drafts is a working, live-verified local tool that lets an AI agent prepare many personalized Gmail emails with file attachments, and lets a human send them safely. It runs on Remington's Mac at `~/gmail-drafts`, as a CLI and as a Claude Desktop MCP server.

- **Status:** all five brief steps are complete, plus a Claude Desktop integration added on request. 69 offline tests pass; ruff and mypy are clean.
- **Live use so far:** three real emails, all to remington.gack@longitudelabs.com. Two were sent from the CLI (a dummy PDF, then the 200 KB "Longitude Labs - Experimental Prediction Licensing.pdf"), and one from inside the Claude Desktop app (same PDF, sent 2026-10-26 21:23 UTC). Remington confirmed the attachments open.
- **Not yet done:** no send to anyone other than Remington, and no batch larger than one row has run live.
- **Core promise:** attachment bytes, credentials and message bodies never enter an AI conversation; drafts come first; reruns never duplicate; uncertain outcomes go to a human.

## Components

One Python engine (`gmail_drafts.py`) does all the work; the CLI and the MCP server are two front doors onto it. Python 3.12 venv at `~/gmail-drafts/.venv` (built with uv); official Gmail API client; OAuth scope `gmail.compose` only.

| Piece | Where | What it does |
| --- | --- | --- |
| Engine + CLI | `~/gmail-drafts/gmail_drafts.py` (1,098 lines) | Validation, MIME building, ledger, Gmail calls; console command `gmail-drafts` via `pyproject.toml` |
| MCP server | `~/gmail-drafts/mcp_server.py` (351 lines) | FastMCP (`mcp>=1.12,<2`), stdio; registered in Claude Desktop as `gmail-drafts` |
| Tests | `test_gmail_drafts.py`, `test_mcp_server.py` | 69 offline tests with a fake Gmail service |
| Docs | `README.md`, `AGENT_BRIEF.md` | Setup, CSV contract, commands, safety model, agent workflow, Desktop setup; original brief |
| Credentials | `~/.config/gmail-drafts/` (dir 700, files 600) | `client_secret.json` (Desktop OAuth client), `token.json` |
| CLI ledgers | `./state/` by default, or `--state-dir` | Append-only `ledger.jsonl` + logs, mode 600 |
| MCP ledgers | `~/.local/state/gmail-drafts/<batch>/` | Same format; the model picks a batch name, never a path |
| Desktop config | `~/Library/Application Support/Claude/claude_desktop_config.json` | `mcpServers.gmail-drafts`; backup at `claude_desktop_config.json.bak-gmail-drafts-20261026` |

**CLI commands:** `auth`, `dry-run`, `draft`, `status`, `send`, `reconcile`, `resolve`, `report`.

**MCP tools:** `dry_run`, `draft`, `status`, `report`, `reconcile`, `prepare_send`, `send`. There is no `resolve` tool: settling flagged rows by hand stays a CLI-only, human action.

**Input contract:** a CSV, one row per message.

- Required columns: `to`, `subject`, `body`.
- Optional columns: `cc`, `bcc`, `reply_to`, `from` (a Gmail send-as alias), `attachments` (per-row override).
- Any other column is a template field: `--template` fills `{{placeholders}}` in the body, and `--html-template` adds an HTML part (multipart/alternative). Missing or empty fields are errors.
- Any process (a CRM export, an agent) can produce this CSV; the tool is not tied to any source.

**Report:** `report` exports the ledger (sent, drafted, flagged rows with Gmail draft, message and thread IDs) as CSV or JSON for write-back to other systems.

## Safety model

Every send needs a human approval, every row is tracked in an append-only ledger, and anything uncertain stops for a person instead of retrying.

**What an agent can and cannot see**

- Agents pass file paths only. Each attachment is read once, locally, and its bytes are reused for every message; they never appear in any output.
- `--json` (CLI) and all MCP results contain addresses, subjects, counts, statuses and Gmail IDs only: no bodies, no base64, no tokens.
- The tool refuses to attach or read credential-like files (e.g. `token.json`, `client_secret.json`) and protected directories, so an agent-written CSV cannot exfiltrate them.
- An optional `GMAIL_DRAFTS_ATTACH_ROOTS` allow-list of attachment folders exists but is not set.

**Send gates**

| Path | Gate |
| --- | --- |
| CLI `send` | Requires an interactive terminal and a typed `SEND <n>` confirmation. `--yes` is disabled unless `GMAIL_DRAFTS_ALLOW_YES=1` (intentionally unset). Refused under `--json`. |
| MCP `prepare_send` | Re-checks each draft still exists; returns exact to/cc/bcc, subjects, attachment names, count, and a single-use token valid 5 minutes, bound to the exact (message, draft) set. |
| MCP `send` | Needs that token and the exact count. Wrong count, expired/reused/unknown token, different batch or limit, or any changed draft = refused, and a failed attempt burns the token. Then either a server-side elicitation (user types the count) if the client supports it, or the Claude Desktop tool-permission prompt. Marked `destructiveHint=true`. |

Remington chose app-side sending with an Allow prompt over terminal-only, accepting that one click is weaker than a typed confirmation. Users must choose **Allow once** for `send`, never **Always allow**.

**Idempotency and uncertain outcomes**

- Each row's key is a sha256 of its recipients, subject, body and attachment hashes. A row already drafted or sent is skipped on rerun.
- A write-ahead `sending` marker is logged before each send.
- HTTP 4xx = definite failure, retryable next run (except 408, treated as uncertain). 5xx, timeouts, connection errors, or a crash after the marker = uncertain, flagged `draft_unknown` / `send_unknown`, never retried automatically.
- Every message carries an `X-Gmail-Drafts-Key` header. `reconcile` uses it to look the row up in Gmail and settle uncertain rows, within `gmail.compose`. `resolve` lets a human settle a row by hand.
- Create and send calls never retry (`num_retries=0`). Read-only calls get backoff. A 429 stops the run. A run stops after 3 consecutive failures.
- Limits: 25 MB per message (after encoding), 200 rows per run by default (`--max-per-run`), and a file lock so two runs cannot process the same ledger at once.
- Auth errors are explicit: an expired or revoked token tells the user to run `auth` instead of silently opening a browser.

## Live-verified behavior and tests

The real Gmail API matched the code's assumptions on every point that could be tested without emailing a third party.

| Behavior | Result (live, 2026-10-26) |
| --- | --- |
| `drafts.create` response | Returns `id` and `message.id`, as the code expects |
| `drafts.get` / `drafts.send` on a deleted, bogus or already-sent draft | 404; `HttpError.resp.status` is an int |
| `messages.attachments.get` under `gmail.compose` | 403 (not allowed); attachment checks use `drafts.get` format=raw instead |
| `drafts.get` `metadataHeaders` parameter | Not supported; the first reconcile patch broke on it and was fixed |
| `X-Gmail-Drafts-Key` custom header | Survives into the stored draft, so `reconcile` can find rows |
| Attachment integrity | Filename, `application/pdf`, `Content-Disposition: attachment` and sha256 all matched the source, including a filename with spaces |
| Rerun after draft and after send | Skipped; ledger unchanged, no duplicate |
| Template + HTML + cc + reply-to + per-row attachment | Live draft matched the expected MIME structure; probe draft deleted |
| MCP over stdio | `dry_run`, `draft`, `status`, `prepare_send` worked; Remington then sent from Claude Desktop |

**Not verified:** that a 4xx on `drafts.send` always means nothing was sent. Proving it needs a send to a real third party. The tool treats 4xx as definite except 408.

**Tests:** 69 offline tests (24 at handover; 25 new for CLI features and bugs; 20 for the MCP layer). They cover MIME integrity (including non-ASCII filenames), validation and header injection, idempotent reruns, definite vs uncertain failures, a crash mid-send, deleted drafts, dry-run side effects, token binding/expiry/reuse/mismatch, elicitation accept/decline/error, no-leak checks, and refusal of credential inputs. Run with `.venv/bin/python -m unittest`.

**Bugs found and fixed:** the `SEND <n>` count overstated sendable rows (deleted drafts were counted); `setup_logging` leaked a file handle on every call (would accumulate in the long-running MCP server); the `metadataHeaders` reconcile bug above.

## Known gaps, risks and open decisions

The biggest gaps are that nothing has been sent to a third party or at batch scale, and that the app-side send gate is a single click.

- **Third-party and batch sends untested.** Every live send so far went to one address, Remington's own. Quota, 429 and per-run-cap behavior at 50 to 200 rows is covered only by offline tests.
- **4xx-on-send assumption unproven.** See the section above; a wrong assumption could cause a duplicate on retry.
- **Which Desktop gate fired is unrecorded.** The app-side send succeeded, but nobody noted whether Claude Desktop showed the elicitation (type the count) or the plain Allow prompt. The ledger does not store it. Worth checking on the next send, since it decides how strong the gate is.
- **"Always allow" risk.** If anyone clicks Always allow on `send` in Claude Desktop, later sends need only a valid `prepare_send` token, which the model can obtain itself. The guard is user discipline plus the token's binding to an exact recipient set.
- **Consent-screen type unknown.** If the OAuth app was created as External / Testing rather than Internal, the token expires every 7 days and `auth` must be rerun.
- **No attachment folder allow-list.** `GMAIL_DRAFTS_ATTACH_ROOTS` is unset, so any readable file except credential-like ones can be attached.
- **Not a git repo.** `~/gmail-drafts` has no version control or remote; `.gitignore` is ready. Stray artifacts sit in the folder (`state_test/`, `state_test2/`, `app_smoke.csv`, `test_attachment.pdf`, `__pycache__`, `.egg-info`).
- **Desktop app only.** claude.ai web and mobile cannot reach the local server. Making them work would mean hosting credentials or files elsewhere, which conflicts with the core constraints.
- **Process note.** During the second test, a one-off verification script printed a full message, putting one PDF's base64 into the building agent's own context. It went no further and the tool itself does not do this, but it shows the rule needs enforcing in ad-hoc scripts too.

## Candidate next steps

These are options, not commitments; the planner should pick and order them. Anything that sends to a new address needs Remington's explicit approval of the exact recipient list.

| # | Step | Why | Needs from Remington |
| --- | --- | --- | --- |
| 1 | Small real batch (3 to 5 internal colleagues who agree to receive it) | First third-party and multi-row send; exercises per-run cap and status at more than one row | Named recipients and approval |
| 2 | Record which Desktop gate fired, and store the gate type in the ledger | Confirms whether the app gate is a typed count or one click | Note what the prompt looked like on the next app send |
| 3 | Put `~/gmail-drafts` under git, clean test artifacts, push to a private repo | Change history and backup | Where to host it |
| 4 | Set `GMAIL_DRAFTS_ATTACH_ROOTS` (e.g. `~/Documents/work`) | Limits what an agent can attach | Which folders |
| 5 | Check the consent-screen type; move to Internal if it is External / Testing | Avoids 7-day token expiry | Look in Google Cloud Console |
| 6 | Wire a real recipient source to the CSV contract (e.g. a CRM or tracker export script) | The tool is built to be fed by another process | Which source and which use case |
| 7 | Optional: a stronger Desktop send gate (e.g. a local confirm dialog outside the model's control) if elicitation is unsupported | Restores typed-confirmation strength in the app | Whether one-click is acceptable long term |
| 8 | Optional: per-recipient sending pace / daily cap to stay under Gmail sending limits on large runs | Large-batch safety | Expected batch sizes |

Out of scope unless asked: calendar invites, CRM updates, hosted-link attachments, widening the OAuth scope beyond `gmail.compose`.
