"""Offline tests: no network, no credentials, no real email."""
import base64
import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email import message_from_bytes, policy
from pathlib import Path
from unittest import mock

import gmail_drafts as gd


def make_pdf(path: Path, size: int = 200_000) -> bytes:
    """A PDF-looking file with every byte value in it, to catch encoding corruption."""
    body = bytes(range(256)) * (size // 256)
    data = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + body + b"\n%%EOF\n"
    path.write_bytes(data)
    return data


def write_csv(path: Path, rows):
    import csv
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["to", "subject", "body"])
        w.writerows(rows)


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__("fake")
        self.resp = type("R", (), {"status": status})()


class FakeGmail:
    """Mimics service.users().drafts().{create,get,send}(...).execute(num_retries=0)."""

    def __init__(self):
        self.store = {}
        self.sent = []
        self.create_calls = 0
        self.send_calls = 0
        self.fail_create = {}  # to-address -> exception
        self.fail_send = {}    # draft_id -> exception
        self._n = 0

    def users(self):
        return self

    class _Call:
        def __init__(self, fn, strict=True):
            self.fn = fn
            self.strict = strict

        def execute(self, num_retries=0):
            if self.strict:
                assert num_retries == 0, "must never auto-retry create/send"
            return self.fn()

    def drafts(self):  # noqa: F811 - attribute shadowing is intentional for the fluent API
        return _Drafts(self)


class _Drafts:
    def __init__(self, g):
        self.g = g

    def create(self, userId, body):
        def fn():
            self.g.create_calls += 1
            raw = base64.urlsafe_b64decode(body["message"]["raw"])
            msg = message_from_bytes(raw, policy=policy.default)
            to = msg["To"]
            if to in self.g.fail_create:
                raise self.g.fail_create[to]
            self.g._n += 1
            did = f"r{self.g._n}"
            self.g.store[did] = raw
            return {"id": did, "message": {"id": f"m{self.g._n}"}}
        return FakeGmail._Call(fn)

    def list(self, userId, maxResults=100, pageToken=None):
        def fn():
            return {"drafts": [{"id": d} for d in self.g.store]}
        return FakeGmail._Call(fn, strict=False)

    def get(self, userId, id, format):
        def fn():
            if id not in self.g.store:
                raise FakeHttpError(404)
            if format == "metadata":
                msg = message_from_bytes(self.g.store[id], policy=policy.default)
                hdrs = [{"name": k, "value": v} for k, v in msg.items()]
                return {"id": id, "message": {"id": "m-" + id, "payload": {"headers": hdrs}}}
            return {"id": id}
        return FakeGmail._Call(fn, strict=False)  # reads may retry

    def send(self, userId, body):
        def fn():
            self.g.send_calls += 1
            did = body["id"]
            if did in self.g.fail_send:
                raise self.g.fail_send[did]
            self.g.store.pop(did)
            self.g.sent.append(did)
            return {"id": f"sent-{did}", "threadId": f"t-{did}"}
        return FakeCall(fn)


FakeCall = FakeGmail._Call


class Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"GMAIL_DRAFTS_ALLOW_YES": "1"})  # tests opt in; production default is off
        env.start()
        os.environ.pop("GMAIL_DRAFTS_ATTACH_ROOTS", None)  # a dev shell may set it; tests use temp dirs
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pdf_path = self.dir / "One Pager – Résumé.pdf"  # non-ASCII filename on purpose
        self.pdf_bytes = make_pdf(self.pdf_path)
        self.csv = self.dir / "r.csv"
        write_csv(self.csv, [
            ["a@example.com", "Hello A", "Hi A,\n\nLine two with ünïcode.\n\nThanks"],
            ["b@example.com", "Hello B, comma", "Hi B"],
            ["c@example.com", "Hello C", "Hi C"],
        ])
        self.state = self.dir / "state"
        self.pdf = gd.load_pdf(self.pdf_path)
        self.rows = gd.load_rows(self.csv, self.pdf)
        gd.setup_logging(self.state)

    def tearDown(self):
        for h in list(gd.log.handlers):
            h.close()
        self.tmp.cleanup()

    def ledger(self):
        return gd.Ledger(self.state / "ledger.jsonl")

    def cli(self, *args, svc=None, expect_stdin=None):
        out, err = io.StringIO(), io.StringIO()
        factory = (lambda: svc) if svc is not None else (lambda: (_ for _ in ()).throw(AssertionError("service must not be created")))
        with redirect_stdout(out), redirect_stderr(err):
            code = gd.main(["--state-dir", str(self.state), *args], service_factory=factory)
        return code, out.getvalue(), err.getvalue()


