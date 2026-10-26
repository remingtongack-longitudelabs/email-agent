"""Offline tests for the MCP layer (fake Gmail, no network, no credentials)."""
import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import mcp_server as ms
except ModuleNotFoundError:  # the `mcp` package is optional for the CLI
    ms = None

import gmail_drafts as gd
from test_gmail_drafts import FakeGmail, FakeHttpError, make_pdf, write_csv

BODY_MARK = "ünïcode-body-marker"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeCtx:
    """Stands in for the FastMCP Context. `answer` is what the human 'types' (None = decline)."""

    def __init__(self, supports=True, answer=None, raises=False):
        caps = SimpleNamespace(elicitation=SimpleNamespace() if supports else None)
        self.request_context = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(capabilities=caps)))
        self.answer, self.raises, self.asked = answer, raises, []

    async def elicit(self, message, schema):
        self.asked.append(message)
        if self.raises:
            raise RuntimeError("boom")
        if self.answer is None:
            return SimpleNamespace(action="decline", data=None)
        return SimpleNamespace(action="accept", data=schema(recipient_count=self.answer))


@unittest.skipIf(ms is None, "mcp package not installed")
class McpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pdf_path = self.dir / "One Pager.pdf"
        self.pdf_bytes = make_pdf(self.pdf_path)
        self.csv = self.dir / "r.csv"
        write_csv(self.csv, [["a@example.com", "Hello A", f"Hi A {BODY_MARK}"],
                             ["b@example.com", "Hello B", "Hi B"],
                             ["c@example.com", "Hello C", "Hi C"]])
        self.g = FakeGmail()
        self.clock = FakeClock()
        patches = [mock.patch.object(ms, "STATE_ROOT", self.dir / "state"),
                   mock.patch.object(ms, "service_factory", lambda: self.g),
                   mock.patch.object(ms, "TOKENS", ms.SendTokens(clock=self.clock)),
                   mock.patch.dict(os.environ, {"GMAIL_DRAFTS_ALLOW_YES": ""})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gd.release_locks)
        self.outputs: list = []

    def kw(self, **extra):
        return {"csv": str(self.csv), "attachments": [str(self.pdf_path)], "batch": "t1", **extra}

    def call(self, name, **extra):
        res = getattr(ms, name)(**self.kw(**extra))
        self.outputs.append(res)
        return res

    def prepare(self, **extra):
        self.call("draft")
        return self.call("prepare_send", **extra)

    def send(self, token, count, ctx, **extra):
        res = asyncio.run(ms.send(ctx, token, count, **self.kw(**extra)))
        self.outputs.append(res)
        return res


class TokenTests(McpBase):
    def test_single_use_expiry_and_batch_binding(self):
        t = ms.TOKENS.issue("b", (("k", "d"),))
        self.assertEqual(ms.TOKENS.consume(t, "b"), (("k", "d"),))
        with self.assertRaises(PermissionError):
            ms.TOKENS.consume(t, "b")  # already used
        t = ms.TOKENS.issue("b", ())
        self.clock.t += ms.TOKEN_TTL_SECONDS + 1
        with self.assertRaises(PermissionError):
            ms.TOKENS.consume(t, "b")  # expired
        t = ms.TOKENS.issue("b", ())
        with self.assertRaises(PermissionError):
            ms.TOKENS.consume(t, "other")  # wrong batch
        with self.assertRaises(PermissionError):
            ms.TOKENS.consume(t, "b")  # a failed attempt burned it
        with self.assertRaises(PermissionError):
            ms.TOKENS.consume("made-up", "b")

    def test_outstanding_tokens_are_capped(self):
        for _ in range(ms.MAX_OUTSTANDING_TOKENS + 5):
            ms.TOKENS.issue("b", ())
        self.assertLessEqual(len(ms.TOKENS._tokens), ms.MAX_OUTSTANDING_TOKENS)


