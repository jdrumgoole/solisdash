"""SolisCloud request signing per V2.0.3 spec §2.2.

StringToSign = VERB + "\\n" + Content-MD5 + "\\n" + Content-Type + "\\n"
             + Date + "\\n" + CanonicalizedResource

Authorization = "API " + apiId + ":" + base64(HmacSHA1(apiSecret, StringToSign))

The Content-Type sent on the wire must match the one used in StringToSign.
The Date must be within ±15 minutes of server time or the request is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from email.utils import format_datetime

CONTENT_TYPE_DEFAULT = "application/json;charset=UTF-8"


def content_md5(body: bytes) -> str:
    """base64(MD5(body)). Empty body MD5 is ``1B2M2Y8AsgTpgAmY7PhCfg==``."""
    return base64.b64encode(hashlib.md5(body).digest()).decode("ascii")


def gmt_date(when: datetime | None = None) -> str:
    """RFC1123 date in GMT, e.g. ``Fri, 26 Jul 2019 06:00:46 GMT``."""
    moment = when or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return format_datetime(moment.astimezone(UTC), usegmt=True)


def string_to_sign(
    verb: str,
    md5: str,
    content_type: str,
    date: str,
    path: str,
) -> str:
    return "\n".join([verb, md5, content_type, date, path])


def sign(key_secret: str, message: str) -> str:
    mac = hmac.new(key_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def build_headers(
    *,
    path: str,
    body: bytes,
    key_id: str,
    key_secret: str,
    verb: str = "POST",
    content_type: str = CONTENT_TYPE_DEFAULT,
    when: datetime | None = None,
) -> dict[str, str]:
    """Return the four signed headers SolisCloud requires.

    ``path`` is the canonicalized resource and must start with ``/`` —
    e.g. ``/v1/api/userStationList``.
    """
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/': {path!r}")
    md5 = content_md5(body)
    date = gmt_date(when)
    signature = sign(
        key_secret,
        string_to_sign(verb.upper(), md5, content_type, date, path),
    )
    return {
        "Content-MD5": md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": f"API {key_id}:{signature}",
    }