class MimeTests(Base):
    def test_attachment_integrity_and_headers(self):
        r = self.rows[0]
        msg = gd.build_message(r.to, r.subject, r.body, self.pdf)
        wire = base64.urlsafe_b64decode(gd.encode_raw(msg))  # what Gmail receives
        parsed = message_from_bytes(wire, policy=policy.default)
        self.assertEqual(parsed["To"], r.to)
        self.assertEqual(parsed["Subject"], r.subject)
        atts = list(parsed.iter_attachments())
        self.assertEqual(len(atts), 1)
        a = atts[0]
        self.assertEqual(a.get_content_type(), "application/pdf")
        self.assertEqual(a.get_filename(), "One Pager – Résumé.pdf")
        self.assertEqual(a.get_content_disposition(), "attachment")
        payload = a.get_payload(decode=True)
        self.assertEqual(payload, self.pdf_bytes)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), self.pdf.sha256)
        body = parsed.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("ünïcode", body)

    def test_each_message_has_its_own_body_same_pdf(self):
        sums = set()
        for r in self.rows:
            m = message_from_bytes(gd.build_message(r.to, r.subject, r.body, self.pdf).as_bytes(), policy=policy.default)
            sums.add(hashlib.sha256(next(m.iter_attachments()).get_payload(decode=True)).hexdigest())
            self.assertEqual(m["To"], r.to)
        self.assertEqual(sums, {self.pdf.sha256})

    def test_pdf_read_once(self):
        # load_pdf returns bytes held in memory; deleting the file must not matter afterwards.
        self.pdf_path.unlink()
        m = gd.build_message("x@example.com", "s", "b", self.pdf)
        self.assertEqual(next(m.iter_attachments()).get_payload(decode=True), self.pdf_bytes)


class ValidationTests(Base):
    def test_bad_pdf(self):
        bad = self.dir / "x.pdf"
        bad.write_bytes(b"not a pdf")
        with self.assertRaises(gd.ValidationError):
            gd.load_pdf(bad)
        with self.assertRaises(gd.ValidationError):
            gd.load_pdf(self.dir / "missing.pdf")

    def test_csv_problems_all_reported(self):
        write_csv(self.csv, [
            ["not-an-email", "S", "B"],
            ["ok@example.com", "Bad\nSubject", "B"],
            ["ok2@example.com", "S", "   "],
            ["ok3@example.com", "S", "B"],
            ["ok3@example.com", "S", "B"],
            ["inject@example.com\nBcc: evil@example.com", "S", "B"],
        ])
        with self.assertRaises(gd.ValidationError) as cm:
            gd.load_rows(self.csv, self.pdf)
        text = "\n".join(cm.exception.problems)
        for needle in ("invalid 'to'", "line break", "empty body", "duplicate"):
            self.assertIn(needle, text)
        self.assertGreaterEqual(len(cm.exception.problems), 5)

    def test_missing_column(self):
        self.csv.write_text("to,subject\na@example.com,hi\n")
        with self.assertRaises(gd.ValidationError):
            gd.load_rows(self.csv, self.pdf)

    def test_example_csv_is_valid(self):
        rows = gd.load_rows(Path(__file__).with_name("recipients.example.csv"), self.pdf)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r.to.endswith(("@example.com", "@example.org")) for r in rows))


