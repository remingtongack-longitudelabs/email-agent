#!/usr/bin/env python3
"""Create personalized Gmail drafts with files attached, then send them in a separate, confirmed step.

Subcommands
-----------
  auth       One-time OAuth login (opens a browser; stores a token locally).
  dry-run    Validate CSV + attachments and preview. No network, no writes.
  draft      Create Gmail drafts (never sends). Safe to re-run: already-handled rows are skipped.
  status     Show the ledger state of every row in the batch.
  send       Send the drafts created by `draft` (explicit, separate, typed confirmation).
  reconcile  Auto-settle uncertain rows by looking in Gmail (read-only).
  resolve    Manually settle a row flagged for review after you checked Gmail.
  report     Export the ledger (CSV/JSON) so results can be written back to other systems.

Design notes
------------
* Each attachment is read from disk once; the same bytes are reused for every message.
* Auth uses the narrowest practical scope: gmail.compose (create/send drafts; no mailbox read access).
* Secrets (client secret, token) live in ~/.config/gmail-drafts (mode 0700/0600), never in the
  working directory, never logged, never printed, and the tool refuses to attach them.
* An append-only ledger (state/ledger.jsonl) records every operation. Each row is keyed by a hash of
  its content, so re-running never duplicates. One run per state dir at a time (file lock).
* Create/send calls are made with num_retries=0. If the outcome is uncertain (timeout, 5xx, 408,
  crash mid-send) the row is flagged for review and NOT retried automatically.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import fcntl
import hashlib
import html as html_lib
import json
import logging
import mimetypes
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
APP_DIR = Path(os.environ.get("GMAIL_DRAFTS_HOME", Path.home() / ".config" / "gmail-drafts"))
CLIENT_SECRET_PATH = Path(os.environ.get("GMAIL_CLIENT_SECRET", APP_DIR / "client_secret.json"))
TOKEN_PATH = APP_DIR / "token.json"
DEFAULT_STATE_DIR = Path(os.environ.get("GMAIL_DRAFTS_STATE", Path.cwd() / "state"))

REQUIRED_COLUMNS = ("to", "subject", "body")
MAX_MESSAGE_BYTES = 25 * 1024 * 1024  # Gmail's total message limit, measured on the encoded message
MAX_PDF_BYTES = 18 * 1024 * 1024  # base64 inflates ~4/3, so 18 MB of PDF stays under 25 MB encoded
DEFAULT_MAX_PER_RUN = 200  # cap on rows processed per draft/send run (override with --max-per-run)
READ_RETRIES = 5  # googleapiclient backoff for 429/5xx; used ONLY on read calls (get/list), never create/send
MARKER_HEADER = "X-Gmail-Drafts-Key"  # row key stamped on each message so draft_unknown rows can be found again
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Ledger statuses
DRAFTED, DRAFT_FAILED, DRAFT_UNKNOWN = "drafted", "draft_failed", "draft_unknown"
SENDING, SENT, SEND_FAILED, SEND_UNKNOWN = "sending", "sent", "send_failed", "send_unknown"
DRAFT_MISSING, CLEARED = "draft_missing", "cleared"
NEEDS_REVIEW = {DRAFT_UNKNOWN, SEND_UNKNOWN, DRAFT_MISSING}

log = logging.getLogger("gmail_drafts")


# --------------------------------------------------------------------------- models
@dataclass(frozen=True)
class Attachment:
    path: Path
    filename: str
    data: bytes
    sha256: str
    maintype: str = "application"
    subtype: str = "pdf"


Pdf = Attachment  # historical name


@dataclass(frozen=True)
class Row:
    line: int
    to: str
    subject: str
    body: str
    key: str
    attachments: tuple[Attachment, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: str = ""
    sender: str = ""
    html: str = ""


class ValidationError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------- input loading
RESERVED_COLUMNS = {"to", "subject", "body", "cc", "bcc", "reply_to", "from", "attachments"}
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Never attach credentials or other obvious secrets, even if a CSV (e.g. written by an agent) asks for them.
BLOCKED_NAME_RE = re.compile(r"(^|[._-])(token|client_secret|credentials?|secrets?|id_rsa|id_ed25519)([._-]|$)|^\.env|\.pem$|\.key$",
                             re.IGNORECASE)


def _blocked_dirs() -> list[Path]:
    home = Path.home()
    dirs = [APP_DIR, DEFAULT_STATE_DIR, home / ".ssh", home / ".aws", home / ".gnupg", home / ".config", home / ".kube"]
    return [d.resolve() for d in dirs]


def check_attachment_path(p: Path) -> list[str]:
    """Refuse secrets/credential locations; optionally enforce an allowlist (GMAIL_DRAFTS_ATTACH_ROOTS)."""
    real = p.resolve()
    problems = []
    if BLOCKED_NAME_RE.search(real.name) or BLOCKED_NAME_RE.search(p.name):
        problems.append(f"refusing to attach {p.name!r}: the name looks like a credential or secret")
    for d in _blocked_dirs():
        if real == d or d in real.parents:
            problems.append(f"refusing to attach {p.name!r}: it is inside a protected directory ({d.name})")
            break
    roots = os.environ.get("GMAIL_DRAFTS_ATTACH_ROOTS")
    if roots:
        allowed = [Path(x).expanduser().resolve() for x in roots.split(os.pathsep) if x]
        if not any(real == a or a in real.parents for a in allowed):
            problems.append(f"{p.name!r} is outside GMAIL_DRAFTS_ATTACH_ROOTS")
    return problems


def load_attachment(path: str | Path, *, cache: dict[Path, Attachment] | None = None) -> Attachment:
    """Read a file from disk exactly once (cached by resolved path) and describe it for MIME."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValidationError([f"Attachment not found: {p}"])
    real = p.resolve()
    if cache is not None and real in cache:
        return cache[real]
    problems = check_attachment_path(p)
    data = b"" if problems else p.read_bytes()
    if not problems:
        if not data:
            problems.append(f"Attachment is empty: {p}")
        if len(data) > MAX_PDF_BYTES:
            problems.append(f"Attachment {p.name!r} is {len(data):,} bytes; limit is {MAX_PDF_BYTES:,}")
        if p.suffix.lower() == ".pdf" and data and not data.lstrip().startswith(b"%PDF-"):
            problems.append(f"File does not look like a PDF (missing %PDF- header): {p}")
    if problems:
        raise ValidationError(problems)
    ctype, _ = mimetypes.guess_type(p.name)
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    att = Attachment(path=p, filename=p.name, data=data, sha256=hashlib.sha256(data).hexdigest(),
                     maintype=maintype, subtype=subtype)
    if cache is not None:
        cache[real] = att
    return att


