# gmail_drafts: personalized Gmail drafts with attachments

Creates one Gmail draft per CSV row, each with its own subject/body and the same file(s)
attached. Drafts are the default. Sending is a separate, confirmed command. Attachment bytes
never pass through an AI conversation; the script reads files locally. Live-verified against
the real Gmail API (see "Verified live behavior").

Requires Python 3.10+.

## One-time setup (about 5 minutes)

This script needs its own Google OAuth login. An AI assistant's Gmail connector credentials
are not available to local code.

1. https://console.cloud.google.com/ : create (or pick) a project.
2. **APIs & Services > Library**: enable **Gmail API**.
3. **OAuth consent screen** (Google Auth platform):
   - Google Workspace account: choose **Internal**. No verification, tokens don't expire weekly.
   - Personal Gmail: choose **External**, leave it in **Testing**, add yourself under **Test users**. Testing-mode tokens expire after 7 days; re-run `auth`.
   - Add the single scope `https://www.googleapis.com/auth/gmail.compose`.
4. **Credentials > Create credentials > OAuth client ID > Desktop app**. Download the JSON.
5. Save it:
   ```bash
   mkdir -p -m 700 ~/.config/gmail-drafts
   mv ~/Downloads/client_secret_*.json ~/.config/gmail-drafts/client_secret.json
   chmod 600 ~/.config/gmail-drafts/client_secret.json
   ```
6. Install and log in:
   ```bash
   uv venv --python 3.12 && uv pip install -e .      # or: python3.12 -m venv .venv && pip install -e .
   .venv/bin/gmail-drafts auth                       # opens a browser; click Allow
   ```
   (`python3 gmail_drafts.py ...` works the same without installing, given the requirements.)

`gmail.compose` is the narrowest scope that can create and send drafts. It does not grant
general mailbox read access. Secrets live only in `~/.config/gmail-drafts/` (dir 700, files 600).
To revoke: delete `token.json` and remove the app at https://myaccount.google.com/permissions.

## CSV format (the recipient-source contract)

Header required, UTF-8, one message per row, multi-line values in quotes. Column names are
case-insensitive.

| column | required | meaning |
|---|---|---|
| `to` | yes | one bare address (`name@domain.com`) |
| `subject` | yes | may contain `{{placeholders}}` |
| `body` | yes, unless `--template` | plain-text body |
| `cc`, `bcc` | no | bare addresses separated by `;` |
| `reply_to` | no | one bare address |
| `from` | no | a Gmail send-as alias of the authorized account |
| `attachments` | no | file names separated by `;`, relative to the CSV's folder. Replaces the default attachment(s) for that row |
| anything else | no | available as `{{column}}` in templates and subjects |

Every address is validated (no names, no line breaks, no header injection). Any problem in any
row fails the whole batch before anything touches Gmail, with every problem listed.

Anything that can write this file can be a recipient source (a CRM export, a script, an AI agent).
Programs can use `write_recipients_csv(path, rows)` from `gmail_drafts.py`. The tool is not tied to
any particular CRM or use case.

### Templates

```bash
--template body.txt                        # {{first_name}} etc. filled from CSV columns
--template body.txt --html-template body.html   # also send HTML (multipart/alternative)
```
Plain text is the default. Missing column, or an empty value for a used placeholder, is an error.
Extra unused columns are a warning (an error with `--strict-fields`). Values are inserted
literally in one pass (never re-expanded), checked for line breaks after substitution, and HTML-escaped
in the HTML body.

### Attachments

