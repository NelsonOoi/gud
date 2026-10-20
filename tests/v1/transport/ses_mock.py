"""An in-process stand-in for SESv2, S3 and SNS (httpx.MockTransport), for CI.

Built from the public API docs:

- SESv2 `POST /v2/email/outbound-emails` (SendEmail, `Content.Raw`) and `GET /v2/email/account`
  (`SendQuota.Max24HourSend`), with SigV4 checked by an independent implementation here. The
  From identity must be verified (`MessageRejected` otherwise); messages over 40 MB are refused;
  `fail_next` injects throttling (`TooManyRequestsException`, HTTP 429). SES rewrites the
  Message-ID to `<message id>@email.amazonses.com` and adds a `DKIM-Signature`.
- Receiving for `receiving_domain`: SES's MX prepends `Return-Path`, `Received`,
  `X-SES-Spam-Verdict`, `X-SES-Virus-Verdict`, `Received-SPF` and an `Authentication-Results:
  amazonses.com` line, then runs the receipt rule: an **S3 action** (object
  `<prefix><inbound message id>`) followed by an SNS notification naming the object, or an
  **SNS action** with Base64 content (only up to 150 KB; larger mail bounces). The notification
  carries `receipt.dmarcVerdict`.
- SNS: notifications are signed (SignatureVersion 1 or 2) with an RSA key whose self-signed
  certificate (CN `sns.amazonaws.com`) is served at the `SigningCertURL` on
  `sns.<region>.amazonaws.com`. A subscription starts with a signed SubscriptionConfirmation
  whose `SubscribeURL` the subscriber must GET.
- S3 `GET /<key>` and `GET /?list-type=2` (ListObjectsV2 XML, paginated by `max_keys`), both
  virtual-hosted and path style.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Callable
from urllib.parse import parse_qsl, quote

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from gud.transport import mime

ACCESS_KEY = "AKIDMOCKSES"
SECRET_KEY = "mock-secret-key"
REGION = "us-east-1"
ACCOUNT = "123456789012"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:gud-inbound"
CERT_URL = f"https://sns.{REGION}.amazonaws.com/SimpleNotificationService-mock0123456789.pem"
SES_MAX = 40_000_000
SNS_MAX = 150_000


def make_cert(common_name: str = "sns.amazonaws.com", *, days: int = 365, key=None):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days)).sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM)


def string_to_sign(msg: dict) -> bytes:
    if msg["Type"] == "Notification":
        keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    else:
        keys = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    return "".join(f"{k}\n{msg[k]}\n" for k in keys if k in msg).encode()


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class S3Object:
    key: str
    data: bytes
    last_modified: float


@dataclass
class MockAws:
    receiving_domain: str = "ses.test"
    verified_domains: tuple = ("ses.test",)
    bucket: str = "gud-inbound"
    prefix: str = "repo/"
    action: str = "s3"                                  # "s3" or "sns"
    signature_version: str = "1"
    max24: float = 50_000.0
    clock: Callable[[], float] = time.time
    sns_sink: Callable[[dict, bytes], None] | None = None
    objects: dict = field(default_factory=dict)         # key -> S3Object
    sent: list = field(default_factory=list)
    fail_next: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    confirmed: list = field(default_factory=list)
    max_keys: int = 1000

    def __post_init__(self) -> None:
        self.key, self.cert_pem = make_cert()
        self.extra_certs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    # ------------------------------------------------------------ SigV4 (independent check)

    def _sigv4_ok(self, request: httpx.Request, service: str) -> bool:
        authz = request.headers.get("authorization", "")
        m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request, "
                     r"SignedHeaders=([^,]+), Signature=([0-9a-f]{64})$", authz)
        if not m or m.group(1) != ACCESS_KEY or m.group(3) != REGION or m.group(4) != service:
            return False
        signed = m.group(5).split(";")
        amz_date = request.headers.get("x-amz-date", "")
        body = request.content
        payload = hashlib.sha256(body).hexdigest()
        if service == "s3" and request.headers.get("x-amz-content-sha256") not in (payload, "UNSIGNED-PAYLOAD"):
            return False
        if service == "s3":
            payload = request.headers["x-amz-content-sha256"]
        enc = lambda s: quote(s, safe="-_.~")  # noqa: E731
        path = request.url.raw_path.split(b"?")[0].decode()
        from urllib.parse import unquote
        canon_path = quote(unquote(path), safe="/-_.~")
        if service != "s3":
            canon_path = quote(canon_path, safe="/-_.~")
        query = sorted((enc(k), enc(v)) for k, v in parse_qsl(request.url.query.decode(), keep_blank_values=True))
        lines = []
        for name in signed:
            value = request.headers.get(name, "") if name != "host" else request.url.netloc.decode()
            lines.append(f"{name}:{' '.join(value.split())}\n")
        canonical = "\n".join([request.method, canon_path, "&".join(f"{k}={v}" for k, v in query),
                               "".join(lines), ";".join(signed), payload])
        scope = f"{m.group(2)}/{REGION}/{service}/aws4_request"
        sts = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        k = ("AWS4" + SECRET_KEY).encode()
        for part in (m.group(2), REGION, service, "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        return hmac.compare_digest(hmac.new(k, sts.encode(), hashlib.sha256).hexdigest(), m.group(6))

    # ------------------------------------------------------------ HTTP

    def _ses_error(self, status: int, code: str, message: str) -> httpx.Response:
        return httpx.Response(status, json={"message": message}, headers={"x-amzn-ErrorType": f"{code}:"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        self.requests.append((request.method, host, path))
        if host == f"sns.{REGION}.amazonaws.com" or host in self.extra_hosts():
            return self._sns(request)
        if host == f"email.{REGION}.amazonaws.com":
            if not self._sigv4_ok(request, "ses"):
                return self._ses_error(403, "SignatureDoesNotMatch", "bad signature")
            if self.fail_next:
                status = self.fail_next.pop(0)
                return self._ses_error(status, "TooManyRequestsException" if status == 429 else "InternalFailure",
                                       "injected")
            if request.method == "POST" and path == "/v2/email/outbound-emails":
                return self._send(json.loads(request.content))
            if request.method == "GET" and path == "/v2/email/account":
                return httpx.Response(200, json={"SendQuota": {"Max24HourSend": self.max24, "MaxSendRate": 14.0,
                                                               "SentLast24Hours": float(len(self.sent))},
                                                 "SendingEnabled": True, "ProductionAccessEnabled": True})
            return self._ses_error(404, "NotFoundException", path)
        bucket, key = None, None
        if host == f"{self.bucket}.s3.{REGION}.amazonaws.com":
            bucket, key = self.bucket, path.lstrip("/")
        elif host == f"s3.{REGION}.amazonaws.com":
            bucket, _, key = path.lstrip("/").partition("/")
        if bucket is not None:
            if not self._sigv4_ok(request, "s3"):
                return httpx.Response(403, text="<Error><Code>SignatureDoesNotMatch</Code><Message>x</Message></Error>")
            if bucket != self.bucket:
                return httpx.Response(404, text="<Error><Code>NoSuchBucket</Code><Message>x</Message></Error>")
            from urllib.parse import unquote
            key = unquote(key)
            if key:
                obj = self.objects.get(key)
                if obj is None:
                    return httpx.Response(404, text="<Error><Code>NoSuchKey</Code><Message>x</Message></Error>")
                return httpx.Response(200, content=obj.data)
            return self._list_objects(request.url.params)
        return httpx.Response(404, text=f"no mock for {host}")

    def extra_hosts(self) -> set[str]:
        return {re.match(r"https://([^/]+)/", u).group(1) for u in self.extra_certs}

    def _sns(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == CERT_URL:
            return httpx.Response(200, content=self.cert_pem)
        if url in self.extra_certs:
            return httpx.Response(200, content=self.extra_certs[url])
        params = dict(request.url.params)
        if params.get("Action") == "ConfirmSubscription":
            self.confirmed.append((params.get("TopicArn"), params.get("Token")))
            return httpx.Response(200, text="<ConfirmSubscriptionResponse><ConfirmSubscriptionResult>"
                                            f"<SubscriptionArn>{params.get('TopicArn')}:sub</SubscriptionArn>"
                                            "</ConfirmSubscriptionResult></ConfirmSubscriptionResponse>")
        return httpx.Response(404, text="not found")

    def _list_objects(self, params) -> httpx.Response:
        prefix = params.get("prefix") or ""
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        token = params.get("continuation-token")
        if token:
            keys = [k for k in keys if k > base64.b64decode(token).decode()]
        page, more = keys[:self.max_keys], len(keys) > self.max_keys
        items = "".join(f"<Contents><Key>{k}</Key><LastModified>{_iso(self.objects[k].last_modified)}</LastModified>"
                        f"<ETag>&quot;{hashlib.md5(self.objects[k].data).hexdigest()}&quot;</ETag>"
                        f"<Size>{len(self.objects[k].data)}</Size><StorageClass>STANDARD</StorageClass></Contents>"
                        for k in page)
        nxt = f"<NextContinuationToken>{base64.b64encode(page[-1].encode()).decode()}</NextContinuationToken>" if more else ""
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
               f"<Name>{self.bucket}</Name><Prefix>{prefix}</Prefix><KeyCount>{len(page)}</KeyCount>"
               f"<MaxKeys>{self.max_keys}</MaxKeys><IsTruncated>{'true' if more else 'false'}</IsTruncated>"
               f"{items}{nxt}</ListBucketResult>")
        return httpx.Response(200, content=xml.encode(), headers={"content-type": "application/xml"})

    # ------------------------------------------------------------ sending and receiving

    def _send(self, body: dict) -> httpx.Response:
        raw_b64 = ((body.get("Content") or {}).get("Raw") or {}).get("Data")
        if not raw_b64:
            return self._ses_error(400, "BadRequestException", "only Raw content is mocked")
        raw = base64.b64decode(raw_b64)
        if len(raw) > SES_MAX:
            return self._ses_error(400, "MessageRejected", "Message length is more than 40 MB")
        sender = body.get("FromEmailAddress") or mime.parse(raw).from_addr
        domain = sender.rsplit("@", 1)[-1].lower()
        if domain not in self.verified_domains:
            return self._ses_error(400, "MessageRejected", "Email address is not verified. The following identities "
                                   f"failed the check in region US-EAST-1: {sender}")
        dest = body.get("Destination") or {}
        rcpts = [*(dest.get("ToAddresses") or []), *(dest.get("CcAddresses") or [])] or list(mime.recipients(raw))
        message_id = f"0100019{uuid.uuid4().hex[:9]}-{uuid.uuid4()}-000000"
        out = self._rewrite_message_id(raw, f"<{message_id}@email.amazonses.com>")
        eol = b"\r\n" if b"\r\n" in raw[:4096] else b"\n"
        out = (f"DKIM-Signature: v=1; a=rsa-sha256; d={domain}; s=mock; c=relaxed/simple; bh=x; b=y".encode()
               + eol + out)
        self.sent.append({"MessageId": message_id, "raw": out, "rcpts": rcpts})
        for rcpt in rcpts:
            if rcpt.rsplit("@", 1)[-1].lower() == self.receiving_domain:
                self.receive(out, rcpt, dmarc="PASS", source=sender)
        return httpx.Response(200, json={"MessageId": message_id})

    @staticmethod
    def _rewrite_message_id(raw: bytes, new_id: str) -> bytes:
        head, sep, body = raw.partition(b"\r\n\r\n") if b"\r\n\r\n" in raw else raw.partition(b"\n\n")
        eol = b"\r\n" if sep == b"\r\n\r\n" else b"\n"
        lines = [ln for ln in head.split(eol) if not ln.lower().startswith(b"message-id:")]
        return eol.join([*lines, b"Message-ID: " + new_id.encode()]) + sep + body

    def inject(self, raw: bytes, recipient: str, verdict: str) -> None:
        """Mail from an outside MTA, which SES rates with this DMARC verdict."""
        self.receive(raw, recipient, dmarc={"pass": "PASS", "fail": "FAIL"}.get(verdict, "GRAY"),
                     source=mime.parse(raw).from_addr)

    def receive(self, raw: bytes, recipient: str, *, dmarc: str, source: str) -> str | None:
        now = self.clock()
        inbound_id = uuid.uuid4().hex[:20] + "o2" + uuid.uuid4().hex[:10]
        domain = source.rsplit("@", 1)[-1].lower()
        eol = b"\r\n" if b"\r\n" in raw[:4096] else b"\n"
        added = [
            f"Return-Path: <{source}>",
            f"Received: from a8-1.smtp-out.mock (a8-1.smtp-out.mock [54.240.8.1]) by inbound-smtp.{REGION}.amazonaws.com "
            f"with SMTP id {inbound_id} for {recipient}; {formatdate(now, usegmt=True)}",
            "X-SES-Spam-Verdict: PASS", "X-SES-Virus-Verdict: PASS",
            f"Received-SPF: pass (spfCheck: domain of {domain} designates 54.240.8.1 as permitted sender)",
            f"Authentication-Results: amazonses.com; spf=pass smtp.mailfrom={source}; dkim=pass header.i=@{domain}; "
            f"dmarc={dmarc.lower()} header.from={domain}",
        ]
        stored = eol.join(a.encode() for a in added) + eol + raw
        action: dict = {"type": "S3", "topicArn": TOPIC, "bucketName": self.bucket,
                        "objectKeyPrefix": self.prefix, "objectKey": self.prefix + inbound_id}
        note: dict = {"notificationType": "Received"}
        if self.action == "sns":
            if len(stored) > SNS_MAX:
                return None  # SES bounces mail over 150 KB for an SNS action
            action = {"type": "SNS", "topicArn": TOPIC, "encoding": "BASE64"}
            note["content"] = base64.b64encode(stored).decode()
        else:
            with self._lock:
                self.objects[self.prefix + inbound_id] = S3Object(self.prefix + inbound_id, stored, now)
        parsed = mime.parse(raw)
        verdict = lambda s: {"status": s}  # noqa: E731
        note["mail"] = {"timestamp": _iso(now), "source": source, "messageId": inbound_id,
                        "destination": [recipient], "headersTruncated": False,
                        "headers": [{"name": k, "value": v} for k, v in parsed.headers][:20],
                        "commonHeaders": {"from": [parsed.from_addr], "to": list(parsed.to),
                                          "messageId": parsed.message_id, "subject": parsed.subject}}
        note["receipt"] = {"timestamp": _iso(now), "processingTimeMillis": 120, "recipients": [recipient],
                           "spamVerdict": verdict("PASS"), "virusVerdict": verdict("PASS"),
                           "spfVerdict": verdict("PASS"), "dkimVerdict": verdict("PASS"),
                           "dmarcVerdict": verdict(dmarc), "action": action}
        if self.sns_sink is not None:
            msg = self.sns_message("Notification", json.dumps(note), subject="Amazon SES Email Receipt Notification")
            self.post(msg)
        return inbound_id

    # ------------------------------------------------------------ SNS

    def sns_message(self, kind: str, message: str, *, subject: str | None = None, topic: str = TOPIC,
                    cert_url: str = CERT_URL, key=None, version: str | None = None) -> dict:
        msg = {"Type": kind, "MessageId": str(uuid.uuid4()), "TopicArn": topic, "Message": message,
               "Timestamp": _iso(self.clock()), "SignatureVersion": version or self.signature_version,
               "SigningCertURL": cert_url}
        if subject is not None:
            msg["Subject"] = subject
        if kind != "Notification":
            msg["Token"] = uuid.uuid4().hex * 2
            msg["SubscribeURL"] = (f"https://sns.{REGION}.amazonaws.com/?Action=ConfirmSubscription"
                                   f"&TopicArn={topic}&Token={msg['Token']}")
        else:
            msg["UnsubscribeURL"] = f"https://sns.{REGION}.amazonaws.com/?Action=Unsubscribe&SubscriptionArn={topic}:s"
        algo = hashes.SHA1() if msg["SignatureVersion"] == "1" else hashes.SHA256()
        msg["Signature"] = base64.b64encode((key or self.key).sign(string_to_sign(msg), padding.PKCS1v15(), algo)).decode()
        return msg

    def headers_for(self, msg: dict) -> dict:
        return {"x-amz-sns-message-type": msg["Type"], "x-amz-sns-message-id": msg["MessageId"],
                "x-amz-sns-topic-arn": msg["TopicArn"], "Content-Type": "text/plain; charset=UTF-8",
                "User-Agent": "Amazon Simple Notification Service Agent"}

    def post(self, msg: dict) -> None:
        body = json.dumps(msg).encode()
        threading.Thread(target=self.sns_sink, args=(self.headers_for(msg), body), daemon=True).start()

    def subscription_confirmation(self) -> dict:
        return self.sns_message("SubscriptionConfirmation", "You have chosen to subscribe to the topic "
                                f"{TOPIC}.\nTo confirm the subscription, visit the SubscribeURL included in this message.")
