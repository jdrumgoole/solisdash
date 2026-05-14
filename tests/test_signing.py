from __future__ import annotations

from datetime import UTC, datetime

import pytest

from solisdash.signing import (
    CONTENT_TYPE_DEFAULT,
    build_headers,
    content_md5,
    gmt_date,
    sign,
    string_to_sign,
)

# Worked example pinned to V2.0.3 spec §2.4.
PDF_BODY = b'{"pageNo":1,"pageSize":10}'
PDF_MD5 = "kxdxk7rbAsrzSIWgEwhH4w=="
PDF_DATE = "Fri, 26 Jul 2019 06:00:46 GMT"
PDF_PATH = "/v1/api/userStationList"
PDF_INSTANT = datetime(2019, 7, 26, 6, 0, 46, tzinfo=UTC)


def test_content_md5_empty_body() -> None:
    assert content_md5(b"") == "1B2M2Y8AsgTpgAmY7PhCfg=="


def test_content_md5_matches_pdf_worked_example() -> None:
    assert content_md5(PDF_BODY) == PDF_MD5


def test_gmt_date_matches_pdf_worked_example() -> None:
    assert gmt_date(PDF_INSTANT) == PDF_DATE


def test_gmt_date_normalises_naive_datetime_to_utc() -> None:
    naive = datetime(2019, 7, 26, 6, 0, 46)
    assert gmt_date(naive) == PDF_DATE


def test_string_to_sign_uses_pdf_field_order() -> None:
    s = string_to_sign("POST", PDF_MD5, "application/json", PDF_DATE, PDF_PATH)
    expected = (
        "POST\n"
        f"{PDF_MD5}\n"
        "application/json\n"
        f"{PDF_DATE}\n"
        f"{PDF_PATH}"
    )
    assert s == expected


def test_sign_known_hmac_sha1_vector() -> None:
    # Stable HMAC-SHA1 reference: base64(HmacSHA1("secret", "hello"))
    assert sign("secret", "hello") == "URIFXAX5RPhXVe/FzYlw4ZTp9Fs="


def test_sign_is_deterministic_and_differs_per_secret() -> None:
    assert sign("a", "msg") == sign("a", "msg")
    assert sign("a", "msg") != sign("b", "msg")


def test_build_headers_matches_pdf_md5_date_and_auth_format() -> None:
    headers = build_headers(
        path=PDF_PATH,
        body=PDF_BODY,
        key_id="test-key-id",
        key_secret="test-secret",
        content_type="application/json",
        when=PDF_INSTANT,
    )
    assert headers["Content-MD5"] == PDF_MD5
    assert headers["Content-Type"] == "application/json"
    assert headers["Date"] == PDF_DATE
    assert headers["Authorization"].startswith("API test-key-id:")
    signature = headers["Authorization"].split(":", 1)[1]
    # base64 of a 20-byte SHA1 digest is always 28 characters.
    assert len(signature) == 28


def test_build_headers_signs_with_actual_content_type_sent() -> None:
    # The Content-Type used in StringToSign must equal the one in the header.
    h1 = build_headers(
        path=PDF_PATH,
        body=PDF_BODY,
        key_id="k",
        key_secret="s",
        content_type="application/json",
        when=PDF_INSTANT,
    )
    h2 = build_headers(
        path=PDF_PATH,
        body=PDF_BODY,
        key_id="k",
        key_secret="s",
        content_type="application/json;charset=UTF-8",
        when=PDF_INSTANT,
    )
    assert h1["Authorization"] != h2["Authorization"]


def test_build_headers_rejects_path_without_leading_slash() -> None:
    with pytest.raises(ValueError, match="must start with '/'"):
        build_headers(
            path="v1/api/userStationList",
            body=b"",
            key_id="k",
            key_secret="s",
        )


def test_default_content_type_is_bare_application_json() -> None:
    # §2.2 prose contradicts §2.4's worked example. The live API rejects
    # the `charset=UTF-8` suffix with 403 `wrong sign`, so the bare form
    # is what we ship.
    assert CONTENT_TYPE_DEFAULT == "application/json"
