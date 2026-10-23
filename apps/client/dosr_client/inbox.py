"""Peer inbox: a dev channel for telling another device "I published a new HEAD".

DEV ONLY. With a real chain, devices would learn about new canonical HEADs from
contract events. Until then each GUI can run this tiny HTTP endpoint on the LAN
(`dosr gui --p2p-port 8790`). After publishing, the client POSTs the share code to
every known peer; their GUI lists it in the inbox with a Pull button. Pasting the share
code by hand (chat, email) does exactly the same thing.

The inbox only carries hints. It is unauthenticated and nothing it says is trusted:
pulling a share code re-verifies the bundle and every signed transition (p2p.py).
It deliberately exposes nothing else (no files, no keys, no repository API), which is
why it is a separate server from the GUI, which stays on 127.0.0.1.
"""

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from . import __version__

MAX_ITEMS = 100
_OID = r"^(sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64})$"


class Announcement(BaseModel):
    share: str = Field(max_length=120, pattern=r"^(local:[0-9a-f]{64}|Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,110})$")
    repo_id: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    name: str = Field(max_length=100)
    canonical_head: str = Field(pattern=_OID)
    accepted_transitions: int = Field(ge=0, le=1_000_000)
    from_name: str = Field("", max_length=200)
    device: str = Field("", max_length=100)
    inbox_url: str | None = Field(None, max_length=200)
    ipfs_addrs: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field("", max_length=500)


class Hello(BaseModel):
    device: str = Field("", max_length=100)
    inbox_url: str | None = Field(None, max_length=200)
    peer_id: str | None = Field(None, max_length=120)
    ipfs_addrs: list[str] = Field(default_factory=list, max_length=20)


class _JsonFile:
    def __init__(self, path: Path, default):
        self.path = Path(path)
        self.default = default
        self.lock = threading.Lock()

    def read(self):
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(self.default))

    def write(self, data) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class Inbox(_JsonFile):
    """Announcements received from peers, newest first."""

    def __init__(self, path):
        super().__init__(path, [])

    def add(self, ann: dict, sender_ip: str | None = None) -> dict:
        with self.lock:
            items = [i for i in self.read() if i.get("share") != ann["share"]]
            item = {**ann, "id": uuid.uuid4().hex[:12], "received_at": int(time.time()), "sender_ip": sender_ip,
                    "status": "new"}
            items.insert(0, item)
            self.write(items[:MAX_ITEMS])
            return item

    def list(self) -> list[dict]:
        return self.read()

    def set_status(self, item_id: str, status: str) -> dict | None:
        with self.lock:
            items = self.read()
            for i in items:
                if i["id"] == item_id:
                    i["status"] = status
                    self.write(items)
                    return i
        return None

    def get(self, item_id: str) -> dict | None:
        return next((i for i in self.read() if i["id"] == item_id), None)


class Peers(_JsonFile):
    """Other devices' inbox URLs (announcements go to each of them)."""

    def __init__(self, path):
        super().__init__(path, [])

    def list(self) -> list[dict]:
        return self.read()

    def upsert(self, url: str, **info) -> dict:
        url = normalize_url(url)
        with self.lock:
            peers = self.read()
            peer = next((p for p in peers if p["url"] == url), None)
            if not peer:
                peer = {"url": url, "added_at": int(time.time())}
                peers.append(peer)
            peer.update({k: v for k, v in info.items() if v is not None})
            self.write(peers)
            return peer

    def remove(self, url: str) -> None:
        url = normalize_url(url)
        with self.lock:
            self.write([p for p in self.read() if p["url"] != url])


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("peer address is empty")
    if "://" not in url:
        url = "http://" + url
    if not url.startswith(("http://", "https://")):
        raise ValueError("peer address must be http://HOST:PORT")
    if url.count(":") < 2:
        url += ":8790"
    return url


def lan_ip() -> str:
    """This machine's LAN address (the interface used for outbound traffic; nothing is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def create_inbox_app(inbox: Inbox, peers: Peers, hello) -> FastAPI:
    """`hello()` returns this device's Hello (device name, inbox URL, IPFS addresses)."""
    app = FastAPI(title="DOSR peer inbox (dev)", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.get("/dosr/hello")
    def get_hello():
        return {"dosr_inbox": "0.1", **hello()}

    @app.post("/dosr/hello")
    def post_hello(body: Hello, request: Request):
        # A device that added us as a peer introduces itself, so peering is mutual.
        if body.inbox_url:
            peers.upsert(body.inbox_url, device=body.device, peer_id=body.peer_id, ipfs_addrs=body.ipfs_addrs,
                         via="introduced", last_seen=int(time.time()))
        return {"dosr_inbox": "0.1", **hello()}

    @app.post("/dosr/announce")
    def announce(body: Announcement, request: Request):
        item = inbox.add(body.model_dump(), request.client.host if request.client else None)
        if body.inbox_url:
            try:
                peers.upsert(body.inbox_url, device=body.device, ipfs_addrs=body.ipfs_addrs,
                             last_seen=int(time.time()))
            except ValueError:
                pass
        return {"ok": True, "id": item["id"]}

    return app


def serve_in_thread(app: FastAPI, host: str, port: int):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None  # only the main thread may; the GUI owns Ctrl+C
    thread = threading.Thread(target=server.run, name="dosr-peer-inbox", daemon=True)
    thread.start()
    return server


def send_hello(url: str, me: dict, timeout: float = 5) -> dict:
    r = httpx.post(normalize_url(url) + "/dosr/hello", json=me, timeout=timeout)
    r.raise_for_status()
    return r.json()


def send_announcement(url: str, payload: dict, timeout: float = 5) -> None:
    r = httpx.post(normalize_url(url) + "/dosr/announce", json=payload, timeout=timeout)
    r.raise_for_status()


def announce_all(peers: Peers, payload: dict) -> list[dict]:
    out = []
    for p in peers.list():
        try:
            send_announcement(p["url"], payload)
            out.append({"url": p["url"], "device": p.get("device"), "ok": True})
        except (httpx.HTTPError, ValueError) as e:
            out.append({"url": p["url"], "device": p.get("device"), "ok": False, "error": str(e) or e.__class__.__name__})
    return out