class DraftTests(Base):
    def test_creates_drafts_once_and_rerun_is_noop(self):
        g = FakeGmail()
        s1 = gd.run_draft(self.rows, self.pdf, self.ledger(), g, pause=0)
        self.assertEqual(s1["drafted"], 3)
        self.assertEqual(g.create_calls, 3)
        self.assertEqual(g.send_calls, 0)  # drafting never sends
        s2 = gd.run_draft(self.rows, self.pdf, self.ledger(), g, pause=0)  # fresh Ledger = reload from disk
        self.assertEqual(s2["drafted"], 0)
        self.assertEqual(s2["skipped"], 3)
        self.assertEqual(g.create_calls, 3)
        # drafts carry the real attachment
        for raw in g.store.values():
            m = message_from_bytes(raw, policy=policy.default)
            self.assertEqual(next(m.iter_attachments()).get_payload(decode=True), self.pdf_bytes)

    def test_definite_failure_retried_uncertain_not(self):
        g = FakeGmail()
        g.fail_create["a@example.com"] = FakeHttpError(400)       # definite
        g.fail_create["b@example.com"] = TimeoutError()           # uncertain
        s = gd.run_draft(self.rows, self.pdf, self.ledger(), g, pause=0)
        self.assertEqual(s["drafted"], 1)
        self.assertEqual(s["failed"], 2)
        self.assertEqual(s["needs_review"], 1)
        led = self.ledger()
        self.assertEqual(led.status(self.rows[0].key), gd.DRAFT_FAILED)
        self.assertEqual(led.status(self.rows[1].key), gd.DRAFT_UNKNOWN)
        g.fail_create.clear()
        s2 = gd.run_draft(self.rows, self.pdf, self.ledger(), g, pause=0)
        self.assertEqual(s2["drafted"], 1)  # only the definite failure is retried
        self.assertEqual(self.ledger().status(self.rows[1].key), gd.DRAFT_UNKNOWN)  # still flagged
        self.assertEqual(g.create_calls, 3 + 1)

    def test_changed_pdf_gets_new_keys(self):
        k1 = self.rows[0].key
        self.pdf_path.write_bytes(self.pdf_bytes + b"\n% v2\n")
        pdf2 = gd.load_pdf(self.pdf_path)
        self.assertNotEqual(gd.load_rows(self.csv, pdf2)[0].key, k1)

    def test_stops_after_consecutive_failures(self):
        g = FakeGmail()
        for r in self.rows:
            g.fail_create[r.to] = FakeHttpError(403)
        gd.run_draft(self.rows, self.pdf, self.ledger(), g, pause=0, max_consecutive_failures=2)
        self.assertEqual(g.create_calls, 2)


class SendTests(Base):
    def setUp(self):
        super().setUp()
        self.g = FakeGmail()
        gd.run_draft(self.rows, self.pdf, self.ledger(), self.g, pause=0)

    def test_send_once_and_rerun_noop(self):
        s = gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(s["sent"], 3)
        self.assertEqual(self.g.send_calls, 3)
        s2 = gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(s2["sent"], 0)
        self.assertEqual(self.g.send_calls, 3)
        ev = self.ledger().event(self.rows[0].key)
        self.assertEqual(ev["status"], gd.SENT)
        self.assertTrue(ev["message_id"].startswith("sent-"))

    def test_uncertain_send_flagged_never_retried(self):
        did = self.ledger().event(self.rows[0].key)["draft_id"]
        self.g.fail_send[did] = TimeoutError()
        gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(self.ledger().status(self.rows[0].key), gd.SEND_UNKNOWN)
        calls = self.g.send_calls
        self.g.fail_send.clear()
        s = gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(self.g.send_calls, calls)  # the uncertain row was NOT re-sent
        self.assertEqual(s["needs_review"], 1)

    def test_definite_send_failure_is_retryable(self):
        did = self.ledger().event(self.rows[1].key)["draft_id"]
        self.g.fail_send[did] = FakeHttpError(403)
        gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(self.ledger().status(self.rows[1].key), gd.SEND_FAILED)
        self.g.fail_send.clear()
        s = gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(s["sent"], 1)

    def test_crash_mid_send_treated_as_uncertain(self):
        led = self.ledger()
        k = self.rows[0].key
        led.record(k, gd.SENDING)  # simulate process dying right after the write-ahead marker
        self.assertEqual(self.ledger().status(k), gd.SEND_UNKNOWN)
        before = self.g.send_calls
        gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        # rows 1 and 2 go out; row 0 does not
        self.assertEqual(self.g.send_calls, before + 2)

    def test_deleted_draft_not_sent(self):
        did = self.ledger().event(self.rows[2].key)["draft_id"]
        del self.g.store[did]  # user deleted it while reviewing
        s = gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(s["sent"], 2)
        self.assertEqual(self.ledger().status(self.rows[2].key), gd.DRAFT_MISSING)

    def test_resolve(self):
        k = self.rows[0].key
        self.ledger().record(k, gd.SEND_UNKNOWN)
        code, _, _ = self.cli("resolve", "--key", k[:10], "--as", "sent")
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger().status(k), gd.SENT)