def load_pdf(path: str | Path) -> Attachment:
    """Read a PDF from disk exactly once; must look like a PDF."""
    p = Path(path)
    if not p.is_file():
        raise ValidationError([f"PDF not found: {p}"])
    att = load_attachment(p)
    if not att.data.lstrip().startswith(b"%PDF-"):
        raise ValidationError([f"File does not look like a PDF (missing %PDF- header): {p}"])
    return att


def row_key(to: str, subject: str, body: str, pdf_sha256: str, extra: list | None = None) -> str:
    """Identity of a message. `extra` (cc/bcc/html/...) is mixed in only when present, so plain rows keep their keys."""
    parts: list = [to.lower(), subject, body, pdf_sha256]
    if extra:
        parts.append(extra)
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def _addr_list(value: str, label: str, problems: list[str]) -> tuple[str, ...]:
    out = []
    for a in re.split(r"[;,]", value):
        a = a.strip()
        if not a:
            continue
        if not EMAIL_RE.match(a):
            problems.append(f"invalid '{label}' address {a!r} (use bare addresses separated by ';')")
        else:
            out.append(a)
    return tuple(out)


def render(template: str, values: dict[str, str], used: set[str], problems: list[str], what: str,
           escape: bool = False) -> str:
    """Fill {{placeholders}} in one pass. Values are inserted literally (never re-expanded or evaluated)."""
    def sub(m):
        name = m.group(1).lower()
        used.add(name)
        if name not in values:
            problems.append(f"{what} uses {{{{{name}}}}} but the CSV has no '{name}' column")
            return m.group(0)
        v = values[name]
        if not v.strip():
            problems.append(f"{what} uses {{{{{name}}}}} but it is empty in this row")
        return html_lib.escape(v) if escape else v
    return PLACEHOLDER_RE.sub(sub, template)


def load_rows(csv_path: str | Path, attachments: Attachment | Sequence[Attachment], *,
              template: str | None = None, html_template: str | None = None,
              strict_fields: bool = False, warnings: list[str] | None = None) -> list[Row]:
    """Parse and validate the CSV. Raises ValidationError listing every problem.

    `attachments` are the defaults for every row; an `attachments` column (paths separated by ';',
    relative to the CSV's folder) replaces them for that row. Each file is read once.
    """
    p = Path(csv_path)
    if not p.is_file():
        raise ValidationError([f"CSV not found: {p}"])
    defaults = tuple([attachments] if isinstance(attachments, Attachment) else attachments)
    cache = {a.path.resolve(): a for a in defaults}
    problems: list[str] = []
    rows: list[Row] = []
    seen: dict[str, int] = {}
    used: set[str] = set()
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        required = ("to", "subject") if template is not None else REQUIRED_COLUMNS
        missing = [c for c in required if c not in header]
        if missing:
            raise ValidationError([f"CSV is missing required column(s): {', '.join(missing)} (found: {header})"])
        if len(set(header)) != len(header):
            raise ValidationError([f"CSV has duplicate column names (case-insensitive): {header}"])
        reader.fieldnames = header
        for rec in reader:
            line = reader.line_num  # last physical line of this record
            values = {k: (v or "").strip() for k, v in rec.items() if k}
            row_problems: list[str] = []
            to = values.get("to", "")
            subject = values.get("subject", "")
            if template is not None:
                body = render(template, values, used, row_problems, "template").strip("\r\n")
                html = render(html_template, values, used, row_problems, "html template", escape=True) \
                    if html_template is not None else ""
            else:
                body = (rec.get("body") or "").strip("\r\n")
                html = ""
            if PLACEHOLDER_RE.search(subject):
                subject = render(subject, values, used, row_problems, "subject")
            if not EMAIL_RE.match(to):
                row_problems.append(f"invalid 'to' address {to!r} (use a bare address, e.g. name@domain.com)")
            if not subject:
                row_problems.append("empty subject")
            elif re.search(r"[\r\n]", subject):
                row_problems.append("subject contains a line break")
            elif len(subject) > 900:
                row_problems.append("subject is too long")
            if not body.strip():
                row_problems.append("empty body")
            cc = _addr_list(values.get("cc", ""), "cc", row_problems)
            bcc = _addr_list(values.get("bcc", ""), "bcc", row_problems)
            reply_to = values.get("reply_to", "")
            if reply_to and not EMAIL_RE.match(reply_to):
                row_problems.append(f"invalid 'reply_to' address {reply_to!r}")
            sender = values.get("from", "")
            if sender and not EMAIL_RE.match(sender):
                row_problems.append(f"invalid 'from' address {sender!r} (must be a bare address of a Gmail send-as alias)")
            row_atts = defaults
            names = [x.strip() for x in values.get("attachments", "").split(";") if x.strip()]
            if names:
                try:
                    row_atts = tuple(load_attachment(p.parent / n, cache=cache) for n in names)
                except ValidationError as ve:
                    row_problems.extend(ve.problems)
            if row_problems:
                problems.extend(f"row ending at line {line}: {m}" for m in row_problems)
                continue
            extra = [list(cc), list(bcc), reply_to, sender, html] if (cc or bcc or reply_to or sender or html) else None
            key = row_key(to, subject, body, "+".join(a.sha256 for a in row_atts), extra)
            if key in seen:
                problems.append(f"row ending at line {line}: duplicate of the row ending at line {seen[key]} (same message)")
                continue
            seen[key] = line
            rows.append(Row(line=line, to=to, subject=subject, body=body, key=key, attachments=row_atts,
                            cc=cc, bcc=bcc, reply_to=reply_to, sender=sender, html=html))
    if not rows and not problems:
        problems.append("CSV has no data rows")
    extras = [h for h in header if h not in RESERVED_COLUMNS and h not in used]
    if extras and template is not None:
        msg = f"CSV column(s) not used by any template or subject: {', '.join(extras)}"
        if strict_fields:
            problems.append(msg)
        elif warnings is not None:
            warnings.append(msg)
    if problems:
        raise ValidationError(problems)
    return rows


