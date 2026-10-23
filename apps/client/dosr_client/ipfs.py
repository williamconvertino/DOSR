"""Content-addressed storage for sharing repositories between devices.

Two backends behind one interface (`add(bytes) -> id`, `cat(id) -> bytes`):

- `IpfsStore`: a local IPFS node (Kubo / IPFS Desktop) through its HTTP RPC API
  (default http://127.0.0.1:5001, env DOSR_IPFS_API). Ids are real CIDs (`bafy…`), so
  any device whose node can reach ours can fetch them.
- `LocalStore`: a plain directory keyed by sha256 (ids `local:<hex>`). Used when no
  IPFS node is running, so publishing still works on one machine (e.g. two demo
  workspaces simulating two devices). Its ids mean nothing on another computer.

`P2PStore` picks IPFS when the node answers and falls back to the local store, and
reads each id from the backend that wrote it.

An IPFS CID is only a transport pointer: whatever is fetched is checked against the
Git OIDs and sha256 digests that the repository's attestations sign (p2p.py).
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx

DEFAULT_IPFS_API = "http://127.0.0.1:5001"
LOCAL_PREFIX = "local:"
_LOCAL_RE = re.compile(r"^local:[0-9a-f]{64}$")
# CIDv0 (Qm…, base58) or CIDv1 (base32 b…, the default text form).
_CID_RE = re.compile(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,})$")


class StoreError(RuntimeError):
    pass


def default_ipfs_api() -> str:
    return (os.environ.get("DOSR_IPFS_API") or DEFAULT_IPFS_API).rstrip("/")


def default_local_store() -> Path:
    env = os.environ.get("DOSR_LOCAL_STORE")
    return Path(env) if env else Path.home() / ".dosr" / "local-store"


def is_cid(value: str) -> bool:
    return bool(_CID_RE.match(value or ""))


def is_local_id(value: str) -> bool:
    return bool(_LOCAL_RE.match(value or ""))


class LocalStore:
    kind = "local"

    def __init__(self, root=None):
        self.root = Path(root) if root else default_local_store()

    def _path(self, ident: str) -> Path:
        if not is_local_id(ident):
            raise StoreError(f"{ident!r} is not a local-store id")
        hexd = ident[len(LOCAL_PREFIX):]
        return self.root / hexd[:2] / hexd

    def add(self, data: bytes, name: str = "blob") -> str:
        ident = LOCAL_PREFIX + hashlib.sha256(data).hexdigest()
        path = self._path(ident)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return ident

    def cat(self, ident: str, timeout: float | None = None) -> bytes:
        path = self._path(ident)
        if not path.exists():
            raise StoreError(f"{ident[:20]}… is not in the local store {self.root} "
                             "(local ids only work on the computer that published them; use IPFS between devices)")
        data = path.read_bytes()
        if LOCAL_PREFIX + hashlib.sha256(data).hexdigest() != ident:
            raise StoreError(f"local store object {ident[:20]}… is corrupted")
        return data


class IpfsStore:
    """Minimal Kubo RPC client (every RPC call is a POST)."""

    kind = "ipfs"

    def __init__(self, api_url: str | None = None, timeout: float = 60.0, http: httpx.Client | None = None):
        self.api_url = (api_url or default_ipfs_api()).rstrip("/")
        self.timeout = timeout
        self._http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=2.0))
        self._probe: tuple[float, bool] | None = None  # (when, up): avoids re-dialing a node that is off

    def _post(self, cmd: str, params=None, files=None, timeout: float | None = None) -> httpx.Response:
        t = timeout or self.timeout
        try:
            # The node is local: if it does not accept within 0.5 s it is not running (Windows
            # otherwise spends ~2 s retrying a refused localhost connection).
            r = self._http.post(f"{self.api_url}/api/v0/{cmd}", params=params, files=files,
                                timeout=httpx.Timeout(t, connect=min(t, 0.5)))
        except httpx.HTTPError as e:
            if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                self._probe = (time.monotonic(), False)
            raise StoreError(f"IPFS node at {self.api_url} is not reachable ({e.__class__.__name__}); "
                             "start it with `demo.py ipfs` or IPFS Desktop") from e
        if r.status_code != 200:
            try:
                msg = r.json().get("Message") or r.text
            except ValueError:
                msg = r.text
            raise StoreError(f"IPFS {cmd} failed: {msg.strip()[:300]}")
        return r

    def id(self) -> dict:
        return self._post("id", timeout=3).json()

    def available(self, max_age: float = 5.0) -> bool:
        if self._probe and time.monotonic() - self._probe[0] < max_age:
            return self._probe[1]
        try:
            self.id()
            up = True
        except StoreError:
            up = False
        self._probe = (time.monotonic(), up)
        return up

    def add(self, data: bytes, name: str = "blob") -> str:
        r = self._post("add", params={"cid-version": "1", "pin": "true", "quieter": "true"},
                       files={"file": (name, data, "application/octet-stream")})
        # One JSON object per line; the last one is the root.
        last = [ln for ln in r.text.splitlines() if ln.strip()][-1]
        return json.loads(last)["Hash"]

    def cat(self, cid: str, timeout: float | None = None) -> bytes:
        if not is_cid(cid):
            raise StoreError(f"{cid!r} is not an IPFS CID")
        t = timeout or self.timeout
        # Kubo's own timeout makes it give up (instead of searching forever) when no peer has it.
        try:
            return self._post("cat", params={"arg": cid, "timeout": f"{int(t)}s"}, timeout=t + 5).content
        except StoreError as e:
            if "deadline" in str(e) or "timeout" in str(e).lower():
                raise StoreError(f"could not find {cid[:16]}… on IPFS within {int(t)} s: is the publishing "
                                 "device online, with its IPFS node running? Connect to it directly "
                                 "(P2P → add peer) and retry") from e
            raise

    def pin(self, cid: str) -> None:
        self._post("pin/add", params={"arg": cid}, timeout=30)

    def peers(self) -> list[dict]:
        return self._post("swarm/peers", timeout=5).json().get("Peers") or []

    def connect(self, addrs: list[str]) -> list[dict]:
        """Dial each multiaddr (/ip4/…/tcp/4001/p2p/<peer id>); returns per-address results."""
        out = []
        for addr in addrs:
            try:
                self._post("swarm/connect", params={"arg": addr}, timeout=10)
                out.append({"addr": addr, "ok": True})
            except StoreError as e:
                out.append({"addr": addr, "ok": False, "error": str(e)})
        return out

    def info(self) -> dict:
        ident = self.id()
        try:
            n_peers = len(self.peers())
        except StoreError:
            n_peers = None
        return {
            "peer_id": ident.get("ID"),
            "agent": ident.get("AgentVersion"),
            "addresses": ident.get("Addresses") or [],
            "shareable_addresses": shareable_addrs(ident.get("Addresses") or []),
            "peers": n_peers,
        }


def shareable_addrs(addrs: list[str]) -> list[str]:
    """Addresses worth giving to another device: drop loopback and link-local ones."""
    out = []
    for a in addrs:
        if a.startswith(("/ip4/127.", "/ip6/::1/", "/ip4/169.254.", "/ip6/fe80")):
            continue
        if "/p2p-circuit" in a:
            continue
        out.append(a)
    return out


class P2PStore:
    """IPFS when the node answers, else the local store; reads route by id type."""

    def __init__(self, ipfs: IpfsStore | None = None, local: LocalStore | None = None, mode: str = "auto"):
        if mode not in ("auto", "ipfs", "local"):
            raise ValueError("p2p store mode must be auto, ipfs or local")
        self.ipfs = ipfs or IpfsStore()
        self.local = local or LocalStore()
        self.mode = mode

    def ipfs_up(self) -> bool:
        return self.mode != "local" and self.ipfs.available()

    def writer(self):
        if self.mode == "local":
            return self.local
        if self.ipfs.available():
            return self.ipfs
        if self.mode == "ipfs":
            raise StoreError(f"IPFS node at {self.ipfs.api_url} is not running")
        return self.local

    def add(self, data: bytes, name: str = "blob") -> str:
        return self.writer().add(data, name)

    def cat(self, ident: str, timeout: float | None = None) -> bytes:
        ident = ident.strip()
        if is_local_id(ident):
            return self.local.cat(ident)
        if is_cid(ident):
            if self.mode == "local":
                raise StoreError("this is an IPFS CID but the client is in local-store mode")
            return self.ipfs.cat(ident, timeout)
        raise StoreError(f"{ident[:40]!r} is neither an IPFS CID (bafy…/Qm…) nor a local-store id (local:…)")

    def pin(self, ident: str) -> None:
        if is_cid(ident):
            try:
                self.ipfs.pin(ident)
            except StoreError:
                pass

    def status(self) -> dict:
        out = {"mode": self.mode, "ipfs_api": self.ipfs.api_url, "local_store": str(self.local.root)}
        if self.mode != "local":
            try:
                if not self.ipfs.available():
                    raise StoreError(f"no IPFS node answering at {self.ipfs.api_url}")
                out.update(backend="ipfs", ipfs=self.ipfs.info())
                return out
            except StoreError as e:
                out["ipfs_error"] = str(e)
        out["backend"] = "local"
        return out
