"""Four reviewed direct-publisher archives; fail closed on fetch or table drift."""

import copy
import hashlib
import http.client
import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from threading import Lock

from chronicle.domain import Occurrence, Revision, SourceAvailability, Unit
from chronicle.errors import ContractError

PARSER_VERSION = "direct-archive-tables-v2"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 20
DEADLINE_SECONDS = 30
_TITLE = "Industrial Production and Capacity Utilization: Summary"
_BLS_TITLE = (
    "Table B-1. Employees on nonfarm payrolls by industry sector and selected industry detail"
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    url: str
    publisher: str
    release_day: date
    namespace: str
    series_key: str
    unit: Unit
    months: tuple[date, ...]
    display_decimals: int


SOURCES = {
    "bls-2024-02-02": SourceSpec(
        "bls-2024-02-02",
        "https://www.bls.gov/news.release/archives/empsit_02022024.htm",
        "U.S. Bureau of Labor Statistics",
        date(2024, 2, 2),
        "bls.ces.archive",
        "employment",
        Unit("thousand_persons", "bls.ces.total_nonfarm_sa.v1"),
        (date(2023, 12, 1), date(2024, 1, 1)),
        0,
    ),
    "bls-2024-03-08": SourceSpec(
        "bls-2024-03-08",
        "https://www.bls.gov/news.release/archives/empsit_03082024.htm",
        "U.S. Bureau of Labor Statistics",
        date(2024, 3, 8),
        "bls.ces.archive",
        "employment",
        Unit("thousand_persons", "bls.ces.total_nonfarm_sa.v1"),
        (date(2023, 12, 1), date(2024, 1, 1), date(2024, 2, 1)),
        0,
    ),
    "frb-2024-02-15": SourceSpec(
        "frb-2024-02-15",
        "https://www.federalreserve.gov/releases/g17/20240215/",
        "Board of Governors of the Federal Reserve System",
        date(2024, 2, 15),
        "frb.g17.archive",
        "production",
        Unit("index", "frb.g17.2017=100.sa.v1"),
        (date(2023, 11, 1), date(2023, 12, 1), date(2024, 1, 1)),
        1,
    ),
    "frb-2024-03-15": SourceSpec(
        "frb-2024-03-15",
        "https://www.federalreserve.gov/releases/g17/20240315/",
        "Board of Governors of the Federal Reserve System",
        date(2024, 3, 15),
        "frb.g17.archive",
        "production",
        Unit("index", "frb.g17.2017=100.sa.v1"),
        (date(2023, 11, 1), date(2023, 12, 1), date(2024, 1, 1), date(2024, 2, 1)),
        1,
    ),
}
_PUBLISHER_LOCKS = {spec.publisher: Lock() for spec in SOURCES.values()}


@dataclass(frozen=True)
class SourceArtifact:
    raw: bytes
    revisions: tuple[Revision, ...]
    manifest: dict


@dataclass(frozen=True)
class SavedSource:
    """Operator-supplied direct-publisher HTML; not remotely authenticated bytes."""

    source_id: str
    raw: bytes
    sha256: str
    received_at: datetime


def read_saved_source(source_id: str, path: Path) -> SavedSource:
    """Read bounded regular HTML with a truthful new local receipt, never file mtime.

    The source ID fixes the expected official URI. The operator is responsible for
    acquiring and supplying that exact publisher document; hashes and semantic
    checks establish integrity and consistency, not publisher authenticity.
    No local filesystem path is retained in the public provenance document.
    """
    _spec(source_id)
    rights_policy(source_id)
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ContractError("SOURCE_FILE_UNSAFE", "Saved source must not use symbolic links")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ContractError("SOURCE_FILE_UNSAFE", "Saved source must be a regular file")
            if metadata.st_size > MAX_BYTES:
                raise ContractError("SOURCE_TOO_LARGE", "Saved source exceeds byte limit")
            raw = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ContractError("SOURCE_FILE_UNAVAILABLE", "Saved source could not be read") from None
    if not raw or len(raw) > MAX_BYTES:
        raise ContractError("SOURCE_TOO_LARGE", "Expected bounded nonempty source bytes")
    return SavedSource(source_id, raw, hashlib.sha256(raw).hexdigest(), datetime.now(UTC))


def parse_saved_source(saved: SavedSource, batch_id: str) -> SourceArtifact:
    artifact = parse_payload(saved.source_id, saved.raw, saved.received_at, batch_id)
    artifact.manifest["acquisition_channel"] = "operator_supplied_direct_publisher_html"
    artifact.manifest["acquisition"] = {
        "method": "local_file",
        "expected_source_uri": SOURCES[saved.source_id].url,
        "authenticity": "operator_asserted_not_independently_authenticated",
        "receipt_basis": "completed_local_file_read",
        "raw_sha256": saved.sha256,
    }
    return artifact


def _spec(source_id: str) -> SourceSpec:
    if source_id not in SOURCES:
        raise ContractError("SOURCE_NOT_ALLOWED", "Only four reviewed source IDs are permitted")
    return SOURCES[source_id]


def rights_policy(source_id: str, override: dict | None = None) -> dict:
    spec = _spec(source_id)
    registry = json.loads(
        files("chronicle").joinpath("resources/sources-rights-v1.json").read_text()
    )
    known = registry["policies"]["bls" if spec.source_id.startswith("bls") else "frb"]
    policy = copy.deepcopy(known if override is None else override)
    if (
        policy.get("policy_id") != known["policy_id"]
        or policy.get("version") != known["version"]
        or policy.get("decision_state") != "approved_scoped"
        or not {"local_ingest", "local_analysis"}.issubset(policy.get("permitted_uses", []))
        or not set(policy.get("permitted_uses", [])).issubset(known["permitted_uses"])
    ):
        raise ContractError("SOURCE_RIGHTS_DENIED", "Current source policy does not allow import")
    return policy


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError("SOURCE_REDIRECT_REJECTED", "Archive redirects require a new review")


def fetch_and_parse(source_id: str, batch_id: str, *, policy: dict | None = None) -> SourceArtifact:
    """Fetch one allowlisted HTTPS page, with bounded bytes and no redirect/crawl.

    Each publisher has one outstanding request per process. The reader has a
    20-second socket timeout and checks a 30-second wall deadline between reads.
    It records receipt after the last byte; no historical receipt time is invented.
    """
    spec = _spec(source_id)
    checked_policy = rights_policy(source_id, policy)
    request = urllib.request.Request(
        spec.url,
        headers={
            "User-Agent": "Chronicle/0.1 (local archive research; four approved public releases)",
            "Accept": "text/html",
            "Accept-Encoding": "identity",
        },
    )
    with _PUBLISHER_LOCKS[spec.publisher]:
        try:
            deadline = time.monotonic() + DEADLINE_SECONDS
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=TIMEOUT_SECONDS
            ) as response:
                if response.status != 200 or response.url != spec.url:
                    raise ContractError("SOURCE_UNAVAILABLE", "Expected exact archive response")
                if response.headers.get_content_type() != "text/html":
                    raise ContractError("SOURCE_CONTENT_TYPE", "Expected HTML source")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ContractError("SOURCE_ENCODING", "Compressed responses are not accepted")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                    raise ContractError("SOURCE_TOO_LARGE", "Source exceeds byte limit")
                chunks, size = [], 0
                while True:
                    if time.monotonic() > deadline:
                        raise ContractError("SOURCE_TIMEOUT", "Source exceeded time budget")
                    chunk = response.read(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ContractError("SOURCE_TOO_LARGE", "Source exceeds byte limit")
                raw = b"".join(chunks)
                if length is not None and size != int(length):
                    raise ContractError("SOURCE_TRUNCATED", "Source response was incomplete")
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise ContractError(
                "SOURCE_UNAVAILABLE", "Source request failed; no import occurred"
            ) from None
    return parse_payload(source_id, raw, datetime.now(UTC), batch_id, policy=checked_policy)


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: "_Node | None" = None

    def text(self) -> str:
        return " ".join(
            " ".join(
                child.text() if isinstance(child, _Node) else child for child in self.children
            ).split()
        )

    def nodes(self, tag: str):
        for child in self.children:
            if isinstance(child, _Node):
                if child.tag == tag:
                    yield child
                yield from child.nodes(tag)


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.node_count = 0

    def handle_starttag(self, tag, attrs):
        self.node_count += 1
        if self.node_count > 50000:
            raise ContractError("SOURCE_STRUCTURE", "HTML node count exceeds parser bound")
        if len(self.stack) > 100:
            raise ContractError("SOURCE_STRUCTURE", "HTML nesting exceeds parser bound")
        node = _Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _table_grid(table: _Node) -> list[list[_Node | None]]:
    if any(table.nodes("table")):
        raise ContractError("SOURCE_STRUCTURE", "Nested source tables are not supported")
    grid = []
    for row_number, row in enumerate(table.nodes("tr")):
        if row_number >= 2000:
            raise ContractError("SOURCE_STRUCTURE", "Too many table rows")
        while len(grid) <= row_number:
            grid.append([None] * 100)
        column = 0
        for cell in row.children:
            if not isinstance(cell, _Node) or cell.tag not in {"td", "th"}:
                continue
            while column < 100 and grid[row_number][column] is not None:
                column += 1
            try:
                rowspan, colspan = (
                    int(cell.attrs.get("rowspan", 1)),
                    int(cell.attrs.get("colspan", 1)),
                )
            except (TypeError, ValueError):
                raise ContractError("SOURCE_STRUCTURE", "Invalid table span") from None
            if not 1 <= rowspan <= 20 or not 1 <= colspan <= 100 or column + colspan > 100:
                raise ContractError("SOURCE_STRUCTURE", "Table span exceeds parser bound")
            for target in range(row_number, row_number + rowspan):
                while len(grid) <= target:
                    grid.append([None] * 100)
                for col in range(column, column + colspan):
                    if grid[target][col] is not None:
                        raise ContractError("SOURCE_STRUCTURE", "Overlapping table cells")
                    grid[target][col] = cell
            column += colspan
    return grid


def _month(headers: list[str]) -> date | None:
    joined = " ".join(header for header in headers if header != "2017=100").lower()
    years = set(re.findall(r"\b(20[0-9]{2})\b", joined))
    months = {
        _MONTHS[word[:3]]
        for word in re.findall(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
            joined,
        )
    }
    if len(years) != 1 or len(months) != 1:
        return None
    return date(int(next(iter(years))), next(iter(months)), 1)


def _extract(spec: SourceSpec, doc: _Document) -> list[dict]:
    is_bls = spec.source_id.startswith("bls")
    tables = [
        table
        for table in doc.root.nodes("table")
        if (
            any(
                re.fullmatch(
                    re.escape(_BLS_TITLE) + r" \[[^\[\]]+\]",
                    c.text().removeprefix("ESTABLISHMENT DATA "),
                )
                for c in table.nodes("caption")
            )
            if is_bls
            else table.attrs.get("title") == _TITLE
        )
    ]
    if len(tables) != 1:
        raise ContractError("SOURCE_HEADER_DRIFT", "Expected one precisely identified source table")
    table = tables[0]
    if is_bls:
        caption = " ".join(c.text() for c in table.nodes("caption"))
        if caption.removeprefix("ESTABLISHMENT DATA ") != _BLS_TITLE + " [In thousands]":
            raise ContractError("SOURCE_UNIT_DRIFT", "Employment units changed")
    else:
        parent = table.parent
        if parent is None or not any(
            "tableunit" in n.attrs.get("class", "").split() and n.text() == "Seasonally adjusted"
            for n in parent.nodes("span")
        ):
            raise ContractError("SOURCE_UNIT_DRIFT", "Production seasonal adjustment changed")
    grid = _table_grid(table)
    wanted_label = "Total nonfarm" if is_bls else "Total index"
    found = [(i, row) for i, row in enumerate(grid) if row[0] and row[0].text() == wanted_label]
    if len(found) != 1:
        raise ContractError("SOURCE_HEADER_DRIFT", "Expected one current total row")
    row_number, row = found[0]
    selected = {}
    for col, cell in enumerate(row[1:], 1):
        if cell is None or cell.tag != "td":
            continue
        headings = []
        for above in grid[:row_number]:
            header = above[col]
            if header is not None and header.tag == "th" and header.text() not in headings:
                headings.append(header.text())
        if ("Seasonally adjusted" if is_bls else "2017=100") not in headings:
            continue
        month = _month(headings)
        if month not in spec.months:
            continue
        if month in selected:
            raise ContractError("SOURCE_HEADER_DRIFT", "Duplicate selected source month")
        raw_value = cell.text()
        # Grouping is source-specific syntax. Preserve it in evidence and remove
        # only validated thousands separators before creating the typed decimal.
        pattern = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)" if is_bls else r"[0-9]+\.[0-9]"
        if raw_value in {"", "—", "-", "NA", "N/A"}:
            value, status = None, "missing"
        elif re.fullmatch(pattern, raw_value):
            value, status = raw_value.replace(",", ""), "value"
        else:
            raise ContractError("SOURCE_VALUE_DRIFT", "Unexpected source numeric syntax")
        selected[month] = {
            "month": month,
            "value": value,
            "status": status,
            "raw_text": raw_value,
            "table": "B-1" if is_bls else _TITLE,
            "row": wanted_label,
            "column_headers": headings,
            "html_headers": cell.attrs.get("headers"),
            "markers": sorted(
                {
                    left or right
                    for left, right in re.findall(
                        r"\[\s*([pr])\s*\]|\(\s*([pr])\s*\)", " ".join(headings)
                    )
                }
            ),
        }
    if set(selected) != set(spec.months):
        raise ContractError("SOURCE_HEADER_DRIFT", "Required calendar columns are absent")
    return [selected[month] for month in spec.months]


