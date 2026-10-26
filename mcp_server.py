#!/usr/bin/env python3
"""Local stdio MCP server for gmail_drafts (for Claude Desktop and other MCP clients).

Tools: dry_run, draft, status, report, reconcile, prepare_send, send.

The model passes PATHS (CSV, template, attachments); files are read locally and their contents
never appear in any tool result. Results are the same JSON as `gmail_drafts.py --json`:
addresses, subjects, statuses, Gmail IDs, sizes and hash prefixes only.

Sending is gated three ways:
  1. prepare_send returns the exact recipient list and a short-lived, single-use token bound to
     that exact set of (ledger key, Gmail draft id) pairs.
  2. send needs that token and the exact count, and re-checks that the set is unchanged.
  3. If the client supports MCP elicitation, send ALSO asks the human, server-side, to type the
     count after showing the recipients (the model cannot answer it). Otherwise the client's own
     tool-permission prompt is the human gate: choose "Allow once" for `send`, never "Always allow".
"""
from __future__ import annotations

import functools
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import gmail_drafts as gd

STATE_ROOT = Path(os.environ.get("GMAIL_DRAFTS_STATE_ROOT", Path.home() / ".local" / "state" / "gmail-drafts"))
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TOKEN_TTL_SECONDS = 300
MAX_OUTSTANDING_TOKENS = 20
MAX_LISTED_RECIPIENTS = 200

# Tests replace this to inject a fake Gmail service.
service_factory: Callable = gd.get_service