# --------------------------------------------------------------------------- MIME
def build_message(to: str, subject: str, body: str, pdf: Attachment | Sequence[Attachment] | None = None,
                  key: str | None = None, *, cc: Sequence[str] = (), bcc: Sequence[str] = (), reply_to: str = "",
                  sender: str = "", html: str = "") -> EmailMessage:
    """Plain-text body (+ optional HTML alternative) and each file as a real attachment (original filename)."""
    atts = [pdf] if isinstance(pdf, Attachment) else list(pdf or [])
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    if reply_to:
        msg["Reply-To"] = reply_to
    if sender:
        msg["From"] = sender
    msg["Subject"] = subject
    if key:
        msg[MARKER_HEADER] = key
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for a in atts:
        msg.add_attachment(a.data, maintype=a.maintype, subtype=a.subtype, filename=a.filename)
    return msg


def encode_raw(msg: EmailMessage) -> str:
    """Gmail API 'raw' payload: URL-safe base64 of the RFC 822 message."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# --------------------------------------------------------------------------- ledger
class Ledger:
    """Append-only JSONL log of every operation; latest event per key is the current state."""

    def __init__(self, path: Path):
        self.path = path
        self.events: list[dict] = []
        self.latest: dict[str, dict] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        raise SystemExit(f"Ledger {path} is corrupt at line {n}; fix or move it aside.")
                    self.events.append(ev)
                    self.latest[ev["key"]] = ev

    def status(self, key: str) -> str | None:
        ev = self.latest.get(key)
        if not ev:
            return None
        # A 'sending' marker with no later result means a previous run died mid-send.
        return SEND_UNKNOWN if ev["status"] == SENDING else ev["status"]

    def event(self, key: str) -> dict:
        return self.latest.get(key, {})

    def record(self, key: str, status: str, **fields) -> None:
        ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "key": key, "status": status}
        prev = self.latest.get(key, {})
        for carry in ("to", "subject", "draft_id"):  # keep identifying info across events
            if carry in prev and carry not in fields:
                ev[carry] = prev[carry]
        ev.update({k: v for k, v in fields.items() if v is not None})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.events.append(ev)
        self.latest[key] = ev

    def find_prefix(self, prefix: str) -> str:
        matches = [k for k in self.latest if k.startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(f"Key prefix {prefix!r} matched {len(matches)} rows; use a longer prefix.")
        return matches[0]


# --------------------------------------------------------------------------- error classification
def classify_error(exc: Exception) -> tuple[bool, str]:
    """Return (definite_failure, description).

    A 4xx HTTP response means Gmail rejected the request, so nothing happened
    (definite). Anything else (5xx, timeouts, connection resets, unknown errors)
    leaves the outcome uncertain. We never log the exception message itself.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status is not None and 400 <= status < 500 and status != 408:  # 408 (request timeout) is ambiguous
        return True, f"HTTP {status}"
    if status is not None:
        return False, f"HTTP {status}"
    return False, type(exc).__name__


def auth_hint(desc: str) -> str:
    if desc in ("HTTP 401", "HTTP 403"):
        return (" Likely an auth problem (revoked/expired token, or the scope is blocked by your admin):"
                " run `auth` again; see README > Troubleshooting.")
    return ""


# --------------------------------------------------------------------------- operations
def run_draft(rows: list[Row], pdf: Attachment | None, ledger: Ledger, service, *, limit: int | None = None,
              max_consecutive_failures: int = 3, pause: float = 0.2) -> Counter:
    summary: Counter = Counter()
    consecutive = 0
    done = 0
    for r in rows:
        st = ledger.status(r.key)
        if st not in (None, CLEARED, DRAFT_FAILED):
            tag = "NEEDS REVIEW" if st in NEEDS_REVIEW else "skip"
            log.info("%s %s [%s]: ledger status=%s", tag, r.to, r.key[:12], st)
            summary["skipped"] += 1
            if st in NEEDS_REVIEW:
                summary["needs_review"] += 1
            continue
        if limit is not None and done >= limit:
            summary["not_attempted"] += 1
            continue
        done += 1
        msg = build_message(r.to, r.subject, r.body, r.attachments or pdf, key=r.key, cc=r.cc, bcc=r.bcc,
                            reply_to=r.reply_to, sender=r.sender, html=r.html)
        raw = encode_raw(msg)  # never logged
        if len(raw) > MAX_MESSAGE_BYTES:
            ledger.record(r.key, DRAFT_FAILED, to=r.to, subject=r.subject, note="message exceeds 25 MB limit")
            log.error("DRAFT FAILED %s [%s]: encoded message is %d bytes (limit %d); not sent to Gmail",
                      r.to, r.key[:12], len(raw), MAX_MESSAGE_BYTES)
            summary["failed"] += 1
            continue
        try:
            resp = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute(num_retries=0)
        except Exception as exc:  # noqa: BLE001 - classified below
            definite, desc = classify_error(exc)
            if definite:
                ledger.record(r.key, DRAFT_FAILED, to=r.to, subject=r.subject, note=desc)
                log.error("DRAFT FAILED %s [%s]: %s (will be retried on re-run)", r.to, r.key[:12], desc)
                summary["failed"] += 1
            else:
                ledger.record(r.key, DRAFT_UNKNOWN, to=r.to, subject=r.subject, note=desc)
                log.error("DRAFT UNCERTAIN %s [%s]: %s. Check Gmail Drafts, then `resolve`. NOT retried.", r.to, r.key[:12], desc)
                summary["failed"] += 1
                summary["needs_review"] += 1
            consecutive += 1
            if desc == "HTTP 429":
                log.error("Rate limited by Gmail (HTTP 429); stopping the run. Wait a while, then re-run.")
                break
            if consecutive >= max_consecutive_failures:
                log.error("Stopping after %d consecutive failures.%s", consecutive, auth_hint(desc))
                break
            continue
        consecutive = 0
        draft_id = resp.get("id")
        ledger.record(r.key, DRAFTED, to=r.to, subject=r.subject, draft_id=draft_id,
                      message_id=(resp.get("message") or {}).get("id"), pdf_sha256="+".join(a.sha256 for a in r.attachments) or None)
        log.info("DRAFTED %s [%s] draft_id=%s", r.to, r.key[:12], draft_id)
        summary["drafted"] += 1
        time.sleep(pause)
    return summary


