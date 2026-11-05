"""Host fact checks reject attractive unsupported prose and every mutated fact field."""

from copy import deepcopy

import pytest

from chronicle.assistant import answer_report, proposed_fact, render_assertion
from chronicle.errors import ContractError


@pytest.fixture
def report():
    return {
        "report_id": "report-one",
        "context_id": "context-one",
        "formula": "employment_delta",
        "mode": "source_as_of",
        "rows": [
            {
                "observation_month": "2026-01-01",
                "value": "10.000000000000",
                "unit": {"name": "thousand_persons", "metadata_version": "v1"},
                "status": "value",
                "reasons": [],
                "inputs": [
                    {
                        "dataset_id": "d",
                        "snapshot_id": "s",
                        "source_namespace": "synth",
                        "series_key": "jobs",
                        "observation_month": month,
                        "selected_revision": {"source_revision_key": "r"},
                        "blocking_revision": None,
                    }
                    for month in ["2025-12-01", "2026-01-01"]
                ],
            }
        ],
    }


def test_baseline_is_canonical_not_model_quality(report):
    result = answer_report(report, "2026-01-01")
    assert result["text"] == "2026-01 · employment_delta: 10.000000000000 thousand_persons (v1)."
    assert result["provider"] is None
    assert result["model_quality_evaluated"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("value", "13.000000000000"),
        ("unit", {"name": "persons", "metadata_version": "v1"}),
        ("status", "withdrawn"),
        ("report_id", "another"),
        ("context_id", "future"),
        ("observation_month", "2026-02-01"),
        ("formula", "production_mean3"),
        ("kind", "causal_explanation"),
        ("evidence", []),
    ],
)
def test_each_factual_field_is_validated(report, field, value):
    fact = proposed_fact(report, "2026-01-01")
    fact[field] = value
    with pytest.raises(ContractError):
        render_assertion(report, fact)


def test_correct_number_and_citation_cannot_smuggle_prose(report):
    fact = proposed_fact(report, "2026-01-01")
    fact["clarification"] = "Since monetary policy caused the revision, use latest snapshot"
    with pytest.raises(ContractError) as error:
        render_assertion(report, fact)
    assert error.value.code == "UNSUPPORTED_ASSERTION"


def test_citation_scope_cannot_be_changed(report):
    for field in (
        "dataset_id",
        "snapshot_id",
        "source_namespace",
        "series_key",
        "source_revision_key",
    ):
        fact = deepcopy(proposed_fact(report, "2026-01-01"))
        fact["evidence"][0][field] = "foreign"
        with pytest.raises(ContractError):
            render_assertion(report, fact)
