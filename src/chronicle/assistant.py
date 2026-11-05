"""Read-only report facts with validated values and canonical prose.

Assertions contain only closed fact objects. They cannot supply free-form prose,
SQL, URLs, paths, permissions, contexts or replacement evidence.
"""

from chronicle.errors import ContractError
from chronicle.reports import canonical_json

ASSERTION_KEYS = {
    "kind",
    "report_id",
    "context_id",
    "observation_month",
    "formula",
    "value",
    "unit",
    "status",
    "evidence",
}


def _citations(row):
    return [
        {
            "dataset_id": item["dataset_id"],
            "snapshot_id": item["snapshot_id"],
            "source_namespace": item["source_namespace"],
            "series_key": item["series_key"],
            "observation_month": item["observation_month"],
            "source_revision_key": (
                item["selected_revision"] or item["blocking_revision"] or {}
            ).get("source_revision_key"),
        }
        for item in row["inputs"]
    ]


def proposed_fact(document: dict, month: str) -> dict:
    matches = [row for row in document["rows"] if row["observation_month"] == month]
    if len(matches) != 1:
        raise ContractError("ASSISTANT_PERIOD_NOT_FOUND", "Requested period is not in this report")
    row = matches[0]
    return {
        "kind": "report_value",
        "report_id": document["report_id"],
        "context_id": document["context_id"],
        "observation_month": month,
        "formula": document["formula"],
        "value": row["value"],
        "unit": row["unit"],
        "status": row["status"],
        "evidence": _citations(row),
    }


def validate_assertion(document: dict, proposal: dict) -> dict:
    """Validate every assertion field against the host's permitted retained report."""
    if not isinstance(proposal, dict) or set(proposal) != ASSERTION_KEYS:
        raise ContractError(
            "UNSUPPORTED_ASSERTION", "Only a closed report-value assertion is accepted"
        )
    if proposal.get("kind") != "report_value":
        raise ContractError("UNSUPPORTED_ASSERTION", "Assertion kind is not supported")
    expected = proposed_fact(document, proposal.get("observation_month"))
    if canonical_json(proposal) != canonical_json(expected):
        raise ContractError("ASSERTION_MISMATCH", "Assertion does not match retained evidence")
    # The publication channel has no factual free-text parameters.
    return expected


def render_assertion(document: dict, proposal: dict) -> dict:
    fact = validate_assertion(document, proposal)
    row = next(
        row for row in document["rows"] if row["observation_month"] == fact["observation_month"]
    )
    if fact["status"] == "value":
        text = (
            f"{fact['observation_month'][:7]} · {fact['formula']}: {fact['value']} "
            f"{fact['unit']['name']} ({fact['unit']['metadata_version']})."
        )
    else:
        text = f"{fact['observation_month'][:7]} · {fact['formula']}: {fact['status']}."
    return {
        "assistant_version": "chronicle.b0.v1",
        "provider": None,
        "assertion": fact,
        "text": text,
        "limitations": [
            "SOURCE_RECONSTRUCTION_NOT_LOCAL_HISTORY"
            if document["mode"] == "source_as_of"
            else "PINNED_SNAPSHOT_ONLY",
            "NO_CAUSAL_EXPLANATION",
        ],
        "input_reasons": row["reasons"],
        "model_quality_evaluated": False,
    }


def answer_report(document: dict, month: str) -> dict:
    return render_assertion(document, proposed_fact(document, month))