`--pdf FILE` / `--attach FILE` (repeatable) set the default attachments for every row. Each file is
read once. Files named `*.pdf` must start with `%PDF-`. Limit: 18 MB of attachments per message
(base64 makes it about 25 MB, Gmail's cap; checked again on the encoded message).

Protected: the tool refuses to attach anything that looks like a credential (names like
`token*`, `client_secret*`, `.env`, `*.pem`, `id_rsa`) or anything inside `~/.config`, `~/.ssh`,
`~/.aws`, `~/.gnupg`, `~/.kube`, the secrets dir, or the state dir. Set
`GMAIL_DRAFTS_ATTACH_ROOTS=/a/dir:/another` to additionally restrict attachments to an allowlist.

## Commands

```bash
B=(--csv recipients.csv --pdf One-Pager.pdf)
gmail-drafts dry-run "${B[@]}"                  # validate + preview; no network, no writes
gmail-drafts draft   "${B[@]}" --limit 1        # trial: one draft, check it in Gmail
gmail-drafts draft   "${B[@]}"                  # all drafts (never sends; re-runs skip finished rows)
#   review/edit/delete drafts in Gmail > Drafts
gmail-drafts send    "${B[@]}"                  # lists recipients, you type SEND <n>
gmail-drafts status  "${B[@]}"                  # per-row state and Gmail IDs
gmail-drafts reconcile "${B[@]}"                # auto-settle uncertain rows (read-only lookups)
gmail-drafts report --format csv --status sent --out sent.csv   # export results (mode 600)
```

State (ledger, log, lock) lives in `./state` (override with `--state-dir` or `GMAIL_DRAFTS_STATE`).
`draft` and `send` handle at most 200 rows per run (`--max-per-run`); re-run to continue.
Edits you make to a draft in Gmail are respected. Deleted drafts are detected and not sent.
`send` computes its "SEND <n>" count only from drafts confirmed to still exist.

## Safety model

- **No duplicates.** Each row is keyed by a hash of (to, subject, body, attachment hashes, plus cc/bcc/reply-to/from/html if used) and recorded in `state/ledger.jsonl`. Editing a row or swapping a file makes a new key, i.e. a new draft by design.
- **One run at a time** per state dir (file lock). A second concurrent run exits immediately.
- **Definite failure** (HTTP 4xx other than 408): `draft_failed` / `send_failed`, retried on the next run. HTTP 429 additionally stops the run (wait, then re-run).
- **Uncertain outcome** (timeout, 5xx, 408, connection error, crash after the write-ahead `sending` marker): `draft_unknown` / `send_unknown`, **never retried automatically**. Run `reconcile`, or check Gmail and use `resolve`.
- Create/send use `num_retries=0`. Only read calls (`drafts.get`, `drafts.list`) get automatic exponential backoff.
- A run stops after 3 consecutive failures.
- Logs and output contain addresses, subjects, statuses, Gmail IDs, sizes and short hash prefixes only. Never bodies, attachment bytes, the encoded message, or tokens. Exception text is never logged.

### reconcile

Every message carries an `X-Gmail-Drafts-Key` header (the row key). For `draft_unknown` rows,
`reconcile` scans recent drafts for that header; found means the row becomes `drafted` with its real
draft ID, not found means it was most likely never created (`resolve --as cleared` to retry).
For `send_unknown` rows: if the draft still exists, the send did not happen (`drafted`); if it is gone,
it was probably sent (or you deleted it), so it stays flagged for a human. Rows younger than 2 minutes
are skipped to allow for Gmail's eventual consistency. This works under `gmail.compose`; no broader
scope is needed. The header is visible to recipients (it is a hash of the message content).

```bash
gmail-drafts resolve --key <prefix> --as sent|drafted|cleared
```

## Agent workflow

For an AI assistant (or script) driving this tool:

1. Agent writes the recipients CSV (and optional template) and runs `--json dry-run`. Fix any `validation_errors`.
2. Agent runs `--json draft`, then reads the result (`rows[].status`, `draft_id`). Output never includes bodies or attachment bytes.
3. **Human** reviews the drafts in Gmail.
4. **Human** runs `send` in a terminal and types `SEND <n>`. 

The agent cannot skip step 4: `send` refuses without an interactive terminal, refuses under `--json`,
and `--yes` is disabled unless an operator sets `GMAIL_DRAFTS_ALLOW_YES=1` in the environment.
Do not set that variable in an agent's environment. Agents also cannot read secrets through the tool:
credentials are never printed, and credential-like files cannot be attached.

`--json` prints exactly one JSON object on stdout (logging goes to `state/gmail_drafts.log` only).
Exit codes: 0 ok, 1 some rows failed or need review, 2 validation failed.

## Claude Desktop (MCP server)

`mcp_server.py` exposes the same engine to the Claude desktop app (or any MCP client) as a local
stdio server. Files are only read locally; the model passes paths, and every result is the same
no-bodies/no-bytes/no-secrets JSON as `--json`.

**Setup** (already done on this Mac; shown for reference):

1. `uv pip install -e ".[mcp]"` (needs `mcp>=1.12,<2`), and complete the OAuth setup above once (`gmail-drafts auth`).
2. Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (use absolute paths):
   ```json
   "mcpServers": {
     "gmail-drafts": {
       "command": "/Users/you/gmail-drafts/.venv/bin/python",
       "args": ["/Users/you/gmail-drafts/mcp_server.py"]
     }
   }
   ```
3. Fully quit (Cmd+Q) and reopen Claude. The tools appear under the connectors/tools menu.

**Tools**

| tool | what it does | writes? |
|---|---|---|
| `dry_run` | validate CSV/template/attachments, preview | no |
| `draft` | create Gmail drafts, never sends; re-runs skip finished rows | drafts only |
| `status` | per-row state and Gmail IDs | no |
| `report` | export the batch ledger | no |
| `reconcile` | settle uncertain rows via read-only Gmail lookups | ledger only |
| `prepare_send` | exact recipient list (to/cc/bcc), subjects, count, plus a single-use confirm token | no (ledger flag if a draft was deleted) |
| `send` | **sends real email**; needs the token + exact count | yes |

Common inputs: `csv`, `attachments` (paths), optional `template` / `html_template`, and `batch`, a short
name (letters, digits, `_-.`) for the mailing. Each batch keeps its ledger under
`~/.local/state/gmail-drafts/<batch>` (override the root with `GMAIL_DRAFTS_STATE_ROOT`); the model
cannot pick other directories. Use the same `batch` for every call about one mailing.

**Approval model for sending**

1. `prepare_send` re-checks that each draft still exists and returns the exact recipients, subjects,
   attachment names and count, with a confirm token that lives 5 minutes, works once, and is bound to
   the exact set of (message, Gmail draft) pairs.
2. `send` needs that token and the exact count. It re-verifies the set (before and after the human answers).
   A changed draft or recipient, a different `limit`/batch, a wrong count, an expired, reused or unknown
   token: refused, nothing sent.
3. If the client supports MCP **elicitation**, `send` also shows the recipient list in a server-raised
   prompt where the user must type the count. The model cannot answer it. If the client advertises
   elicitation but the prompt fails or is declined, nothing is sent. The result says which gate was used
   (`"confirmation": "elicitation"` or `"tool_permission_prompt"`). Set `GMAIL_MCP_REQUIRE_ELICITATION=1`
   in the server's `env` to refuse sending on clients without elicitation.
4. Otherwise the app's own tool-permission prompt is the human gate. `send` is marked
   `destructiveHint=true`, `readOnlyHint=false`.

**Choose "Allow once" for `send`, never "Always allow".** "Always allow" removes the one prompt
that stands between a model and your outbox. Allowing the read-only tools and `draft` always is fine.
This is weaker than the CLI's typed confirmation at a terminal, which is unchanged (`send` there still
needs a tty and `--yes` stays disabled unless `GMAIL_DRAFTS_ALLOW_YES=1`). Extra hardening: set
`GMAIL_DRAFTS_ATTACH_ROOTS` in the server's `env` so only your document folders can be attached or used as
CSV/templates. Credential-looking files and protected directories are always refused.

