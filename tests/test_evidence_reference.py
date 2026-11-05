"""Literal evidence expectations anchor the independent model before production SQL."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.reference.evidence_model import select_evidence

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "evidence"
FIXTURE = json.loads((FIXTURE_DIR / "evidence-v1.json").read_text())


@pytest.mark.parametrize("query", FIXTURE["queries"], ids=lambda item: item["name"])
def test_independent_scan_matches_hand_authored_evidence(query):
    assert select_evidence(FIXTURE, query, "$dataset") == query["expected"]


def test_evidence_corpus_bytes_are_frozen():
    digest, filename = (FIXTURE_DIR / "SHA256SUMS").read_text().split()
    assert hashlib.sha256((FIXTURE_DIR / filename).read_bytes()).hexdigest() == digest
