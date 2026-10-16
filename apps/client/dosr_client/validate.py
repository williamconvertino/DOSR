"""Client-side validation of a review request before it is sent to the attestor.

Runs the same checks as the attestor (services/attestor/attestor/review.py), with the
same names, on exactly the bytes about to be sent: request JSON, policy, Git bundle,
and prompt. The bundle is opened in a scratch repository and the PR and prompt are
rebuilt from it, as the attestor does. So an honest client learns before sending (and
paying for a model call) whether the attestor would reject.

This is a convenience, not a security boundary: a modified client can skip it
(`--force` simulates that), and the attestor re-checks everything itself.
"""

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from dosr_chain import MockChain
from dosr_git import GitError, GitRepo, pr_inputs
from dosr_protocol import PROTOCOL_VERSION
from dosr_protocol.canonical import policy_hash, sha256_0x, sha256_qualified
from dosr_protocol.decision import SCHEMAS, build_prompt
from dosr_protocol.policy import PolicyError, validate_policy
from dosr_protocol.pr_format import PRFormatError, build_pr, render_markdown, template_for_policy
from dosr_protocol.versions import explain_unapproved, find_approved_model

from .config import POLICY_FILE, PROTECTED_PATHS, provider_key_env

# Stages, in the attestor's order. "client" checks have no attestor equivalent.
STAGES = ("request", "chain", "bundle", "prompt", "client")

# Top-level and nested keys of the wire request (attestor models.ReviewRequest, extra="forbid").
_REQUEST_SHAPE = {
    "protocol_version": None,
    "request_id": None,
    "repository": {"repo_id", "chain_id", "contract", "policy_hash"},
    "transition": {"parent_git_oid", "candidate_git_oid", "bundle"},
    "review": {"provider", "model", "model_version"},
    "client": {"client_version"},
}
_OID_LEN = {"sha1": 40, "sha256": 64}


@dataclass
class LocalCheck:
    name: str
    stage: str
    ok: bool | None  # None = not run (an earlier check failed)
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Rebuilt:
    """What the attestor will derive from the bundle (if it could be opened)."""

    pr: dict | None = None
    prompt: str | None = None


def _valid_oid(oid, fmt: str) -> bool:
    if not isinstance(oid, str):
        return False
    algo, _, hexpart = oid.partition(":")
    return algo == fmt and len(hexpart) == _OID_LEN[fmt] and all(c in "0123456789abcdef" for c in hexpart)


def _request_shape_errors(request) -> list[str]:
    if not isinstance(request, dict):
        return ["request is not a JSON object"]
    errors = []
    extra = set(request) - set(_REQUEST_SHAPE)
    missing = set(_REQUEST_SHAPE) - set(request)
    if extra:
        errors.append(f"unknown field(s) {sorted(extra)} (the attestor rejects unknown fields)")
    if missing:
        errors.append(f"missing field(s) {sorted(missing)}")
    for key, sub in _REQUEST_SHAPE.items():
        if sub is None or not isinstance(request.get(key), dict):
            continue
        bad = set(request[key]) - sub
        if bad:
            errors.append(f"unknown field(s) {key}.{sorted(bad)}")
        if sub - set(request[key]):
            errors.append(f"missing field(s) {key}.{sorted(sub - set(request[key]))}")
    if request.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"protocol_version must be {PROTOCOL_VERSION!r}")
    return errors


def _open_bundle(local: GitRepo, bundle: bytes, fmt: str, parent: str, candidate: str, mode: str,
                 template, repo: dict, tmp: Path) -> tuple[GitRepo, dict, bytes | None]:
    """Unbundle into a scratch bare repo, like the attestor. A thin bundle borrows the
    local objects (standing in for the attestor's cache of earlier verified bundles);
    a full bundle must be self-contained. Raises ValueError with a readable reason."""
    work = GitRepo(tmp / "work.git")
    work.path.mkdir()
    work.run("init", "-q", "--bare", f"--object-format={fmt}")
    if mode == "thin":
        objects = (local.git_dir / "objects").resolve()
        (work.path / "objects" / "info" / "alternates").write_text(f"{objects}\n", newline="\n")
    path = tmp / "change.bundle"
    path.write_bytes(bundle)
    try:
        heads = work.run("bundle", "list-heads", str(path)).split()
        work.run("bundle", "unbundle", str(path))
        work.run("rev-list", "--objects", "--quiet", candidate)
    except GitError as e:
        raise ValueError(f"bundle cannot be opened: {e.stderr.splitlines()[-1] if e.stderr else e}")
    if candidate not in heads:
        raise ValueError("bundle does not contain the candidate commit")
    if not work.has_commit(parent):
        raise ValueError(f"bundle does not contain the declared parent commit {parent[:12]}")
    if not work.is_ancestor(parent, candidate):
        raise ValueError(f"candidate does not descend from the declared parent {parent[:12]}")
    pr = build_pr(
        template=template,
        repo={"name": repo.get("name"), "repo_id": repo["repo_id"]},
        parent_git_oid=f"{fmt}:{parent}",
        candidate_git_oid=f"{fmt}:{candidate}",
        **pr_inputs(work, template, parent, candidate),
    )
    try:
        policy_file = work.run_bytes("show", f"{candidate}:{POLICY_FILE}")
    except GitError:
        policy_file = None
    return work, pr, policy_file


