"""HTTP client for the attestor API (services/attestor/API.md)."""

import json
import os
import time
from dataclasses import dataclass
from typing import Callable

import httpx

_CHUNK = 64 * 1024


class AttestorError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ReviewCall:
    status_code: int
    body: dict
    upload_bytes: int
    upload_ms: float
    wait_ms: float


def _format_detail(status: int, detail) -> str:
    """Human-readable version of the attestor's 400/422 bodies."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and "errors" in detail:
        errs = "; ".join(
            f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}" for e in detail["errors"][:5]
        )
        return f"invalid `{detail.get('field')}` part — {errs}"
    if isinstance(detail, list):  # FastAPI default (e.g. a missing multipart part)
        return "; ".join(f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}" for e in detail[:5])
    return json.dumps(detail)[:500]


def _multipart(parts: list[tuple[str, str, str, bytes]]) -> tuple[str, list[bytes]]:
    boundary = "dosr-" + os.urandom(12).hex()
    chunks = []
    for field, filename, ctype, data in parts:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, chunks


class AttestorClient:
    def __init__(self, base_url: str, http: httpx.Client | None = None, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0))

    def _get(self, path: str) -> dict:
        try:
            r = self.http.get(self.base_url + path)
        except httpx.HTTPError as e:
            raise AttestorError(f"cannot reach attestor at {self.base_url}: {e}") from e
        if r.status_code != 200:
            raise AttestorError(f"attestor {path} returned HTTP {r.status_code}", r.status_code)
        return r.json()

    def health(self) -> dict:
        return self._get("/v1/health")

    def keys(self) -> dict:
        return self._get("/v1/keys")

    def submit_review(
        self,
        request_bytes: bytes,
        policy_bytes: bytes,
        bundle_bytes: bytes,
        on_uploaded: Callable[[], None] | None = None,
    ) -> ReviewCall:
        boundary, chunks = _multipart(
            [
                ("request", "request.json", "application/json", request_bytes),
                ("policy", "policy.json", "application/json", policy_bytes),
                ("bundle", "change.bundle", "application/octet-stream", bundle_bytes),
            ]
        )
        total = sum(len(c) for c in chunks)
        marks = {"start": time.perf_counter(), "uploaded": None}

        def body():
            for c in chunks:
                for i in range(0, len(c), _CHUNK):
                    yield c[i : i + _CHUNK]
            marks["uploaded"] = time.perf_counter()
            if on_uploaded:
                on_uploaded()

        try:
            r = self.http.post(
                self.base_url + "/v1/reviews",
                content=body(),
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(total),
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as e:
            raise AttestorError(f"request to attestor at {self.base_url} failed: {e}") from e
        done = time.perf_counter()
        uploaded = marks["uploaded"] or done

        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text[:2000]}
        if r.status_code != 200:
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            raise AttestorError(
                f"attestor rejected the request (HTTP {r.status_code}): {_format_detail(r.status_code, detail)}",
                r.status_code,
                detail,
            )
        return ReviewCall(
            status_code=r.status_code,
            body=payload,
            upload_bytes=total,
            upload_ms=(uploaded - marks["start"]) * 1000,
            wait_ms=(done - uploaded) * 1000,
        )