class CliTests(Base):
    def test_dry_run_no_side_effects_no_service(self):
        code, out, _ = self.cli("dry-run", "--csv", str(self.csv), "--pdf", str(self.pdf_path))
        self.assertEqual(code, 0)
        self.assertIn("a@example.com", out)
        self.assertIn("Hello A", out)
        self.assertIn("no drafts were created", out)
        self.assertFalse((self.state / "ledger.jsonl").exists())

    def test_dry_run_reports_validation_errors_exit_2(self):
        self.csv.write_text("to,subject,body\nbad,,\n")
        code, _, err = self.cli("dry-run", "--csv", str(self.csv), "--pdf", str(self.pdf_path))
        self.assertEqual(code, 2)
        self.assertIn("invalid 'to'", err)

    def test_draft_command_creates_drafts_only(self):
        g = FakeGmail()
        code, out, _ = self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        self.assertEqual(code, 0)
        self.assertEqual((g.create_calls, g.send_calls), (3, 0))

    def test_send_requires_confirmation_non_interactive(self):
        g = FakeGmail()
        self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        with self.assertRaises(SystemExit):
            self.cli("send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        self.assertEqual(g.send_calls, 0)
        code, _, _ = self.cli("send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), "--yes", svc=g)
        self.assertEqual(code, 0)
        self.assertEqual(g.send_calls, 3)

    def test_send_count_excludes_deleted_drafts(self):
        g = FakeGmail()
        self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        g.store.pop(next(iter(g.store)))  # user deletes one draft in Gmail
        with self.assertRaises(SystemExit):
            self.cli("send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        out = self.cli("send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), "--yes", svc=g)[1]
        self.assertIn("About to SEND 2 ", out)
        self.assertEqual(g.send_calls, 2)

    def test_limit_trial_run(self):
        g = FakeGmail()
        self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), "--limit", "1", svc=g)
        self.assertEqual(g.create_calls, 1)