def validate_submission(
    *,
    local_git: GitRepo,
    repo_config: dict,
    request: dict,
    policy_bytes: bytes,
    bundle_bytes: bytes,
    bundle_mode: str,
    prompt: str | None,
    client_pr: dict | None,
    chain: MockChain,
    attestor_keys: list[dict] | None,
    provider_key: str | None,
) -> tuple[list[LocalCheck], Rebuilt]:
    checks: list[LocalCheck] = []
    rebuilt = Rebuilt()

    def check(name, stage, ok, detail=""):
        checks.append(LocalCheck(name, stage, bool(ok), "" if ok else detail))
        return bool(ok)

    def skip(name, stage, why="not checked: an earlier check failed"):
        checks.append(LocalCheck(name, stage, None, why))

    # 1. request vs policy --------------------------------------------------------
    shape = _request_shape_errors(request)
    check("request_is_well_formed", "request", not shape, "; ".join(shape))
    try:
        policy = json.loads(policy_bytes)
        validate_policy(policy)
        policy_ok = True
    except (ValueError, PolicyError) as e:
        policy, policy_ok = None, False
        check("policy_is_valid", "request", False, f"policy part is invalid: {e}")
    if not policy_ok or shape:
        for name in ("policy_hash_matches", "model_is_approved", "attestor_is_listed", "prompt_template_is_approved",
                     "bundle_hash_matches"):
            skip(name, "request")
        return checks, rebuilt

    repo, tr, rv = request["repository"], request["transition"], request["review"]
    review = policy["review"]
    fmt = policy["git"]["object_format"]
    computed = policy_hash(policy)
    check("policy_hash_matches", "request", computed == repo["policy_hash"],
          f"policy hashes to {computed}, request claims {repo['policy_hash']}")
    approved = review["approved_models"]
    check("model_is_approved", "request", find_approved_model(approved, rv["provider"], rv["model"], rv["model_version"]),
          explain_unapproved(approved, rv["provider"], rv["model"], rv["model_version"]))
    listed = {a["key_id"]: a["address"] for a in policy["attestors"]}
    if attestor_keys is None:
        skip("attestor_is_listed", "request", "attestor keys unavailable")
    else:
        mine = [k for k in attestor_keys if k.get("key_id") in listed]
        if check("attestor_is_listed", "request", mine,
                 f"attestor key(s) {', '.join(k.get('key_id', '?') for k in attestor_keys) or 'none'} are not "
                 f"listed in the policy (trusted: {', '.join(listed)})"):
            k = mine[0]
            check("attestor_address_matches", "request",
                  listed[k["key_id"]].lower() == (k.get("address") or "").lower(),
                  f"policy lists {k['key_id']} as {listed[k['key_id']]}, but the attestor signs as {k.get('address')}")
    try:
        template = template_for_policy(review["prompt_template"], review.get("context_files", []),
                                       review.get("template"))
        check("prompt_template_is_approved", "request", True)
    except PRFormatError as e:
        template = None
        check("prompt_template_is_approved", "request", False, str(e))
    check("decision_schema_is_known", "request", review["decision_schema"] in SCHEMAS,
          f"unknown decision schema {review['decision_schema']!r}")
    for name in ("parent_git_oid", "candidate_git_oid"):
        check(f"{name}_format", "request", _valid_oid(tr[name], fmt),
              f"{name} {str(tr[name])[:20]}… is not a {fmt} Git OID (the policy's object format)")
    check("candidate_differs_from_parent", "request", tr["parent_git_oid"] != tr["candidate_git_oid"],
          "candidate is the same commit as parent")
    check("bundle_format_matches", "request", tr["bundle"].get("format") == policy["storage"]["bundle_format"],
          "bundle format does not match the policy")
    actual = sha256_qualified(bundle_bytes)
    check("bundle_hash_matches", "request", bundle_bytes and actual == tr["bundle"].get("sha256"),
          "bundle is empty" if not bundle_bytes else
          f"bundle hashes to {actual[:23]}…, request claims {str(tr['bundle'].get('sha256'))[:23]}…")

    # 2. chain state (the mock chain file stands in for the contract) -----------------
    onchain = None
    if check("chain_matches", "chain",
             repo["chain_id"] == chain.chain_id and str(repo["contract"]).lower() == chain.contract.lower(),
             f"the chain is {chain.chain_id} / {chain.contract}, request says {repo['chain_id']} / {repo['contract']}"):
        onchain = chain.get_repository(repo["repo_id"])
        if check("repository_exists", "chain", onchain is not None, "repository is not registered on the chain"):
            check("policy_hash_matches_genesis", "chain", onchain["policy_hash"] == computed,
                  "policy does not match the policy hash registered at genesis")
            check("parent_is_canonical_head", "chain", onchain["canonical_head"] == tr["parent_git_oid"],
                  f"stale parent: canonical HEAD is {onchain['canonical_head'][:17]}…, request builds on "
                  f"{str(tr['parent_git_oid'])[:17]}…")

    # 3. bundle -----------------------------------------------------------------------
    can_open = (template is not None and bundle_bytes and _valid_oid(tr["parent_git_oid"], fmt)
                and _valid_oid(tr["candidate_git_oid"], fmt) and tr["parent_git_oid"] != tr["candidate_git_oid"])
    if not can_open:
        for name in ("bundle_contains_transition", "protected_paths_unchanged", "policy_file_matches",
                     "within_max_changed_files", "within_max_patch_bytes"):
            skip(name, "bundle")
    else:
        parent, candidate = tr["parent_git_oid"].partition(":")[2], tr["candidate_git_oid"].partition(":")[2]
        with tempfile.TemporaryDirectory(prefix="dosr-validate-") as tmp:
            try:
                _, pr, policy_file = _open_bundle(local_git, bundle_bytes, fmt, parent, candidate, bundle_mode,
                                                  template, repo_config, Path(tmp))
                check("bundle_contains_transition", "bundle", True)
            except (ValueError, GitError, PRFormatError) as e:
                pr = None
                check("bundle_contains_transition", "bundle", False, str(e))
        if pr is not None:
            rebuilt.pr = pr
            touched = {p for f in pr["files"] for p in (f["path"], f.get("old_path")) if p}
            protected = sorted(touched & set(PROTECTED_PATHS))
            check("protected_paths_unchanged", "bundle", not protected,
                  f"change modifies {', '.join(protected)}; the repository policy is immutable (fork instead)")
            try:
                file_hash = policy_hash(json.loads(policy_file)) if policy_file is not None else None
            except (ValueError, TypeError):
                file_hash = None
            check("policy_file_matches", "bundle", file_hash == computed,
                  f"candidate's {POLICY_FILE} does not match the policy hash")
            n, patch_bytes = len(pr["files"]), len(pr["diff"].encode("utf-8"))
            check("within_max_changed_files", "bundle", n <= review["max_changed_files"],
                  f"{n} changed files exceeds policy max_changed_files={review['max_changed_files']}")
            check("within_max_patch_bytes", "bundle", patch_bytes <= review["max_patch_bytes"],
                  f"patch is {patch_bytes} bytes, exceeds policy max_patch_bytes={review['max_patch_bytes']}")

    # 4. PR + prompt: what we built vs what the attestor will derive -------------------
    if rebuilt.pr is None:
        skip("pr_matches_bundle", "prompt")
        skip("client_prompt_matches", "prompt")
    else:
        rebuilt.prompt = build_prompt(template, render_markdown(rebuilt.pr, template), review["decision_schema"])
        if client_pr is not None:
            check("pr_matches_bundle", "prompt", client_pr["pr_hash"] == rebuilt.pr["pr_hash"],
                  f"our PR (hash {client_pr['pr_hash'][:14]}…) differs from the PR the bundle produces "
                  f"({rebuilt.pr['pr_hash'][:14]}…)")
        if prompt is not None:
            mine, theirs = sha256_0x(prompt.encode("utf-8")), sha256_0x(rebuilt.prompt.encode("utf-8"))
            check("client_prompt_matches", "prompt", mine == theirs,
                  f"the prompt we would send (sha256 {mine[:14]}…) differs from the prompt the attestor will "
                  f"derive from the bundle and policy ({theirs[:14]}…)")

    # 5. client-only ----------------------------------------------------------------------
    env = provider_key_env(rv["provider"])
    check("provider_key_available", "client", not env or provider_key,
          f"{rv['provider']} needs the contributor's API key: set {env} (environment or the repo's .env)")
    return checks, rebuilt


def summarize(checks: list[LocalCheck]) -> tuple[int, int, int]:
    """(passed, failed, skipped)"""
    return (sum(c.ok is True for c in checks), sum(c.ok is False for c in checks), sum(c.ok is None for c in checks))
