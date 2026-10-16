"""EIP-712 test vector, decision schema parsing, and canonical_json's JCS guards."""

import json
from pathlib import Path

import pytest

from dosr_protocol.canonical import MAX_SAFE_INTEGER, canonical_json
from dosr_protocol.decision import DECISION_V1, DecisionError, build_prompt, parse_decision
from dosr_protocol.eip712 import ATTESTATION_FIELDS, oid_to_bytes32, recover_signer, typed_data
from dosr_protocol.pr_format import get_template

ROOT = Path(__file__).resolve().parents[3]
VECTOR = json.loads((ROOT / "examples/wire/6. eip712-test-vector.json").read_text())

eth_account = pytest.importorskip("eth_account")


# ---------- EIP-712 ----------


def test_vector_hashes_are_reproduced():
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    msg = encode_typed_data(full_message=typed_data(VECTOR["attestation"]))
    assert "0x" + msg.header.hex() == VECTOR["domain_separator"]
    assert "0x" + msg.body.hex() == VECTOR["struct_hash"]
    assert "0x" + keccak(b"\x19\x01" + msg.header + msg.body).hex() == VECTOR["digest"]
    assert "0x" + keccak(text=VECTOR["attestation_type_string"]).hex() == VECTOR["attestation_type_hash"]
    names = ",".join(f"{t} {n}" for n, t, _ in ATTESTATION_FIELDS)
    assert VECTOR["attestation_type_string"] == f"Attestation({names})"


def test_vector_signature():
    from eth_account import Account

    assert recover_signer(VECTOR["attestation"], VECTOR["signature"]) == VECTOR["signer"]
    # secp256k1 signing is deterministic (RFC 6979), so re-signing gives the same bytes.
    signed = Account.sign_typed_data(VECTOR["private_key"], full_message=typed_data(VECTOR["attestation"]))
    assert "0x" + bytes(signed.signature).hex() == VECTOR["signature"]


@pytest.mark.parametrize("field", [k for _, _, k in ATTESTATION_FIELDS])
def test_every_signed_field_is_bound(field):
    att = dict(VECTOR["attestation"])
    value = att[field]
    if isinstance(value, bool):
        att[field] = not value
    elif isinstance(value, int):
        att[field] = value + 1
    elif field.endswith("git_oid"):
        att[field] = "sha1:" + "f" * 40
    elif value and value.startswith(("0x", "sha256:")):
        att[field] = value[:-1] + ("0" if value[-1] != "0" else "1")
    else:
        att[field] = (value or "") + "x"
    assert recover_signer(att, VECTOR["signature"]) != VECTOR["signer"]


def test_domain_is_bound():
    for key, value in (("chain_id", 1), ("verifying_contract", "0x" + "55" * 20)):
        att = {**VECTOR["attestation"], key: value}
        assert recover_signer(att, VECTOR["signature"]) != VECTOR["signer"]


def test_sha1_oid_is_right_padded():
    assert oid_to_bytes32("sha1:" + "ab" * 20) == bytes.fromhex("ab" * 20) + b"\0" * 12
    assert oid_to_bytes32("sha256:" + "cd" * 32) == bytes.fromhex("cd" * 32)


# ---------- decision schema ----------


def test_prompt_ends_with_schema_instructions():
    prompt = build_prompt(get_template("standard-code-review-v1"), "# PR\n\ndiff\n", DECISION_V1)
    assert prompt.startswith("# PR\n\ndiff\n\n## Response format")
    with pytest.raises(DecisionError):
        build_prompt(get_template("standard-code-review-v1"), "x", "unknown-schema")


@pytest.mark.parametrize("raw", [
    '{"approved": true, "summary": "ok", "issues": []}',
    '  \n```json\n{"approved": true, "summary": "ok", "issues": []}\n```\n',
    '```\n{"approved": true, "summary": "ok", "issues": []}\n```',
])
def test_valid_decisions(raw):
    assert parse_decision(raw, DECISION_V1) == {"approved": True, "summary": "ok", "issues": []}


@pytest.mark.parametrize("raw", [
    "LGTM, approved!",
    'Sure: {"approved": true, "summary": "ok", "issues": []}',
    '{"approved": "yes", "summary": "ok", "issues": []}',
    '{"approved": true, "summary": "", "issues": []}',
    '{"approved": true, "summary": "ok"}',
    '{"approved": true, "summary": "ok", "issues": [], "confidence": 1}',
    '{"approved": true, "summary": "ok", "issues": [1]}',
    '[true]',
    '```json\n{"approved": true, "summary": "ok", "issues": []}\n``` and ```more```',
])
def test_invalid_decisions(raw):
    with pytest.raises(DecisionError):
        parse_decision(raw, DECISION_V1)


# ---------- canonical_json ----------


def test_canonical_json_matches_jcs_for_safe_values():
    assert canonical_json({"b": [1, True, None], "a": "é\n\u0001"}) == '{"a":"é\\n\\u0001","b":[1,true,null]}'.encode()
    canonical_json({"n": MAX_SAFE_INTEGER, "m": -MAX_SAFE_INTEGER})


@pytest.mark.parametrize("value", [
    {"x": 1.5},
    {"x": MAX_SAFE_INTEGER + 1},
    {"x": -(MAX_SAFE_INTEGER + 1)},
    {"é": 1},
    {"nested": [{"\U0001F600": 1}]},
])
def test_canonical_json_rejects_values_where_python_and_jcs_differ(value):
    with pytest.raises(TypeError):
        canonical_json(value)
