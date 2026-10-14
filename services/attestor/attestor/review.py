"""Review pipeline: request vs policy -> chain state -> Git bundle -> reviewer -> signature.

Check names follow examples/wire/2. attestor-things-to-verify.json. Cheap checks run
first; the bundle is only opened, and the reviewer only called, once everything
before it has passed. Every outcome that gets this far is signed, rejections included.

Deterministic code decides everything except "is this change good": the prompt is
built here from the bundle (its sha256 is signed as review_input_hash), the model's
raw output is hashed as-is (response_hash) and then parsed by the policy's decision
schema; output that does not parse is a rejection.

Model backends live in reviewers.py (mock, plus any OpenAI-compatible provider). Not implemented yet: rate limiting.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass

from dosr_protocol.canonical import canonical_json, policy_hash, sha256_0x
from dosr_protocol.decision import SCHEMAS, DecisionError, build_prompt, parse_decision
from dosr_protocol.pr_format import PRFormatError, get_template, render_markdown

from . import gitcheck, reviewers as backends
from .config import Settings
from .models import PROTOCOL_VERSION, Policy, ReviewRequest
from .signing import SCHEME, Signer

# review_input_hash / response_hash when the model was never called.
NO_HASH = "0x" + "00" * 32


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class Attestor:
    def __init__(self, settings: Settings, reviewers: dict | None = None):
        self.settings = settings
        self.signer = Signer(settings.private_key)
        self.cache = gitcheck.ObjectCache(settings.cache_dir, settings.git_timeout_seconds)
        self.reviewers = backends.default_reviewers(settings.providers_file) if reviewers is None else reviewers

    @property
    def key_id(self) -> str:
        return self.settings.key_id

    def review(self, req: ReviewRequest, raw_request: dict, policy: Policy, raw_policy: dict,
               bundle: bytes, provider_key: str | None = None) -> dict:
        """Raises gitcheck.MissingPrerequisites for a thin bundle the attestor cannot open,
        and reviewers.ReviewerError if the model cannot be called (nothing is signed)."""
        checks: list[Check] = []

        def check(name: str, ok: bool, detail: str = "") -> bool:
            checks.append(Check(name, bool(ok), "" if ok else detail))
            return bool(ok)

        tr, rv, repo = req.transition, req.review, req.repository
        fmt = policy.git.object_format
        computed_policy_hash = policy_hash(raw_policy)

        # 1. request vs policy --------------------------------------------------
        check("policy_hash_matches", computed_policy_hash == repo.policy_hash,
              f"policy hashes to {computed_policy_hash}, request claims {repo.policy_hash}")
        approved_models = {(m.provider, m.model, m.version) for m in policy.review.approved_models}
        check("model_is_approved", (rv.provider, rv.model, rv.model_version) in approved_models,
              f"model {rv.provider}/{rv.model}@{rv.model_version} is not approved by the policy")
        check("provider_is_supported", rv.provider in self.reviewers,
              f"this attestor cannot call provider {rv.provider!r}")
        listed = {a.key_id: a.address for a in policy.attestors}
        if check("attestor_is_listed", self.key_id in listed, f"attestor {self.key_id} is not listed in the policy"):
            check("attestor_address_matches", listed[self.key_id].lower() == self.signer.address.lower(),
                  f"policy lists {self.key_id} as {listed[self.key_id]}, but this attestor signs as "
                  f"{self.signer.address}")
        try:
            template = get_template(policy.review.prompt_template)
            check("prompt_template_is_approved", True)
        except PRFormatError as e:
            template = None
            check("prompt_template_is_approved", False, str(e))
        check("decision_schema_is_known", policy.review.decision_schema in SCHEMAS,
              f"unknown decision schema {policy.review.decision_schema!r}")
        for name in ("parent_git_oid", "candidate_git_oid"):
            check(f"{name}_format", getattr(tr, name).startswith(fmt + ":"),
                  f"{name} does not use the policy's object format {fmt}")
        check("candidate_differs_from_parent", tr.parent_git_oid != tr.candidate_git_oid,
              "candidate is the same commit as parent")
        check("bundle_format_matches", tr.bundle.format == policy.storage.bundle_format,
              "bundle format does not match the policy")
        bundle_sha = "sha256:" + hashlib.sha256(bundle).hexdigest()
        check("bundle_hash_matches", bundle_sha == tr.bundle.sha256,
              f"bundle hashes to {bundle_sha}, request claims {tr.bundle.sha256}")

        # 2. chain state -------------------------------------------------------
        chain = self.settings.chain()
        onchain = None
        if check("chain_matches", repo.chain_id == chain.chain_id and repo.contract.lower() == chain.contract.lower(),
                 f"this attestor follows chain {chain.chain_id} contract {chain.contract}"):
            onchain = chain.get_repository(repo.repo_id)
            if check("repository_exists", onchain is not None, "repository is not registered on the chain"):
                check("policy_hash_matches_genesis", onchain["policy_hash"] == computed_policy_hash,
                      "policy does not match the policy hash registered at genesis")
                check("parent_is_canonical_head", onchain["canonical_head"] == tr.parent_git_oid,
                      f"stale parent: canonical HEAD is {onchain['canonical_head']}")

        # 3. Git bundle (only once everything above holds) ---------------------
        transition = None
        if all(c.ok for c in checks):
            try:
                transition = gitcheck.open_transition(
                    self.cache, repo.repo_id, fmt, bundle,
                    parent=tr.parent_git_oid.partition(":")[2],
                    candidate=tr.candidate_git_oid.partition(":")[2],
                    template=template,
                    repo_name=onchain.get("name"),
                )
                check("bundle_contains_transition", True)
            except gitcheck.MissingPrerequisites:
                raise
            except gitcheck.BundleError as e:
                check("bundle_contains_transition", False, str(e))

        if transition is not None:
            protected = sorted(transition.touched_paths & gitcheck.PROTECTED_PATHS)
            check("protected_paths_unchanged", not protected,
                  f"change modifies {', '.join(protected)}; the repository policy is immutable")
            check("policy_file_matches", _policy_file_hash(transition.policy_file) == computed_policy_hash,
                  f"candidate's {gitcheck.POLICY_PATH} does not match the policy hash")
            n_files, patch_bytes = len(transition.changed_files), len(transition.patch.encode("utf-8"))
            check("within_max_changed_files", n_files <= policy.review.max_changed_files,
                  f"{n_files} changed files exceeds policy max_changed_files={policy.review.max_changed_files}")
            check("within_max_patch_bytes", patch_bytes <= policy.review.max_patch_bytes,
                  f"patch is {patch_bytes} bytes, exceeds policy max_patch_bytes={policy.review.max_patch_bytes}")

        # 4. reviewer ----------------------------------------------------------
        review_input_hash = response_hash = NO_HASH
        failures = [c.detail for c in checks if not c.ok]
        if failures:
            result = {"approved": False, "summary": "Rejected: " + "; ".join(failures), "issues": failures}
        else:
            prompt = build_prompt(template, render_markdown(transition.pr), policy.review.decision_schema)
            review_input_hash = sha256_0x(prompt.encode("utf-8"))
            raw = self.reviewers[rv.provider].complete(prompt, rv, provider_key)
            response_hash = sha256_0x(raw.encode("utf-8"))
            try:
                result = parse_decision(raw, policy.review.decision_schema)
                check("model_output_is_valid", True)
            except DecisionError as e:
                check("model_output_is_valid", False, str(e))
                result = {"approved": False, "summary": f"Rejected: {e}", "issues": [str(e)]}

        derived = None
        if transition is not None:
            derived = {
                "pr_hash": transition.pr["pr_hash"],
                "commit_count": len(transition.pr["commits"]),
                "changed_files": transition.changed_files,
                "changed_file_count": len(transition.changed_files),
                "patch_bytes": len(transition.patch.encode("utf-8")),
                "patch_sha256": "sha256:" + hashlib.sha256(transition.patch.encode("utf-8")).hexdigest(),
            }
        return self._signed_response(req, raw_request, result, checks, derived, review_input_hash, response_hash)

    def _signed_response(self, req: ReviewRequest, raw_request: dict, result: dict, checks: list[Check],
                         derived: dict | None, review_input_hash: str, response_hash: str) -> dict:
        """Builds a response shaped like examples/wire/3. attestor-response-*.json."""
        request_hash = sha256_0x(canonical_json(raw_request))
        now = int(time.time())
        attestation = {
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
            "approved": result["approved"],
            "request_hash": request_hash,
            "review_input_hash": review_input_hash,
            "response_hash": response_hash,
            "issued_at": now,
            "expires_at": now + self.settings.attestation_ttl_seconds,
            "attestor_key_id": self.key_id,
        }
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": req.request_id,
            "result": result,
            "evidence": {
                "canonical_request_hash": request_hash,
                "model_response_hash": response_hash,
                "checks": [asdict(c) for c in checks],
                "derived_review_input": derived,
            },
            "attestation": attestation,
            "signature": {
                "scheme": SCHEME,
                "signer": self.signer.address,
                "value": self.signer.sign(attestation),
            },
        }


def _policy_file_hash(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return policy_hash(json.loads(data))
    except (ValueError, TypeError):
        return None
