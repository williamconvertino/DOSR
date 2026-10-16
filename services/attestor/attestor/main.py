"""DOSR attestor HTTP server.

Run from services/attestor/ (settings: see attestor/config.py):
    uvicorn attestor.main:app --port 8080 --reload
"""

import json
from functools import lru_cache

from fastapi import Depends, FastAPI, File, Header, HTTPException, Path, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .gitcheck import MissingPrerequisites
from .models import PROTOCOL_VERSION, Policy, ReviewRequest
from .review import Attestor
from .reviewers import MissingProviderKey, ReviewerError
from .signing import SCHEME

app = FastAPI(title="DOSR Attestor", version=PROTOCOL_VERSION)

MAX_PROMPT_BYTES = 4 * 1024 * 1024


@lru_cache
def get_attestor() -> Attestor:
    # Tests swap this out with app.dependency_overrides[get_attestor].
    return Attestor(Settings.from_env())


def _parse(raw: bytes, model: type[BaseModel], field: str) -> tuple[BaseModel, dict]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(400, f"`{field}` is not valid JSON: {e}")
    try:
        return model.model_validate(data), data
    except ValidationError as e:
        raise HTTPException(
            422, {"field": field, "errors": e.errors(include_url=False, include_context=False)}
        )


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/v1/health")
def health():
    return {"status": "ok", "protocol_version": PROTOCOL_VERSION}


@app.get("/v1/keys")
def keys(attestor: Attestor = Depends(get_attestor)):
    return {"keys": [{"key_id": attestor.key_id, "scheme": SCHEME, "address": attestor.signer.address}]}


@app.get("/v1/repos/{repo_id}/commits/{git_oid}")
def has_commit(
    repo_id: str = Path(pattern=r"^0x[0-9a-fA-F]{64}$"),
    git_oid: str = Path(pattern=r"^(sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64})$"),
    attestor: Attestor = Depends(get_attestor),
):
    """Whether the attestor already holds this commit, i.e. a thin bundle based on it will work."""
    return {"present": attestor.cache.has_commit(repo_id, git_oid.partition(":")[2])}


@app.post("/v1/reviews")
async def create_review(
    request: UploadFile = File(..., description="JSON matching examples/wire/1. attestor-request.json"),
    policy: UploadFile = File(..., description="Immutable policy JSON (.dosr/policy.json)"),
    bundle: UploadFile = File(..., description="Git bundle for parent -> candidate"),
    prompt: UploadFile | None = File(
        None, description="Optional: the review prompt the client built. Never sent to the model; "
                          "the request is rejected if it differs from the prompt derived from the bundle"),
    attestor: Attestor = Depends(get_attestor),
    x_dosr_provider_key: str | None = Header(
        None, description="Contributor's API key for the review model's provider; used once, never stored"),
):
    req, raw_req = _parse(await request.read(), ReviewRequest, "request")
    pol, raw_pol = _parse(await policy.read(), Policy, "policy")

    client_prompt = None
    if prompt is not None:
        try:
            client_prompt = (await prompt.read(MAX_PROMPT_BYTES + 1)).decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "`prompt` is not UTF-8 text")
        if len(client_prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise HTTPException(413, f"`prompt` exceeds {MAX_PROMPT_BYTES} bytes")

    limit = attestor.settings.max_bundle_bytes
    bundle_bytes = await bundle.read(limit + 1)
    if not bundle_bytes:
        raise HTTPException(400, "`bundle` is empty")
    if len(bundle_bytes) > limit:
        raise HTTPException(413, f"`bundle` exceeds {limit} bytes")

    try:
        # Git work is blocking; keep it off the event loop.
        return await run_in_threadpool(attestor.review, req, raw_req, pol, raw_pol, bundle_bytes,
                                       x_dosr_provider_key, client_prompt)
    except MissingProviderKey as e:
        raise HTTPException(400, str(e))
    except ReviewerError as e:
        raise HTTPException(502, f"review model unavailable: {e}")
    except MissingPrerequisites as e:
        raise HTTPException(409, {
            "code": "missing_prerequisites",
            "message": f"{e}; send a full bundle",
            "missing": e.missing,
        })