class HardeningTests(Base):
    def draft_all(self, g):
        return gd.run_draft(self.rows, self.pdf, self.ledger(), g, pause=0)

    def test_marker_header_on_every_message(self):
        g = FakeGmail()
        self.draft_all(g)
        for did, raw in g.store.items():
            self.assertEqual(message_from_bytes(raw, policy=policy.default)[gd.MARKER_HEADER], self.ledger().event(
                next(k for k, e in self.ledger().latest.items() if e["draft_id"] == did))["key"])

    def test_http_429_stops_run_and_is_definite(self):
        g = FakeGmail()
        for r in self.rows:
            g.fail_create[r.to] = FakeHttpError(429)
        s = self.draft_all(g)
        self.assertEqual(g.create_calls, 1)  # stopped after the first 429, did not hammer
        self.assertEqual(self.ledger().status(self.rows[0].key), gd.DRAFT_FAILED)
        self.assertEqual(s["needs_review"], 0)

    def test_http_408_is_uncertain(self):
        self.assertEqual(gd.classify_error(FakeHttpError(408)), (False, "HTTP 408"))
        self.assertEqual(gd.classify_error(FakeHttpError(404)), (True, "HTTP 404"))

    def test_oversize_message_never_reaches_gmail(self):
        g = FakeGmail()
        old = gd.MAX_MESSAGE_BYTES
        gd.MAX_MESSAGE_BYTES = 100
        try:
            s = self.draft_all(g)
        finally:
            gd.MAX_MESSAGE_BYTES = old
        self.assertEqual(g.create_calls, 0)
        self.assertEqual(s["failed"], 3)

    def test_max_per_run_cap(self):
        g = FakeGmail()
        code, out, _ = self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path),
                                "--max-per-run", "2", svc=g)
        self.assertEqual(g.create_calls, 2)
        self.assertIn("not attempted", out)
        self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), "--max-per-run", "2", svc=g)
        self.assertEqual(g.create_calls, 3)

    def test_second_concurrent_run_is_refused(self):
        gd.acquire_lock(self.state)
        try:
            with self.assertRaises(SystemExit) as cm:
                gd.acquire_lock(self.state)
            self.assertIn("Another", str(cm.exception))
        finally:
            gd.release_locks()
        gd.acquire_lock(self.state)  # free again after release
        gd.release_locks()

    def test_reconcile_finds_unknown_draft_via_marker(self):
        g = FakeGmail()
        self.draft_all(g)
        key = self.rows[1].key
        led = self.ledger()
        did = led.event(key)["draft_id"]
        led.record(key, gd.DRAFT_UNKNOWN, note="TimeoutError")  # simulate: create timed out but Gmail made it
        s = gd.run_reconcile(self.rows, self.ledger(), g, min_age=0)
        self.assertEqual(s["drafted"], 1)
        ev = self.ledger().event(key)
        self.assertEqual((ev["status"], ev["draft_id"]), (gd.DRAFTED, did))

    def test_reconcile_not_found_stays_flagged(self):
        g = FakeGmail()
        led = self.ledger()
        led.record(self.rows[0].key, gd.DRAFT_UNKNOWN, to=self.rows[0].to, note="TimeoutError")
        s = gd.run_reconcile(self.rows, self.ledger(), g, min_age=0)
        self.assertEqual(s["not_found"], 1)
        self.assertEqual(self.ledger().status(self.rows[0].key), gd.DRAFT_UNKNOWN)

    def test_reconcile_send_unknown(self):
        g = FakeGmail()
        self.draft_all(g)
        gd.run_send(self.rows, self.ledger(), g, pause=0)
        k_gone, k_alive = self.rows[0].key, self.rows[1].key
        # row 1: send "timed out" but the draft is still there -> not sent
        did = self.ledger().event(k_alive)["draft_id"]
        g.store[did] = b"To: x@example.com\r\n\r\nx"
        led = self.ledger()
        led.record(k_alive, gd.SEND_UNKNOWN, note="TimeoutError")
        led.record(k_gone, gd.SEND_UNKNOWN, note="TimeoutError")  # draft gone -> probably sent
        s = gd.run_reconcile(self.rows, self.ledger(), g, min_age=0)
        self.assertEqual(self.ledger().status(k_alive), gd.DRAFTED)
        self.assertEqual(self.ledger().status(k_gone), gd.SEND_UNKNOWN)
        self.assertEqual((s["drafted"], s["still_flagged"]), (1, 1))

    def test_reconcile_respects_min_age(self):
        g = FakeGmail()
        self.draft_all(g)
        led = self.ledger()
        led.record(self.rows[0].key, gd.SEND_UNKNOWN, note="x")
        s = gd.run_reconcile(self.rows, self.ledger(), g, min_age=3600)
        self.assertEqual(s["too_recent"], 1)

    def test_no_token_is_actionable_and_never_opens_browser(self):
        old_path = gd.TOKEN_PATH
        gd.TOKEN_PATH = self.dir / "nope.json"
        try:
            with self.assertRaises(SystemExit) as cm:
                gd.get_service()
        finally:
            gd.TOKEN_PATH = old_path
        self.assertIn("auth", str(cm.exception))


