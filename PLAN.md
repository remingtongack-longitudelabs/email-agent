# gmail-drafts: Planner's plan (2026-10-26)

You're the build session for gmail-drafts (~/gmail-drafts). A planner reviewed BUILD_REPORT.md and produced the plan below. Read BUILD_REPORT.md, README.md and AGENT_BRIEF.md first, run the 69 tests to confirm they pass, then work through the plan in order.

## Standing rules (from the original brief, still binding)
- No email goes to anyone unless Remington has explicitly approved the exact recipient list in this session. So far everything has gone only to remington.gack@longitudelabs.com.
- Never print, log or paste credentials (client_secret.json, token.json), message bodies, or attachment base64. This includes your own one-off scripts.
- Don't widen the OAuth scope beyond gmail.compose without asking first and explaining why.
- Attachments stay real file attachments, never hosted links.
- Calendar, CRM writes, and anything other than drafting/sending email are out of scope unless Remington asks.
- Keep all 69 tests passing and add tests for anything you change.
- Nothing in this plan is pre-approved by the planner. Code changes in step 3 need Remington's go-ahead. Every send needs his approval of the exact recipient list.

## Plan, in order

1. Quick checks (Remington does these; ask him)
   - Is the Google Cloud OAuth consent screen Internal or External/Testing? External/Testing means the token expires every 7 days; if he's on Workspace, suggest switching to Internal.
   - Is `send` set to "Always allow" in Claude Desktop? It must be "Allow once"; "Always allow" lets the model fetch its own prepare_send token and removes the human gate.
   - Ask him which prompt appeared on the earlier Desktop send (typed-count elicitation or plain Allow button), if he remembers.

2. Baseline hygiene (small; ask him first for the git host and attachment folder(s))
   - Put ~/gmail-drafts under git with a private remote. Clear stray artifacts: state_test/, state_test2/, app_smoke.csv, test_attachment.pdf, __pycache__, .egg-info. Confirm .gitignore covers secrets, state, PDFs and real recipient lists before the first commit. Don't commit anything from ~/.config/gmail-drafts.
   - Set GMAIL_DRAFTS_ATTACH_ROOTS (in his shell and the Claude Desktop MCP config) to the folder(s) he names, so agents can only attach from there. Back up the Desktop config before editing.

3. Code changes (about 1-2 hours; start only after Remington says go)
   - Make the MCP `send` write which gate fired (elicitation typed-count vs plain permission prompt) into the ledger.
   - Before retrying any `send_failed` row, run `reconcile` so a 4xx that actually went through can't cause a duplicate. This hedges the unproven assumption that a 4xx on drafts.send always means nothing was sent.
   - Add tests for both (roughly 69 -> 75); keep ruff and mypy clean.

4. First third-party batch of 3-5 recipients (needs his exact approved list)
   - Suggest 2-3 colleagues who've agreed to receive it plus one external address he controls (e.g. personal Gmail) to check cross-domain delivery.
   - Run via the Claude Desktop path with --max-per-run set low. Afterwards, check `status` and `report` across the rows and record which gate fired. Remington should choose "Allow once" for send.

5. Recipient generator (only after step 4 is clean)
   - A separate script, outside the engine, that reads Lightfield (read-only) and emits the CSV contract: one row per life-science opportunity in stage "2.75 Scheduling", with a short subject/body tailored from the one-pager. Respect known exceptions: Jack Clark is already booked Oct 29; GenXys asked for the first week of November via his own booking link; skip contacts with no email and report them.
   - Confirm with Remington first: that this is the source and use case, and whether to include stages other than Scheduling.
   - He approves the generated recipient table before anything is drafted, and separately before anything is sent.

6. First real outreach in tranches of 5-10, each tranche needing his approval of its exact recipient list. Calendar invites stay out of scope.

## Deferred on purpose
- Stronger Desktop send gate: decide after step 3 shows which gate fires. If it's one click, propose having MCP send refuse unless elicitation is supported, keeping sending in the CLI.
- Pacing/daily caps: not needed at roughly 25 emails; the 200-row per-run cap suffices for now.
- Web/mobile access: it would mean hosting credentials or files elsewhere, which breaks the core promise.

## Start here
Read the three docs, run the tests, then ask Remington the step 1 and step 2 questions. Don't change code or send anything until he approves.
