"""Independent literal cells plus hostile table and bounded-fetch cases."""

import hashlib
import io
import json
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import pytest

from chronicle.domain import SourceAvailability
from chronicle.errors import ContractError
from chronicle.sources import (
    MAX_BYTES,
    SOURCES,
    _NoRedirect,
    fetch_and_parse,
    parse_payload,
    parse_saved_source,
    read_saved_source,
    rights_policy,
    validate_release_order,
)

FIXTURES = Path(__file__).parent / "fixtures/sources"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
RECEIVED = datetime(2026, 10, 29, 13, 18, 9, tzinfo=UTC)


def payload(key):
    suffix = "-modeled" if key.startswith("bls") else ""
    return (FIXTURES / (key + suffix + ".html")).read_bytes()


@pytest.mark.parametrize("key", list(EXPECTED))
def test_reviewed_literal_values_headers_units_and_actual_receipt_identity(key):
    raw = payload(key)
    artifact = parse_payload(key, raw, RECEIVED, "batch")
    assert {r.observation_month.isoformat(): r.value for r in artifact.revisions} == EXPECTED[key]
    assert artifact.manifest["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifact.manifest["received_at"] == RECEIVED.isoformat()
    assert artifact.manifest["source_release_day"] == SOURCES[key].release_day.isoformat()
    assert artifact.manifest["source_display_decimals"] == (0 if key.startswith("bls") else 1)
    for revision in artifact.revisions:
        assert revision.occurrences[0].received_at == RECEIVED
        assert revision.source_order == SOURCES[key].release_day.toordinal()
        assert revision.order_rule.value == "date_ordinal"
    assert "model_input" not in artifact.manifest["rights_policy"]["permitted_uses"]
    assert "public_export" not in artifact.manifest["rights_policy"]["permitted_uses"]
    validate_release_order((artifact,))


def test_bls_adjacent_unadjusted_poison_cannot_supply_levels():
    artifact = parse_payload("bls-2024-02-02", payload("bls-2024-02-02"), RECEIVED, "batch")
    # The fixture's adjacent unadjusted cells contain999901..999904, not the SA levels.
    assert [r.value for r in artifact.revisions] == ["157347", "157700"]
    assert artifact.manifest["cells"][0]["raw_text"] == "157,347"
    assert [cell["column_headers"][0] for cell in artifact.manifest["cells"]] == [
        "Seasonally adjusted",
        "Seasonally adjusted",
    ]


def test_frb_previous_estimate_row_is_not_current_and_markers_are_retained():
    artifact = parse_payload("frb-2024-02-15", payload("frb-2024-02-15"), RECEIVED, "batch")
    assert [r.value for r in artifact.revisions] == ["102.7", "102.7", "102.6"]
    assert [cell["markers"] for cell in artifact.manifest["cells"]] == [["r"], ["r"], ["p"]]


@pytest.mark.parametrize(
    "old,new,code",
    [
        (b"2017=100", b"2022=100", "SOURCE_HEADER_DRIFT"),
        (b"Seasonally adjusted", b"Not seasonally adjusted", "SOURCE_UNIT_DRIFT"),
        (b"Total&nbsp;index", b"Previously estimated total", "SOURCE_HEADER_DRIFT"),
        (b"February 15, 2024", b"February 16, 2024", "SOURCE_RELEASE_DRIFT"),
        (b">102.7</td>", b">1e2</td>", "SOURCE_VALUE_DRIFT"),
        (b"colspan=6", b"colspan=1000000", "SOURCE_STRUCTURE"),
    ],
)
def test_source_drift_is_quarantinable_not_silent_normalization(old, new, code):
    changed = payload("frb-2024-02-15").replace(old, new)
    with pytest.raises(ContractError) as caught:
        parse_payload("frb-2024-02-15", changed, RECEIVED, "batch")
    assert caught.value.code == code


def test_missing_source_cell_is_an_explicit_missing_revision_not_zero():
    raw = payload("frb-2024-02-15").replace(
        b'headers=" hdr1 hdr3 hdr12 hdr23">102.7', b'headers=" hdr1 hdr3 hdr12 hdr23">NA'
    )
    artifact = parse_payload("frb-2024-02-15", raw, RECEIVED, "batch")
    assert artifact.revisions[0].status.value == "missing"
    assert artifact.revisions[0].value is None
    assert len(artifact.revisions) == 3


@pytest.mark.parametrize(
    "key,value",
    [
        ("decision_state", "revoked"),
        ("policy_id", "unknown-policy"),
        ("permitted_uses", ["local_ingest", "local_analysis", "model_input"]),
        ("permitted_uses", ["local_analysis"]),
    ],
)
def test_unknown_revoked_or_expanded_rights_do_not_allow_import(key, value):
    policy = rights_policy("frb-2024-02-15")
    policy[key] = value
    with pytest.raises(ContractError) as caught:
        parse_payload("frb-2024-02-15", payload("frb-2024-02-15"), RECEIVED, "batch", policy=policy)
    assert caught.value.code == "SOURCE_RIGHTS_DENIED"


def test_source_allowlist_rejects_arbitrary_url_before_network():
    with pytest.raises(ContractError, match="Only four"):
        fetch_and_parse("http://127.0.0.1/metadata", "batch")


def test_unknown_or_inverted_source_dates_cannot_invent_precedence():
    artifact = parse_payload("frb-2024-02-15", payload("frb-2024-02-15"), RECEIVED, "batch")
    base = artifact.revisions[0]
    for revision in (
        replace(base, order_rule="documented_sequence", availability=SourceAvailability("unknown")),
        replace(base, order_rule="documented_sequence", source_order=base.source_order + 1),
    ):
        with pytest.raises(ContractError) as caught:
            validate_release_order((replace(artifact, revisions=(revision,)),))
        assert caught.value.code == "SOURCE_ORDER_UNPROVEN"


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, *, headers=None):
        super().__init__(body)
        self.url = SOURCES["frb-2024-02-15"].url
        self.headers = Message()
        self.headers["Content-Type"] = "text/html"
        for key, value in (headers or {}).items():
            self.headers[key] = value