def gone_status(ledger: Ledger, key: str) -> tuple[str, str]:
    """Status for a row whose Gmail draft no longer exists. A sent draft disappears, so if an earlier send
    attempt returned a 4xx we cannot assume it failed: flag it as uncertain instead of 'deleted'."""
    if ledger.status(key) == SEND_FAILED:
        return SEND_UNKNOWN, ("an earlier send attempt got an HTTP 4xx and the draft is now gone: it may have "
                              "been sent. Check Sent, then `resolve`")
    return DRAFT_MISSING, "draft not found in Gmail (deleted?)"


def verify_sendable(rows: list[Row], ledger: Ledger, service) -> list[Row]:
    """Rows whose Gmail draft is confirmed to still exist (so the typed SEND <n> count is accurate).

    A 404 flags the row draft_missing; any other lookup error leaves it out of this run.
    """
    ok = []
    for r in rows:
        if ledger.status(r.key) not in (DRAFTED, SEND_FAILED):
            continue
        draft_id = ledger.event(r.key).get("draft_id")
        if not draft_id:
            ledger.record(r.key, DRAFT_MISSING, note="no draft id")
            log.error("NEEDS REVIEW %s [%s]: no draft id in ledger", r.to, r.key[:12])
            continue
        try:
            service.users().drafts().get(userId="me", id=draft_id, format="minimal").execute(num_retries=READ_RETRIES)
        except Exception as exc:  # noqa: BLE001
            _, desc = classify_error(exc)
            if desc == "HTTP 404":
                new_status, note = gone_status(ledger, r.key)
                ledger.record(r.key, new_status, note=note)
                log.error("NEEDS REVIEW %s [%s]: draft %s no longer exists; not sent", r.to, r.key[:12], draft_id)
            else:
                log.error("SKIPPED %s [%s]: could not verify draft (%s); nothing was sent", r.to, r.key[:12], desc)
            continue
        ok.append(r)
    return ok


def run_send(rows: list[Row], ledger: Ledger, service, *, limit: int | None = None,
             max_consecutive_failures: int = 3, pause: float = 0.5, extra: dict | None = None) -> Counter:
    """`extra` fields (e.g. which confirmation gate approved this send) are recorded on every send event."""
    extra = extra or {}
    summary: Counter = Counter()
    consecutive = 0
    done = 0
    for r in rows:
        st = ledger.status(r.key)
        if st not in (DRAFTED, SEND_FAILED):
            tag = "NEEDS REVIEW" if st in NEEDS_REVIEW else "skip"
            log.info("%s %s [%s]: ledger status=%s", tag, r.to, r.key[:12], st)
            summary["skipped"] += 1
            if st in NEEDS_REVIEW:
                summary["needs_review"] += 1
            continue
        if limit is not None and done >= limit:
            summary["not_attempted"] += 1
            continue
        done += 1
        draft_id = ledger.event(r.key).get("draft_id")
        if not draft_id:
            log.error("NEEDS REVIEW %s [%s]: no draft id in ledger", r.to, r.key[:12])
            ledger.record(r.key, DRAFT_MISSING, note="no draft id")
            summary["needs_review"] += 1
            continue
        # Confirm the draft still exists (you may have deleted it while reviewing).
        try:
            service.users().drafts().get(userId="me", id=draft_id, format="minimal").execute(num_retries=READ_RETRIES)
        except Exception as exc:  # noqa: BLE001
            definite, desc = classify_error(exc)
            if desc == "HTTP 404":
                new_status, note = gone_status(ledger, r.key)
                ledger.record(r.key, new_status, note=note)
                log.error("NEEDS REVIEW %s [%s]: draft %s no longer exists; not sent", r.to, r.key[:12], draft_id)
                summary["needs_review"] += 1
            else:
                log.error("SKIPPED %s [%s]: could not verify draft (%s); nothing was sent", r.to, r.key[:12], desc)
                summary["failed"] += 1
            consecutive += 1
            if consecutive >= max_consecutive_failures:
                log.error("Stopping after %d consecutive failures.", consecutive)
                break
            continue
        # Write-ahead marker: if we crash after this line, the row is treated as uncertain.
        ledger.record(r.key, SENDING, draft_id=draft_id, **extra)
        try:
            resp = service.users().drafts().send(userId="me", body={"id": draft_id}).execute(num_retries=0)
        except Exception as exc:  # noqa: BLE001
            definite, desc = classify_error(exc)
            if definite:
                ledger.record(r.key, SEND_FAILED, note=desc, **extra)
                log.error("SEND FAILED %s [%s]: %s (draft intact; can be re-run)", r.to, r.key[:12], desc)
                summary["failed"] += 1
            else:
                ledger.record(r.key, SEND_UNKNOWN, note=desc, **extra)
                log.error("SEND UNCERTAIN %s [%s]: %s. Check your Sent folder, then `resolve`. NOT retried.",
                          r.to, r.key[:12], desc)
                summary["failed"] += 1
                summary["needs_review"] += 1
            consecutive += 1
            if desc == "HTTP 429":
                log.error("Rate limited by Gmail (HTTP 429); stopping the run. Wait a while, then re-run.")
                break
            if consecutive >= max_consecutive_failures:
                log.error("Stopping after %d consecutive failures.%s", consecutive, auth_hint(desc))
                break
            continue
        consecutive = 0
        ledger.record(r.key, SENT, message_id=resp.get("id"), thread_id=resp.get("threadId"), **extra)
        log.info("SENT %s [%s] message_id=%s thread_id=%s", r.to, r.key[:12], resp.get("id"), resp.get("threadId"))
        summary["sent"] += 1
        time.sleep(pause)
    return summary