def parse_payload(
    source_id: str, raw: bytes, received_at: datetime, batch_id: str, *, policy: dict | None = None
) -> SourceArtifact:
    """Parse retained publisher bytes without performing publication or writing files."""
    spec = _spec(source_id)
    checked_policy = rights_policy(source_id, policy)
    receipt = Occurrence(batch_id, received_at)
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ContractError("SOURCE_TOO_LARGE", "Expected bounded, nonempty source bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ContractError("SOURCE_ENCODING", "Source must use UTF-8") from None
    document = _Document()
    document.feed(text)
    document.close()
    release_label = (
        f"{spec.release_day.strftime('%B')} {spec.release_day.day}, {spec.release_day.year}"
    )
    if source_id.startswith("bls"):
        release_headers = [
            node.text()[:300]
            for node in document.root.nodes("pre")
            if node.text().startswith("Transmission of material")
        ]
        release_matches = [
            header
            for header in release_headers
            if re.search(
                r"embargoed until.{0,80}8:30 a\.m\. \(ET\) Friday, " + re.escape(release_label),
                header,
            )
        ]
    else:
        release_matches = [
            node
            for node in document.root.nodes("div")
            if "dates" in node.attrs.get("class", "").split()
            and node.text() == "Release Date: " + release_label
        ]
    if len(release_matches) != 1:
        raise ContractError("SOURCE_RELEASE_DRIFT", "Archive release date evidence changed")
    extracted = _extract(spec, document)
    revisions = tuple(
        Revision(
            source_namespace=spec.namespace,
            series_key=spec.series_key,
            observation_month=row["month"],
            source_revision_key=source_id,
            source_order=spec.release_day.toordinal(),
            order_rule="date_ordinal",
            availability=SourceAvailability("date", spec.release_day),
            value=row["value"],
            status=row["status"],
            unit=spec.unit,
            occurrences=(receipt,),
        )
        for row in extracted
    )
    manifest = {
        "schema_version": "chronicle.source.v1",
        "source_id": source_id,
        "source_uri": spec.url,
        "publisher": spec.publisher,
        "acquisition_channel": "direct_publisher_archive",
        "parser_version": PARSER_VERSION,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_size": len(raw),
        "received_at": receipt.received_at.isoformat(),
        "source_release_day": spec.release_day.isoformat(),
        "availability_precision": "date",
        "order_policy": "evidenced-release-date-ordinal-v1",
        "source_unit": spec.unit.name,
        "unit_metadata_version": spec.unit.metadata_version,
        "source_display_decimals": spec.display_decimals,
        "seasonal_adjustment": "SA",
        "rights_policy": checked_policy,
        "attribution": (
            f"Source: {spec.publisher}; release {spec.release_day.isoformat()}; direct archive."
        ),
        "cells": [{**row, "month": row["month"].isoformat()} for row in extracted],
        "synthetic": False,
    }
    return SourceArtifact(raw, revisions, manifest)


def validate_release_order(artifacts: tuple[SourceArtifact, ...]) -> None:
    """Real adapters use known evidenced release dates, never receipt ordering."""
    by_namespace = {}
    for artifact in artifacts:
        for row in artifact.revisions:
            if (
                row.availability.precision.value != "date"
                or row.availability.date is None
                or row.source_order != row.availability.date.toordinal()
            ):
                raise ContractError("SOURCE_ORDER_UNPROVEN", "Release date and order must agree")
            known = by_namespace.setdefault(row.source_namespace, {})
            previous = known.setdefault(row.source_order, row.source_revision_key)
            if previous != row.source_revision_key:
                raise ContractError("AMBIGUOUS_SOURCE_ORDER", "Same-day releases need new evidence")