def patch_response(monkeypatch, response):
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == SOURCES["frb-2024-02-15"].url
            assert timeout == 20
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())


def test_fetch_records_receipt_after_download_not_release_time(monkeypatch):
    before = datetime.now(UTC)
    patch_response(monkeypatch, Response(payload("frb-2024-02-15")))
    artifact = fetch_and_parse("frb-2024-02-15", "batch")
    after = datetime.now(UTC)
    assert before <= artifact.revisions[0].occurrences[0].received_at <= after


@pytest.mark.parametrize(
    "headers,body,code",
    [
        ({"Content-Length": str(MAX_BYTES + 1)}, b"small", "SOURCE_TOO_LARGE"),
        ({"Content-Length": "1000"}, b"small", "SOURCE_TRUNCATED"),
        ({}, b"x" * (MAX_BYTES + 1), "SOURCE_TOO_LARGE"),
        ({"Content-Encoding": "gzip"}, b"zip", "SOURCE_ENCODING"),
    ],
)
def test_fetch_byte_limits_also_cover_chunked_and_compressed_payloads(
    monkeypatch, headers, body, code
):
    patch_response(monkeypatch, Response(body, headers=headers))
    with pytest.raises(ContractError) as caught:
        fetch_and_parse("frb-2024-02-15", "batch")
    assert caught.value.code == code


def test_redirect_rejected_before_a_second_request():
    with pytest.raises(ContractError) as caught:
        _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://localhost/")
    assert caught.value.code == "SOURCE_REDIRECT_REJECTED"


def test_network_denial_never_creates_fabricated_source_artifact(monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError(SOURCES["frb-2024-02-15"].url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    with pytest.raises(ContractError) as caught:
        fetch_and_parse("frb-2024-02-15", "batch")
    assert caught.value.code == "SOURCE_UNAVAILABLE"


def test_payload_limits_and_receipt_timezone_are_enforced():
    with pytest.raises(ContractError):
        parse_payload("frb-2024-02-15", b"x" * (MAX_BYTES + 1), RECEIVED, "batch")
    with pytest.raises(ContractError):
        parse_payload("frb-2024-02-15", payload("frb-2024-02-15"), datetime(2024, 2, 15), "batch")


def test_small_but_excessively_complex_html_is_bounded():
    with pytest.raises(ContractError) as caught:
        parse_payload("frb-2024-02-15", b"<br>" * 50001, RECEIVED, "batch")
    assert caught.value.code == "SOURCE_STRUCTURE"


def test_saved_direct_html_has_local_receipt_and_explicit_unverified_authenticity(tmp_path):
    path = tmp_path / "private-file-name.html"
    path.write_bytes(payload("frb-2024-02-15"))
    before = datetime.now(UTC)
    saved = read_saved_source("frb-2024-02-15", path)
    artifact = parse_saved_source(saved, "batch")
    assert before <= saved.received_at <= datetime.now(UTC)
    assert artifact.manifest["received_at"] == saved.received_at.isoformat()
    assert artifact.manifest["acquisition_channel"] == "operator_supplied_direct_publisher_html"
    assert artifact.manifest["acquisition"]["authenticity"] == (
        "operator_asserted_not_independently_authenticated"
    )
    assert artifact.manifest["acquisition"]["raw_sha256"] == hashlib.sha256(saved.raw).hexdigest()
    assert "private-file-name" not in json.dumps(artifact.manifest)
    assert artifact.manifest["source_uri"] == SOURCES["frb-2024-02-15"].url


def test_saved_source_rejects_symlink_directory_fifo_and_oversized_files(tmp_path):
    target = tmp_path / "source.html"
    target.write_bytes(payload("frb-2024-02-15"))
    link = tmp_path / "linked.html"
    link.symlink_to(target)
    for path in (link, tmp_path):
        with pytest.raises(ContractError) as caught:
            read_saved_source("frb-2024-02-15", path)
        assert caught.value.code in {"SOURCE_FILE_UNSAFE", "SOURCE_FILE_UNAVAILABLE"}
    import os

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ContractError) as caught:
        read_saved_source("frb-2024-02-15", fifo)
    assert caught.value.code == "SOURCE_FILE_UNSAFE"
    target.write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ContractError) as caught:
        read_saved_source("frb-2024-02-15", target)
    assert caught.value.code == "SOURCE_TOO_LARGE"


