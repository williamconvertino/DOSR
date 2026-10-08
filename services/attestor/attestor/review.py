"""Mock review: decides approve/reject from cheap request-vs-policy checks only.

Not implemented yet (see services/attestor/README.md for the full list):
reading HEAD/policy hash from the contract, hashing the policy, opening the Git
bundle, deriving the diff, calling the LLM, and producing a real EIP-712 signature.
"""

import hashlib
import json
import os
import time

from .models import PROTOCOL_VERSION, Policy, ReviewRequest

ATTESTATION_TTL_SECONDS = 3600

ATTESTOR_KEY_ID = os.environ.get("ATTESTOR_KEY_ID", "local-dev-attestor-1")
ATTESTOR_ADDRESS = os.environ.get(
    "ATTESTOR_ADDRESS", "0x1111111111111111111111111111111111111111"
)

# TODO: replace with a real EIP-712 signature over the attestation.
PLACEHOLDER_SIGNATURE = "0x" + "00" * 65


def _sha256_hex(data: bytes) -> str:
    return "0x" + hashlib.sha256(data).hexdigest()


def check(req: ReviewRequest, policy: Policy) -> list[str]:
    """Returns the list of failed checks; empty means approve."""
    failures = []

    approved = {(m.provider, m.model, m.version) for m in policy.review.approved_models}
    if (req.review.provider, req.review.model, req.review.model_version) not in approved:
        failures.append(
            f"model {req.review.provider}/{req.review.model}@{req.review.model_version} "
            "is not approved by the policy"
        )

    if ATTESTOR_KEY_ID not in {a.key_id for a in policy.attestors}:
        failures.append(f"attestor {ATTESTOR_KEY_ID} is not listed in the policy")

    fmt = policy.git.object_format
    for name in ("parent_git_oid", "candidate_git_oid"):
        if not getattr(req.transition, name).startswith(fmt + ":"):
            failures.append(f"{name} does not use the policy's object format {fmt}")

    if req.transition.parent_git_oid == req.transition.candidate_git_oid:
        failures.append("candidate is the same commit as parent")

    if req.transition.bundle.format != policy.storage.bundle_format:
        failures.append("bundle format does not match the policy")

    return failures


def review(req: ReviewRequest, policy: Policy, raw_request: bytes) -> dict:
    """Builds a response shaped like examples/wire/3. attestor-response-*.json."""
    failures = check(req, policy)
    approved = not failures
    summary = (
        "No policy-blocking issues were found in the proposed transition."
        if approved
        else "Rejected: " + "; ".join(failures)
    )
    result = {"approved": approved, "summary": summary}

    request_hash = _sha256_hex(raw_request)
    response_hash = _sha256_hex(json.dumps(result, sort_keys=True).encode())
    now = int(time.time())

    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": req.request_id,
        "result": result,
        "evidence": {
            "canonical_request_hash": request_hash,
            "model_response_hash": response_hash,
        },
        "attestation": {
            "repo_id": req.repository.repo_id,
            "chain_id": req.repository.chain_id,
            "verifying_contract": req.repository.contract,
            "policy_hash": req.repository.policy_hash,
            "parent_git_oid": req.transition.parent_git_oid,
            "candidate_git_oid": req.transition.candidate_git_oid,
            "bundle_sha256": req.transition.bundle.sha256,
            "bundle_cid": req.transition.bundle.ipfs_cid,
            "provider": req.review.provider,
            "model": req.review.model,
            "model_version": req.review.model_version,
            "approved": approved,
            "request_hash": request_hash,
            "response_hash": response_hash,
            "issued_at": now,
            "expires_at": now + ATTESTATION_TTL_SECONDS,
            "attestor_key_id": ATTESTOR_KEY_ID,
        },
        "signature": {
            "scheme": "eip712-secp256k1",
            "signer": ATTESTOR_ADDRESS,
            "value": PLACEHOLDER_SIGNATURE,
        },
    }
