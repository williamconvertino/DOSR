"""PR submission pipeline: working tree -> commit -> bundle -> PR -> attestor -> decision.

Each stage is a named step with status, latency, detail text, and error. The CLI
and GUI both observe the same `Submission` object (via listeners or snapshots),
so adding a step here makes it appear in both front-ends automatically.
"""

import json
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field

from dosr_chain import MockChain
from dosr_git import create_bundle
from dosr_protocol.canonical import new_request_id, qualify_oid, unqualify_oid
from dosr_protocol.policy import format_model
from dosr_protocol.pr_format import (
    build_pr,
    check_limits,
    get_template,
    render_markdown,
    select_context_files,
)
from dosr_protocol.wire import build_review_request, verify_response

from .attestor import AttestorClient, AttestorError
from .config import PROTECTED_PATHS, ClientProfile, DosrRepo
from .timings import TimingStore

STEPS = [
    ("preflight", "Preflight checks"),
    ("commit", "Build git commit"),
    ("parent", "Resolve canonical parent"),
    ("bundle", "Bundle changes"),
    ("pr", "Build PR"),
    ("request", "Build review request"),
    ("upload", "Send PR to attestor"),
    ("await", "Wait for attestor decision"),
    ("verify", "Verify attestation"),
    ("register", "Register on chain (mock)"),
]


class StepFailed(Exception):
    """Expected, user-facing failure (no traceback needed)."""


@dataclass
class Step:
    id: str
    label: str
    status: str = "pending"  # pending | running | done | failed | skipped
    started_at: float | None = None
    duration_ms: float | None = None
    detail: str = ""
    error: str | None = None
    estimate_ms: float = 0.0
    _t0: float | None = None

    def to_dict(self, now: float) -> dict:
        elapsed = self.duration_ms
        if self.status == "running" and self._t0 is not None:
            elapsed = (now - self._t0) * 1000
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": None if elapsed is None else round(elapsed, 1),
            "estimate_ms": round(self.estimate_ms, 1),
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class SubmitOptions:
    message: str | None = None  # commit message, if there are uncommitted changes
    model: dict | None = None  # {provider, model, version}; overrides profile/policy default
    register: bool = True  # submit approved transitions to the (mock) chain
    dry_run: bool = False  # stop after building the review request
    bundle_mode: str | None = None  # "full" | "thin"; defaults to the profile's