class Step3Tests(Base):
    def rows_from(self, text, **kw):
        self.csv.write_text(text, encoding="utf-8")
        return gd.load_rows(self.csv, kw.pop("atts", self.pdf), **kw)

    def test_optional_address_columns_and_headers(self):
        rows = self.rows_from("to,subject,body,cc,bcc,reply_to,from\n"
                              "a@example.com,S,B,c1@example.com; c2@example.com,b@example.com,r@example.com,me@example.com\n")
        r = rows[0]
        m = message_from_bytes(gd.build_message(r.to, r.subject, r.body, r.attachments, cc=r.cc, bcc=r.bcc,
                                                reply_to=r.reply_to, sender=r.sender).as_bytes(), policy=policy.default)
        self.assertEqual(m["Cc"], "c1@example.com, c2@example.com")
        self.assertEqual((m["Bcc"], m["Reply-To"], m["From"]), ("b@example.com", "r@example.com", "me@example.com"))

    def test_address_columns_reject_injection(self):
        for col in ("cc", "bcc", "reply_to", "from"):
            with self.assertRaises(gd.ValidationError, msg=col) as cm:
                self.rows_from(f'to,subject,body,{col}\na@example.com,S,B,"x@example.com\nBcc: evil@example.com"\n')
            self.assertIn(f"'{col}'", str(cm.exception))

    def test_cc_changes_key_plain_rows_keep_old_key(self):
        plain = self.rows_from("to,subject,body\na@example.com,S,B\n")[0]
        self.assertEqual(plain.key, gd.row_key("a@example.com", "S", "B", self.pdf.sha256))
        with_cc = self.rows_from("to,subject,body,cc\na@example.com,S,B,c@example.com\n")[0]
        self.assertNotEqual(plain.key, with_cc.key)

    def test_template_fills_and_reports_missing(self):
        rows = self.rows_from("to,subject,first_name\na@example.com,Hi {{first_name}},Ann\n",
                              template="Dear {{ first_name }},\nbye")
        self.assertEqual((rows[0].subject, rows[0].body), ("Hi Ann", "Dear Ann,\nbye"))
        with self.assertRaises(gd.ValidationError) as cm:
            self.rows_from("to,subject,first_name\na@example.com,S,Ann\n", template="Hi {{nickname}}")
        self.assertIn("{{nickname}}", str(cm.exception))
        with self.assertRaises(gd.ValidationError) as cm:
            self.rows_from("to,subject,first_name\na@example.com,S,\n", template="Hi {{first_name}}")
        self.assertIn("empty", str(cm.exception))

    def test_template_value_cannot_inject_headers_or_be_reexpanded(self):
        with self.assertRaises(gd.ValidationError) as cm:
            self.rows_from('to,subject,n\na@example.com,Hi {{n}},"x\nBcc: evil@example.com"\n', template="b")
        self.assertIn("line break", str(cm.exception))
        rows = self.rows_from("to,subject,n,m\na@example.com,S,{{m}},SECRET\n", template="v={{n}}")
        self.assertEqual(rows[0].body, "v={{m}}")  # single pass: inserted text is never re-expanded

    def test_unused_columns_warn_or_fail_strict(self):
        warns: list = []
        self.rows_from("to,subject,n,extra\na@example.com,S,x,y\n", template="{{n}}", warnings=warns)
        self.assertTrue(any("extra" in w for w in warns))
        with self.assertRaises(gd.ValidationError):
            self.rows_from("to,subject,n,extra\na@example.com,S,x,y\n", template="{{n}}", strict_fields=True)

    def test_html_alternative_is_escaped_and_attachment_kept(self):
        rows = self.rows_from("to,subject,n\na@example.com,S,<b>x</b>\n", template="plain {{n}}",
                              html_template="<p>{{n}}</p>")
        r = rows[0]
        m = message_from_bytes(gd.build_message(r.to, r.subject, r.body, r.attachments, html=r.html).as_bytes(),
                               policy=policy.default)
        self.assertEqual(m.get_content_type(), "multipart/mixed")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", m.get_body(preferencelist=("html",)).get_content())
        self.assertIn("plain <b>x</b>", m.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(len(list(m.iter_attachments())), 1)

    def test_multiple_and_per_row_attachments_read_once(self):
        second = self.dir / "notes.txt"
        second.write_text("hello")
        self.csv.write_text("to,subject,body,attachments\na@example.com,S,B,\nb@example.com,S,B,notes.txt\n"
                            "c@example.com,S,B,notes.txt; notes.txt\n")
        reads = []
        orig = Path.read_bytes
        with mock.patch.object(Path, "read_bytes", lambda self_: (reads.append(self_.name), orig(self_))[1]):
            rows = gd.load_rows(self.csv, self.pdf)
        self.assertEqual([a.filename for a in rows[0].attachments], [self.pdf.filename])
        self.assertEqual([a.filename for a in rows[1].attachments], ["notes.txt"])
        self.assertEqual(reads.count("notes.txt"), 1)
        m = message_from_bytes(gd.build_message("a@example.com", "s", "b", rows[2].attachments).as_bytes(), policy=policy.default)
        self.assertEqual([p.get_content_type() for p in m.iter_attachments()], ["text/plain", "text/plain"])

    def test_secret_files_cannot_be_attached(self):
        for name in ("token.json", "client_secret_123.json", ".env", "id_rsa"):
            f = self.dir / name
            f.write_text("x")
            with self.assertRaises(gd.ValidationError, msg=name):
                gd.load_attachment(f)
        with mock.patch.object(gd, "APP_DIR", self.dir / "app"):
            (self.dir / "app").mkdir()
            (self.dir / "app" / "innocent.pdf").write_bytes(self.pdf_bytes)
            with self.assertRaises(gd.ValidationError):
                gd.load_attachment(self.dir / "app" / "innocent.pdf")
        with mock.patch.dict(os.environ, {"GMAIL_DRAFTS_ATTACH_ROOTS": str(self.dir / "only")}):
            with self.assertRaises(gd.ValidationError):
                gd.load_attachment(self.pdf_path)

    def test_report_export_csv_and_json(self):
        g = FakeGmail()
        self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        out = self.dir / "r.out.csv"
        self.cli("report", "--status", "drafted", "--out", str(out))
        import csv as _csv
        with out.open() as fh:
            rows = list(_csv.DictReader(fh))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["draft_id"] and r["status"] == "drafted" for r in rows))
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        _, o, _ = self.cli("--json", "report")
        import json as _json
        self.assertEqual(len(_json.loads(o)), 3)