class ToolTests(McpBase):
    def test_tool_list_and_annotations(self):
        tools = {t.name: t for t in asyncio.run(ms.mcp.list_tools())}
        self.assertEqual(set(tools), {"dry_run", "draft", "status", "report", "reconcile", "prepare_send", "send"})
        self.assertTrue(tools["send"].annotations.destructiveHint)
        self.assertFalse(tools["send"].annotations.readOnlyHint)
        for name in ("dry_run", "status", "report"):
            self.assertTrue(tools[name].annotations.readOnlyHint, name)

    def test_dry_run_draft_status_report(self):
        self.assertTrue(self.call("dry_run")["ok"])
        self.assertEqual(self.g.create_calls, 0)
        d = self.call("draft")
        self.assertEqual(d["summary"], {"drafted": 3})
        self.assertEqual(self.call("draft")["summary"], {"skipped": 3})  # idempotent
        self.assertEqual({r["status"] for r in self.call("status")["rows"]}, {"drafted"})
        rep = ms.report(batch="t1", statuses=["drafted"])
        self.assertEqual(rep["count"], 3)
        self.assertEqual(self.g.send_calls, 0)  # nothing in these tools can send

    def test_bad_batch_name_and_validation_errors(self):
        self.assertIn("validation_errors", ms.dry_run(csv=str(self.csv), attachments=[str(self.pdf_path)], batch="../evil"))
        self.csv.write_text("to,subject,body\nnot-an-email,S,B\n")
        res = self.call("dry_run")
        self.assertFalse(res["ok"])
        self.assertIn("validation_errors", res)

    def test_credential_inputs_are_refused(self):
        secret = self.dir / "token.json"
        secret.write_text("{}")
        for kw in ({"attachments": [str(secret)]}, {"csv": str(secret)}, {"template": str(secret)}):
            res = self.call("dry_run", **kw)
            self.assertFalse(res["ok"], kw)
            self.assertIn("validation_errors", res)
        with mock.patch.object(gd, "APP_DIR", self.dir / "app"):
            (self.dir / "app").mkdir()
            inside = self.dir / "app" / "notes.pdf"
            inside.write_bytes(self.pdf_bytes)
            self.assertFalse(self.call("dry_run", attachments=[str(inside)])["ok"])

    def test_outputs_never_contain_bodies_bytes_or_secrets(self):
        token = self.prepare()["confirm_token"]
        self.send(token, 3, FakeCtx(supports=False))
        self.call("status")
        ms.report(batch="t1")
        blob = json.dumps(self.outputs, ensure_ascii=False)
        self.assertNotIn(BODY_MARK, blob)
        self.assertNotIn("Hi B", blob)
        for enc in (base64.b64encode(self.pdf_bytes[:200]).decode(), base64.urlsafe_b64encode(self.pdf_bytes[:200]).decode()):
            self.assertNotIn(enc, blob)
        self.assertNotIn(self.pdf_bytes[:16].decode("latin-1"), blob)
        self.assertNotIn("refresh_token", blob)
        self.assertNotIn("client_secret", blob)

    def test_nothing_written_to_stdout(self):  # stdout is the MCP protocol channel
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.call("dry_run")
            token = self.prepare()["confirm_token"]
            self.send(token, 3, FakeCtx(supports=False))
            self.call("status")
        self.assertEqual(buf.getvalue(), "")


