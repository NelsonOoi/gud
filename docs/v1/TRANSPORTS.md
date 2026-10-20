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

Built-in names: `fake`, `imap_smtp`, `primitive`, `jmap`, `ses`, `postmark`, `gmail`, all implemented. A built-in whose module is missing raises `TransportError("... not implemented yet")`. A later adapter only needs a module `gud/transport/<name>.py` with a `from_config(cfg)` and a `Mailbox` class. It joins the conformance suite by adding a harness builder in `tests/v1/transport/test_conformance.py`.

## Capabilities at a glance

| | `fake` | `imap_smtp` | `primitive` | `jmap` | `ses` | `postmark` | `gmail` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `max_message_bytes` | 10 MiB (configurable) | configured, else SMTP `SIZE`, else 10 MiB | 41 943 040 | min(`maxSizeUpload`, MIME size implied by `maxSizeAttachmentsPerEmail`), or configured | 40 000 000 (S3 action); 150 000 with an SNS action only | 10 000 000 | 25 000 000 |
| `push` | `none` | `idle` when listed, else `none` | `webhook` (+ long-poll) | `eventsource` (polls without `eventSourceUrl`) | `webhook` (SNS) | `webhook` | `pubsub` with a topic, else `none` (polls) |
| `preserves_message_id` | yes | yes (configurable) | **no** | yes (raw bytes submitted) | **no**: `<id>@email.amazonses.com` (`<region>.amazonses.com` outside us-east-1) | **no**: `<MessageID>@mtasv.net` (configurable) | **no**: Gmail's own, read back after send |
| `auth_results` | `header` (`fake.gud.invalid`) | `header` (configured) | `synthesized` (`primitive.gud.invalid`) | `header` (configured, the server's MX) | `synthesized` (`ses.gud.invalid`) | `none` (Postmark has no DMARC verdict) | `header` (`mx.google.com`) |
| `send_quota_per_day` | none | configured | `/account` (10 000) | configured | `GetAccount` `Max24HourSend` (sandbox 200) | configured (none by default) | 500 consumer / 2 000 Workspace (configurable) |
| `received_time_resolution_ms` | 1 | 1000 | 1 | 1000 (configurable; `receivedAt` is often whole seconds) | 1 | 1 | 1 |
| `raw_mime` | yes | yes | yes | yes | yes | `RawEmailEnabled` from `GET /server` | yes |
| §6 envelope budget `B` | 7 340 032 | 7 340 032 at 10 MiB | 7 340 032 | 7 340 032 | 7 340 032 (S3; 61 723 SNS-only) | 7 259 800 | 7 340 032 |

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

## Webhook-fed adapters (SES, Postmark)

SES and Postmark have no mailbox-listing API comparable to IMAP, JMAP or Gmail: mail reaches gud only when the provider calls a webhook. Both adapters follow the v0 `HostedMailbox` pattern. The webhook handler calls `accept_webhook(headers, body)`, which verifies the request and writes the message into a `gud.transport.spool.Spool`. That is an on-disk store, safe across processes, at `GUD_HOME/inbound/<transport>/<address>/` unless `store` is configured. `list`, `wait` and `subscribe` read the spool. The cursor is the spool sequence number. Each provider id is stored once, and the first copy wins, so the stored bytes (and an unsigned message's gud id) never change. `catch_up()` fetches from the provider's own store whatever the webhook missed: S3 for SES, message search for Postmark. `subscribe` runs it every `catchup_interval` seconds (default 60) and `wait` runs it while idle. Mount the webhook on HTTPS. Answer 2xx when `accept_webhook` returns, and 401/403 when it raises, so the provider retries real failures only.

## `jmap`

`JmapMailbox(session_url, token, address, *, username=None, password=None, account_id=None, authserv_id=None, max_message_bytes=None, send_quota_per_day=None, received_time_resolution_ms=1000, preserves_message_id=True, ping=15, poll_interval=10)` implements RFC 8620 and RFC 8621 with httpx.

- **Session**: `GET session_url` with a Bearer token, or Basic auth with username and password. The account is the one named after the address, else `primaryAccounts[urn:ietf:params:jmap:mail]`. The session is refetched when `sessionState` changes and on 401.
- **Send**: upload the raw bytes (`message/rfc822`). Then one request does `Email/import` (into the `sent` mailbox, else `drafts`, keyword `$seen`) and `EmailSubmission/set` with `emailId: "#e"` and an explicit envelope (From → To + Cc). The identity is the one whose email equals From, or a `*@domain` wildcard; with none, the adapter raises `forbiddenFrom` before uploading. An `alreadyExists` import (the same bytes sent twice) submits the existing Email. Messages over `max_message_bytes` raise `tooLarge` before upload. `overQuota`, `rateLimit` and HTTP 429 after retries raise `JmapQuotaError`.
- **List**: `Email/changes` from the cursor's state, then `Email/get` (`blobId`, `receivedAt`, `mailboxIds`), then a blob download. Emails only in `sent`/`drafts` are the account's own sends and are skipped. Junk is read. `receivedAt` is the delivery time. `list(since=None)` walks the mailbox with `Email/query` sorted by `receivedAt` and then continues with changes. `cannotCalculateChanges` falls back to a walk from the cursor's last `receivedAt` (at least once). There is no recipient filter: the account is assumed to be dedicated to the repo address.
- **Cursor**: `<c|q>|<state>|<receivedAt ms>|<ids at that time>|<partial>`. Ties at one `receivedAt` (common, since servers report whole seconds) keep the server's order, and every item's cursor resumes exactly after it.
- **Push**: one EventSource stream (`types=Email&closeafter=no&ping=<ping>`). Each `state` event triggers a `list()` catch-up, and so does each (re)connect. The adapter connects first and then catches up, so nothing falls in between. Reconnects use exponential backoff.

Verified against mock only: `tests/v1/transport/jmap_mock.py`, built from RFC 8620/8621 (session limits and URL templates, the methods above with creation-id back-references, `maxChanges`/`hasMoreChanges`, `cannotCalculateChanges`, content-addressed blobs, `forbiddenFrom`, one-second `receivedAt`, RFC 7807 errors, an EventSource with `state` and `ping` events, and forced stream drops). Its MTA prepends `Authentication-Results: mx.jmap.test; dmarc=...`. All 11 conformance checks pass, including `subscribe_reconnect`. Unit tests (`test_jmap.py`): same-second batches resumed from every item by a fresh instance, the `cannotCalculateChanges` fallback, junk read and Sent copies skipped, a duplicate import, `forbiddenFrom`, size limits from both session limits, retries and quota, and config.

**Live setup (not done: no account).** Fastmail, for example: Settings → Privacy & Security → Integrations → API tokens, with access to Email and Email submission. Then:

```toml
transport = "jmap"
session_url = "https://api.fastmail.com/jmap/session"
address = "repo@example.org"          # an identity of the account (the account dedicated to the repo)
token_env = "REPO_JMAP_TOKEN"
authserv_id = "<the authserv-id in the topmost Authentication-Results of a message you received>"
```

Run with `GUD_LIVE_JMAP=1 GUD_LIVE_JMAP_SESSION_URL=... GUD_LIVE_JMAP_TOKEN=... GUD_LIVE_JMAP_ADDRESS=... GUD_LIVE_JMAP_AUTHSERV_ID=... GUD_LIVE_JMAP_SENDER_TOKEN=... GUD_LIVE_JMAP_SENDER_ADDRESS=... .venv/bin/python -m pytest tests/v1/transport/test_conformance.py -k jmap_live`. Not checked live: whether the provider's authserv-id is stable across its MX hosts, the real `receivedAt` resolution, whether `Email/import` forbids duplicates, and EventSource behaviour behind real proxies.

## `ses`

`SesMailbox(region, credentials, address, *, inbound_bucket=None, inbound_prefix="", topic_arns=(), store=None, authserv_id="ses.gud.invalid", max_message_bytes=None, send_quota_per_day=None, configuration_set=None, catchup_interval=60)`. `credentials` is `AwsCredentials(access_key_id, secret_access_key, session_token=None)`. In config, use `access_key_id`/`secret_access_key`, else the `AWS_*` environment. No boto3.

- **SigV4** is implemented with `hmac`/`hashlib` (`sigv4_headers`). It is checked against the AWS test-suite vectors (`get-vanilla` and the IAM `ListUsers` example).
- **Send**: SESv2 `POST /v2/email/outbound-emails` with `Content.Raw.Data` and `Destination` = To + Cc. SES **rewrites the Message-ID** to `<MessageId@email.amazonses.com>` (`<region>.amazonses.com` outside us-east-1); `Sent.message_id` reports it. Throttling, `LimitExceededException` and `SendingPausedException` raise `SesQuotaError`. An unverified From raises `SesError("MessageRejected")`.
- **Receive**: a receipt rule for the repo address with either an **S3 action** (bucket, prefix, SNS topic) or an **SNS action** with Base64 encoding. The S3 action is the only one that hosts a repo: SNS content is capped at 150 KB, which gives a 61 723-byte envelope budget. The notification's `receipt.timestamp` is the delivery time. S3 objects are fetched only from the configured `inbound_bucket`.
- **SNS verification**: the `SigningCertURL` must be `https://sns.<topic region>.amazonaws.com/...pem`, with no userinfo and no port. Only then is the certificate fetched (over verified TLS). It must parse, be valid now, and name `sns.amazonaws.com`. It is pinned per URL. SignatureVersion 1 (SHA1withRSA) and 2 (SHA256withRSA) are supported. The `TopicArn` must be in `topic_arns`: any AWS account's topic is signed by AWS, so the signature alone does not show the message is for this repo. A `SubscriptionConfirmation` from an allowed topic is confirmed by fetching its `SubscribeURL` (same host rule).
- **Authentication-Results**: synthesized `ses.gud.invalid; dmarc=<pass|fail|none|temperror> header.from=<From domain>` from `receipt.dmarcVerdict` (PASS, FAIL, GRAY, PROCESSING_FAILED). Copies found by S3 catch-up have no verdict and get `dmarc=none`. A repo on SES pins `authserv-id ses.gud.invalid`.
- **Catch-up**: `ListObjectsV2` of `inbound_prefix`, oldest `LastModified` first (`LastModified` is the received time). Every object under the prefix is taken to be this address's mail, so give the repo's rule a prefix of its own. Skip `AMAZON_SES_SETUP_NOTIFICATION`. Add an S3 lifecycle rule, because every catch-up lists the whole prefix.

Verified against mock only: `tests/v1/transport/ses_mock.py`. It has SESv2 SendEmail and GetAccount with SigV4 checked by an independent implementation, verified identities, a 40 MB cap and the Message-ID rewrite. Receiving adds `X-SES-*`, `Received-SPF` and an `amazonses.com` A-R line, then runs an S3 action or an SNS action (150 KB bounce). SNS messages are signed with a self-signed RSA certificate for `sns.amazonaws.com` served at an allow-listed URL, and subscription confirmation is modelled. S3 has GetObject and paginated ListObjectsV2. All 11 conformance checks pass (no disconnect hook: the intake has no connection to drop). Unit tests (`test_ses.py`): SigV4 vectors, the Message-ID rewrite as reported, SNS v1/v2 positive and tampered, eight forged `SigningCertURL` forms refused **without a fetch**, a wrong key, a foreign topic, a non-SNS certificate at an allowed URL, certificate pinning, subscription confirmation (and a forged `SubscribeURL`), the SNS-action 150 KB path, catch-up dedupe against the webhook and resume, DMARC fail with a forged inner A-R, the recipient filter, quota, an unverified sender and a bad signature.

**Live setup (not done: no account).**

1. In a region with SES receiving (for example us-east-1, us-west-2, eu-west-1): verify the domain identity (Easy DKIM) and request production access (the sandbox allows 200 sends a day, to verified recipients only).
2. Point the repo domain's MX at `inbound-smtp.<region>.amazonaws.com`.
3. Create an S3 bucket whose policy lets `ses.amazonaws.com` `s3:PutObject` (condition `aws:SourceAccount`), and an SNS topic.
4. In the active receipt rule set, add a rule for the repo address: an S3 action (bucket, prefix such as `repo/`, the topic).
5. Subscribe the HTTPS webhook (`accept_webhook`) to the topic; the adapter confirms it.
6. Give an IAM principal `ses:SendRawEmail`/`ses:SendEmail`, `ses:GetAccount`, `s3:ListBucket` (on the prefix) and `s3:GetObject`.

```toml
transport = "ses"
region = "us-east-1"
address = "repo@example.org"
access_key_id_env = "AWS_ACCESS_KEY_ID"
secret_access_key_env = "AWS_SECRET_ACCESS_KEY"
inbound_bucket = "gud-inbound"
inbound_prefix = "repo/"
topic_arns = "arn:aws:sns:us-east-1:123456789012:gud-inbound"
```

`GUD_LIVE_SES=1 GUD_LIVE_SES_REGION=... GUD_LIVE_SES_REPO=... GUD_LIVE_SES_SENDER=... GUD_LIVE_SES_BUCKET=... GUD_LIVE_SES_PREFIX=... GUD_LIVE_SES_TOPIC=...` (plus `AWS_*`) runs the suite with S3 catch-up every 2 s; the SNS webhook needs a public URL and is covered by the mock only. Not checked live: whether SES's stored raw MIME carries its own `Authentication-Results: amazonses.com` (the mock assumes it; the adapter does not rely on it), the exact receiving size cap, and real SNS certificate subjects.

## `postmark`

`PostmarkMailbox(server_token, address, *, store=None, message_stream="outbound", webhook_username=None, webhook_password=None, allowed_ips=(), inbound_aliases=(), authserv_id=None, max_message_bytes=10_000_000, send_quota_per_day=None, preserves_message_id=False, catchup_interval=60)`.

- **Send**: `POST /email`. Postmark has no raw send, so the adapter renders the message like `primitive`: `TextBody` = the human part, the envelope as attachment `gud.env` (bytes exact), and `Message-ID`/`In-Reply-To`/`References` in `Headers`. A raw-bytes `send` is re-rendered from its parsed fields. The delivered Message-ID is reported as `<MessageID>@mtasv.net`; set `preserves_message_id` if your server keeps custom Message-IDs. Messages over 10 MB are refused before the call. ErrorCode 405 (out of credits) and HTTP 429 raise `PostmarkQuotaError`. ErrorCode 400 (From is not a Sender Signature) raises `PostmarkError`.
- **Receive**: the inbound webhook JSON must carry `RawEmail` ("Include raw email content in JSON payload"); `capabilities().raw_mime` reports `RawEmailEnabled` from `GET /server`. Mail for addresses other than `address` and `inbound_aliases` (`OriginalRecipient`, `ToFull`, `CcFull`, `BccFull`) is ignored. Postmark's webhook has no delivery timestamp, so the received time is the local arrival time (catch-up copies use the search result's `ReceivedAt`).
- **Webhook auth**: Postmark does not sign webhooks. Put Basic credentials in the webhook URL (`https://user:pass@host/hook`) and set `allowed_ips = "postmark"` (the documented webhook IPs `3.134.147.250`, `50.31.156.6`, `50.31.156.77`, `18.217.206.57`). The source IP comes from `remote_addr=` or the `X-Gud-Remote-Addr` header, which the webhook server must set from the socket peer and strip from client input. With neither credentials nor an allow-list configured, nothing verifies.
- **Authentication-Results**: Postmark gives no DMARC verdict for inbound mail, so the adapter reports `auth_results="none"` and **no unsigned human message counts on a Postmark repo**. Humans sign their votes instead (`gud approve`). Pinning `authserv_id` is safe only if a forwarding MTA you control writes the topmost A-R before Postmark. Postmark adds none of its own, so a sender's forged header would otherwise be topmost (a unit test shows this).
- **Catch-up**: `GET /messages/inbound?recipient=` (newest first, paged until known messages), then `GET /messages/inbound/{id}/details` for `RawEmail`. A message whose details lack `RawEmail` cannot be stored byte-exactly and is skipped.

Verified against mock only: `tests/v1/transport/postmark_mock.py`. It has `POST /email` with Sender Signature, 10 MB, credits and rate-limit errors. It renders the MIME with Python's email package and forces `Message-ID: <id@mtasv.net>`. Inbound prepends `Received`/`X-Spam-*` and POSTs the documented inbound JSON with Basic auth from a documented IP. It also serves `GET /server`, inbound search and details. 11 conformance checks pass, with no positive control for human verification (nothing can verify) and no disconnect hook. Unit tests (`test_postmark.py`): send shape and threading headers, the Message-ID rewrite as reported, Basic auth and IP allow-list positive and negative, `RawEmail` required, `RawEmailEnabled=false`, the recipient filter and aliases, catch-up order and dedupe, the forged-A-R case, quota and credits, the size limit and config.

**Live setup (not done: no account).**

1. Create a server.
2. Add a Sender Signature or domain (DKIM, Return-Path).
3. In the inbound stream set the webhook URL `https://<user>:<pass>@<host>/postmark` and enable "Include raw email content in JSON payload".
4. Either point the repo domain's MX at `inbound.postmarkapp.com` (inbound domain forwarding) or forward the repo address to the server's inbound address, and list that address in `inbound_aliases`.

```toml
transport = "postmark"
address = "repo@example.org"
server_token_env = "POSTMARK_SERVER_TOKEN"
webhook_username = "gud"
webhook_password_env = "POSTMARK_HOOK_PASSWORD"
allowed_ips = "postmark"
```

`GUD_LIVE_POSTMARK=1 GUD_LIVE_POSTMARK_TOKEN=... GUD_LIVE_POSTMARK_SENDER=... GUD_LIVE_POSTMARK_REPO=...` runs the suite with search catch-up every 2 s; the webhook needs a public URL. Not checked live, and partly assumed from the docs: that inbound details include `RawEmail`, how `RawEmail` represents 8-bit bytes, whether Postmark honours a custom Message-ID header, its exact size accounting, and the current webhook IP list.

## `gmail`

`GmailMailbox(credentials_path, address, *, oauth=None, token_cache=None, account_type="auto", send_quota_per_day=None, max_message_bytes=25_000_000, pubsub_topic=None, push_audience=None, push_service_account=None, poll_interval=10, preserves_message_id=False)`.

- **OAuth**: `authorize_installed_app(client_id, client_secret)` runs the one-time installed-app consent (loopback redirect on 127.0.0.1, PKCE S256, `access_type=offline`) and returns a refresh token. Store it in an `authorized_user` JSON (`client_id`, `client_secret`, `refresh_token`). Access tokens are refreshed at `oauth2.googleapis.com/token` and cached (mode 0600) in `GUD_HOME/gmail/<address>.token.json`. A 401 refreshes once. `invalid_grant` raises `GmailAuthError`. Scopes: `gmail.readonly`, `gmail.send`.
- **Send**: `users.messages.send` with `raw` (base64url), or the media-upload endpoint (`message/rfc822`) above 4 MB. Gmail sets its own Message-ID, and the adapter reads it back (`format=metadata`) into `Sent.message_id`. Gmail replaces a From that is not the account or a send-as alias. Sending-limit errors (`rateLimitExceeded`, `dailyLimitExceeded`, ..., HTTP 429 after retries) raise `GmailQuotaError`; a 403 quota error is not retried.
- **Receive**: `users.history.list(historyTypes=messageAdded)` from the cursor's historyId, then `messages.get(format=raw)`. `internalDate` is the delivery time. Drafts and the account's own sends (SENT without INBOX/SPAM) are skipped; Spam is read. `list(since=None)` walks `messages.list(includeSpamTrash)` oldest first, then continues with history. A 404 from history (an expired historyId) falls back to a walk from the cursor's last `internalDate` (at least once).
- **Push**: `users.watch` on `pubsub_topic` (INBOX and SPAM), renewed once less than 6 days of its 7 remain. The Pub/Sub push webhook (`accept_webhook`) checks the OIDC JWT: RS256 against Google's JWKS (cached per `Cache-Control`, refetched at most once a minute for an unknown `kid`), `iss` `accounts.google.com`, `aud` = `push_audience`, `email` = `push_service_account` with `email_verified`, `exp`/`iat` within 60 s. It then wakes the subscriptions, which list from their cursors (a push carries only a historyId). Without a public URL the subscription polls history every `poll_interval` seconds.
- **Authentication-Results**: Gmail's MX writes `mx.google.com; ... dmarc=<result> (p=...) header.from=<domain>` on SMTP-received mail; pin `authserv-id mx.google.com`. Mail the account sends to itself does not pass the MX and has none.
- **Quota**: 500 a day for consumer accounts (`@gmail.com`, `@googlemail.com`), 2 000 for Workspace (by domain, or `account_type`, or `send_quota_per_day`). The Gmail API does not expose the remaining quota, so these are the documented values, not a probe.

Verified against mock only: `tests/v1/transport/gmail_mock.py`. It has the token endpoint (refresh and code grants, expiry, `invalid_grant`), JWKS, getProfile, send (JSON and media upload, From and Message-ID rewrite, a daily limit), messages.get/list, history.list (paging, 404 for old ids) and watch. Its MX prepends a Gmail-style A-R with comments, and a Pub/Sub push carries a signed JWT. All 11 conformance checks pass (30 s wake bound; measured latency is the push path). Unit tests (`test_gmail.py`): token cache (0600, reuse across instances, 401 refresh, expiry margin, `invalid_grant`, both credentials file formats), JWT positive plus ten negatives (wrong key, audience, issuer, service account, unverified email, expired, unknown kid, `alg=none`, HS256, malformed), push waking a subscription whose poll interval is 60 s, history cursors resumed one item at a time by fresh instances, the 404 fallback, the walk reading spam and skipping own sends, the Message-ID and From rewrites as reported, quotas and size limits, media upload, retries, and the installed-app flow end to end against a real loopback socket.

**Live setup (not done: no account).**

1. In a Google Cloud project, enable the Gmail API.
2. Configure the OAuth consent screen (add the repo account as a test user; refresh tokens of apps in "Testing" status expire after 7 days, and `gmail.readonly` is a restricted scope that needs verification for production) and create an OAuth client of type "Desktop app".
3. Get a refresh token: `python -c "from gud.transport.gmail import authorize_installed_app as a; print(a('<id>', '<secret>', login_hint='repo@gmail.com'))"`.
4. For push: create a Pub/Sub topic and grant `gmail-api-push@system.gserviceaccount.com` the Publisher role on it. Create a push subscription to the HTTPS webhook with authentication enabled (a service account, audience = the endpoint URL).

```toml
transport = "gmail"
address = "repo@gmail.com"
credentials_path = "/path/authorized_user.json"
pubsub_topic = "projects/<project>/topics/gud-gmail"
push_audience = "https://hooks.example.org/gmail"
push_service_account = "gud-push@<project>.iam.gserviceaccount.com"
```

`GUD_LIVE_GMAIL=1 GUD_LIVE_GMAIL_REPO_CREDENTIALS=... GUD_LIVE_GMAIL_REPO=... GUD_LIVE_GMAIL_SENDER_CREDENTIALS=... GUD_LIVE_GMAIL_SENDER=...` runs the suite polling every 5 s (keep `GUD_LIVE_MAX_SENDS` well under 500). Not checked live: whether Gmail keeps or rewrites a custom Message-ID (the adapter reads the real one back either way), the JSON `raw` size limit (hence media upload above 4 MB), history-id expiry timing, and real push latency.

## Conformance suite

`tests/v1/transport/conformance.py` holds the §20.2 checks, written against `Mailbox` only. `tests/v1/transport/test_conformance.py` runs them once per transport:

```
.venv/bin/python -m pytest tests/v1/transport/test_conformance.py -k imap_smtp
GUD_LIVE_PRIMITIVE=1 GUD_LIVE_ENV_FILE=/path/.env .venv/bin/python -m pytest tests/v1/transport/test_conformance.py -k primitive_live
.venv/bin/python -m pytest tests/v1/transport/test_conformance.py -k "jmap_mock or ses_mock or postmark_mock or gmail_mock"
GUD_LIVE_JMAP=1 / GUD_LIVE_SES=1 / GUD_LIVE_POSTMARK=1 / GUD_LIVE_GMAIL=1 ...   # live variants, skipped by default (see each adapter)
GUD_CONF_REPORT=report.json ...      # write every measurement as JSON
```

| Check | §20.2 | What it does |
| --- | --- | --- |
| `capabilities` | 8 | fields valid, values equal the documented ones (table above), `raw_mime` true |
| `roundtrip` | 1 | three envelopes (random bytes with NUL, lone CR, lone LF, CRLF, `.` lines, `From ` lines, 998+ and 5000-byte lines, non-UTF-8; a signed comment with non-ASCII text) arrive with identical gud ids and identical `gud.env` bytes; subject tag and `Gud-Id` line survive |
| `dedupe` | 2 | the same message sent twice gives one gud id; an intake keyed by gud id logs it once |
| `ordering` | 3 | three messages 1.1 s apart: `received_at` in ms, non-decreasing, list order equals send order |
| `wait_latency` | 4 | `wait` returns within 5 s (API, fake) or 30 s (IDLE, Gmail) of delivery |
| `subscribe_latency` | 4 | the push intake fires within the same bound |
| `subscribe_reconnect` | 4, 7 | IMAP and JMAP: the IDLE socket or EventSource stream is dropped; the subscription reconnects and catches up |
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

New adapters, same day, against their mocks only (`jmap_mock`, `ses_mock`, `postmark_mock`, `gmail_mock`): all 11 checks pass on each (44 tests). The whole transport conformance run (88 passed, 55 live variants skipped) takes about 110 s. Measured:

| | `wait` after delivery | `subscribe` after delivery | 25 MiB blob path | Message-ID | spoofed human |
| --- | --- | --- | --- | --- | --- |
| jmap_mock | 0.35 s (`receivedAt` whole seconds) | 0.36 s (EventSource); reconnect after a dropped stream 1.0 s | 4 chunks, largest MIME 10 044 929 B of 50 000 000, 9.8 s | preserved | submission refused (`forbiddenFrom`); genuine control verified |
| ses_mock | 0.005 s | 0.004 s (SNS webhook) | 4 chunks, 10 044 926 B of 40 000 000, 8.4 s | rewritten, reported | refused (`MessageRejected`); genuine control verified |
| postmark_mock | 0.002 s | 0.003 s (webhook) | 4 chunks, 9 935 133 B of 10 000 000, 6.2 s | rewritten, reported | refused (not a Sender Signature); nothing verifies (no DMARC verdict) |
| gmail_mock | 0.008 s | 0.005 s (Pub/Sub push; poll fallback 10 s) | 4 chunks, 10 044 937 B of 25 000 000, 18.1 s | rewritten, read back | accepted with From rewritten to the account; genuine control verified |

These numbers show the adapters' logic and overheads against faithful-as-possible models of each API. They are not provider latencies. No adapter added here has been run against a real account.