# --------------------------------------------------------------------------- auth
INTERACTIVE_AUTH = False  # only the `auth` subcommand may open a browser


def get_service():
    """Build a Gmail API client using a locally stored OAuth token (scope: gmail.compose)."""
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise SystemExit("Missing dependencies. Run: pip install -r requirements.txt")

    APP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except (ValueError, KeyError):
            log.warning("Saved token file is unreadable.")
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                log.warning("Saved token was rejected by Google.")
        if not refreshed:
            if not INTERACTIVE_AUTH:
                raise SystemExit(
                    "Not authorized, or the saved token expired or was revoked.\n"
                    "Run `python3 gmail_drafts.py auth` to log in again. (Apps left in 'Testing' mode with an "
                    "External consent screen expire tokens after 7 days; an Internal Workspace app does not.)"
                )
            if not CLIENT_SECRET_PATH.exists():
                raise SystemExit(
                    f"OAuth client file not found at {CLIENT_SECRET_PATH}.\n"
                    "See README.md > One-time setup (create a Desktop OAuth client and save the JSON there)."
                )
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
                creds = flow.run_local_server(port=0)
            except (ValueError, KeyError):
                raise SystemExit(f"{CLIENT_SECRET_PATH} is not a valid OAuth client file. "
                                 "Re-download it (credential type must be 'Desktop app').")
            except Exception as exc:  # noqa: BLE001 - never echo the exception text (may contain codes)
                raise SystemExit(
                    f"Authorization failed ({type(exc).__name__}). Common causes: you clicked Deny; your address "
                    "is not a Test user (External app); or your Workspace admin blocked this OAuth client or the "
                    "gmail.compose scope ('access blocked' / admin_policy_enforced), in which case ask the admin "
                    "to allow it."
                )
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        os.chmod(TOKEN_PATH, 0o600)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------- CLI
def setup_logging(state_dir: Path | None, console: bool = True) -> None:
    log.setLevel(logging.INFO)
    for h in list(log.handlers):  # a long-running process (the MCP server) must not leak file handles
        h.close()
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        log.addHandler(ch)
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(state_dir / "gmail_drafts.log", encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    log.propagate = False


def _human(args) -> bool:
    return not getattr(args, "json", False)


_JSON_SINK: list | None = None  # when set (by call_json/capture_json), JSON results are collected instead of printed


def print_json(payload: dict | list) -> None:
    """Machine-readable result. Only addresses, subjects, statuses, ids, sizes and hash prefixes; never bodies or bytes."""
    if _JSON_SINK is not None:
        _JSON_SINK.append(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False))


@contextlib.contextmanager
def capture_json():
    """Collect print_json() results in a list (used by the MCP server, whose stdout is the protocol channel)."""
    global _JSON_SINK
    sink: list = []
    prev, _JSON_SINK = _JSON_SINK, sink
    try:
        yield sink
    finally:
        _JSON_SINK = prev


def call_json(argv: list[str], service_factory: Callable | None = None) -> tuple[int, dict | list]:
    """Run one CLI command in --json mode in-process and return (exit code, parsed result)."""
    with capture_json() as sink:
        try:
            code = main(["--json", *argv], service_factory=service_factory or get_service)
        except SystemExit as exc:
            return 1, {"ok": False, "error": str(exc.code)}
    return code, (sink[-1] if sink else {"ok": code == 0})


def row_report(rows: list[Row], ledger: Ledger) -> list[dict]:
    out = []
    for r in rows:
        ev = ledger.event(r.key)
        item = {"key": r.key[:12], "to": r.to, "subject": r.subject, "status": ledger.status(r.key) or "new"}
        for k in ("draft_id", "message_id", "thread_id", "gate", "note"):
            if ev.get(k):
                item[k] = ev[k]
        out.append(item)
    return out


def finish(args, label: str, summary: Counter, rows: list[Row], ledger: Ledger) -> int:
    bad = bool(summary.get("failed", 0) or summary.get("needs_review", 0))
    if _human(args):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(summary.items())) or "nothing to do"
        print(f"\n{label}: {parts}")
        if summary.get("needs_review"):
            print("Some rows need manual review. Run the `status` command for details.")
    else:
        print_json({"command": args.cmd, "ok": not bad, "summary": dict(summary), "rows": row_report(rows, ledger)})
    return 1 if bad else 0


def print_summary(label: str, summary: Counter) -> int:  # kept for callers/tests
    parts = ", ".join(f"{k}={v}" for k, v in sorted(summary.items())) or "nothing to do"
    print(f"\n{label}: {parts}")
    bad = summary.get("failed", 0) or summary.get("needs_review", 0)
    if summary.get("needs_review"):
        print("Some rows need manual review. Run the `status` command for details.")
    return 1 if bad else 0