class SendGateTests(McpBase):
    def test_prepare_lists_exact_recipients(self):
        self.csv.write_text("to,subject,body,cc,bcc\n"
                            "a@example.com,Hello A,Hi,c@example.com,b@example.com\n")
        p = self.prepare()
        self.assertEqual(p["count"], 1)
        r = p["recipients"][0]
        self.assertEqual((r["to"], r["cc"], r["bcc"], r["subject"]), ("a@example.com", ["c@example.com"],
                                                                      ["b@example.com"], "Hello A"))
        self.assertIn("One Pager.pdf", r["attachments"][0])
        self.assertEqual(self.g.send_calls, 0)

    def test_prepare_with_nothing_to_send_has_no_token(self):
        p = self.call("prepare_send")  # nothing drafted yet
        self.assertEqual(p["count"], 0)
        self.assertNotIn("confirm_token", p)

    def test_happy_path_then_token_cannot_be_replayed(self):
        p = self.prepare()
        res = self.send(p["confirm_token"], 3, FakeCtx(supports=False))
        self.assertEqual(res["summary"], {"sent": 3})
        self.assertEqual(res["confirmation"], "tool_permission_prompt")
        self.assertEqual(self.g.send_calls, 3)
        again = self.send(p["confirm_token"], 3, FakeCtx(supports=False))
        self.assertIn("refused", again)
        self.assertEqual(self.g.send_calls, 3)

    def test_wrong_count_refused_and_token_burned(self):
        p = self.prepare()
        res = self.send(p["confirm_token"], 2, FakeCtx(supports=False))
        self.assertIn("refused", res)
        self.assertEqual(self.g.send_calls, 0)
        self.assertIn("refused", self.send(p["confirm_token"], 3, FakeCtx(supports=False)))  # burned
        self.assertEqual(self.g.send_calls, 0)

    def test_missing_unknown_and_expired_tokens_refused(self):
        p = self.prepare()
        self.assertIn("refused", self.send("", 3, FakeCtx(supports=False)))
        self.assertIn("refused", self.send("guess", 3, FakeCtx(supports=False)))
        self.clock.t += ms.TOKEN_TTL_SECONDS + 1
        self.assertIn("refused", self.send(p["confirm_token"], 3, FakeCtx(supports=False)))
        self.assertEqual(self.g.send_calls, 0)

    def test_token_is_bound_to_the_exact_set(self):
        p = self.prepare()
        self.g.store.pop(next(iter(self.g.store)))  # a draft is deleted after prepare
        res = self.send(p["confirm_token"], 3, FakeCtx(supports=False))
        self.assertIn("changed", res["refused"])
        self.assertEqual(self.g.send_calls, 0)

    def test_token_cannot_be_used_with_a_different_limit(self):
        p = self.prepare()
        res = self.send(p["confirm_token"], 3, FakeCtx(supports=False), limit=2)
        self.assertIn("refused", res)
        self.assertEqual(self.g.send_calls, 0)

    def test_token_from_another_batch_refused(self):
        p = self.prepare()
        res = asyncio.run(ms.send(FakeCtx(supports=False), p["confirm_token"], 3, **{**self.kw(), "batch": "t2"}))
        self.assertIn("refused", res)
        self.assertEqual(self.g.send_calls, 0)

    def test_elicitation_confirm_sends(self):
        p = self.prepare()
        ctx = FakeCtx(supports=True, answer=3)
        res = self.send(p["confirm_token"], 3, ctx)
        self.assertEqual(res["confirmation"], "elicitation")
        self.assertEqual(self.g.send_calls, 3)
        self.assertEqual(len(ctx.asked), 1)
        for addr in ("a@example.com", "b@example.com", "c@example.com"):
            self.assertIn(addr, ctx.asked[0])
        self.assertNotIn(BODY_MARK, ctx.asked[0])

    def test_elicitation_decline_wrong_number_or_error_means_no_send(self):
        for ctx in (FakeCtx(answer=None), FakeCtx(answer=2), FakeCtx(raises=True)):
            p = self.prepare()
            res = self.send(p["confirm_token"], 3, ctx)
            self.assertIn("refused", res)
            self.assertEqual(self.g.send_calls, 0)

    def test_require_elicitation_env(self):
        p = self.prepare()
        with mock.patch.dict(os.environ, {"GMAIL_MCP_REQUIRE_ELICITATION": "1"}):
            res = self.send(p["confirm_token"], 3, FakeCtx(supports=False))
        self.assertIn("refused", res)
        self.assertEqual(self.g.send_calls, 0)

    def test_per_run_cap_and_rate_limit_stop_still_apply(self):
        p = self.prepare()
        ids = list(self.g.store)
        self.g.fail_send[ids[0]] = FakeHttpError(429)
        res = self.send(p["confirm_token"], 3, FakeCtx(supports=False))
        self.assertEqual(self.g.send_calls, 1)  # stopped at the first 429
        self.assertEqual(res["rows"][0]["status"], "send_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
