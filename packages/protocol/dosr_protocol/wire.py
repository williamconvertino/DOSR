"""Attestor wire payloads (examples/wire/1.* and 3.*).

`build_review_request` must stay in sync with
services/attestor/attestor/models.py::ReviewRequest, which rejects unknown fields.
"""

import time

from . import PROTOCOL_VERSION

PLACEHOLDER_SIGNATURE = "0x" + "00" * 65


def build_review_request(
    *,
    request_id: str,
    repo: dict,
    parent_git_oid: str,
    candidate_git_oid: str,
    bundle_sha256: str,
    model: dict,
    client_version: str,
    ipfs_cid: str | None = None,
) -> dict:
    """`repo` needs repo_id, chain_id, contract, policy_hash; `model` needs
    provider, model, version."""
    bundle = {"format": "git-bundle-v1", "sha256": bundle_sha256}
    if ipfs_cid:
        bundle["ipfs_cid"] = ipfs_cid
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "repository": {
            "repo_id": repo["repo_id"],
            "chain_id": repo["chain_id"],
            "contract": repo["contract"],
            "policy_hash": repo["policy_hash"],
        },
        "transition": {
            "parent_git_oid": parent_git_oid,
            "candidate_git_oid": candidate_git_oid,
            "bundle": bundle,
        },
        "review": {
            "provider": model["provider"],
            "model": model["model"],
            "model_version": model["version"],
        },
        "client": {"client_version": client_version},
    }


def verify_response(request: dict, response: dict, policy: dict, now: int | None = None):
    """Client-side sanity check that an attestor response is bound to *our* request.

    Returns (errors, warnings). This is a convenience for the honest client only;
    the contract is the real enforcement point.
    """
    errors: list[str] = []
    warnings: list[str] = []
    now = int(time.time()) if now is None else now

    for key in ("protocol_version", "request_id", "result", "attestation", "signature"):
        if key not in response:
            errors.append(f"response is missing `{key}`")
    if errors:
        return errors, warnings

    if response["request_id"] != request["request_id"]:
        errors.append("request_id does not match")

    att = response["attestation"]
    repo = request["repository"]
    tr = request["transition"]
    rv = request["review"]
    expected = {
        "repo_id": repo["repo_id"],
        "chain_id": repo["chain_id"],
        "verifying_contract": repo["contract"],
        "policy_hash": repo["policy_hash"],
        "parent_git_oid": tr["parent_git_oid"],
        "candidate_git_oid": tr["candidate_git_oid"],
        "bundle_sha256": tr["bundle"]["sha256"],
        "provider": rv["provider"],
        "model": rv["model"],
        "model_version": rv["model_version"],
    }
    for key, want in expected.items():
        if key not in att:
            # The rejected-response example omits some fields; only flag if approved.
            (errors if response["result"].get("approved") else warnings).append(
                f"attestation is missing `{key}`"
            )
        elif att[key] != want:
            errors.append(f"attestation.{key} = {att[key]!r}, expected {want!r}")

    if att.get("approved") != response["result"].get("approved"):
        errors.append("attestation.approved disagrees with result.approved")

    signer = response["signature"].get("signer", "").lower()
    key_id = att.get("attestor_key_id")
    trusted = {(a["key_id"], a["address"].lower()) for a in policy["attestors"]}
    if (key_id, signer) not in trusted:
        msg = f"signer {key_id}/{signer} is not an attestor listed in the policy"
        (errors if response["result"].get("approved") else warnings).append(msg)

    expires = att.get("expires_at")
    if isinstance(expires, int) and expires <= now:
        errors.append("attestation has already expired")

    if response["signature"].get("value") == PLACEHOLDER_SIGNATURE:
        warnings.append("signature is the attestor's all-zero placeholder (not verifiable yet)")

    return errors, warnings