def load_batch(args) -> tuple[list[Attachment], list[Row], list[str]]:
    """Read every file once, render templates, validate everything. Raises ValidationError."""
    cache: dict[Path, Attachment] = {}
    defaults = [load_attachment(x, cache=cache) for x in (args.attach or [])]
    template = html_template = None
    problems: list[str] = []
    if args.template:
        tp = Path(args.template)
        template = tp.read_text(encoding="utf-8") if tp.is_file() else None
        if template is None:
            problems.append(f"Template not found: {tp}")
    if args.html_template:
        hp = Path(args.html_template)
        html_template = hp.read_text(encoding="utf-8") if hp.is_file() else None
        if html_template is None:
            problems.append(f"HTML template not found: {hp}")
        elif template is None:
            problems.append("--html-template needs --template (the plain-text body is always included)")
    if problems:
        raise ValidationError(problems)
    warnings: list[str] = []
    rows = load_rows(args.csv, defaults, template=template, html_template=html_template,
                     strict_fields=args.strict_fields, warnings=warnings)
    for r in rows:
        if not r.attachments and not args.no_attachment:
            problems.append(f"row ending at line {r.line}: no attachment (use --attach/--pdf, an 'attachments' "
                            "column, or --no-attachment)")
        elif sum(len(a.data) for a in r.attachments) * 4 // 3 > MAX_MESSAGE_BYTES:
            problems.append(f"row ending at line {r.line}: attachments too large for one message (25 MB limit)")
    if problems:
        raise ValidationError(problems)
    return defaults, rows, warnings


def cmd_dry_run(args) -> int:
    _, rows, warnings = load_batch(args)
    ledger = Ledger(Path(args.state_dir) / "ledger.jsonl")  # read-only
    report: list[dict] = []
    for r in rows:
        msg = build_message(r.to, r.subject, r.body, r.attachments, key=r.key, cc=r.cc, bcc=r.bcc,
                            reply_to=r.reply_to, sender=r.sender, html=r.html)  # proves it builds; contents not printed
        st = ledger.status(r.key)
        item: dict = {"key": r.key[:12], "to": r.to, "subject": r.subject, "size_bytes": len(msg.as_bytes()),
                "would": "create_draft" if st in (None, CLEARED, DRAFT_FAILED) else "skip",
                "ledger_status": st or "new",
                "attachments": [{"filename": a.filename, "bytes": len(a.data), "sha256_prefix": a.sha256[:16]}
                                for a in r.attachments]}
        for k, v in (("cc", r.cc), ("bcc", r.bcc), ("reply_to", r.reply_to), ("from", r.sender)):
            if v:
                item[k] = v if isinstance(v, str) else list(v)
        if r.html:
            item["html"] = True
        report.append(item)
    if not _human(args):
        print_json({"command": "dry-run", "ok": True, "rows": report, "warnings": warnings})
        return 0
    seen_files = {a.sha256: a for r in rows for a in r.attachments}
    for a in seen_files.values():
        print(f"File: {a.filename}  ({len(a.data):,} bytes, sha256 {a.sha256[:16]}...)  [{a.maintype}/{a.subtype}]")
    print(f"CSV : {args.csv}  ({len(rows)} valid rows)\n")
    for w in warnings:
        print(f"WARNING: {w}")
    width = max(len(r.to) for r in rows)
    for i, item in enumerate(report, 1):
        note = "new -> would create draft" if item["would"] == "create_draft" else f"already '{item['ledger_status']}' -> would skip"
        extras = "".join(f" [{k}]" for k in ("cc", "bcc", "reply_to", "from", "html") if k in item)
        print(f"{i:>3}. {item['to']:<{width}}  | {item['subject'][:60]!r:<64} | {item['size_bytes']:,} B | {note}{extras}")
    print("\nDry run only: no drafts were created and nothing was sent.")
    return 0


_LOCKS: list = []  # lock files held by this process; released when main() returns


def release_locks() -> None:
    while _LOCKS:
        _LOCKS.pop().close()


