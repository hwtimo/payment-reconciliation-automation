# Design Decisions

The engineering decisions behind the remittance reconciler, with the alternatives that were rejected and the trade-offs accepted. For how the pieces fit together, see [ARCHITECTURE.md](ARCHITECTURE.md).

One principle drives most of these decisions:

> **Automating less is acceptable. Writing the wrong thing is not.**
> The cost of leaving a row for a person is minutes. The cost of a wrong or duplicate payment post is a corrupted ledger that someone has to discover and unwind.

---

## Contents

1. [Ambiguity halts or goes to a person instead of being guessed](#1-ambiguity-halts-or-goes-to-a-person-instead-of-being-guessed)
2. [Financial writes are validated before, at and after the click](#2-financial-writes-are-validated-before-at-and-after-the-click)
3. [The portal is never a source of authority](#3-the-portal-is-never-a-source-of-authority)
4. [One write primitive for supervised and unattended runs](#4-one-write-primitive-for-supervised-and-unattended-runs)
5. [Unknown vendors are quarantined](#5-unknown-vendors-are-quarantined)
6. [Durable state drives the work, not the mailbox](#6-durable-state-drives-the-work-not-the-mailbox)
7. [Deterministic rules only: no AI, LLM or OCR](#7-deterministic-rules-only-no-ai-llm-or-ocr)
8. [Browser automation built for reliability, not convenience](#8-browser-automation-built-for-reliability-not-convenience)
9. [Intake trust is anchored on the forwarder's mail domain](#9-intake-trust-is-anchored-on-the-forwarders-mail-domain)
10. [Two amount parsers, both exact](#10-two-amount-parsers-both-exact)
11. [Identifiers are compared case-insensitively but never rewritten](#11-identifiers-are-compared-case-insensitively-but-never-rewritten)
12. [Throttling is not failure, and manual review is terminal](#12-throttling-is-not-failure-and-manual-review-is-terminal)
13. [Backup and recovery strategy](#13-backup-and-recovery-strategy)
14. [Secrets, configuration and runtime data are separated from source](#14-secrets-configuration-and-runtime-data-are-separated-from-source)
15. [Unattended scheduled execution](#15-unattended-scheduled-execution)
16. [Silence is treated as a failure](#16-silence-is-treated-as-a-failure)
17. [Pure decision modules and structural tests](#17-pure-decision-modules-and-structural-tests)

---

## 1. Ambiguity halts or goes to a person instead of being guessed

**Context.** Real remittances contain edge cases:
- credits and clawbacks;
- invoices already paid by hand;
- partial payments;
- identifiers with a malformed suffix;
- rows whose invoice is outside the expected date window;
- statements re-sent with different content.

A heuristic could "fix" most of these, and would occasionally fix them wrongly.

**Decision.** Every uncertain case resolves to no write:

- **Row-level doubt** becomes `MANUAL_REVIEW` with a machine-readable reason: amount or payer mismatch, credit, not found, partial collection, detail mismatch, or an action that doesn't resolve to exactly one target.
- **Statement-level doubt** is quarantined: invariant violations, unknown vendors, stale statements, and same-key content conflicts.
- **An unconfirmed write** is recorded as `UNKNOWN_OUTCOME`. It is never retried, and it **halts the entire run**: if the system cannot tell what just happened, it should not keep acting.
- **A malformed identifier** is isolated rather than corrected, even when the "obvious" correction is one character, because `-B01` and `-B02` are different invoices.

**Alternatives rejected.**
- *Fuzzy matching or auto-correcting identifiers*: a plausible wrong match is worse than no match.
- *Retrying ambiguous clicks*: a retry loop around a financial action eventually becomes a double payment.
- *Picking another eligible invoice when the target fails*: nothing about the substitute was authorized.

**Trade-offs.**
- Some rows that a person would approve in seconds are left for them.
- A single ambiguous outcome stops that night's remaining writes.

Both are accepted. The review list is explicit and actionable because each row keeps its verdict and error code, plus an attribution where money may have moved.

## 2. Financial writes are validated before, at and after the click

**Context.** Between reconciliation and the click, the world can change. Staff may post the payment manually, the single-page app may still show a stale pane, or a link may point at a different invoice. The click itself can time out even though it succeeded.

**Decision.** A write passes through layered checks:

1. **Before.** A read-only detail validation stage confirms, on the live page: the identifier, total equal to the remittance net, unpaid status, and a single exact action. Only then does the row become `DETAIL_VALIDATED`.
2. **At the boundary.**
   - Eligibility is checked again: provenance, state, verdict, kill switch, caps, existing claims, and (in the nightly path) the per-statement amount guard.
   - The detail pane is re-read from scratch; handles from earlier stages are never reused.
   - The exact action is resolved again.
   - The pane must still show the target invoice.
3. **Claim, then click.** A write claim is committed (journal line fsync'd first, then the database row) *immediately before* the single click.
4. **After.** Success counts only when all of these hold:
   - persisted server state shows Paid/Settled;
   - exactly one payment row matches the amount;
   - the same state appears after re-fetching the invoice.

**Why claim immediately before the click.**
- *Claiming earlier* (for example before navigation) would permanently block invoices that were never touched whenever a read failed.
- *Claiming after* the click would leave a crash window in which a real click has no record.

Committing just before the click shrinks that window to milliseconds. It also keeps the invariant *claims ⊇ clicks*: an error produces an extra claim, never a missing one.

**Alternatives rejected.**
- *Trusting UI transitions*, such as a notification, a closed menu or the action disappearing. The UI offers no durable confirmation, and the action remains available after posting, so neither proves anything.
- *Gating re-clicks on row state.* State is recomputed and can be edited, so only the existence of a claim is a durable "never again".

## 3. The portal is never a source of authority

**Context.** An unpaid invoice from the right payer with a matching amount, sitting in the portal, *looks* ready to pay. It proves nothing about whether the payer actually remitted it.

**Decision.** Authorization flows only from the remittance:

> trusted forwarded email → parsed statement → immutable parsed row → reconciliation → detail validation → claim

- `verify_provenance` fails closed.
- Provenance columns are written once at ingestion and cannot be updated.
- At write time the statement's immutable row amounts must still sum to its deposit, which detects rows inserted into or edited in the database after parsing.
- Candidate listings (`tools/select_candidate.py`) come from the database, never from portal searches, and a structural test checks that the tool references no claim, click or browser-session code.

**Trade-off.** An invoice the payer did pay, but that is missing from the parsed statement, is never auto-posted. That case belongs to a person anyway.

## 4. One write primitive for supervised and unattended runs

**Context.** A live rollout needs supervised runs (one invoice, then small allow-listed batches) before unattended operation. If supervised runs used their own implementation, the path verified by hand would not be the path running at 03:00.

**Decision.** `writepath.execute_one_authorized_work` is the only code that can claim and click.
- The nightly loop and `tools/run_smoke.py` both call it.
- Supervised runs differ only in *selection*: an immutable allow-list of database row ids, never invoice numbers.
- Arming requires an operator-typed cap that must equal the exact total of the targets.
- Tests parse the source to prove there is no second claim or click implementation and that only the database layer writes claims.

**Trade-off.** The primitive must serve both callers, so selection policies (batch halting, budgets) live outside it. That separation is what keeps the boundary small enough to audit.

## 5. Unknown vendors are quarantined

**Context.** Each clinic location has its own vendor number with the payer. A statement for a vendor the system does not know could belong to another entity, or could mean the configuration is incomplete.

**Decision.** V9 is a hard gate: an unknown vendor number rejects the whole statement before any row is considered. The quarantine is *recoverable*: once an operator adds the vendor number to `config.yaml`, the next run re-parses the message and ingests it. The report entry for the quarantine includes the vendor number, so the fix is obvious.

**Alternatives rejected.**
- *Processing unknown vendors and relying on invoice matching.* Identifiers are only unique within the portal, not across entities.
- *Permanent quarantine.* Statements would be silently lost even after the configuration was fixed.

**Trade-off.** When a new location starts receiving remittances, its statements wait until configuration catches up. The report makes the wait visible.

## 6. Durable state drives the work, not the mailbox

**Context.** A mailbox-driven loop ("process messages I have not seen") has a subtle flaw. If a run ingests a statement and then crashes, the message is marked seen and the statement is never processed. Remittances do not arrive every day, so nothing would retry it.

**Decision.**
- **Queue.** The work queue is `SELECT … FROM statements WHERE state = 'PENDING'`. A seen message id means "do not parse again", never "already processed".
- **Atomic ingestion.** Statement, rows and email link are written in one transaction, so a crash leaves either nothing or a complete `PENDING` statement.
- **Durable ledger.** SQLite runs in WAL mode with `synchronous=FULL` on every connection. The claim ledger is never deleted, and each claim insert is also appended to an fsync'd journal.
- **Content identity.** Statements are identified by `(vendor_no, payment_document_no)`. A canonical, row-order-independent SHA-256 fingerprint detects a resend with different content. The new email is quarantined, and an unfinished original is halted rather than paid from a possibly stale version.

**Why SQLite.** One process runs at a time on one host. Transactions, durability settings and a single auditable file matter more here than concurrency, and SQLite provides all three without operating a server.

**Trade-off.** Every stage must be idempotent, because every stage is re-run. The design embraces this: a statement stays `PENDING` until all of its rows are terminal.

## 7. Deterministic rules only: no AI, LLM or OCR

**Context.** Parsing semi-structured emails and matching records are tasks where a model can look attractive.

**Decision.** Every decision is explicit code over exact values:
- HTML parsing by column name;
- `Decimal` comparisons;
- a fixed classification table;
- exact UI roles and names.

**Why.**
- The same input must always produce the same decision, so the decision table can be tested exhaustively and every outcome can be explained after the fact.
- A model that is right 99.9% of the time is wrong on some night, silently, with money involved.
- The failure mode of explicit rules is a loud rejection that a person can inspect.

**Trade-off.** Format changes require code changes. The parser's invariants make such changes fail visibly (quarantine plus report) instead of degrading silently.

## 8. Browser automation built for reliability, not convenience

**Context.** The portal has no API for this workflow. UI automation is notoriously brittle, and here a brittle selector is a financial risk: the action menu places "Record Payment", "Record Payment Plan…" and a write-off item side by side.

**Decisions.**
- **Semantic, scoped, exact targeting.** Find the trigger through the menu it controls (not by CSS class), and confirm the menu opened via `aria-expanded`. Scope to that container, then require exactly one `role=menuitem` with the exact name that is visible and enabled.
  - *Rejected for the write action:* coordinates, `nth-child`, menu positions, keyboard selection and partial text. A partial "Record Payment" match hits the wrong item.
- **Identity before action.** Every detail read waits for the pane to show the *expected* identifier (`IdentityMismatch` otherwise). The pane is checked again immediately before the claim.
- **Read data from the export, not the page.** Invoice data comes from the portal's CSV export; the page supplies only navigation links and the single action. A broken link can at worst produce a manual-review row, because detail validation stops the write.
- **Judge readiness from observable state.** Filters are applied explicitly and the date picker navigates to exact day cells. The on-screen count must be identical across consecutive reads, and the export row count must match it (G-7). Short fixed pauses are used only between polls and UI steps, never as evidence of success.
- **Confirmation from the server.** Success is re-observed after re-fetching the invoice, not inferred from UI state.
- **Sessions are reused, logins are rare.** A persistent profile keeps the session between nights. An expired session gets exactly one Keychain-backed login; repeated automated logins invite CAPTCHAs and lockouts.
- **Challenges stop the run.** A *visible* CAPTCHA, verification form or challenge text raises `LOGIN_REQUIRED`: no bypass attempts, no retries. Visibility is checked, not DOM presence, because normal login pages embed invisible CAPTCHA widgets.
- **Headless parity.** Headless runs present the standard browser user agent, and the process runs at normal priority, so unattended behavior matches supervised behavior.
- **A central DOM contract.** Key selectors are named constants in one module, and the test suite drives the adapter through fakes that model the portal's behavior.

**Trade-off.** Strictness means UI changes cause rejections (`MenuError`, `LOGIN_REQUIRED`, integrity failures) rather than best-effort clicks. A rejection before the claim never blocks an invoice permanently, so the next run retries once the cause is fixed.

## 9. Intake trust is anchored on the forwarder's mail domain

**Context.** Staff forward remittances to the automation mailbox after confirming the bank deposit. Forwarding replaces the original sender, and the payer's DKIM signature does not survive. The quoted "From" line in the body is plain text that anyone can forge.

**Decision.** Trust the forward, not the quoted original.
- **Sender.** The `From` header must parse to exactly one allow-listed address. The RFC 5322 parser is used because a regex can be fooled by a display name containing a trusted address.
- **Authentication results.** DKIM and DMARC must pass according to the *topmost* `Authentication-Results` header written by the configured receiving server. Senders can inject their own results headers further down.
- **Domain alignment.** When the receiving server records the domain DMARC evaluated, it must match the sender's domain.
- **Persistence.** The gate's result is persisted as `intake_trusted` and anchors provenance.

**Alternatives rejected.**
- *Requiring the payer's original sender*: it rejects every forwarded message.
- *Trusting quoted sender text*: it can be forged.
- *Scanning all Authentication-Results headers*: they can be injected.
- *Relying on mailbox labels*: labels are a manual process and differ between mailboxes.

## 10. Two amount parsers, both exact

**Context.** Remittance emails always use two decimals and signal credits in three notations. The portal's CSV export often omits trailing zeros (`86.0`).

**Decision.** Keep two deliberately separate parsers.
- **`parse_amount` (email)** is strict: exactly two decimals, sign preserved across `-x`, `x-` and `(x)`. It rejects everything else. Stripping non-digit characters would turn `-100.00` into a payment of `100.00`.
- **`parse_portal_amount` (export)** accepts one or two decimals and normalizes to two.
- Both return `Decimal`, and `Decimal("97.0") == Decimal("97.00")`, so comparisons stay exact.

**Alternatives rejected.**
- *One strict parser*: it rejects most valid export rows.
- *One lenient parser*: it weakens the email-side protection against format drift and lost signs.

## 11. Identifiers are compared case-insensitively but never rewritten

**Context.** An identifier such as `300248-B01` includes a suffix, and `-B01` and `-B02` are different invoices with different portal records. The payer sometimes writes the suffix in lowercase; the portal always uses uppercase.

**Decision.**
- Stored identifiers keep their original spelling. Normalization only strips whitespace and a leading `#`.
- Comparisons use a case-folded key that never touches the suffix digits.
- The claim ledger's unique index is case-insensitive, so `-b01` and `-B01` cannot both be claimed.
- No code path strips suffixes or merges invoices to a base number.

**Trade-off.** A case-sensitive comparison would have looked "stricter", but it silently fails every lowercase row with `NOT_FOUND`, and it would let a second claim slip into the ledger.

## 12. Throttling is not failure, and manual review is terminal

**Context.** Caps, the kill switch and supervised allow-lists deliberately skip rows. If skipped rows were marked as failures, healthy rows would drift into manual review, and retry counters would eventually abandon statements that did nothing wrong.

**Decision.**
- **Throttles never change state.** A fixed set of throttle reasons skips a row without touching its state.
- **Earlier outcomes are kept, but only verified ones stay out of review.** When the write stage skips a row, a `MANUAL_REVIEW` row keeps its verdict and reason, so a mismatch keeps the reason a person needs to act on. A `NO_ACTION` (already paid) row stays out of review only while its provenance verifies. Keeping a row *in* review hides nothing; keeping one *out* must rest on data the system can still vouch for, such as a well-formed identifier.
- **Real rejections are recorded.** Any other rejection is recorded as `MANUAL_REVIEW` with its reason. This only affects classification: every rejected row is skipped, and none can become write-eligible.
- **`MANUAL_REVIEW` is terminal** from the automation's point of view: a statement with confirmed rows plus a few exceptions is `COMPLETED`, not retried forever.
- **Empty statements never complete.** A statement with zero rows is never `COMPLETED`, so a broken ingestion cannot masquerade as success.

## 13. Backup and recovery strategy

**Context.** The database is the audit trail and the at-most-once ledger. Losing it would lose the record of which invoices crossed the write boundary.

**Decisions.**
- **Journal before database.** Every claim is appended to an fsync'd, owner-only JSON-lines journal, a separate file, *before* the database row is written, so a record of every claim insert survives loss of the database file. Outcomes are stored only in the database, and restoring from the journal is a manual step.
- **Online, verified snapshots.** `tools/backup_db.py` uses SQLite's online backup API against a read-only connection, so committed WAL content is captured without stopping anything. For each snapshot it:
  1. runs `PRAGMA integrity_check`;
  2. compares claim, confirmed-claim, work-row and statement counts against the live database;
  3. copies the claim journal alongside;
  4. applies retention, deleting only the oldest snapshots.

  Snapshots are `0600` in a `0700` directory.
- **Restore drills without risk.** `--verify-restore` restores a snapshot into a temporary location, re-checks integrity, confirms that no live claim is missing, and never touches the production database.
- **Recovery is read-only.** An unresolved claim (a crash between claim and confirmation) is resolved only by *observing* the portal under the full success contract, automatically on the next run or on demand with `tools/recover_claim.py`. Recovery can confirm; it can never click.
- **Re-drivable work.** Because the queue is database-driven and every stage is idempotent, recovering from a crash is simply the next run.

**Trade-off.** Backups are an operator-run tool rather than a scheduled job, and restoring a snapshot is a deliberate manual step. For a single-host system this keeps restores intentional. Scheduling backups is listed as a known limitation.

## 14. Secrets, configuration and runtime data are separated from source

**Context.** The system handles portal credentials, an OAuth token, browser session cookies, financial records and exports that may contain patient-related data. None of it may reach version control or logs.

**Decisions.**
- **Credentials outside the repository.**
  - Portal credentials live in the macOS Keychain and are read at login time; they never appear in config, environment variables, logs or source.
  - The Gmail OAuth token lives under `secrets/` (`0600`, directory `0700`), with read-only and send scopes only.
  - The browser profile is treated as a credential (`0700`).
- **Configuration is a template.**
  - `config.example.yaml` is tracked and contains only placeholders; the real `config.yaml` is git-ignored.
  - The loader refuses the placeholder cutover date and timezone-naive timestamps, so a copied template cannot run.
  - Money values are strings in YAML to avoid binary floats.
- **Runtime data is ignored by default.** `data/`, `logs/`, `reports/`, `scratch/`, `secrets/` and `backups/` exist in the repository only as empty placeholders. `.gitignore` also blocks token, credential, browser-state, database, journal, log, export, screenshot, recording and document file types anywhere in the tree.
- **Patient data is minimized at the source.**
  - The export parser never reads patient, patient ID, service or provider columns.
  - Export files are `0600` and deleted in a `finally` block right after parsing; leftovers are removed at the next start, after the lock.
  - The first export uses the narrowest date window.
  - Reports contain billing identifiers only.

**Trade-off.** Operating the system requires a short setup: Keychain item, OAuth consent, one interactive portal login and config. The rest of the checkout can be shared freely.

## 15. Unattended scheduled execution

**Context.**
- Remittances arrive during business days, and posting them overnight is timely enough.
- Staff use the same portal during the day, and a bot working alongside them risks interference.
- The host is a Mac mini with FileVault enabled.

**Decisions.**
- **A nightly job, not a service.**
  - A launchd **LaunchAgent** runs at 03:00 via `StartCalendarInterval`.
  - `RunAtLoad` is false and there is no `KeepAlive`. A resident service would add failure modes (restart loops, retries burning through limits) without adding value.
- **A fixed local time.** `StartCalendarInterval` expresses the schedule as a time of day. launchd coalesces missed runs and may start one as soon as the machine wakes or the agent loads, so the schedule alone does not keep runs out of business hours; the start window below does.
- **Start-window enforcement in the application.** launchd may fire a missed job as soon as the agent loads, for example when someone logs in in the morning. The application therefore refuses to *start* outside its configured window. It never interrupts a running job, because stopping a write midway is worse than finishing late.
- **LaunchAgent, not LaunchDaemon.**
  - The job needs the logged-in user's Keychain and a GUI-capable session for the browser.
  - With FileVault, nothing runs before the disk is unlocked, so a daemon would not add availability.
  - The accepted trade-off: after any reboot a person must log in. Screens are locked, never logged out.
- **Single-instance and path safety.** An exclusive `flock` prevents a manual run and the scheduled run from overlapping. The scheduled job derives every path from the configuration file's location, never from the working directory; operator tools default to paths relative to the checkout.
- **Distinct exit codes.** Success, failure, login required, outside window, lock held and halted each have their own code, so the operator can tell them apart from logs alone.
- **Staged rollout.** Operation started in shadow mode (`dry_run: true`), moved to supervised single-invoice and allow-listed batches, and only then to unattended runs. Per-run caps remain configurable; the per-invoice gates always apply.

**Alternatives rejected.**
- *Running during business hours*: it collides with staff activity.
- *Disabling FileVault for automatic login*: it removes encryption at rest from a machine holding financial records and credentials.
- *A cloud runner*: it would move browser sessions, credentials and patient-adjacent exports off a controlled host.

## 16. Silence is treated as a failure

**Context.** The worst failure of an unattended system is silence. "No exceptions today" and "the job never ran" must never look the same, and an alert sent by a process that is not running will never arrive.

**Decisions.**
- **One report per run.** Every processing run enqueues exactly one report:
  - a summary when there was work, intake exceptions or abandoned statements;
  - a heartbeat when there was nothing to do;
  - an immediate failure, halt or login-required alert when appropriate.
- **Absence is the signal.** Because a heartbeat arrives every night, a missing email *is* the outage signal, and it does not depend on the failed process.
- **Reports are delivered through an outbox.** A delivery failure never rolls back or re-triggers financial work, and a report is marked sent only after delivery. The `--no-send` flag is the exception: it marks queued reports sent without delivering them.
- **Early exits are journaled.** A held lock or a closed start window is written to an fsync'd incident journal.
- **Abandoned statements stay visible.** They are named in every subsequent summary, so they cannot quietly disappear.

**Trade-off.** Recipients get an email every day, including uneventful ones. That is the price of detecting an outage within a day.

## 17. Pure decision modules and structural tests

**Context.** Safety properties that live only in reviewers' heads erode as code changes.

**Decisions.**
- **Pure decision modules.** `parser.py`, `reconcile.py` and `portal_csv.py` have no file or network access, clock or randomness (the parser only logs); anything time-dependent is passed in. Classification, terminal-state rules, date windows, age guards and export checks can therefore be tested as exhaustive tables, and "why did yesterday's input pass?" always has a reproducible answer.
- **Structural tests guard the architecture.** Several tests parse the source rather than run it, to prove that:
  - there is exactly one claim and click implementation;
  - only the database layer writes `write_claims`;
  - `insert_claim` requires provenance;
  - authentication precedes every portal stage;
  - the claim is committed between target resolution and the click;
  - the candidate-listing tool cannot use the browser layer.
- **Regressions are pinned.** Past failure classes each have a named test: state-gated re-clicks, mailbox-driven queues, a reversed age calculation, and invisible CAPTCHA widgets treated as challenges.

**Trade-off.** Structural tests are coupled to code structure and must be updated in deliberate refactors. That coupling is intentional: moving the write boundary *should* require touching a test that explains why it exists.