class AgentSafetyTests(Base):
    def test_json_output_has_no_bodies_or_bytes(self):
        import json as _json
        g = FakeGmail()
        outs = []
        outs.append(self.cli("--json", "dry-run", "--csv", str(self.csv), "--pdf", str(self.pdf_path))[1])
        outs.append(self.cli("--json", "draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)[1])
        outs.append(self.cli("--json", "status", "--csv", str(self.csv), "--pdf", str(self.pdf_path))[1])
        outs.append(self.cli("--json", "send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), "--yes", svc=g)[1])
        for o in outs:
            doc = _json.loads(o)  # stdout is pure JSON (no log lines mixed in)
            self.assertIn("rows", doc)
            self.assertNotIn("ünïcode", o)
            self.assertNotIn("Line two", o)
            self.assertNotIn(base64.b64encode(self.pdf_bytes[:200]).decode(), o)
            self.assertNotIn(self.pdf_bytes[:20].decode("latin-1"), o)
        self.assertEqual({r["status"] for r in _json.loads(outs[3])["rows"]}, {"sent"})

    def test_json_validation_errors(self):
        import json as _json
        self.csv.write_text("to,subject,body\nbad,S,B\n")
        code, o, _ = self.cli("--json", "dry-run", "--csv", str(self.csv), "--pdf", str(self.pdf_path))
        self.assertEqual(code, 2)
        self.assertFalse(_json.loads(o)["ok"])

    def test_yes_is_disabled_by_default(self):
        g = FakeGmail()
        self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        with mock.patch.dict(os.environ, {"GMAIL_DRAFTS_ALLOW_YES": ""}):
            with self.assertRaises(SystemExit) as cm:
                self.cli("send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), "--yes", svc=g)
            self.assertIn("disabled", str(cm.exception))
            with self.assertRaises(SystemExit):  # no tty, no --yes: refused
                self.cli("send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
            with self.assertRaises(SystemExit):  # --json can never prompt
                self.cli("--json", "send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        self.assertEqual(g.send_calls, 0)


class FailedSendRecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.g = FakeGmail()
        gd.run_draft(self.rows, self.pdf, self.ledger(), self.g, pause=0)
        self.key = self.rows[0].key
        self.did = self.ledger().event(self.key)["draft_id"]
        self.g.fail_send[self.did] = FakeHttpError(400)
        gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(self.ledger().status(self.key), gd.SEND_FAILED)
        del self.g.fail_send[self.did]

    def test_failed_send_whose_draft_vanished_is_flagged_not_resent(self):
        self.g.store.pop(self.did)  # the "4xx" actually went through: a sent draft disappears
        before = self.g.send_calls
        ok = gd.verify_sendable(self.rows, self.ledger(), self.g)
        self.assertNotIn(self.key, [r.key for r in ok])
        self.assertEqual(self.ledger().status(self.key), gd.SEND_UNKNOWN)
        self.assertIn("may have been sent", self.ledger().event(self.key)["note"])
        gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(self.g.send_calls, before)

    def test_failed_send_with_intact_draft_is_retried(self):
        s = gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(self.ledger().status(self.key), gd.SENT)
        self.assertEqual(s["sent"], 1)

    def test_reconcile_flags_failed_send_with_missing_draft(self):
        self.g.store.pop(self.did)
        s = gd.run_reconcile(self.rows, self.ledger(), self.g, min_age=0)
        self.assertEqual(self.ledger().status(self.key), gd.SEND_UNKNOWN)
        self.assertEqual(s["needs_review"], 1)
        before = self.g.send_calls
        gd.run_send(self.rows, self.ledger(), self.g, pause=0)
        self.assertEqual(self.g.send_calls, before)

    def test_run_send_records_extra_fields_on_every_send_event(self):
        gd.run_send(self.rows, self.ledger(), self.g, pause=0, extra={"gate": "elicitation"})
        evs = [e for e in self.ledger().events if e["key"] == self.key][-2:]  # the retry's marker + result
        self.assertEqual([e["status"] for e in evs], [gd.SENDING, gd.SENT])
        self.assertTrue(all(e["gate"] == "elicitation" for e in evs))



class LeakTests(Base):
    def test_logs_and_ledger_contain_no_encoded_pdf(self):
        g = FakeGmail()
        self.cli("draft", "--csv", str(self.csv), "--pdf", str(self.pdf_path), svc=g)
        self.cli("send", "--csv", str(self.csv), "--pdf", str(self.pdf_path), "--yes", svc=g)
        for h in gd.log.handlers:
            h.flush()
        blob = (self.state / "gmail_drafts.log").read_text() + (self.state / "ledger.jsonl").read_text()
        for enc in (base64.b64encode(self.pdf_bytes[:300]).decode(),
                    base64.urlsafe_b64encode(self.pdf_bytes[:300]).decode()):
            self.assertNotIn(enc[:80], blob)
        self.assertLess(len(blob), 20_000)  # tiny: ids and statuses only
        self.assertNotIn("token", blob.lower())

    def test_ledger_file_mode(self):
        gd.run_draft(self.rows, self.pdf, self.ledger(), FakeGmail(), pause=0)
        mode = os.stat(self.state / "ledger.jsonl").st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