def acquire_lock(state_dir: Path) -> None:
    """One writer per state dir: a second concurrent run fails fast instead of double-processing."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = open(state_dir / ".lock", "w")  # noqa: SIM115 - held until process exit
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(f"Another gmail_drafts run is using {state_dir}. Wait for it to finish.")
    _LOCKS.append(fh)


def _batch(args):
    defaults, rows, _ = load_batch(args)
    state_dir = Path(args.state_dir)
    setup_logging(state_dir, console=_human(args))
    acquire_lock(state_dir)
    return defaults, rows, Ledger(state_dir / "ledger.jsonl")


def effective_limit(args) -> int:
    cap = getattr(args, "max_per_run", None) or DEFAULT_MAX_PER_RUN
    return min(args.limit, cap) if args.limit is not None else cap


def run_reconcile(rows: list[Row], ledger: Ledger, service, *, min_age: float = 120, max_scan: int = 1000) -> Counter:
    """Auto-settle uncertain rows using read-only lookups (works under gmail.compose).

    draft_unknown: scan recent drafts for the marker header stamped on every message.
    send_unknown : if the draft still exists the send did not happen; if it is gone it was sent
                   (or deleted by a human), so that case stays flagged for a person.
    """
    summary: Counter = Counter()
    now = datetime.now(timezone.utc)

    def old_enough(key: str) -> bool:
        ts = datetime.fromisoformat(ledger.event(key)["ts"])
        return (now - ts).total_seconds() >= min_age

    d_unknown = [r for r in rows if ledger.status(r.key) == DRAFT_UNKNOWN]
    if d_unknown:
        found: dict[str, dict] = {}
        scanned, token = 0, None
        while scanned < max_scan:
            resp = service.users().drafts().list(userId="me", maxResults=100, pageToken=token).execute(num_retries=READ_RETRIES)
            for d in resp.get("drafts", []):
                scanned += 1
                meta = service.users().drafts().get(userId="me", id=d["id"], format="metadata").execute(num_retries=READ_RETRIES)
                for h in (meta.get("message", {}).get("payload", {}) or {}).get("headers", []):
                    if h["name"].lower() == MARKER_HEADER.lower():
                        found[h["value"]] = {"draft_id": d["id"], "message_id": (meta.get("message") or {}).get("id")}
            token = resp.get("nextPageToken")
            if not token:
                break
        for r in d_unknown:
            if not old_enough(r.key):
                log.info("TOO RECENT %s [%s]: try again in a couple of minutes", r.to, r.key[:12])
                summary["too_recent"] += 1
            elif r.key in found:
                ledger.record(r.key, DRAFTED, **found[r.key], note="reconciled: draft found via marker header")
                log.info("RECONCILED %s [%s] as drafted (draft_id=%s)", r.to, r.key[:12], found[r.key]["draft_id"])
                summary["drafted"] += 1
            else:
                log.info("NOT FOUND %s [%s]: no draft carries its marker (scanned %d). It was most likely never "
                         "created; `resolve --as cleared` to retry.", r.to, r.key[:12], scanned)
                summary["not_found"] += 1
    for r in rows:  # a 4xx on send is only presumed to mean "not sent": if the draft is gone, flag it
        if ledger.status(r.key) != SEND_FAILED or not ledger.event(r.key).get("draft_id"):
            continue
        try:
            service.users().drafts().get(userId="me", id=ledger.event(r.key)["draft_id"], format="minimal").execute(
                num_retries=READ_RETRIES)
        except Exception as exc:  # noqa: BLE001
            if classify_error(exc)[1] == "HTTP 404":
                new_status, note = gone_status(ledger, r.key)
                ledger.record(r.key, new_status, note=note)
                log.error("NEEDS REVIEW %s [%s]: %s", r.to, r.key[:12], note)
                summary["needs_review"] += 1
    for r in rows:
        if ledger.status(r.key) != SEND_UNKNOWN:
            continue
        draft_id = ledger.event(r.key).get("draft_id")
        if not draft_id:
            summary["still_flagged"] += 1
            continue
        if not old_enough(r.key):
            log.info("TOO RECENT %s [%s]: try again in a couple of minutes", r.to, r.key[:12])
            summary["too_recent"] += 1
            continue
        try:
            service.users().drafts().get(userId="me", id=draft_id, format="minimal").execute(num_retries=READ_RETRIES)
        except Exception as exc:  # noqa: BLE001
            _, desc = classify_error(exc)
            if desc == "HTTP 404":
                log.info("GONE %s [%s]: draft no longer exists, so it was probably sent (or you deleted it). Check "
                         "Sent, then `resolve --as sent|cleared`.", r.to, r.key[:12])
            else:
                log.error("LOOKUP FAILED %s [%s]: %s", r.to, r.key[:12], desc)
            summary["still_flagged"] += 1
            continue
        ledger.record(r.key, DRAFTED, note="reconciled: draft still exists, so the send did not happen")
        log.info("RECONCILED %s [%s] as drafted (send did not happen)", r.to, r.key[:12])
        summary["drafted"] += 1
    return summary


def cmd_draft(args, service_factory) -> int:
    defaults, rows, ledger = _batch(args)
    log.info("Creating drafts for %d row(s); %d default attachment(s)", len(rows), len(defaults))
    summary = run_draft(rows, defaults[0] if defaults else None, ledger, service_factory(), limit=effective_limit(args))
    code = finish(args, "Draft run", summary, rows, ledger)
    if _human(args):
        if summary.get("not_attempted"):
            print(f"{summary['not_attempted']} row(s) not attempted (per-run cap {effective_limit(args)}); re-run to continue.")
        print("Review the drafts in Gmail > Drafts, then use the `send` command when ready.")
    return code


def cmd_status(args) -> int:
    _, rows, _ = load_batch(args)
    ledger = Ledger(Path(args.state_dir) / "ledger.jsonl")
    if not _human(args):
        print_json({"command": "status", "ok": True, "rows": row_report(rows, ledger)})
        return 0
    print(f"{'key':<13} {'status':<14} {'to':<35} ids / note")
    for r in rows:
        ev = ledger.event(r.key)
        st = ledger.status(r.key) or "(not yet drafted)"
        ids = " ".join(f"{k}={ev[k]}" for k in ("draft_id", "message_id") if ev.get(k))
        flag = "  <-- REVIEW" if st in NEEDS_REVIEW else ""
        print(f"{r.key[:12]:<13} {st:<14} {r.to:<35} {ids} {ev.get('note', '')}{flag}")
    return 0


def yes_allowed() -> bool:
    return os.environ.get("GMAIL_DRAFTS_ALLOW_YES") == "1"


def cmd_send(args, service_factory) -> int:
    if args.yes and not yes_allowed():
        raise SystemExit("--yes is disabled. Sending needs a human to type the confirmation at a terminal. "
                         "(Operators can opt in to unattended sends with GMAIL_DRAFTS_ALLOW_YES=1.)")
    if not args.yes and (not sys.stdin.isatty() or not _human(args)):
        raise SystemExit("Refusing to send without an interactive typed confirmation (run `send` yourself in a terminal).")
    defaults, rows, ledger = _batch(args)
    service = service_factory()
    sendable = verify_sendable(rows, ledger, service)
    sendable = sendable[: effective_limit(args)]
    if not sendable:
        if _human(args):
            print("No reviewed drafts are waiting to be sent for this batch.")
        else:
            print_json({"command": "send", "ok": True, "summary": {}, "rows": row_report(rows, ledger)})
        return 0
    if _human(args):
        print(f"About to SEND {len(sendable)} email(s) from your Gmail account:")
        for r in sendable:
            print(f"  - {r.to}  |  {r.subject[:70]}")
    expected = f"SEND {len(sendable)}"
    if not args.yes and input(f"\nType '{expected}' to confirm: ").strip() != expected:
        print("Aborted. Nothing was sent.")
        return 1
    summary = run_send(sendable, ledger, service)
    return finish(args, "Send run", summary, rows, ledger)


def cmd_reconcile(args, service_factory) -> int:
    _, rows, ledger = _batch(args)
    summary = run_reconcile(rows, ledger, service_factory())
    return finish(args, "Reconcile", summary, rows, ledger)


def cmd_report(args) -> int:
    """Export the ledger (latest state per message) as CSV/JSON, to write results back to other systems."""
    ledger = Ledger(Path(args.state_dir) / "ledger.jsonl")
    fields = ["key", "to", "subject", "status", "draft_id", "message_id", "thread_id", "gate", "ts", "note"]
    items = []
    for key, ev in ledger.latest.items():
        st = ledger.status(key)
        if args.status and st not in args.status:
            continue
        items.append({"key": key, **{f: ev.get(f, "") for f in fields[1:]}, "status": st})
    fmt = "json" if args.json else args.format
    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else sys.stdout  # noqa: SIM115
    try:
        if fmt == "json" and not args.out and _JSON_SINK is not None:
            print_json(items)
        elif fmt == "json":
            out.write(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
        else:
            w = csv.DictWriter(out, fieldnames=fields)
            w.writeheader()
            w.writerows(items)
    finally:
        if args.out:
            out.close()
            os.chmod(args.out, 0o600)
    return 0


def write_recipients_csv(path: str | Path, rows: Sequence[dict], columns: Sequence[str] | None = None) -> None:
    """Helper for programs that emit the recipient CSV (see README > Recipient source contract)."""
    cols = list(columns or dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def cmd_resolve(args) -> int:
    setup_logging(Path(args.state_dir), console=_human(args))
    acquire_lock(Path(args.state_dir))
    ledger = Ledger(Path(args.state_dir) / "ledger.jsonl")
    key = ledger.find_prefix(args.key)
    new_status = {"sent": SENT, "drafted": DRAFTED, "cleared": CLEARED}[args.as_]
    ledger.record(key, new_status, note=f"manually resolved as {args.as_}")
    log.info("RESOLVED %s as %s", key[:12], args.as_)
    if not _human(args):
        print_json({"command": "resolve", "ok": True, "key": key[:12], "status": new_status})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gmail_drafts.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="Where ledger and log live (default: ./state)")
    p.add_argument("--json", action="store_true", help="Machine-readable output (no message bodies or attachment bytes)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def batch_args(sp, limit=False):
        sp.add_argument("--csv", required=True, help="CSV with columns: to, subject, body (+ optional columns; see README)")
        sp.add_argument("--pdf", "--attach", dest="attach", action="append", metavar="FILE",
                        help="File attached to every email (repeatable). A row's 'attachments' column overrides it")
        sp.add_argument("--no-attachment", action="store_true", help="Allow rows that have no attachment")
        sp.add_argument("--template", help="Body template file with {{placeholders}} filled from CSV columns")
        sp.add_argument("--html-template", help="Also send an HTML body (multipart/alternative); needs --template")
        sp.add_argument("--strict-fields", action="store_true", help="Treat unused extra CSV columns as errors")
        if limit:
            sp.add_argument("--limit", type=int, help="Only process the first N eligible rows (good for a trial run)")
            sp.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN,
                            help=f"Hard cap on rows handled in one run (default {DEFAULT_MAX_PER_RUN}); re-run to continue")

    sub.add_parser("auth", help="One-time OAuth login")
    batch_args(sub.add_parser("dry-run", help="Validate and preview; no network, no writes"))
    batch_args(sub.add_parser("draft", help="Create Gmail drafts (never sends)"), limit=True)
    batch_args(sub.add_parser("status", help="Show ledger state for each row"))
    batch_args(sub.add_parser("reconcile", help="Auto-settle draft_unknown/send_unknown rows by looking in Gmail (read-only)"))
    sp = sub.add_parser("send", help="Send previously created drafts")
    batch_args(sp, limit=True)
    sp.add_argument("--yes", action="store_true",
                    help="Skip the typed confirmation. Disabled unless GMAIL_DRAFTS_ALLOW_YES=1 is set")
    sp = sub.add_parser("resolve", help="Settle a row flagged for review")
    sp.add_argument("--key", required=True, help="Row key or unique prefix (see `status`)")
    sp.add_argument("--as", dest="as_", required=True, choices=["sent", "drafted", "cleared"],
                    help="sent: it really went out. drafted: it did not, draft still exists. "
                         "cleared: forget it; the next `draft` run creates a fresh draft")
    sp = sub.add_parser("report", help="Export the ledger (sent/drafted/flagged rows with Gmail IDs) as CSV or JSON")
    sp.add_argument("--format", choices=["csv", "json"], default="csv")
    sp.add_argument("--status", action="append", help="Only these statuses (repeatable), e.g. --status sent")
    sp.add_argument("--out", help="Write to a file (mode 600) instead of stdout")
    return p


def main(argv=None, service_factory: Callable = get_service) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args, service_factory)
    finally:
        release_locks()


def _dispatch(args, service_factory: Callable) -> int:
    try:
        if args.cmd == "auth":
            global INTERACTIVE_AUTH
            INTERACTIVE_AUTH = True
            setup_logging(None)
            service_factory()
            print(f"Authorized. Token saved to {TOKEN_PATH} (mode 600).")
            return 0
        if args.cmd in ("dry-run", "status"):
            setup_logging(None, console=_human(args))
            return cmd_dry_run(args) if args.cmd == "dry-run" else cmd_status(args)
        if args.cmd == "report":
            return cmd_report(args)
        if args.cmd == "resolve":
            return cmd_resolve(args)
        if args.cmd in ("draft", "send", "reconcile"):
            return {"draft": cmd_draft, "send": cmd_send, "reconcile": cmd_reconcile}[args.cmd](args, service_factory)
    except ValidationError as ve:
        if getattr(args, "json", False):
            print_json({"command": args.cmd, "ok": False, "validation_errors": ve.problems})
        else:
            print("Validation failed; nothing was created or sent:", file=sys.stderr)
            for m in ve.problems:
                print(f"  - {m}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
