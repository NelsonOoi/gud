# gud v1 transports

Each provider adapter in `gud/transport/` implements the `Mailbox` contract of SPEC-v1 §20.1 (`docs/v1/INTERFACES.md`, "gud/transport/base.py"). This file says, per adapter, how to configure it, what `capabilities()` reports, and what was verified and how. The choices the spec left open are under "Implementation notes (transport)" at the end of `SPEC-v1.md`.

## Choosing a transport

`gud.transport.registry` opens adapters by name, importing them lazily:

```python
from gud.transport import registry
mb = registry.open_mailbox("imap_smtp", {"imap_host": "imap.fastmail.com", ...})
mb = registry.from_env()                          # GUD_TRANSPORT=<name>, GUD_<NAME>_<KEY>=<value>
name, cfg = registry.member_config(home, "@alice") # GUD_HOME/members/@alice/transport.toml
```

Every member may use a different provider. `transport.toml` has `transport = "<name>"` plus string keys, either at the top level or in a table named after the transport. Any `<key>_env = "VAR"` is read from the environment into `<key>`, so secrets stay out of the file:

```toml
transport = "imap_smtp"
imap_host = "imap.fastmail.com"
smtp_host = "smtp.fastmail.com"
username = "repo@example.org"
password_env = "REPO_IMAP_PASSWORD"
authserv_id = "mx.messagingengine.com"
```

Built-in names: `fake`, `imap_smtp`, `primitive` (implemented); `jmap`, `ses`, `postmark`, `gmail` (registered, not implemented yet: `open_mailbox` raises `TransportError("... not implemented yet")`). A later adapter only needs a module `gud/transport/<name>.py` with a `from_config(cfg)` and a `Mailbox` class. It joins the conformance suite by adding a harness builder in `tests/v1/transport/test_conformance.py`.

## Capabilities at a glance

