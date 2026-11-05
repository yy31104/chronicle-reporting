"""Access control tests use independent principals and revoked policy records."""

import json

import pytest

from chronicle.access import authenticate, configure_access, rotate_access
from chronicle.errors import ContractError


def test_distinct_local_identities_and_scope_revocation(tmp_path):
    policy, credentials = configure_access(tmp_path / "private", ["owned"])
    tokens = json.loads(credentials.read_text())
    assert tokens["viewer"] != tokens["operator"]
    viewer = authenticate(tokens["viewer"], policy)
    operator = authenticate(tokens["operator"], policy)
    viewer.require("owned")
    operator.require("owned", operator=True)
    for principal, dataset, op in ((viewer, "owned", True), (operator, "other", False)):
        with pytest.raises(ContractError, match="Access denied"):
            principal.require(dataset, operator=op)
    updated = json.loads(policy.read_text())
    updated["identities"][0]["enabled"] = False
    policy.write_text(json.dumps(updated))
    with pytest.raises(ContractError, match="Authentication required"):
        authenticate(tokens["viewer"], policy)
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert policy.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ContractError) as error:
        configure_access(tmp_path / "private", ["new"])
    assert error.value.code == "ACCESS_CONFIG_EXISTS"
    assert json.loads(credentials.read_text()) == tokens


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": "false"},
        {"datasets": "owned"},
        {"role": "admin"},
        {"token_sha256": "not-a-hash"},
        {"extra": "unsupported"},
    ],
)
def test_malformed_policy_fails_closed(tmp_path, change):
    policy, credentials = configure_access(tmp_path / "private", ["owned"])
    tokens = json.loads(credentials.read_text())
    data = json.loads(policy.read_text())
    data["identities"][0].update(change)
    policy.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        authenticate(tokens["viewer"], policy)


def test_rotation_revokes_both_tokens_and_preserves_scope_and_disabled_state(tmp_path):
    directory = tmp_path / "private"
    policy, credentials = configure_access(directory, ["owned", "second"])
    old = json.loads(credentials.read_text())
    payload = json.loads(policy.read_text())
    payload["identities"][1]["enabled"] = False
    policy.write_text(json.dumps(payload))
    rotate_access(directory)
    new = json.loads(credentials.read_text())
    for role in old:
        assert new[role] != old[role]
        with pytest.raises(ContractError, match="Authentication required"):
            authenticate(old[role], policy)
    assert authenticate(new["viewer"], policy).datasets == frozenset({"owned", "second"})
    with pytest.raises(ContractError, match="Authentication required"):
        authenticate(new["operator"], policy)
    assert credentials.stat().st_mode & 0o777 == policy.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in directory.rglob(".access-*")] == [".access-rotate.lock"]


def test_interrupted_rotation_keeps_valid_policy_and_can_be_retried(tmp_path, monkeypatch):
    import chronicle.access as access

    directory = tmp_path / "private"
    policy, credentials = configure_access(directory, ["owned"])
    old = json.loads(credentials.read_text())
    replace = access._replace_private_json

    def fail_policy(path, value):
        if path == policy:
            raise OSError("interrupted before policy publication")
        replace(path, value)

    monkeypatch.setattr(access, "_replace_private_json", fail_policy)
    with pytest.raises(OSError):
        rotate_access(directory)
    assert authenticate(old["viewer"], policy).name == "local-viewer"
    monkeypatch.setattr(access, "_replace_private_json", replace)
    rotate_access(directory)
    assert authenticate(json.loads(credentials.read_text())["viewer"], policy)
    with pytest.raises(ContractError):
        authenticate(old["viewer"], policy)