def test_saved_source_does_not_skip_release_units_or_table_validation(tmp_path):
    path = tmp_path / "source.html"
    path.write_bytes(payload("frb-2024-02-15").replace(b"2017=100", b"2022=100"))
    with pytest.raises(ContractError) as caught:
        parse_saved_source(read_saved_source("frb-2024-02-15", path), "batch")
    assert caught.value.code == "SOURCE_HEADER_DRIFT"


def publisher_shaped_bls(key):
    """Modeled test markup, not retained original source bytes.

    The real archives put ESTABLISHMENT DATA before the table title and split
    preliminary footnote markers across nested inline elements. Literal levels
    remain the independently frozen fixture values; adjacent NSA poison stays.
    """
    raw = (
        payload(key)
        .replace(
            b"<caption>Table B-1.",
            b'<caption><span class="tableTitle">ESTABLISHMENT DATA<br /> Table B-1.',
        )
        .replace(
            b" [In thousands]</caption>",
            b'<br /></span><span class="headnote">[In thousands]</span></caption>',
        )
    )
    preliminary = (
        (b"Dec. 2023", b"Jan. 2024") if key.endswith("02-02") else (b"Jan. 2024", b"Feb. 2024")
    )
    for month in preliminary:
        raw = raw.replace(
            month + b"</th>",
            month + b'<span class="footnoteRefs">(<a title="Preliminary">p</a>)</span></th>',
        )
    return raw.replace(
        b"Change from previous month",
        b"Change from: Dec.2023 - Jan.2024"
        if key.endswith("02-02")
        else b"Change from: Jan.2024 - Feb.2024",
    )


@pytest.mark.parametrize("key", ["bls-2024-02-02", "bls-2024-03-08"])
def test_bls_publisher_caption_and_nested_preliminary_markers(key):
    artifact = parse_payload(key, publisher_shaped_bls(key), RECEIVED, "batch")
    assert {r.observation_month.isoformat(): r.value for r in artifact.revisions} == EXPECTED[key]
    expected_markers = [["p"], ["p"]] if key.endswith("02-02") else [[], ["p"], ["p"]]
    assert [cell["markers"] for cell in artifact.manifest["cells"]] == expected_markers


@pytest.mark.parametrize(
    "old,new,code",
    [
        (b"Employees on nonfarm payrolls", b"Aggregate weekly hours", "SOURCE_HEADER_DRIFT"),
        (b"[In thousands]", b"[In millions]", "SOURCE_UNIT_DRIFT"),
        (b">Seasonally adjusted<", b">Not seasonally adjusted<", "SOURCE_HEADER_DRIFT"),
    ],
)
def test_publisher_shaped_bls_still_rejects_title_unit_and_adjustment_drift(old, new, code):
    raw = publisher_shaped_bls("bls-2024-02-02").replace(old, new)
    with pytest.raises(ContractError) as caught:
        parse_payload("bls-2024-02-02", raw, RECEIVED, "batch")
    assert caught.value.code == code


def test_duplicate_publisher_bls_table_is_ambiguous():
    raw = publisher_shaped_bls("bls-2024-02-02")
    table = raw[raw.index(b"<table>") : raw.index(b"</table>") + len(b"</table>")]
    with pytest.raises(ContractError) as caught:
        parse_payload(
            "bls-2024-02-02", raw.replace(b"</body>", table + b"</body>"), RECEIVED, "batch"
        )
    assert caught.value.code == "SOURCE_HEADER_DRIFT"