With `GMAIL_DRAFTS_ATTACH_ROOTS` set, the same folders also limit where the CSV and template paths passed to the
MCP tools may live, so keep recipient CSVs inside an allowed folder. Every send records which gate approved it
(`gate` in the ledger, `status` and `report`). Claude Desktop does not currently support elicitation, so there the
gate is the one-click permission prompt: use **Allow once**.

Debugging: Claude Desktop logs the server under `~/Library/Logs/Claude/mcp-server-gmail-drafts.log`.
Nothing is ever printed to stdout except protocol messages.

## Verified live behavior (Gmail API, 2026-10-26)

- `drafts.create` returns `{id, message:{id, threadId, labelIds}}`; `drafts.send` returns `{id, threadId}`.
- Errors are `HttpError` with `resp.status` as an int.
- `drafts.get` on a deleted, bogus, or already-sent draft returns **404**. `drafts.send` on such a draft also returns 404.
- Under `gmail.compose`: `drafts.get` (minimal/full/raw/metadata) and `drafts.list` work. `messages.attachments.get` returns 403 (insufficient scope), so verify attachments with `drafts.get` format=raw instead.
- `drafts.get` has no `metadataHeaders` parameter.
- Custom headers (`X-Gmail-Drafts-Key`) and Cc/Reply-To/multipart/alternative survive into the draft.
- Not provable without sending to a real third party: that a 4xx on `drafts.send` always means nothing was sent. The tool treats it as definite (Google documents 4xx as request rejected); 408 is treated as uncertain.

## Troubleshooting

- **"Not authorized, or the saved token expired or was revoked"**: run `gmail-drafts auth`. External apps in Testing mode expire tokens after 7 days; Internal Workspace apps do not.
- **"OAuth client file not found"**: save the Desktop-app JSON to `~/.config/gmail-drafts/client_secret.json`.
- **"is not a valid OAuth client file"**: re-download; the type must be *Desktop app*.
- **Browser says "access blocked" / admin_policy_enforced**: your Workspace admin restricts the app or scope. Ask them to allow the OAuth client for `gmail.compose`.
- **HTTP 403 mid-run**: usually revoked auth or a blocked scope; run `auth`. **HTTP 429**: wait and re-run.
- **"Another gmail_drafts run is using ..."**: a run is in progress (or use a different `--state-dir`).
- **A row shows `draft_unknown`/`send_unknown`**: `reconcile`, then `resolve` for anything left.

## Development

```bash
python -m unittest -v                       # offline; no credentials, nothing sent
ruff check . && mypy gmail_drafts.py mcp_server.py
```
The suite uses a fake Gmail service (MIME integrity, validation, idempotency, uncertain vs definite
failures, crash mid-send, deleted drafts, templates, per-row attachments, secrets blocking, locking,
reconcile, JSON output safety, `--yes` gating, leak checks).
