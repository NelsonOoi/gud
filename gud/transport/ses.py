"""Amazon SES mailbox: SESv2 raw sends out, SES receipt rules -> SNS -> HTTPS webhook in.

Outbound: `POST /v2/email/outbound-emails` (SESv2 SendEmail) with `Content.Raw.Data`, the raw
RFC 5322 bytes base64-encoded, and `Destination` = To + Cc. Requests are signed with AWS
Signature Version 4, implemented here with `hmac`/`hashlib` (no boto3). SES **rewrites the
Message-ID** to `<SES message id>@<region>.amazonses.com` (`email.amazonses.com` in us-east-1);
`Sent.message_id` reports that.

Inbound: SES has no mailbox API. A receipt rule for the repo address delivers each message with
either

- an **SNS action** with encoding Base64 (or UTF-8): the notification carries the raw MIME in
  `content`. SNS notifications are capped at 150 KB, so this only suits small repos; or
- an **S3 action** with an SNS topic: the notification names the object (`receipt.action.
  bucketName` / `objectKey`) and the adapter fetches it from S3 (SigV4, the configured
  `inbound_bucket` only). Messages up to 40 MB.

The webhook handler calls `accept_webhook(headers, body)`. It verifies the SNS signature
(SignatureVersion 1 = SHA1withRSA, 2 = SHA256withRSA) against the signing certificate, which is
fetched only from `https://sns.<region>.amazonaws.com/...pem` (the topic's region), checked for
validity and subject, and pinned per URL. The TopicArn must be one of `topic_arns` (any AWS
account can create a topic whose messages AWS signs, so the signature alone proves nothing
about who sent it). A `SubscriptionConfirmation` from an allowed topic is confirmed by fetching
its `SubscribeURL` (same host rule).

Accepted messages go into a local `Spool` (the v0 HostedMailbox pattern); `list`/`wait`/
`subscribe` read it. `catch_up()` lists the S3 prefix and ingests any object the webhook missed.
Received time is `receipt.timestamp` (ms), or the S3 object's `LastModified` for catch-up.

Authentication-Results: SES reports DMARC as structured data (`receipt.dmarcVerdict.status`).
The adapter prepends `ses.gud.invalid; dmarc=<pass|fail|none|temperror> header.from=<From
domain>`. Catch-up copies have no verdict and get `dmarc=none`, so an unsigned human message
counts only when it arrived by webhook.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlsplit

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gud.core.types import Ms

from . import auth, mime
from .base import (
    Capabilities, Outgoing, QuotaExceeded, Received, Sent, Subscription, ThreadSubscription,
    TransportError, cfg_int, cfg_list, safe_call,
)
from .httputil import RETRY_STATUSES, header, parse_iso_ms, retry_delay
from .spool import Spool

AUTHSERV_ID = "ses.gud.invalid"
MAX_MESSAGE_BYTES = 40_000_000           # SESv2 SendEmail and S3-action receiving: 40 MB
SNS_CONTENT_MAX = 150_000                # SNS-action receiving: 150 KB
SNS_HOST = re.compile(r"^sns\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?$")
SETUP_OBJECT = "AMAZON_SES_SETUP_NOTIFICATION"
_QUOTA_TYPES = {"TooManyRequestsException", "LimitExceededException", "SendingPausedException",
                "Throttling", "ThrottlingException"}
_MAX_ATTEMPTS = 4
_DMARC = {"PASS": "pass", "FAIL": "fail", "GRAY": "none", "PROCESSING_FAILED": "temperror"}


class SesError(TransportError):
    def __init__(self, code: str, message: str, *, status: int | None = None):
        self.code, self.message, self.status = code, message, status
        super().__init__(f"{code}: {message}")


class SesQuotaError(SesError, QuotaExceeded):
    """Throttled, the daily quota is used up, or sending is paused."""


class SnsVerificationError(TransportError):
    """An SNS message whose signature, certificate URL or topic does not check out."""


@dataclass(frozen=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AwsCredentials":
        env = os.environ if env is None else env
        key, secret = env.get("AWS_ACCESS_KEY_ID"), env.get("AWS_SECRET_ACCESS_KEY")
        if not key or not secret:
            raise TransportError("AWS credentials missing (access_key_id / secret_access_key)")
        return cls(key, secret, env.get("AWS_SESSION_TOKEN") or None)


# ---------------------------------------------------------------- Signature Version 4


def _uri_encode(value: str, *, safe: str = "") -> str:
    return quote(value, safe="-_.~" + safe)


def sigv4_headers(method: str, url: str, *, region: str, service: str, credentials: AwsCredentials,
                  body: bytes = b"", headers: Mapping[str, str] | None = None,
                  amz_date: str | None = None, unsigned_payload: bool = False) -> dict[str, str]:
    """Headers to add to a request so AWS accepts it (Authorization, X-Amz-Date, ...)."""
    parts = urlsplit(url)
    amz_date = amz_date or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    payload_hash = "UNSIGNED-PAYLOAD" if unsigned_payload else hashlib.sha256(body).hexdigest()
    out = {k: v for k, v in (headers or {}).items()}
    out["X-Amz-Date"] = amz_date
    if service == "s3":
        out["X-Amz-Content-Sha256"] = payload_hash
    if credentials.session_token:
        out["X-Amz-Security-Token"] = credentials.session_token
    host = parts.hostname or ""
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    canon_headers = {"host": host}
    for key, value in out.items():
        canon_headers[key.lower()] = re.sub(r"\s+", " ", str(value).strip())
    signed = ";".join(sorted(canon_headers))
    path = parts.path or "/"
    canon_uri = _uri_encode(path, safe="/") if service == "s3" else _uri_encode(_uri_encode(path, safe="/"), safe="/")
    query = []
    for item in parts.query.split("&") if parts.query else []:
        key, _, value = item.partition("=")
        query.append((_uri_encode(unquote(key)), _uri_encode(unquote(value))))
    canon_query = "&".join(f"{k}={v}" for k, v in sorted(query))
    canonical = "\n".join([
        method.upper(), canon_uri, canon_query,
        "".join(f"{k}:{canon_headers[k]}\n" for k in sorted(canon_headers)), signed, payload_hash,
    ])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + credentials.secret_access_key).encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


# ---------------------------------------------------------------- SNS message verification

_NOTIFICATION_KEYS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SUBSCRIPTION_KEYS = ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type")


def sns_string_to_sign(msg: Mapping[str, Any]) -> bytes:
    """The canonical bytes SNS signs: `Key\\nValue\\n` for the type's keys in order (Subject
    only when present)."""
    kind = msg.get("Type")
    keys = _NOTIFICATION_KEYS if kind == "Notification" else _SUBSCRIPTION_KEYS
    out = []
    for key in keys:
        if key not in msg or msg[key] is None:
            if key == "Subject":
                continue
            raise SnsVerificationError(f"SNS {kind} lacks {key}")
        out.append(f"{key}\n{msg[key]}\n")
    return "".join(out).encode("utf-8")


def check_sns_url(url: str, *, region: str | None = None, pem: bool = True) -> str:
    """The URL's region when it is an HTTPS URL on `sns.<region>.amazonaws.com`, else raise.
    No userinfo, no explicit port other than 443, and (for certificates) a `.pem` path."""
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError as exc:
        raise SnsVerificationError(f"malformed SNS URL {url!r}") from exc
    host = (parts.hostname or "").lower()
    match = SNS_HOST.match(host)
    if parts.scheme != "https" or not match or parts.username or parts.password or port not in (None, 443) \
            or "@" in parts.netloc:
        raise SnsVerificationError(f"SNS URL {url!r} is not on an allow-listed sns.<region>.amazonaws.com host")
    if pem and not parts.path.endswith(".pem"):
        raise SnsVerificationError(f"SNS signing certificate URL {url!r} is not a .pem file")
    if region and match.group(1) != region:
        raise SnsVerificationError(f"SNS URL {url!r} is not in region {region}")
    return match.group(1)


def _topic_region(arn: str) -> str | None:
    parts = (arn or "").split(":")
    return parts[3] if len(parts) >= 6 and parts[0] == "arn" and parts[2] == "sns" else None


class SnsVerifier:
    """SNS signature verification with a per-URL certificate pin."""

    def __init__(self, client: httpx.Client, topic_arns: tuple[str, ...], *,
                 clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.topic_arns = tuple(topic_arns)
        self.clock = clock
        self._certs: dict[str, x509.Certificate] = {}
        self._lock = threading.Lock()

    def certificate(self, url: str) -> x509.Certificate:
        with self._lock:
            cert = self._certs.get(url)
        if cert is None:
            resp = self.client.get(url, timeout=30.0)
            if resp.status_code != 200:
                raise SnsVerificationError(f"fetching {url} gave HTTP {resp.status_code}")
            try:
                cert = x509.load_pem_x509_certificate(resp.content)
            except ValueError as exc:
                raise SnsVerificationError(f"{url} is not a PEM certificate") from exc
            names = [a.value for a in cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)]
            try:
                names += cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            except x509.ExtensionNotFound:
                pass
            if not any(str(n).lower().rstrip(".") in ("sns.amazonaws.com", "sns.amazonaws.com.cn")
                       or SNS_HOST.match(str(n).lower()) for n in names):
                raise SnsVerificationError(f"{url} is not an SNS signing certificate (subject {names})")
            if not isinstance(cert.public_key(), rsa.RSAPublicKey):
                raise SnsVerificationError("SNS signing certificate has no RSA key")
            with self._lock:
                cert = self._certs.setdefault(url, cert)   # pinned: the first certificate wins
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
            raise SnsVerificationError(f"SNS signing certificate {url} is not valid now")
        return cert

    def verify(self, msg: Mapping[str, Any]) -> None:
        """Raise SnsVerificationError unless `msg` is a genuine SNS message from an allowed topic."""
        if not isinstance(msg, Mapping):
            raise SnsVerificationError("SNS message must be a JSON object")
        topic = str(msg.get("TopicArn") or "")
        if topic not in self.topic_arns:
            raise SnsVerificationError(f"TopicArn {topic!r} is not allowed")
        version = str(msg.get("SignatureVersion") or "")
        algorithm = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(version)
        if algorithm is None:
            raise SnsVerificationError(f"unsupported SignatureVersion {version!r}")
        url = str(msg.get("SigningCertURL") or msg.get("SigningCertUrl") or "")
        check_sns_url(url, region=_topic_region(topic))
        try:
            signature = base64.b64decode(str(msg.get("Signature") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise SnsVerificationError("Signature is not base64") from exc
        cert = self.certificate(url)
        key = cert.public_key()
        assert isinstance(key, rsa.RSAPublicKey)
        try:
            key.verify(signature, sns_string_to_sign(msg), padding.PKCS1v15(), algorithm)
        except InvalidSignature as exc:
            raise SnsVerificationError("SNS signature does not verify") from exc


# ---------------------------------------------------------------- mailbox


def _domain_of(raw: bytes) -> str | None:
    try:
        sender = mime.parse(raw).from_addr
    except mime.MimeError:
        return None
    return sender.rsplit("@", 1)[1].lower() if "@" in sender else None


def message_id_domain(region: str) -> str:
    return "email.amazonses.com" if region == "us-east-1" else f"{region}.amazonses.com"


class SesMailbox:
    def __init__(self, region: str, credentials: AwsCredentials, address: str, *,
                 inbound_bucket: str | None = None, inbound_prefix: str = "",
                 topic_arns: tuple[str, ...] = (), store: Path | str | None = None,
                 client: httpx.Client | None = None, ses_endpoint: str | None = None,
                 s3_endpoint: str | None = None, authserv_id: str = AUTHSERV_ID,
                 max_message_bytes: int | None = None, send_quota_per_day: int | None = None,
                 configuration_set: str | None = None, catchup_interval: float = 60.0,
                 confirm_subscriptions: bool = True,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.region = region
        self.credentials = credentials
        self.address = address
        self.inbound_bucket = inbound_bucket
        self.inbound_prefix = inbound_prefix
        self.topic_arns = tuple(topic_arns)
        self.ses_endpoint = (ses_endpoint or f"https://email.{region}.amazonaws.com").rstrip("/")
        self.s3_endpoint = (s3_endpoint or f"https://s3.{region}.amazonaws.com").rstrip("/")
        self._s3_path_style = bool(s3_endpoint)
        self.authserv_id = authserv_id
        self.configuration_set = configuration_set
        self.catchup_interval = catchup_interval
        self.confirm_subscriptions = confirm_subscriptions
        self.clock = clock
        self._sleep = sleep
        self._max = max_message_bytes
        self._quota = send_quota_per_day
        self._quota_probed = send_quota_per_day is not None
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        if store is None:
            home = os.environ.get("GUD_HOME") or str(Path.home() / ".gud")
            store = Path(home) / "inbound" / "ses" / address.lower()
        self.spool = Spool(store)
        self.sns = SnsVerifier(self._client, self.topic_arns, clock=clock)
        self.confirmed: list[str] = []

    # ------------------------------------------------------------ AWS requests

    def _aws(self, method: str, url: str, service: str, *, body: bytes = b"",
             headers: dict | None = None, timeout: float = 60.0) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            signed = sigv4_headers(method, url, region=self.region, service=service, credentials=self.credentials,
                                   body=body, headers=headers or {})
            try:
                resp = self._client.request(method, url, headers=signed, content=body or None, timeout=timeout)
            except httpx.HTTPError as exc:
                last = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    self._sleep(min(8.0, 0.5 * 2 ** attempt))
                    continue
                raise SesError("NetworkError", str(exc)) from exc
            if resp.status_code in RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                self._sleep(retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise _aws_error(resp)
            return resp
        raise SesError("NetworkError", str(last))

    # ------------------------------------------------------------ contract

    def capabilities(self) -> Capabilities:
        if not self._quota_probed:
            self._quota_probed = True
            try:
                account = self._aws("GET", f"{self.ses_endpoint}/v2/email/account", "ses").json()
                max24 = float((account.get("SendQuota") or {}).get("Max24HourSend"))
                self._quota = None if max24 < 0 else int(max24)
            except (TransportError, ValueError, TypeError, AttributeError):
                self._quota = None
        if self._max:
            limit = self._max
        elif self.topic_arns and not self.inbound_bucket:
            limit = SNS_CONTENT_MAX
        else:
            limit = MAX_MESSAGE_BYTES
        return Capabilities(
            name="ses", max_message_bytes=limit, push="webhook", preserves_message_id=False,
            auth_results="synthesized", authserv_id=self.authserv_id, send_quota_per_day=self._quota,
            received_time_resolution_ms=1, raw_mime=True,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, Outgoing):
            raw = mime.build(message)
        elif isinstance(message, (bytes, bytearray)):
            raw = bytes(message)
        else:
            raise TypeError("send() takes an Outgoing or raw bytes")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise SesError("MessageRejected", f"message is {len(raw)} bytes; SES takes at most {MAX_MESSAGE_BYTES}")
        parsed = mime.parse(raw)
        if not (parsed.to or parsed.cc):
            raise TransportError("message has no recipients")
        payload: dict[str, Any] = {
            "FromEmailAddress": parsed.from_addr,
            "Destination": {"ToAddresses": list(parsed.to), "CcAddresses": list(parsed.cc)},
            "Content": {"Raw": {"Data": base64.b64encode(raw).decode("ascii")}},
        }
        if self.configuration_set:
            payload["ConfigurationSetName"] = self.configuration_set
        body = json.dumps(payload).encode()
        resp = self._aws("POST", f"{self.ses_endpoint}/v2/email/outbound-emails", "ses", body=body,
                         headers={"Content-Type": "application/json"}, timeout=300.0)
        message_id = str((resp.json() or {}).get("MessageId") or "")
        if not message_id:
            raise SesError("InvalidResponse", "SendEmail returned no MessageId")
        return Sent(provider_id=message_id, message_id=f"<{message_id}@{message_id_domain(self.region)}>",
                    accepted_at=int(self.clock() * 1000))

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        return self.spool.list(since=since, limit=limit)

    def head(self) -> str | None:
        return self.spool.head()

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        return self.spool.wait(since=since, timeout=timeout,
                               on_idle=self.catch_up if self.inbound_bucket else None,
                               idle_every=self.catchup_interval)

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        start = since if since is not None else self.head()

        def loop(stop: threading.Event) -> None:
            cursor = start
            next_catch_up = 0.0
            while not stop.is_set():
                if self.inbound_bucket and time.monotonic() >= next_catch_up:
                    try:
                        self.catch_up()
                    except TransportError:
                        pass
                    next_catch_up = time.monotonic() + self.catchup_interval
                items, cursor = self.spool.list(since=cursor, limit=100)
                for item in items:
                    if stop.is_set():
                        return
                    safe_call(callback, item)
                if not items:
                    with self.spool._cond:
                        self.spool._cond.wait(self.spool.poll_interval)

        return ThreadSubscription(loop, name=f"ses-spool-{self.address}")

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    # ------------------------------------------------------------ webhook (WebhookVerifier)

    def verify(self, headers: Mapping[str, str], body: bytes, now: Ms) -> bool:
        try:
            self._verified(headers, body)
        except (TransportError, ValueError):
            return False
        return True

    def _verified(self, headers: Mapping[str, str], body: bytes) -> dict:
        try:
            msg = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnsVerificationError("SNS body is not JSON") from exc
        declared = header(headers, "x-amz-sns-message-type")
        if declared and isinstance(msg, dict) and declared != msg.get("Type"):
            raise SnsVerificationError("x-amz-sns-message-type does not match the body")
        self.sns.verify(msg)
        return msg

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[Received]:
        """A verified SNS body -> the delivered message (other types and recipients -> [])."""
        try:
            msg = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError("SNS body is not JSON") from exc
        if not isinstance(msg, dict) or msg.get("Type") != "Notification":
            return []
        try:
            note = json.loads(msg.get("Message") or "")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(note, dict) or note.get("notificationType") != "Received":
            return []
        mail, receipt = note.get("mail") or {}, note.get("receipt") or {}
        recipients = [str(r).lower() for r in receipt.get("recipients") or mail.get("destination") or []]
        if self.address.lower() not in recipients:
            return []
        pid = str(mail.get("messageId") or "")
        if not pid:
            raise TransportError("SES notification has no mail.messageId")
        action = receipt.get("action") or {}
        raw = self._content(note, action)
        dmarc = _DMARC.get(str((receipt.get("dmarcVerdict") or {}).get("status") or "").upper(), "none")
        when = parse_iso_ms(receipt.get("timestamp")) or parse_iso_ms(mail.get("timestamp")) or int(self.clock() * 1000)
        return [Received(raw_mime=self._with_auth(raw, dmarc), received_at=when, provider_id=pid, cursor="")]

    def accept_webhook(self, headers: Mapping[str, str], body: bytes, now: Ms | None = None) -> list[Received]:
        """Verify, confirm a subscription, or store the delivered message in the spool. Returns
        the newly stored items (with spool cursors). Raises SnsVerificationError on a bad
        message; the webhook route should answer 403 then, and 200 otherwise."""
        msg = self._verified(headers, body)
        kind = msg.get("Type")
        if kind == "SubscriptionConfirmation":
            if self.confirm_subscriptions:
                self.confirm(msg)
            return []
        out = []
        for item in self.parse(headers, body):
            stored = self.spool.ingest(item.raw_mime, item.received_at, item.provider_id, source="sns")
            if stored is not None:
                out.append(stored)
        return out

    def confirm(self, msg: Mapping[str, Any]) -> None:
        url = str(msg.get("SubscribeURL") or "")
        check_sns_url(url, region=_topic_region(str(msg.get("TopicArn") or "")), pem=False)
        resp = self._client.get(url, timeout=30.0)
        if resp.status_code >= 400:
            raise SesError("SubscriptionConfirmationFailed", f"HTTP {resp.status_code}")
        self.confirmed.append(str(msg.get("TopicArn")))

    # ------------------------------------------------------------ S3

    def _s3_url(self, key: str = "", query: str = "") -> str:
        """Virtual-hosted style by default; path style when `s3_endpoint` is configured."""
        if self._s3_path_style:
            url = f"{self.s3_endpoint}/{self.inbound_bucket}"
        else:
            url = f"https://{self.inbound_bucket}.s3.{self.region}.amazonaws.com"
        if key:
            url += "/" + _uri_encode(key, safe="/")
        return url + (f"?{query}" if query else "")

    def _s3_get(self, bucket: str, key: str) -> bytes:
        if not self.inbound_bucket or bucket != self.inbound_bucket:
            raise SesError("AccessDenied", f"notification names bucket {bucket!r}, not the configured inbound bucket")
        return self._aws("GET", self._s3_url(key), "s3", timeout=300.0).content

    def _s3_list(self) -> list[tuple[str, Ms]]:
        out: list[tuple[str, Ms]] = []
        token: str | None = None
        while True:
            params = [("list-type", "2"), ("prefix", self.inbound_prefix)]
            if token:
                params.append(("continuation-token", token))
            query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in params)
            root = ET.fromstring(self._aws("GET", self._s3_url(query=query), "s3").content)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for item in root.iter(f"{ns}Contents"):
                key = item.findtext(f"{ns}Key") or ""
                when = parse_iso_ms(item.findtext(f"{ns}LastModified")) or 0
                out.append((key, when))
            if (root.findtext(f"{ns}IsTruncated") or "").lower() != "true":
                break
            token = root.findtext(f"{ns}NextContinuationToken")
            if not token:
                break
        return out

    def catch_up(self) -> int:
        """Ingest S3 objects the webhook missed (oldest LastModified first). Returns the count.
        Every object under `inbound_prefix` is taken to be this address's mail, so the receipt
        rule for the repo address must write to a prefix of its own."""
        if not self.inbound_bucket:
            return 0
        have = self.spool.ids()
        added = 0
        for key, when in sorted(self._s3_list(), key=lambda kv: (kv[1], kv[0])):
            pid = key[len(self.inbound_prefix):] if key.startswith(self.inbound_prefix) else key
            if not pid or pid.endswith(SETUP_OBJECT) or pid in have:
                continue
            raw = self._s3_get(self.inbound_bucket, key)
            if self.spool.ingest(self._with_auth(raw, "none"), when, pid, source="s3") is not None:
                added += 1
        return added

    # ------------------------------------------------------------ internals

    def _content(self, note: dict, action: dict) -> bytes:
        kind = str(action.get("type") or "")
        content = note.get("content")
        if isinstance(content, str) and content:
            if str(action.get("encoding") or "").upper() == "BASE64":
                try:
                    return base64.b64decode(content, validate=False)
                except ValueError as exc:
                    raise TransportError("SES notification content is not base64") from exc
            return content.encode("utf-8")
        if kind == "S3":
            return self._s3_get(str(action.get("bucketName") or ""), str(action.get("objectKey") or ""))
        raise TransportError("SES notification carries no content and no S3 object")

    def _with_auth(self, raw: bytes, dmarc: str) -> bytes:
        domain = _domain_of(raw)
        if not domain:
            return raw
        return auth.prepend(raw, auth.synthesize(self.authserv_id, dmarc, domain))


def _aws_error(resp: httpx.Response) -> SesError:
    code = (resp.headers.get("x-amzn-ErrorType") or "").split(":")[0] or f"HTTP{resp.status_code}"
    message = (resp.text or "")[:500]
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = str(body.get("__type") or code).split("#")[-1]
            message = str(body.get("message") or body.get("Message") or message)
    except ValueError:
        try:
            root = ET.fromstring(resp.content)
            code = root.findtext(".//Code") or code
            message = root.findtext(".//Message") or message
        except ET.ParseError:
            pass
    cls = SesQuotaError if (resp.status_code == 429 or code in _QUOTA_TYPES) else SesError
    return cls(code, message, status=resp.status_code)


def from_config(cfg: Mapping[str, str]) -> SesMailbox:
    """Keys: region, address; access_key_id + secret_access_key (+ session_token), else the
    AWS_* environment; inbound: topic_arns (comma list), inbound_bucket, inbound_prefix, store;
    optional authserv_id, max_message_bytes, send_quota_per_day, configuration_set,
    ses_endpoint, s3_endpoint, catchup_interval."""
    if not cfg.get("region"):
        raise TransportError("ses needs region")
    if not cfg.get("address"):
        raise TransportError("ses needs address")
    if cfg.get("access_key_id") and cfg.get("secret_access_key"):
        creds = AwsCredentials(cfg["access_key_id"], cfg["secret_access_key"], cfg.get("session_token") or None)
    else:
        creds = AwsCredentials.from_env()
    return SesMailbox(
        cfg["region"], creds, cfg["address"], inbound_bucket=cfg.get("inbound_bucket") or None,
        inbound_prefix=cfg.get("inbound_prefix") or "", topic_arns=cfg_list(cfg, "topic_arns", ()),
        store=cfg.get("store") or None, ses_endpoint=cfg.get("ses_endpoint") or None,
        s3_endpoint=cfg.get("s3_endpoint") or None, authserv_id=cfg.get("authserv_id") or AUTHSERV_ID,
        max_message_bytes=cfg_int(cfg, "max_message_bytes", None),
        send_quota_per_day=cfg_int(cfg, "send_quota_per_day", None),
        configuration_set=cfg.get("configuration_set") or None,
        catchup_interval=float(cfg_int(cfg, "catchup_interval", 60) or 60),
    )