| | `fake` | `imap_smtp` | `primitive` |
| --- | --- | --- | --- |
| `max_message_bytes` | 10 MiB (configurable) | configured, else SMTP `SIZE`, else 10 MiB | 41 943 040 (30 MiB of decoded attachments as base64) |
| `push` | `none` (file watch / in-process) | `idle` when the server lists IDLE, else `none` | `webhook` (plus long-poll when no public URL) |
| `preserves_message_id` | yes | yes (configurable) | **no**: rewritten to `<primitive-direct-<sent id>@<from domain>>` |
| `auth_results` | `header` (`fake.gud.invalid`) | `header` (the provider's MX; `authserv_id` from config) | `synthesized` (`primitive.gud.invalid`) |
| `send_quota_per_day` | none | configured | from `GET /account` `limits.send_per_day` (10 000 on the developer plan) |
| `received_time_resolution_ms` | 1 | 1000 (INTERNALDATE) | 1 |
| `raw_mime` | yes | yes | yes |
| §6 envelope budget `B` | 7 340 032 | 7 340 032 at 10 MiB | 7 340 032 |

`B` comes from `gud.transport.base.envelope_budget`, which corrects the §6 formula for base64 line breaks (spec note 1).

## `fake`

`FakeMailbox(root, address, *, clock=time.time, max_message_bytes=10 MiB)` is an on-disk mail system shared by every address under `root`. It is safe across processes: an `fcntl` lock guards one clock for the whole root, and every delivery gets a strictly increasing `received_at`. The clock is injectable. It plays the receiving server: mail submitted through `send` gets `Authentication-Results: fake.gud.invalid; dmarc=pass` only when `From` is the sending mailbox's own address, else `dmarc=fail`. `deliver(raw)` injects mail from an "outside MTA" with nothing added. Config: `root`, `address`, optional `max_message_bytes`, `authserv_id`.

`FakeNetwork` is the in-memory version for protocol tests. It has a virtual clock (`advance(ms)`) and deterministic fault injection (`Faults(delay_ms, jitter_ms, duplicate, drop, seed)`). Subscribers are called synchronously, and Message-IDs and dates derive from a counter and the virtual clock, so a script produces the same bytes on every run.

Verified: the conformance suite, always, for both `fake` and `fake_network`. Unit tests (`tests/v1/transport/test_fake.py`) cover four processes sending concurrently (60 deliveries, strictly increasing times, no losses), clock injection, the size limit, and fault determinism (the same seed gives the same deliveries; drops, duplicates and reordering all occur).

## `imap_smtp`

`ImapSmtpMailbox(imap_host, imap_port, smtp_host, smtp_port, username, password, address, *, folders=("INBOX", "Junk"), imap_tls="ssl", smtp_tls="starttls", ...)` uses only the stdlib (`imaplib`, `smtplib`; `imapclient` is not installed).

| Key | Default |
| --- | --- |
| `imap_host`, `imap_port` | port 993 for `ssl`, else 143 |
| `smtp_host`, `smtp_port` | `imap_host`; port 587 for `starttls`, 465 for `ssl`, 25 for `none` |
| `username`, `password`, `address` | `address` defaults to `username` when it is an address |
| `smtp_username`, `smtp_password` | the IMAP login |
| `imap_tls`, `smtp_tls` | `ssl`, `starttls` (`none` only for local test servers) |
| `folders` | `INBOX,Junk`; `Junk`/`Spam` fall back to the folder LIST marks `\Junk`, missing folders are skipped |
| `max_message_bytes`, `send_quota_per_day` | SMTP `SIZE` else 10 MiB; none |
| `authserv_id` | the provider's MX authserv-id (what the repo pins), reported in `capabilities()` |
| `idle_poll` | 10 s: IDLE is re-issued this often, which also bounds how late junk-folder mail is noticed |

- **Cursor**: `<folder>:<UIDVALIDITY>:<UID>` per folder, joined by `;`. A UIDVALIDITY change rescans that folder (duplicates are deduped downstream by gud id).
- **Received time**: INTERNALDATE (one-second resolution).
- **Push intake**: IDLE on INBOX over a dedicated connection with reconnect and exponential backoff, catching up with `list()` after every wake-up and reconnect. Python 3.13's `imaplib` has no IDLE, so that connection reads its socket unbuffered and waits with `select()`.
- **Authentication-Results**: real providers' MXes (Gmail `mx.google.com`, Fastmail, Microsoft 365, Dovecot with OpenDMARC) prepend one; set `authserv_id` to the value they write. GreenMail adds none.

Verified against a local **GreenMail 2.1.3** (`.tools/greenmail-standalone.jar`, Java 11), started by `tests/v1/transport/greenmail.py` on free ports (`-Dgreenmail.smtp.port`, `-Dgreenmail.imap.port`, `-Dgreenmail.api.port` with the matching `.hostname` properties; the `greenmail.setup.test.*` presets must not be combined with explicit ports). Tests skip when java or the jar is missing. GreenMail prepends `Return-Path` and `Received` but no `Authentication-Results`. The human-verification check therefore plays the receiving MTA: it submits the message over SMTP with `Authentication-Results: mx.gud.test; dmarc=<verdict> header.from=<domain>` prepended, with `mx.gud.test` as the pinned authserv-id. The forged message also carries its own forged `dmarc=pass` header with that id further down, and still fails because the MTA's header is topmost. Extra tests cover junk-folder reading (APPEND into `Junk`), a UIDVALIDITY change, a missing junk folder, IDLE reconnect after the socket is shut down, and bad logins. Not yet verified against a hosted IMAP provider (Fastmail, Workspace): PLAN milestone 2 lists one.

## `primitive`

`PrimitiveMailbox(api_key, address, *, base_url="https://api.primitive.dev/v1", webhook_secret=None, subscribe_wait=10)` is the v1 port of `gud/mail_primitive.py`. Config keys: `api_key`, `address`, optional `base_url`, `webhook_secret`, `authserv_id`, `send_quota_per_day`, `max_message_bytes`, `subscribe_wait`.

- **Send**: `POST /send-mail` with the envelope as an inline attachment (`filename: gud.env`, `application/octet-stream`). Primitive builds the MIME from these fields, which keeps sending simple. A raw-bytes `send` is rendered from its parsed fields with the envelope bytes kept exactly. `to` takes one address; further To addresses go in `cc`. Each call carries a fresh `Idempotency-Key`, so retries of one call are safe and a deliberate resend is really sent. HTTP 429 and 5xx are retried with `Retry-After`; exhausted 429s raise `PrimitiveQuotaError` (a `QuotaExceeded`).
- **Receive**: `GET /emails?recipient=<address>&since=<cursor>` (the forward tail, oldest first), then `GET /emails/{id}/raw` (checked against `X-Content-SHA256`) and `GET /emails/{id}` for the `auth` verdicts. Rows in every status are read (`rejected` is Primitive's spam verdict, the analogue of a junk folder). Received time is the row's `received_at`.
- **Cursor**: `<primitive tail cursor>#<skip>`. Primitive's forward-tail cursor marks each page, so a position inside a page is "that page's start, skipping n rows". When a page is used up the cursor settles on Primitive's exact tail cursor.
- **Authentication-Results**: Primitive reports DMARC as structured JSON next to the raw MIME. The adapter turns that verdict into a header by prepending `primitive.gud.invalid; dmarc=<auth.dmarc> header.from=<From domain>`. Primitive evaluates DMARC on the organisational domain (`dmarcFromDomain`), so a `pass` is kept only when the From domain is that domain or a subdomain of it (spec note 8). A repo on Primitive pins `authserv-id primitive.gud.invalid`.
- **Push intake**: `subscribe` long-polls `GET /emails?since=&wait=` (it returns as soon as mail arrives), which works without a public URL. With a public URL, mount `accept_webhook(headers, body)` (or `verify` + `parse` from `WebhookVerifier`) on the webhook route. It checks `Primitive-Signature: t=<s>,v1=<hex>` (HMAC-SHA256 over `<t>.<raw body>` with the account secret as UTF-8, ±300 s; the legacy `MyMX-Signature` is accepted too). It takes the raw MIME from the payload when inlined, else from the signed download URL, else by id, and hands the message to subscribers, deduped against the long-poll by provider id.

Verified in CI against `tests/v1/transport/primitive_mock.py`, an `httpx.MockTransport` stand-in. The mock renders MIME with Python's `email` package (a different layout from `gud.transport.mime`, with LF-only line endings like the live service), rewrites Message-IDs like the live service, keeps raw MIME without A-R, reports org-domain DMARC, pages with `<µs>|<id>` cursors and honours `wait`. It also signs webhooks, which tests both with the raw inlined and with fallback fetch by id. Extra tests: retries and quota, refusing an unverified sender domain, cursors inside pages resumed from a fresh instance, and the dmarc alignment rule.

Live: see "Live run" below. The live runs used long-poll intake. The webhook path follows Primitive's documented SDK event (`email.content.raw`, `content.download.url`) and is covered by the mock; a live webhook run needs a public URL for the host.

### Live run

One run on 2026-10-20 (`GUD_LIVE_PRIMITIVE=1`), from `agent-conf-<stamp>@<domain>` to a fresh `repo-conf-<stamp>@<domain>` on the account's managed `*.primitive.email` domain. **All 11 checks passed.** 19 send calls; one was the forged-From submission, which Primitive refused, so 18 emails were delivered, against a budget of 40.

| Check | Result |
| --- | --- |
| capabilities | as documented; `send_quota_per_day` 10 000 read from `/account` |
| roundtrip | 3 hostile envelopes, gud ids and `gud.env` bytes identical |
| dedupe | 2 copies, 2 provider ids, 1 gud id |
| ordering | `received_at` gaps 1251 ms and 1235 ms for sends 1.1 s apart; order kept |
| wait | returned **0.60 s after delivery** (0.20 s after `send()` returned) |
| subscribe (long-poll) | fired **0.56 s after delivery** |
| large blob | 25 MiB in 4 chunks; 7 340 000-byte envelopes arrived intact; largest stored message 9 916 921 B; 322 s, mostly uploading about 53 MB of JSON |
| spoofed human | submission From `human@example.com` refused (`cannot_send_from_domain`); display-name spoof parsed as the sender's own address; nothing verified as the human |
| raw retained | bytes stable across reads; Message-ID rewritten (see below); synthesized A-R present |
| resume | 2 messages missed while closed, returned once each by a fresh instance |

How Primitive delivers the mail (observed on the stored raw MIME, read back afterwards with read-only API calls), and how gud uses it:

- **Primitive renders its own MIME.** LF-only line endings (no CRLF anywhere), its own boundary (`--_NmP-<hex>-Part_1`), the text part as `7bit`, the attachment as `Content-Type: application/octet-stream; name=gud.env` and `Content-Disposition: attachment; filename=gud.env` (unquoted), base64 in 76-character lines. Primitive signs the message with DKIM (d=`primitive.email`) and adds `X-Primitive-Sent-At`. The envelope bytes arrive unchanged, which is all replay reads.
- **Primitive assigns the Message-ID** (`<primitive-direct-<sent id>@<from domain>>`), and `Sent.message_id` reports it. gud references messages by content hash, so human replies resolve through the subject tag or that id (§8.2).
- **No Authentication-Results** in the stored raw MIME. DMARC comes as JSON, evaluated on the organisational domain (`dmarcFromDomain: primitive.email` for mail from `x.primitive.email`), hence the synthesized header and alignment rule.
- **Size**: a 9.9 MB message (7 MiB envelope) went through. The documented caps are 30 MiB of decoded attachments and 262 144 bytes of `body_text` per send, so the 40 MiB `max_message_bytes` is derived from them; the envelope cap of 7 MiB keeps every gud message near 10 MB.
- **Covered by the local suites**: the outside-MTA forgery variants (positive control and forged A-R) run on fake, GreenMail and the mock, since the test machine sends no outbound port-25 mail; live webhooks need a public URL for the host.

Not covered here: the host noticing mail through `/endpoints` webhooks, and the `pending` → `accepted` status transition (the adapter re-reads a `pending` row's detail for up to 5 s before synthesizing A-R).

## Conformance suite

`tests/v1/transport/conformance.py` holds the §20.2 checks, written against `Mailbox` only. `tests/v1/transport/test_conformance.py` runs them once per transport:

```
.venv/bin/python -m pytest tests/v1/transport/test_conformance.py -k imap_smtp
GUD_LIVE_PRIMITIVE=1 GUD_LIVE_ENV_FILE=/path/.env .venv/bin/python -m pytest tests/v1/transport/test_conformance.py -k primitive_live
GUD_CONF_REPORT=report.json ...      # write every measurement as JSON
```

| Check | §20.2 | What it does |
| --- | --- | --- |
| `capabilities` | 8 | fields valid, values equal the documented ones (table above), `raw_mime` true |
| `roundtrip` | 1 | three envelopes (random bytes with NUL, lone CR, lone LF, CRLF, `.` lines, `From ` lines, 998+ and 5000-byte lines, non-UTF-8; a signed comment with non-ASCII text) arrive with identical gud ids and identical `gud.env` bytes; subject tag and `Gud-Id` line survive |
| `dedupe` | 2 | the same message sent twice gives one gud id; an intake keyed by gud id logs it once |
| `ordering` | 3 | three messages 1.1 s apart: `received_at` in ms, non-decreasing, list order equals send order |
| `wait_latency` | 4 | `wait` returns within 5 s (API, fake) or 30 s (IDLE) of delivery |
| `subscribe_latency` | 4 | the push intake fires within the same bound |
| `subscribe_reconnect` | 4, 7 | IMAP only: the IDLE socket is shut down; the subscription reconnects and catches up |
| `large_blob` | 5 | a 25 MiB random file is split into `blob` envelopes of at most `B` bytes; each MIME message is checked ≤ `max_message_bytes` (about 96% of a 10 MiB limit) and the file is reassembled by hash |
| `spoofed_human` | 6 | unsigned `approve` with the human's From through the submission path, a display-name spoof, and an outside forgery carrying its own forged A-R all fail §8.1(2); a genuine message (MTA verdict pass) verifies, as a positive control where the transport allows injection |
| `raw_retained` | – | raw bytes are identical across two reads (stable unsigned id = sha256 of them), the text part and a 1200-character line survive, and Message-ID preservation and the receiving side's A-R are recorded |
| `resume` | 7 | a reader closes, two messages arrive, a fresh instance lists from the old cursor and gets both exactly once |

### Local results (2026-10-20)

All checks pass on `fake`, `fake_network`, `imap_smtp` (GreenMail) and `primitive_mock`: 44 tests, about 45 s, most of it the four 25 MiB blob runs. Measured:

| | `wait` after delivery | `subscribe` after delivery | 25 MiB blob path |
| --- | --- | --- | --- |
| fake | 0.004 s | 0.005 s | 4 chunks, largest MIME 10 044 926 B of 10 485 760, 6.3 s |
| fake_network (virtual 200 ms transit) | 0.0 s virtual (0.2 s from send) | 0.0 s virtual | 4 chunks |
| imap_smtp / GreenMail | 0.11 s (INTERNALDATE truncates to the second) | 0.27 s; reconnect after a dropped IDLE socket 0.98 s | 4 chunks, largest received 10 045 091 B, 5.9 s |
| primitive_mock | 0.004 s | 0.004 s (webhook path < 1 s) | 4 chunks, 5.9 s |