class Submission:
    def __init__(self, repo: DosrRepo, profile: ClientProfile, attestor: AttestorClient,
                 chain: MockChain, options: SubmitOptions | None = None):
        self.repo = repo
        self.profile = profile
        self.attestor = attestor
        self.chain = chain
        self.options = options or SubmitOptions()
        self.request_id = new_request_id()
        self.timings = TimingStore(repo.state_dir / "timings.json")
        self.steps = [Step(i, label, estimate_ms=self.timings.estimate_ms(i)) for i, label in STEPS]
        self.outcome = "pending"  # pending | running | approved | rejected | error | dry-run
        self.decision: dict | None = None
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: dict = {"repo": repo.name, "profile": profile.name, "attestor_url": attestor.base_url}
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._t0: float | None = None
        self._lock = threading.RLock()
        self._listeners = []
        self.artifacts_dir = repo.submissions_dir / self.request_id

    # ---------------------------------------------------------------- observers

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def _emit(self) -> None:
        for fn in self._listeners:
            try:
                fn(self)
            except Exception:
                pass

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)

    @contextmanager
    def _run_step(self, step_id: str):
        s = self.step(step_id)
        with self._lock:
            s.status, s.started_at, s._t0 = "running", time.time(), time.perf_counter()
        self._emit()
        try:
            yield s
        except Exception as e:
            with self._lock:
                s.status = "failed"
                s.duration_ms = (time.perf_counter() - s._t0) * 1000
                s.error = str(e) or e.__class__.__name__
                self.errors.append(f"{s.label}: {s.error}")
                if not isinstance(e, (StepFailed, AttestorError)):
                    self.info.setdefault("tracebacks", []).append(traceback.format_exc())
            self._emit()
            raise
        else:
            self._finish(s)

    def _finish(self, s: Step, duration_ms: float | None = None) -> None:
        with self._lock:
            if s.status != "running":
                return
            s.status = "done"
            s.duration_ms = duration_ms if duration_ms is not None else (time.perf_counter() - s._t0) * 1000
            self.timings.record(s.id, s.duration_ms)
        self._emit()

    def _skip_rest(self, reason: str) -> None:
        with self._lock:
            for s in self.steps:
                if s.status == "pending":
                    s.status, s.detail = "skipped", reason

    # ---------------------------------------------------------------- snapshot

    def to_dict(self) -> dict:
        with self._lock:
            now = time.perf_counter()
            steps = [s.to_dict(now) for s in self.steps]
            remaining = 0.0
            for s in steps:
                if s["status"] == "pending":
                    remaining += s["estimate_ms"]
                elif s["status"] == "running":
                    remaining += max(s["estimate_ms"] - (s["duration_ms"] or 0), 0)
            if self._t0 is None:
                elapsed = 0.0
            elif self.finished_at is not None:
                elapsed = (self.finished_at - self.started_at) * 1000
            else:
                elapsed = (now - self._t0) * 1000
            running = next((s for s in steps if s["status"] == "running"), None)
            return {
                "request_id": self.request_id,
                "outcome": self.outcome,
                "current_step": running["id"] if running else None,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_ms": round(elapsed, 1),
                "remaining_ms": 0 if self.finished_at else round(remaining, 1),
                "steps": steps,
                "decision": self.decision,
                "errors": list(self.errors),
                "warnings": list(self.warnings),
                "info": {k: v for k, v in self.info.items() if k != "tracebacks"},
                "artifacts_dir": str(self.artifacts_dir),
                "options": {
                    "message": self.options.message,
                    "register": self.options.register,
                    "dry_run": self.options.dry_run,
                },
            }

    # ---------------------------------------------------------------- pipeline

    def run(self) -> dict:
        with self._lock:
            self.outcome = "running"
            self.started_at = time.time()
            self._t0 = time.perf_counter()
        self._emit()
        try:
            self._pipeline()
        except Exception:
            with self._lock:
                self.outcome = "error"
            self._skip_rest("not run (earlier step failed)")
        finally:
            with self._lock:
                # derived from the perf counter so elapsed stays consistent with step timings
                self.finished_at = self.started_at + (time.perf_counter() - self._t0)
            self.timings.save()
            self._save_run()
            self._emit()
        return self.to_dict()

    def _write(self, name: str, data) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        elif isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _save_run(self) -> None:
        try:
            self._write("run.json", self.to_dict())
        except OSError:
            pass

    def _pipeline(self) -> None:
        repo, git, opts = self.repo, self.repo.git, self.options
        fmt = git.object_format()

        # 1. preflight --------------------------------------------------------
        with self._run_step("preflight") as s:
            cfg = repo.config
            policy = repo.policy
            policy_bytes = repo.policy_bytes()
            if repo.computed_policy_hash() != cfg["policy_hash"]:
                raise StepFailed(".dosr/policy.json does not match the policy hash in .dosr/repo.json")
            onchain = self.chain.get_repository(cfg["repo_id"])
            if not onchain:
                raise StepFailed("repository is not registered on the chain (run `dosr register`)")
            if onchain["policy_hash"] != cfg["policy_hash"]:
                raise StepFailed("on-chain policy hash differs from the local policy")
            template = get_template(policy["review"]["prompt_template"])

            model = opts.model or self.profile.review_model or policy["review"]["approved_models"][0]
            model = {"provider": model["provider"], "model": model["model"],
                     "version": model.get("version") or model.get("model_version")}
            approved = {(m["provider"], m["model"], m["version"]) for m in policy["review"]["approved_models"]}
            if (model["provider"], model["model"], model["version"]) not in approved:
                self.warnings.append(f"model {format_model(model)} is not in the policy's approved models")

            t = time.perf_counter()
            health = self.attestor.health()
            ping_ms = (time.perf_counter() - t) * 1000
            if health.get("protocol_version") != "0.1":
                raise StepFailed(f"attestor speaks protocol {health.get('protocol_version')}, client speaks 0.1")
            keys = self.attestor.keys().get("keys", [])
            listed = {a["key_id"] for a in policy["attestors"]}
            if not any(k.get("key_id") in listed for k in keys):
                self.warnings.append(
                    f"attestor key(s) {[k.get('key_id') for k in keys]} are not listed in this repo's policy"
                )
            self.info.update(model=format_model(model), template=template.id, attestor_ping_ms=round(ping_ms, 1))
            s.detail = f"policy ok · {format_model(model)} · attestor up ({ping_ms:.0f} ms)"

        # 2. commit -------------------------------------------------------------
        with self._run_step("commit") as s:
            if git.is_dirty():
                if not (opts.message and opts.message.strip()):
                    raise StepFailed("there are uncommitted changes; pass a commit message (-m) or commit first")
                n = len(git.status())
                git.add_all()
                candidate = git.commit(opts.message, self.profile.name, self.profile.email)
                s.detail = f"committed {n} change(s) as {candidate[:10]} by {self.profile.name}"
            else:
                candidate = git.head()
                if not candidate:
                    raise StepFailed("repository has no commits")
                s.detail = f"working tree clean · using HEAD {candidate[:10]}"
            self.info["candidate_git_oid"] = qualify_oid(fmt, candidate)

        # 3. parent -------------------------------------------------------------
        with self._run_step("parent") as s:
            canonical_q = self.chain.get_head(cfg["repo_id"])
            cfmt, parent = unqualify_oid(canonical_q)
            if cfmt != fmt:
                raise StepFailed(f"chain records {cfmt} OIDs but the repository uses {fmt}")
            if not git.has_commit(parent):
                raise StepFailed(f"canonical HEAD {parent[:10]} is not in the local repository; fetch it first")
            if parent == candidate:
                raise StepFailed("nothing to submit: HEAD is already the canonical HEAD")
            if not git.is_ancestor(parent, candidate):
                raise StepFailed(
                    f"candidate does not build on canonical HEAD {parent[:10]} (stale parent); rebase and retry"
                )
            n = int(git.run("rev-list", "--count", f"{parent}..{candidate}").strip())
            self.info["parent_git_oid"] = canonical_q
            s.detail = f"parent {parent[:10]} (canonical) · {n} commit(s) ahead"

        # 4. bundle -------------------------------------------------------------
        with self._run_step("bundle") as s:
            mode = opts.bundle_mode or self.profile.bundle_mode or "full"
            info = create_bundle(git, candidate, self.artifacts_dir / "change.bundle", parent=parent, mode=mode)
            self.info["bundle"] = {"sha256": info.sha256, "size_bytes": info.size_bytes, "mode": info.mode}
            s.detail = f"{info.mode} bundle · {info.size_bytes / 1024:.1f} KB · {info.sha256[:19]}…"

        # 5. PR -----------------------------------------------------------------
        with self._run_step("pr") as s:
            files = git.changed_files(parent, candidate)
            touched = {f["path"] for f in files} | {f.get("old_path") for f in files if f.get("old_path")}
            protected = sorted(touched & set(PROTECTED_PATHS))
            if protected:
                raise StepFailed(
                    f"change modifies {', '.join(protected)}; the repository policy is immutable (fork instead)"
                )
            diff_text = git.diff(parent, candidate, template.diff_context_lines)
            problems = check_limits(policy, files, diff_text)
            if problems:
                raise StepFailed("; ".join(problems))
            full_files = {}
            if template.include_full_files:
                for f in files:
                    if f["status"] != "D":
                        text = git.show_file(candidate, f["path"], template.max_file_bytes)
                        if text is not None:
                            full_files[f["path"]] = text
            changed = {f["path"] for f in files}
            context = {}
            for path in select_context_files(template, git.ls_tree(candidate), changed):
                text = git.show_file(candidate, path, template.max_file_bytes)
                if text is not None:
                    context[path] = text
            pr = build_pr(
                template=template,
                repo=cfg,
                parent_git_oid=canonical_q,
                candidate_git_oid=qualify_oid(fmt, candidate),
                commits=git.commits_between(parent, candidate),
                files=files,
                diff_text=diff_text,
                full_files=full_files,
                context_files=context,
            )
            self._write("pr.json", pr)
            self._write("pr.md", render_markdown(pr))
            st = pr["stats"]
            self.info["pr"] = {"title": pr["title"], "pr_hash": pr["pr_hash"], **st}
            s.detail = (
                f"\"{pr['title']}\" · {st['changed_files']} file(s) +{st['additions']}/-{st['deletions']} · "
                f"{st['patch_bytes']} B patch · {len(context)} context file(s)"
            )

        # 6. request ------------------------------------------------------------
        with self._run_step("request") as s:
            request = build_review_request(
                request_id=self.request_id,
                repo=cfg,
                parent_git_oid=canonical_q,
                candidate_git_oid=qualify_oid(fmt, candidate),
                bundle_sha256=info.sha256,
                model=model,
                client_version=self.profile.client_version,
            )
            request_bytes = json.dumps(request, indent=2).encode("utf-8")
            self._write("request.json", request_bytes)
            self._write("policy.json", policy_bytes)
            s.detail = f"request {self.request_id} · {len(request_bytes)} B"

        if opts.dry_run:
            with self._lock:
                self.outcome = "dry-run"
            self._skip_rest("dry run (nothing sent)")
            return

        # 7+8. upload / await ---------------------------------------------------
        bundle_bytes = (self.artifacts_dir / "change.bundle").read_bytes()
        upload = self.step("upload")
        wait = self.step("await")
        upload_cm = self._run_step("upload")
        upload_cm.__enter__()
        state = {"phase": "upload", "await_cm": None}

        def on_uploaded():
            # Called from inside the HTTP request once the last body byte is sent.
            upload.detail = f"{(len(bundle_bytes) + len(request_bytes) + len(policy_bytes)) / 1024:.1f} KB uploaded"
            upload_cm.__exit__(None, None, None)
            state["phase"] = "await"
            state["await_cm"] = self._run_step("await")
            state["await_cm"].__enter__()
            wait.detail = f"attestor reviewing with {format_model(model)}…"
            self._emit()

        try:
            call = self.attestor.submit_review(request_bytes, policy_bytes, bundle_bytes, on_uploaded=on_uploaded)
        except Exception as e:
            cm = state["await_cm"] if state["phase"] == "await" else upload_cm
            try:
                cm.__exit__(type(e), e, e.__traceback__)
            except Exception:
                pass
            raise
        if state["phase"] == "upload":  # transport never pulled the body (unlikely)
            on_uploaded()
        response = call.body
        self._write("response.json", response)
        result = response.get("result", {})
        approved = bool(result.get("approved"))
        with self._lock:
            self.decision = {
                "approved": approved,
                "summary": result.get("summary", ""),
                "attestor_key_id": response.get("attestation", {}).get("attestor_key_id"),
                "signer": response.get("signature", {}).get("signer"),
                "expires_at": response.get("attestation", {}).get("expires_at"),
                "upload_ms": round(call.upload_ms, 1),
                "server_wait_ms": round(call.wait_ms, 1),
            }
            wait.detail = ("APPROVED" if approved else "REJECTED") + f" · {result.get('summary', '')}"
        state["await_cm"].__exit__(None, None, None)

        # 9. verify -------------------------------------------------------------
        with self._run_step("verify") as s:
            errors, warnings = verify_response(request, response, policy)
            self.warnings.extend(warnings)
            if errors:
                raise StepFailed("; ".join(errors))
            s.detail = "attestation matches request" + (f" · {len(warnings)} warning(s)" if warnings else "")

        with self._lock:
            self.outcome = "approved" if approved else "rejected"

        # 10. register ----------------------------------------------------------
        if not approved:
            self._skip_rest("not approved — canonical HEAD unchanged")
            return
        if not opts.register:
            self._skip_rest("skipped (--no-register)")
            return
        try:
            with self._run_step("register") as s:
                event = self.chain.accept_transition(response)
                self._write("chain-event.json", event)
                self.info["chain_event"] = event
                s.detail = f"canonical HEAD → {event['candidate_git_oid'][:17]}… (block {event['block']})"
        except Exception:
            pass  # recorded on the step; the review decision itself still stands


def submit(repo: DosrRepo, profile: ClientProfile, attestor: AttestorClient, chain: MockChain,
           options: SubmitOptions | None = None, listener=None) -> Submission:
    sub = Submission(repo, profile, attestor, chain, options)
    if listener:
        sub.add_listener(listener)
    sub.run()
    return sub