# --------------------------------------------------------------------------- send tokens
class SendTokens:
    """Short-lived, single-use confirmation tokens bound to an exact set of (key, draft_id) pairs."""

    def __init__(self, ttl: float = TOKEN_TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._tokens: dict[str, tuple[float, str, tuple]] = {}

    def issue(self, batch: str, binding: tuple) -> str:
        now = self.clock()
        self._tokens = {t: v for t, v in self._tokens.items() if v[0] > now}  # drop expired
        while len(self._tokens) >= MAX_OUTSTANDING_TOKENS:
            self._tokens.pop(next(iter(self._tokens)))
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (now + self.ttl, batch, binding)
        return token

    def consume(self, token: str, batch: str) -> tuple:
        """Return the bound set and invalidate the token. Raises PermissionError on any problem."""
        entry = self._tokens.pop(token or "", None)  # single use, even if the checks below fail
        if entry is None:
            raise PermissionError("Unknown, expired or already-used confirmation token. Call prepare_send again.")
        expires, bound_batch, binding = entry
        if self.clock() >= expires:
            raise PermissionError("Confirmation token expired. Call prepare_send again.")
        if bound_batch != batch:
            raise PermissionError("Confirmation token belongs to a different batch. Call prepare_send again.")
        return binding


TOKENS = SendTokens()


# --------------------------------------------------------------------------- input handling
def state_dir_for(batch: str) -> Path:
    """The model names a batch; it cannot choose an arbitrary directory."""
    if not BATCH_RE.match(batch or ""):
        raise gd.ValidationError([f"invalid batch name {batch!r}: use letters, digits, '_', '-', '.' (max 64)"])
    return STATE_ROOT / batch


def make_args(cmd: str, csv: str, attachments: list[str] | None, template: str | None, html_template: str | None,
              no_attachment: bool, strict_fields: bool, batch: str, limit: int | None = None) -> SimpleNamespace:
    """Same shape as the CLI's parsed arguments, after refusing secret-looking inputs."""
    problems: list[str] = []
    for path in (csv, template, html_template):
        if path:
            problems += gd.check_attachment_path(Path(path).expanduser())
    if problems:
        raise gd.ValidationError(problems)
    return SimpleNamespace(
        cmd=cmd, json=True, state_dir=str(state_dir_for(batch)), csv=str(Path(csv).expanduser()),
        attach=[str(Path(a).expanduser()) for a in (attachments or [])],
        template=template and str(Path(template).expanduser()),
        html_template=html_template and str(Path(html_template).expanduser()),
        no_attachment=no_attachment, strict_fields=strict_fields, limit=limit, max_per_run=gd.DEFAULT_MAX_PER_RUN)


def argv_for(cmd: str, args: SimpleNamespace) -> list[str]:
    argv = ["--state-dir", args.state_dir, cmd, "--csv", args.csv]
    for a in args.attach:
        argv += ["--attach", a]
    if args.template:
        argv += ["--template", args.template]
    if args.html_template:
        argv += ["--html-template", args.html_template]
    if args.no_attachment:
        argv.append("--no-attachment")
    if args.strict_fields:
        argv.append("--strict-fields")
    if args.limit is not None:
        argv += ["--limit", str(args.limit)]
    return argv


def guarded(fn):
    """Turn every failure into a small JSON error. Exception text is never echoed (it can hold secrets)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except gd.ValidationError as ve:
            return {"ok": False, "validation_errors": ve.problems}
        except PermissionError as pe:
            return {"ok": False, "refused": str(pe)}
        except SystemExit as se:
            return {"ok": False, "error": str(se.code)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}
    return wrapper


def run_cli(cmd: str, args: SimpleNamespace) -> dict:
    code, payload = gd.call_json(argv_for(cmd, args), service_factory)
    return payload if isinstance(payload, dict) else {"ok": code == 0, "rows": payload}


# --------------------------------------------------------------------------- send plumbing
def eligible_rows(args: SimpleNamespace):
    """(rows, ledger, service, sendable) with each draft confirmed to still exist. Caller holds the lock."""
    _, rows, ledger = gd._batch(args)
    service = service_factory()
    sendable = gd.verify_sendable(rows, ledger, service)[: gd.effective_limit(args)]
    return rows, ledger, service, sendable


def binding_of(sendable, ledger) -> tuple:
    return tuple(sorted((r.key, ledger.event(r.key).get("draft_id", "")) for r in sendable))


def recipient_listing(sendable, ledger) -> list[dict]:
    out = []
    for r in sendable[:MAX_LISTED_RECIPIENTS]:
        item: dict[str, Any] = {"key": r.key[:12], "to": r.to, "subject": r.subject,
                                "attachments": [f"{a.filename} ({len(a.data):,} bytes)" for a in r.attachments]}
        if r.cc:
            item["cc"] = list(r.cc)
        if r.bcc:
            item["bcc"] = list(r.bcc)
        out.append(item)
    return out


def elicitation_supported(ctx: Any) -> bool:
    try:
        caps = ctx.request_context.session.client_params.capabilities
    except AttributeError:
        return False
    return getattr(caps, "elicitation", None) is not None


class ConfirmSend(BaseModel):
    recipient_count: int = Field(description="Type the number of emails listed above to confirm sending them")


def confirmation_text(listing: list[dict], total: int) -> str:
    lines = [f"Send {total} email(s) from your Gmail account to:"]
    for it in listing:
        extra = "".join(f" {k}: {', '.join(it[k])};" for k in ("cc", "bcc") if k in it)
        lines.append(f"- {it['to']} | {it['subject'][:70]} | {'; '.join(it['attachments']) or 'no attachment'}{extra}")
    if total > len(listing):
        lines.append(f"... and {total - len(listing)} more")
    lines.append(f"\nType {total} to confirm. Decline to cancel.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- tools
mcp = FastMCP("gmail-drafts")

_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_DRAFT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

_COMMON = """
    csv: path to the recipients CSV (to, subject, body + optional cc, bcc, reply_to, from, attachments, any template columns).
    attachments: paths of files attached to every email (a row's `attachments` column overrides them).
    template / html_template: optional body template files with {{placeholders}} filled from CSV columns.
    batch: a short name for this mailing; its ledger lives in a private state folder. Reuse the same name for
        dry_run, draft, status, prepare_send and send."""


@mcp.tool(annotations=_READ)
@guarded
def dry_run(csv: str, attachments: list[str] | None = None, template: str | None = None,
            html_template: str | None = None, no_attachment: bool = False, strict_fields: bool = False,
            batch: str = "default") -> dict:
    """Validate the CSV, template and attachments and preview recipients/subjects. No Gmail call, no drafts, nothing sent.
    Returns JSON with no message bodies or file contents."""
    return run_cli("dry-run", make_args("dry-run", csv, attachments, template, html_template, no_attachment,
                                        strict_fields, batch))


@mcp.tool(annotations=_DRAFT)
@guarded
def draft(csv: str, attachments: list[str] | None = None, template: str | None = None,
          html_template: str | None = None, no_attachment: bool = False, strict_fields: bool = False,
          batch: str = "default", limit: int | None = None) -> dict:
    """Create Gmail drafts (never sends). Re-running skips rows already handled, so it cannot create duplicates.
    limit: only handle the first N eligible rows (use 1 for a trial)."""
    return run_cli("draft", make_args("draft", csv, attachments, template, html_template, no_attachment,
                                      strict_fields, batch, limit))


@mcp.tool(annotations=_READ)
@guarded
def status(csv: str, attachments: list[str] | None = None, template: str | None = None,
           html_template: str | None = None, no_attachment: bool = False, strict_fields: bool = False,
           batch: str = "default") -> dict:
    """Show each row's state (new, drafted, sent, needs review) with Gmail draft/message IDs."""
    return run_cli("status", make_args("status", csv, attachments, template, html_template, no_attachment,
                                       strict_fields, batch))


@mcp.tool(annotations=_READ)
@guarded
def report(batch: str = "default", statuses: list[str] | None = None, max_rows: int = 500) -> dict:
    """Export the batch ledger (latest state per message, with Gmail IDs). Optionally filter by statuses, e.g. ["sent"]."""
    argv = ["--state-dir", str(state_dir_for(batch)), "report", "--format", "json"]
    for st in statuses or []:
        argv += ["--status", st]
    _, payload = gd.call_json(argv, service_factory)
    rows = payload if isinstance(payload, list) else []
    return {"ok": True, "count": len(rows), "rows": rows[: max(1, min(max_rows, 2000))]}


@mcp.tool(annotations=_DRAFT)
@guarded
def reconcile(csv: str, attachments: list[str] | None = None, template: str | None = None,
              html_template: str | None = None, no_attachment: bool = False, strict_fields: bool = False,
              batch: str = "default") -> dict:
    """Settle rows whose outcome was uncertain (timeouts etc.) by looking in Gmail. Read-only lookups; never sends."""
    return run_cli("reconcile", make_args("reconcile", csv, attachments, template, html_template, no_attachment,
                                          strict_fields, batch))


@mcp.tool(annotations=_DRAFT)
@guarded
def prepare_send(csv: str, attachments: list[str] | None = None, template: str | None = None,
                 html_template: str | None = None, no_attachment: bool = False, strict_fields: bool = False,
                 batch: str = "default", limit: int | None = None) -> dict:
    """Step 1 of sending. Confirms each draft still exists and returns the exact recipients (to/cc/bcc), subjects,
    count, and a short-lived single-use confirm_token bound to exactly that set. Sends nothing.
    Show the recipient list to the user, then call `send`."""
    args = make_args("send", csv, attachments, template, html_template, no_attachment, strict_fields, batch, limit)
    try:
        _, ledger, _, sendable = eligible_rows(args)
        listing = recipient_listing(sendable, ledger)
        binding = binding_of(sendable, ledger)
    finally:
        gd.release_locks()
    if not sendable:
        return {"ok": True, "count": 0, "recipients": [], "note": "No reviewed drafts are waiting to be sent."}
    token = TOKENS.issue(batch, binding)
    return {"ok": True, "count": len(sendable), "recipients": listing,
            "recipients_truncated": len(sendable) > len(listing),
            "confirm_token": token, "expires_in_seconds": int(TOKENS.ttl),
            "next": f"Show the user this list. To send, call `send` with this confirm_token and expected_count={len(sendable)}."}


@mcp.tool(annotations=_SEND)
async def send(ctx: Context, confirm_token: str, expected_count: int, csv: str, attachments: list[str] | None = None,
               template: str | None = None, html_template: str | None = None, no_attachment: bool = False,
               strict_fields: bool = False, batch: str = "default", limit: int | None = None) -> dict:
    """SENDS REAL EMAIL from the user's Gmail account. Requires the confirm_token from prepare_send and the exact count,
    and the same csv/attachments/template/batch/limit. Refuses if anything changed since prepare_send.
    The user must approve this call each time (choose "Allow once", never "Always allow")."""
    return await _send(ctx, confirm_token, expected_count, csv, attachments, template, html_template,
                       no_attachment, strict_fields, batch, limit)


async def _send(ctx: Any, confirm_token: str, expected_count: int, csv: str, attachments: list[str] | None,
                template: str | None, html_template: str | None, no_attachment: bool, strict_fields: bool,
                batch: str, limit: int | None) -> dict:
    try:
        bound = TOKENS.consume(confirm_token, batch)  # invalidated now, whatever happens next
        if expected_count != len(bound):
            raise PermissionError(f"expected_count {expected_count} does not match the {len(bound)} prepared. Refusing.")
        args = make_args("send", csv, attachments, template, html_template, no_attachment, strict_fields, batch, limit)
        try:  # re-verify the exact set, then release the lock while the human decides
            _, ledger, _, sendable = eligible_rows(args)
            listing = recipient_listing(sendable, ledger)
            if binding_of(sendable, ledger) != bound:
                raise PermissionError("The drafts or recipients changed since prepare_send. Nothing sent; call prepare_send again.")
        finally:
            gd.release_locks()
        method = "tool_permission_prompt"
        if elicitation_supported(ctx):
            method = "elicitation"
            try:
                answer = await ctx.elicit(confirmation_text(listing, len(sendable)), ConfirmSend)
            except Exception as exc:  # fail closed: a client that advertises elicitation must complete it
                raise PermissionError("Could not obtain the user's confirmation. Nothing sent.") from exc
            if answer.action != "accept" or answer.data is None or answer.data.recipient_count != len(sendable):
                raise PermissionError("The user did not confirm. Nothing sent.")
        elif os.environ.get("GMAIL_MCP_REQUIRE_ELICITATION") == "1":
            raise PermissionError("This client does not support confirmation prompts and GMAIL_MCP_REQUIRE_ELICITATION=1. Nothing sent.")
        try:  # re-verify once more after the human's answer, then send
            _, ledger, service, sendable = eligible_rows(args)
            if binding_of(sendable, ledger) != bound:
                raise PermissionError("The drafts or recipients changed during confirmation. Nothing sent.")
            with gd.capture_json() as sink:
                summary = gd.run_send(sendable, ledger, service)
                gd.finish(args, "Send run", summary, sendable, ledger)
        finally:
            gd.release_locks()
        result = sink[-1] if sink else {"ok": True}
        result["confirmation"] = method
        return result
    except gd.ValidationError as ve:
        return {"ok": False, "validation_errors": ve.problems}
    except PermissionError as pe:
        return {"ok": False, "refused": str(pe)}
    except SystemExit as se:
        return {"ok": False, "error": str(se.code)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def main() -> None:
    gd.log.handlers.clear()  # stdout is the protocol channel; nothing may print there
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
