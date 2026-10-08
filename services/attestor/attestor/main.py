"""DOSR attestor HTTP server.

Run from services/attestor/:
    uvicorn attestor.main:app --port 8080 --reload
"""

import json

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ValidationError

from . import review as reviewer
from .models import PROTOCOL_VERSION, Policy, ReviewRequest

app = FastAPI(title="DOSR Attestor", version=PROTOCOL_VERSION)


def _parse(raw: bytes, model: type[BaseModel], field: str) -> BaseModel:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(400, f"`{field}` is not valid JSON: {e}")
    try:
        return model.model_validate(data)
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
def keys():
    return {
        "keys": [
            {
                "key_id": reviewer.ATTESTOR_KEY_ID,
                "scheme": "eip712-secp256k1",
                "address": reviewer.ATTESTOR_ADDRESS,
            }
        ]
    }


@app.post("/v1/reviews")
async def create_review(
    request: UploadFile = File(..., description="JSON matching examples/wire/1. attestor-request.json"),
    policy: UploadFile = File(..., description="Immutable policy JSON (.dosr/policy.json)"),
    bundle: UploadFile = File(..., description="Git bundle for parent -> candidate"),
):
    raw_request = await request.read()
    req = _parse(raw_request, ReviewRequest, "request")
    pol = _parse(await policy.read(), Policy, "policy")

    # TODO: verify bundle sha256 and that it contains parent/candidate.
    if not await bundle.read():
        raise HTTPException(400, "`bundle` is empty")

    return reviewer.review(req, pol, raw_request)
