"""EVM adapter for the DOSR repository contract (contracts/src/DOSRRegistry.sol).

Same method names and return shapes as `MockChain`, so the client pipeline, the
client-side validator and the attestor work with either. The difference is that this
chain is *shared*: every client and attestor pointed at the same RPC URL and contract
reads one canonical HEAD per repository, and the contract (not each device) decides
which approved transition lands first.

Talks plain JSON-RPC over HTTP (httpx) and encodes calls with eth-abi; transactions
are signed locally with eth-account, so it works against any node (anvil in the demo).

Conflicts: the contract accepts a transition only if its attested parent is the
current HEAD. When two approved PRs race on the same parent, the one mined first
wins; the other reverts with `StaleParent` and `accept_transition` raises
`StaleParentError` carrying the new HEAD, so the client can sync, rebase and resubmit.
"""

import json
import threading
import time
from functools import lru_cache
from pathlib import Path

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from dosr_protocol.canonical import canonical_json, policy_hash, qualify_oid, sha256_0x, unqualify_oid
from dosr_protocol.eip712 import ATTESTATION_FIELDS, oid_to_bytes32, typed_data

from .dev import DEFAULT_RPC_URL, DEV_CONTRACT_ADDRESS, dev_address_book, dev_key
from .errors import ChainError, StaleParentError

ARTIFACT = Path(__file__).parent / "contract" / "DOSRRegistry.json"
FORMATS = {"sha1": 1, "sha256": 2}
FORMAT_NAMES = {v: k for k, v in FORMATS.items()}
ZERO32 = b"\0" * 32


@lru_cache(maxsize=1)
def load_artifact() -> dict:
    return json.loads(ARTIFACT.read_text("utf-8"))


# ---------------------------------------------------------------- ABI helpers


def _abi_type(p: dict) -> str:
    t = p["type"]
    if t.startswith("tuple"):
        return "(" + ",".join(_abi_type(c) for c in p["components"]) + ")" + t[len("tuple"):]
    return t


def _named(p: dict, value):
    """Decoded ABI value -> plain Python (structs become dicts keyed by field name)."""
    t = p["type"]
    if t.startswith("tuple"):
        if t.endswith("[]"):
            inner = {**p, "type": t[:-2]}
            return [_named(inner, v) for v in value]
        return {c["name"]: _named(c, v) for c, v in zip(p["components"], value)}
    if t.endswith("[]"):
        inner = {**p, "type": t[:-2]}
        return [_named(inner, v) for v in value]
    if t == "address":
        return to_checksum_address(value)
    return value


class Abi:
    def __init__(self, abi: list[dict]):
        self.functions, self.by_selector, self.events, self.errors = {}, {}, {}, {}
        for item in abi:
            if item["type"] == "function":
                types = [_abi_type(i) for i in item["inputs"]]
                sel = keccak(text=f"{item['name']}({','.join(types)})")[:4]
                self.functions[item["name"]] = (sel, types, item)
                self.by_selector[sel] = (item["name"], types, item)
            elif item["type"] == "event":
                types = [_abi_type(i) for i in item["inputs"]]
                topic = keccak(text=f"{item['name']}({','.join(types)})")
                self.events[topic] = item
                self.events[item["name"]] = (topic, item)
            elif item["type"] == "error":
                types = [_abi_type(i) for i in item["inputs"]]
                self.errors[keccak(text=f"{item['name']}({','.join(types)})")[:4]] = item

    def encode_call(self, name: str, *args) -> bytes:
        sel, types, _ = self.functions[name]
        return sel + abi_encode(types, list(args))

    def decode_output(self, name: str, data: bytes):
        item = self.functions[name][2]
        values = abi_decode([_abi_type(o) for o in item["outputs"]], data)
        return [_named(o, v) for o, v in zip(item["outputs"], values)]

    def decode_input(self, data: bytes) -> tuple[str, dict] | None:
        entry = self.by_selector.get(bytes(data[:4]))
        if not entry:
            return None
        name, types, item = entry
        values = abi_decode(types, bytes(data[4:]))
        return name, {i["name"]: _named(i, v) for i, v in zip(item["inputs"], values)}

    def topic(self, event: str) -> bytes:
        return self.events[event][0]

    def decode_log(self, log: dict) -> tuple[str, dict] | None:
        topics = [bytes.fromhex(t[2:]) for t in log["topics"]]
        item = self.events.get(topics[0]) if topics else None
        if not item:
            return None
        indexed = [i for i in item["inputs"] if i["indexed"]]
        plain = [i for i in item["inputs"] if not i["indexed"]]
        out = {}
        for i, t in zip(indexed, topics[1:]):
            out[i["name"]] = _named(i, abi_decode([_abi_type(i)], t)[0])
        values = abi_decode([_abi_type(i) for i in plain], bytes.fromhex(log["data"][2:]))
        for i, v in zip(plain, values):
            out[i["name"]] = _named(i, v)
        return item["name"], out

    def decode_error(self, data: bytes) -> tuple[str, dict]:
        sel = bytes(data[:4])
        if sel == bytes.fromhex("08c379a0"):  # Error(string)
            return "Error", {"message": abi_decode(["string"], bytes(data[4:]))[0]}
        if sel == bytes.fromhex("4e487b71"):  # Panic(uint256)
            return "Panic", {"code": abi_decode(["uint256"], bytes(data[4:]))[0]}
        item = self.errors.get(sel)
        if not item:
            return "UnknownError", {"data": "0x" + bytes(data).hex()}
        values = abi_decode([_abi_type(i) for i in item["inputs"]], bytes(data[4:]))
        return item["name"], {i["name"]: _named(i, v) for i, v in zip(item["inputs"], values)}


# ---------------------------------------------------------------- JSON-RPC


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def revert_data(self) -> bytes | None:
        d = self.data
        if isinstance(d, dict):
            d = d.get("data")
        if isinstance(d, str) and d.startswith("0x") and len(d) >= 10:
            return bytes.fromhex(d[2:])
        return None


class Rpc:
    def __init__(self, url: str, timeout: float = 10):
        self.url = url
        self.http = httpx.Client(timeout=timeout)
        self._id = 0

    def __call__(self, method: str, *params):
        self._id += 1
        try:
            r = self.http.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)})
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ChainError(f"cannot reach the chain at {self.url} ({e.__class__.__name__}: {e}); is the dev "
                             "chain running? (demo: `demo.py chain`)") from None
        if "error" in body:
            err = body["error"]
            raise RpcError(err.get("code"), err.get("message", ""), err.get("data"))
        return body["result"]


def _hex(n: int) -> str:
    return hex(n)


def _b32(value: str) -> bytes:
    raw = bytes.fromhex(value[2:] if value.startswith("0x") else value)
    if len(raw) != 32:
        raise ChainError(f"not a 32-byte value: {value!r}")
    return raw


def _0x(b: bytes) -> str:
    return "0x" + bytes(b).hex()


def oid_from_bytes32(raw: bytes, fmt: str) -> str:
    return qualify_oid(fmt, bytes(raw)[: 20 if fmt == "sha1" else 32].hex())


# ---------------------------------------------------------------- adapter


class EvmChain:
    """DOSR registry contract over JSON-RPC. `private_key` is who pays for writes."""

    kind = "evm"
    shared = True  # every device reads the same canonical HEAD from here

    def __init__(self, rpc_url: str | None = None, contract: str | None = None, private_key: str | None = None,
                 confirmations: int = 0, timeout: float = 10, _shared=None):
        self.rpc_url = rpc_url or DEFAULT_RPC_URL
        self.contract = to_checksum_address(contract or DEV_CONTRACT_ADDRESS)
        self.confirmations = max(0, int(confirmations))
        self._key = private_key or dev_key(0)
        from eth_account import Account

        self.account = Account.from_key(self._key)
        # State shared by copies made with `as_account` (RPC connection, caches, nonce lock).
        self._s = _shared or {"rpc": Rpc(self.rpc_url, timeout), "chain_id": None, "code_ok": None,
                              "lock": threading.RLock(), "blocks": {}, "tx": {}, "labels": {}}
        self.abi = Abi(load_artifact()["abi"])

    def as_account(self, private_key: str | None) -> "EvmChain":
        """The same chain connection, sending transactions from another account."""
        if not private_key or private_key == self._key:
            return self
        return EvmChain(self.rpc_url, self.contract, private_key, self.confirmations, _shared=self._s)

    def label_accounts(self, labels: dict) -> None:
        """address -> display name (e.g. profile names) for the explorer."""
        self._s["labels"].update({a.lower(): n for a, n in labels.items()})

    # ---------- plumbing

    @property
    def rpc(self) -> Rpc:
        return self._s["rpc"]

    @property
    def chain_id(self) -> int:
        if self._s["chain_id"] is None:
            self._s["chain_id"] = int(self.rpc("eth_chainId"), 16)
        return self._s["chain_id"]

    @property
    def location(self) -> str:
        return f"{self.rpc_url} · contract {self.contract}"

    def _ensure_code(self) -> None:
        if self._s["code_ok"]:
            return
        code = self.rpc("eth_getCode", self.contract, "latest")
        if code in ("0x", "0x0", None):
            raise ChainError(f"no DOSR contract at {self.contract} on {self.rpc_url} (chain {self.chain_id}); "
                             "deploy it first (demo: `demo.py chain`)")
        self._s["code_ok"] = True

    def _tag(self) -> str:
        if not self.confirmations:
            return "latest"
        return _hex(max(0, int(self.rpc("eth_blockNumber"), 16) - self.confirmations))

    def _call(self, name: str, *args, tag: str | None = None):
        self._ensure_code()
        data = self.abi.encode_call(name, *args)
        try:
            out = self.rpc("eth_call", {"to": self.contract, "data": _0x(data)}, tag or self._tag())
        except RpcError as e:
            raise self._revert(e, name) from None
        return self.abi.decode_output(name, bytes.fromhex(out[2:]))

    def _revert(self, e: RpcError, context: str, fmt: str = "sha1") -> ChainError:
        data = e.revert_data()
        if data is None:
            return ChainError(f"{context}: {e.message}")
        name, args = self.abi.decode_error(data)
        return self._error(name, args, context, fmt)

    def _error(self, name: str, args: dict, context: str, fmt: str = "sha1") -> ChainError:
        prefix = {"acceptTransition": "transition rejected by contract",
                  "registerRepository": "registration rejected by contract",
                  "publishSnapshot": "snapshot rejected by contract"}.get(context, f"{context} reverted")
        oid = lambda b: oid_from_bytes32(b, fmt)  # noqa: E731
        msg = {
            "UnknownRepository": "unknown repository",
            "AlreadyRegistered": f"repository {_0x(args.get('repoId', b''))[:18]}… is already registered",
            "PolicyMismatch": "policy hash does not match the genesis policy",
            "UnknownAttestor": f"attestor is not approved by the repository policy (key id {args.get('keyId')!r})",
            "BadSignature": f"signature does not match the attestation (recovered {args.get('recovered')}, the "
                            f"policy lists {args.get('expected')})",
            "NotApproved": "attestation is not an approval",
            "Expired": "attestation has expired",
            "NotYetValid": "attestation is issued in the future",
            "NoChange": "candidate equals parent",
            "AlreadyCanonical": "candidate is already part of the canonical history",
            "InvalidRegistration": f"invalid registration: {args.get('reason')}",
        }.get(name)
        if name == "StaleParent":
            head = oid(args["head"])
            return StaleParentError(f"{prefix}: stale parent: attested parent {oid(args['parent'])} != HEAD {head} "
                                    "(another approved transition landed on this parent first)", head=head)
        if name == "NotCanonicalHead":
            return ChainError(f"{prefix}: {oid(args['given'])} is not the canonical HEAD {oid(args['head'])}")
        if msg is None:
            msg = f"{name} {args}" if args else name
        return ChainError(f"{prefix}: {msg}", reason=name)

    # ---------- transactions

    def _send(self, data: bytes, context: str, fmt: str = "sha1", to: str | None = "contract",
              wait: bool = True, simulate: bool = True) -> dict:
        """Sign and send a transaction from this account. Simulates first, so a revert is
        reported with the contract's reason before any gas is spent."""
        to_addr = self.contract if to == "contract" else to
        if to_addr:
            self._ensure_code()
        frm = self.account.address
        call = {"from": frm, "data": _0x(data), **({"to": to_addr} if to_addr else {})}
        with self._s["lock"]:
            try:
                if simulate and to_addr:
                    self.rpc("eth_call", call, "latest")
                gas = int(self.rpc("eth_estimateGas", call), 16) if simulate else 3_000_000
            except RpcError as e:
                raise self._revert(e, context, fmt) from None
            tx = {"data": _0x(data), "value": 0, "gas": int(gas * 1.25) + 21_000,
                  "gasPrice": int(self.rpc("eth_gasPrice"), 16), "chainId": self.chain_id,
                  "nonce": int(self.rpc("eth_getTransactionCount", frm, "pending"), 16)}
            if to_addr:
                tx["to"] = to_addr
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            try:
                tx_hash = self.rpc("eth_sendRawTransaction", _0x(raw))
            except RpcError as e:
                raise ChainError(f"{context}: node refused the transaction ({e.message})") from None
        if not wait:
            return {"tx_hash": tx_hash}
        receipt = self.wait(tx_hash)
        if int(receipt["status"], 16) != 1:
            raise self._mined_revert(tx_hash, context, fmt)
        return receipt

    def wait(self, tx_hash: str, timeout: float = 60) -> dict:
        deadline = time.time() + timeout
        while True:
            receipt = self.rpc("eth_getTransactionReceipt", tx_hash)
            if receipt:
                return receipt
            if time.time() > deadline:
                raise ChainError(f"transaction {tx_hash} was not mined within {timeout:.0f}s")
            time.sleep(0.05)

    def revert_reason(self, tx_hash: str) -> tuple[str, dict] | None:
        """The custom error a mined, failed transaction reverted with: from the node's tracer,
        or (no trace, e.g. history loaded from a state dump) by replaying the call on the
        current state, where a race loser's parent is still stale."""
        try:
            trace = self.rpc("debug_traceTransaction", tx_hash, {"tracer": "callTracer"})
            out = trace.get("output")
            if out and len(out) >= 10:
                return self.abi.decode_error(bytes.fromhex(out[2:]))
        except (RpcError, ChainError):
            pass
        tx = self._tx(tx_hash)
        if not tx or not tx.get("to"):
            return None
        try:
            self.rpc("eth_call", {"from": tx["from"], "to": tx["to"], "data": tx["input"]}, "latest")
        except RpcError as e:
            data = e.revert_data()
            return self.abi.decode_error(data) if data else None
        return None

    def _mined_revert(self, tx_hash: str, context: str, fmt: str) -> ChainError:
        reason = self.revert_reason(tx_hash)
        if reason:
            e = self._error(*reason, context, fmt)
        else:
            e = ChainError(f"{context}: transaction reverted")
        e.tx_hash = tx_hash
        e.args = (f"{e.args[0]} (tx {tx_hash})",)
        return e

    # ---------- reads

    def block_number(self) -> int:
        return int(self.rpc("eth_blockNumber"), 16)

    def is_registered(self, repo_id: str) -> bool:
        return bool(self._call("isRegistered", _b32(repo_id))[0])

    def get_repository(self, repo_id: str, events: bool = True) -> dict | None:
        try:
            rid = _b32(repo_id)
        except (ChainError, ValueError):
            return None
        if not self._call("isRegistered", rid)[0]:
            return None
        repo, count = self._call("getRepository", rid)
        fmt = FORMAT_NAMES.get(repo["objectFormat"], "sha1")
        out = {
            "repo_id": repo_id.lower(),
            "name": repo["name"],
            "policy_hash": _0x(repo["policyHash"]),
            "attestors": [{"key_id": a["keyId"], "scheme": "eip712-secp256k1", "address": a["signer"]}
                          for a in self._call("getAttestors", rid)[0]],
            "git_object_format": fmt,
            "genesis_git_oid": oid_from_bytes32(repo["genesisGitOid"], fmt),
            "canonical_head": oid_from_bytes32(repo["head"], fmt),
            "accepted_transition_count": count,
            "registered_block": repo["registeredBlock"],
            "registered_at": repo["registeredAt"],
            "head_block": repo["headBlock"],
            "creator": repo["creator"],
            "snapshot": ({"cid": repo["snapshotCid"], "head": oid_from_bytes32(repo["snapshotHead"], fmt)}
                         if repo["snapshotCid"] else None),
        }
        if events:
            out["events"] = self.transitions(repo_id, fmt=fmt, count=count)
        return out

    def get_head(self, repo_id: str) -> str:
        repo = self.get_repository(repo_id, events=False)
        if not repo:
            raise ChainError(f"repository {repo_id[:18]}… is not registered on the chain")
        return repo["canonical_head"]

    def list_repositories(self) -> dict:
        n = self._call("repositoryCount")[0]
        ids = self._call("repositoryIds", 0, n)[0] if n else []
        out = {}
        for rid in ids:
            repo = self.get_repository(_0x(rid))
            if repo:
                out[repo["repo_id"]] = repo
        return out

    def _logs(self, event: str, *topics) -> list[dict]:
        flt = {"address": self.contract, "fromBlock": "0x0", "toBlock": "latest",
               "topics": [_0x(self.abi.topic(event)), *[_0x(t) if isinstance(t, bytes) else t for t in topics]]}
        return self.rpc("eth_getLogs", flt)

    def transitions(self, repo_id: str, fmt: str | None = None, count: int | None = None) -> list[dict]:
        """Every accepted transition, oldest first, from contract storage (tx hashes from the logs)."""
        rid = _b32(repo_id)
        if fmt is None or count is None:
            repo, count = self._call("getRepository", rid)
            fmt = FORMAT_NAMES.get(repo["objectFormat"], "sha1")
        rows = self._call("getTransitions", rid, 0, count)[0] if count else []
        txs = {}
        for log in self._logs("TransitionAccepted", rid):
            _, ev = self.abi.decode_log(log)
            txs[ev["seq"]] = (log["transactionHash"], ev["bundleCid"])
        out = []
        for i, t in enumerate(rows, start=1):
            tx_hash, cid = txs.get(i, (None, None))
            out.append({
                "seq": i,
                "block": t["blockNumber"],
                "timestamp": t["timestamp"],
                "repo_id": repo_id.lower(),
                "parent_git_oid": oid_from_bytes32(t["parentGitOid"], fmt),
                "candidate_git_oid": oid_from_bytes32(t["candidateGitOid"], fmt),
                "attestor": t["attestor"],
                "submitter": t["submitter"],
                "attestation_digest": _0x(t["attestationDigest"]),
                "bundle_cid": cid or None,
                "tx_hash": tx_hash,
            })
        return out

    def canonical_index(self, repo_id: str, git_oid: str) -> int:
        """Position of a commit in the canonical history (genesis = 1); 0 = never canonical."""
        return self._call("canonicalIndex", _b32(repo_id), oid_to_bytes32(git_oid))[0]

    def signed_transitions(self, repo_id: str) -> list[dict | None]:
        """The signed attestation behind each accepted transition, rebuilt from the
        transaction's calldata: the chain itself is a complete, verifiable record of who
        approved what. None for a transition not sent as a direct contract call."""
        repo = self.get_repository(repo_id)
        if not repo:
            raise ChainError(f"repository {repo_id[:18]}… is not registered on the chain")
        out = []
        for ev in repo["events"]:
            out.append(self._attestation_from_tx(ev, repo["git_object_format"]) if ev["tx_hash"] else None)
        return out

    def _tx(self, tx_hash: str) -> dict:
        cache = self._s["tx"]
        if tx_hash not in cache:
            cache[tx_hash] = self.rpc("eth_getTransactionByHash", tx_hash)
        return cache[tx_hash]

    def _attestation_from_tx(self, ev: dict, fmt: str) -> dict | None:
        tx = self._tx(ev["tx_hash"])
        decoded = self.abi.decode_input(bytes.fromhex(tx["input"][2:])) if tx else None
        if not decoded or decoded[0] != "acceptTransition":
            return None
        a, sig = decoded[1]["att"], decoded[1]["signature"]
        att = {
            "repo_id": _0x(a["repoId"]),
            "chain_id": self.chain_id,
            "verifying_contract": self.contract,
            "policy_hash": _0x(a["policyHash"]),
            "parent_git_oid": oid_from_bytes32(a["parentGitOid"], fmt),
            "candidate_git_oid": oid_from_bytes32(a["candidateGitOid"], fmt),
            "bundle_sha256": "sha256:" + bytes(a["bundleSha256"]).hex(),
            "bundle_cid": a["bundleCid"] or None,
            "provider": a["provider"],
            "model": a["model"],
            "model_version": a["modelVersion"],
            "approved": a["approved"],
            "request_hash": _0x(a["requestHash"]),
            "review_input_hash": _0x(a["reviewInputHash"]),
            "response_hash": _0x(a["responseHash"]),
            "issued_at": a["issuedAt"],
            "expires_at": a["expiresAt"],
            "attestor_key_id": a["attestorKeyId"],
        }
        return {"attestation": att,
                "signature": {"scheme": "eip712-secp256k1", "signer": ev["attestor"], "value": _0x(sig)},
                "tx_hash": ev["tx_hash"], "block": ev["block"]}

    # ---------- writes

    def register_repository(self, *, repo_id: str, name: str, policy: dict, genesis_git_oid: str,
                            object_format: str) -> dict:
        if object_format not in FORMATS:
            raise ChainError(f"unsupported git object format {object_format!r}")
        attestors = [(a["key_id"], to_checksum_address(a["address"])) for a in policy.get("attestors", [])]
        data = self.abi.encode_call("registerRepository", _b32(repo_id), name, _b32(policy_hash(policy)),
                                    oid_to_bytes32(genesis_git_oid), FORMATS[object_format], attestors)
        receipt = self._send(data, "registerRepository", object_format)
        repo = self.get_repository(repo_id)
        return {**repo, "tx_hash": receipt["transactionHash"], "gas_used": int(receipt["gasUsed"], 16)}

    def _transition_call(self, response: dict) -> tuple[bytes, dict, str]:
        att, sig = response["attestation"], response["signature"]
        if att.get("chain_id") != self.chain_id or (att.get("verifying_contract") or "").lower() != self.contract.lower():
            raise ChainError("transition rejected by contract: attestation is for a different chain/contract "
                             f"(domain mismatch: signed for {att.get('chain_id')} / {att.get('verifying_contract')}, "
                             f"this chain is {self.chain_id} / {self.contract})")
        msg = typed_data(att)["message"]
        values = tuple(msg[name] for name, _, _ in ATTESTATION_FIELDS)
        try:
            sig_bytes = bytes.fromhex((sig.get("value") or "")[2:])
        except ValueError:
            raise ChainError("transition rejected by contract: attestation signature is malformed") from None
        fmt, _ = unqualify_oid(att["parent_git_oid"])
        return self.abi.encode_call("acceptTransition", values, sig_bytes), att, fmt

    def accept_transition(self, response: dict, now: int | None = None) -> dict:
        """Mirror of MockChain.accept_transition. `now` is ignored: the chain uses block time."""
        data, att, fmt = self._transition_call(response)
        receipt = self._send(data, "acceptTransition", fmt)
        return self._event_from_receipt(receipt, att)

    def send_transition(self, response: dict, simulate: bool = False) -> str:
        """Send an acceptTransition without waiting (race demos: several in one block)."""
        data, _, fmt = self._transition_call(response)
        return self._send(data, "acceptTransition", fmt, wait=False, simulate=simulate)["tx_hash"]

    def transition_result(self, tx_hash: str, response: dict) -> dict:
        """Outcome of a transaction sent with `send_transition`: the event, or raises."""
        att = response["attestation"]
        fmt, _ = unqualify_oid(att["parent_git_oid"])
        receipt = self.wait(tx_hash)
        if int(receipt["status"], 16) != 1:
            raise self._mined_revert(tx_hash, "acceptTransition", fmt)
        return self._event_from_receipt(receipt, att)

    def _event_from_receipt(self, receipt: dict, att: dict) -> dict:
        fmt, _ = unqualify_oid(att["parent_git_oid"])
        for log in receipt["logs"]:
            if log["address"].lower() != self.contract.lower():
                continue
            decoded = self.abi.decode_log(log)
            if decoded and decoded[0] == "TransitionAccepted":
                ev = decoded[1]
                block = int(receipt["blockNumber"], 16)
                return {
                    "block": block,
                    "seq": ev["seq"],
                    "repo_id": _0x(ev["repoId"]),
                    "parent_git_oid": oid_from_bytes32(ev["parentGitOid"], fmt),
                    "candidate_git_oid": oid_from_bytes32(ev["candidateGitOid"], fmt),
                    "attestor": ev["attestor"],
                    "submitter": ev["submitter"],
                    "attestation_hash": sha256_0x(canonical_json(att)),
                    "attestation_digest": _0x(ev["attestationDigest"]),
                    "bundle_cid": ev["bundleCid"] or None,
                    "timestamp": int(self._block(block)["timestamp"], 16),
                    "tx_hash": receipt["transactionHash"],
                    "gas_used": int(receipt["gasUsed"], 16),
                    "chain": "evm",
                }
        raise ChainError("transaction succeeded but emitted no TransitionAccepted event")

    def publish_snapshot(self, repo_id: str, head: str, cid: str) -> dict:
        fmt, _ = unqualify_oid(head)
        data = self.abi.encode_call("publishSnapshot", _b32(repo_id), oid_to_bytes32(head), cid)
        receipt = self._send(data, "publishSnapshot", fmt)
        return {"tx_hash": receipt["transactionHash"], "block": int(receipt["blockNumber"], 16), "cid": cid,
                "head": head, "gas_used": int(receipt["gasUsed"], 16)}

    def snapshots(self, repo_id: str) -> list[dict]:
        repo = self.get_repository(repo_id, events=False)
        fmt = repo["git_object_format"] if repo else "sha1"
        out = []
        for log in self._logs("SnapshotPublished", _b32(repo_id)):
            _, ev = self.abi.decode_log(log)
            out.append({"head": oid_from_bytes32(ev["head"], fmt), "cid": ev["cid"], "publisher": ev["publisher"],
                        "block": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"]})
        return out

    # ---------- dev node controls (anvil)

    def set_automine(self, on: bool) -> None:
        self.rpc("evm_setAutomine", bool(on))

    def mine(self) -> None:
        self.rpc("evm_mine")

    # ---------- explorer

    def _block(self, n: int) -> dict:
        cache = self._s["blocks"]
        if n not in cache:
            b = self.rpc("eth_getBlockByNumber", _hex(n), True)
            if n < self.block_number():  # only cache sealed history
                cache[n] = b
            return b
        return cache[n]

    def describe(self) -> dict:
        """Network info for the GUI / CLI."""
        out = {"kind": self.kind, "shared": True, "rpc_url": self.rpc_url, "contract": self.contract,
               "account": self.account.address, "confirmations": self.confirmations, "up": False}
        try:
            out["chain_id"] = self.chain_id
            out["block"] = self.block_number()
            out["balance_wei"] = int(self.rpc("eth_getBalance", self.account.address, "latest"), 16)
            out["client"] = self.rpc("web3_clientVersion")
            code = self.rpc("eth_getCode", self.contract, "latest")
            out["deployed"] = code not in ("0x", "0x0", None)
            out["code_matches_source"] = code.lower() == load_artifact()["deployed_bytecode"].lower()
            out["up"] = True
            if out["deployed"]:
                out["repositories"] = self._call("repositoryCount")[0]
        except (ChainError, RpcError) as e:
            out["error"] = str(e)
        return out

    def _label(self, addr: str | None) -> str | None:
        if not addr:
            return None
        a = addr.lower()
        if a in self._s["labels"]:
            return self._s["labels"][a]
        if a == self.contract.lower():
            return "DOSR contract"
        i = dev_address_book().get(a)
        return {0: "deployer (dev #0)", 1: "dev attestor (#1)"}.get(i, f"dev account #{i}" if i is not None else None)

    def activity(self, limit: int = 40, scan_blocks: int = 400) -> dict:
        """Recent transactions to (or creating) the contract, newest first, decoded:
        function, sender, status, gas, events, and the revert reason of failed ones."""
        latest = self.block_number()
        txs = []
        for n in range(latest, max(-1, latest - scan_blocks), -1):
            b = self._block(n)
            for tx in reversed(b.get("transactions") or []):
                to = (tx.get("to") or "").lower()
                if to and to != self.contract.lower():
                    continue
                receipt = self._receipt(tx["hash"])
                if not to and (receipt.get("contractAddress") or "").lower() != self.contract.lower():
                    continue
                txs.append(self._describe_tx(tx, receipt, b))
                if len(txs) >= limit:
                    return {"latest_block": latest, "transactions": txs}
        return {"latest_block": latest, "transactions": txs}

    def _receipt(self, tx_hash: str) -> dict:
        key = "r:" + tx_hash
        cache = self._s["tx"]
        if key not in cache:
            cache[key] = self.rpc("eth_getTransactionReceipt", tx_hash)
        return cache[key]

    def _describe_tx(self, tx: dict, receipt: dict, block: dict) -> dict:
        ok = int(receipt["status"], 16) == 1
        row = {"hash": tx["hash"], "block": int(tx["blockNumber"], 16), "timestamp": int(block["timestamp"], 16),
               "from": tx["from"], "from_label": self._label(tx["from"]), "status": "success" if ok else "reverted",
               "gas_used": int(receipt["gasUsed"], 16), "function": None, "summary": "", "args": {}, "events": [],
               "error": None}
        if not tx.get("to"):
            row["function"], row["summary"] = "deploy", f"deployed DOSRRegistry at {receipt.get('contractAddress')}"
            return row
        decoded = self.abi.decode_input(bytes.fromhex(tx["input"][2:]))
        if decoded:
            name, args = decoded
            row["function"] = name
            row["summary"], row["args"] = self._summarize(name, args)
        for log in receipt.get("logs", []):
            d = self.abi.decode_log(log) if log["address"].lower() == self.contract.lower() else None
            if d:
                row["events"].append({"name": d[0], **{k: (_0x(v) if isinstance(v, bytes) else v)
                                                       for k, v in d[1].items()}})
        if not ok:
            reason = self.revert_reason(tx["hash"])
            if reason:
                fmt = row["args"].get("_fmt", "sha1")
                row["error"] = {"name": reason[0], "message": str(self._error(*reason, row["function"] or "call", fmt)),
                                "args": {k: (_0x(v) if isinstance(v, bytes) else v) for k, v in reason[1].items()}}
        row["args"].pop("_fmt", None)
        return row

    def _summarize(self, name: str, args: dict) -> tuple[str, dict]:
        if name == "registerRepository":
            fmt = FORMAT_NAMES.get(args["objectFormat"], "sha1")
            return (f"register {args['name']!r} (genesis {oid_from_bytes32(args['genesisGitOid'], fmt)[:17]}…)",
                    {"repo_id": _0x(args["repoId"]), "name": args["name"], "policy_hash": _0x(args["policyHash"]),
                     "genesis": oid_from_bytes32(args["genesisGitOid"], fmt),
                     "attestors": [a["keyId"] for a in args["attestors"]], "_fmt": fmt})
        if name == "acceptTransition":
            a = args["att"]
            fmt = "sha1" if bytes(a["parentGitOid"])[20:] == b"\0" * 12 else "sha256"
            parent, cand = oid_from_bytes32(a["parentGitOid"], fmt), oid_from_bytes32(a["candidateGitOid"], fmt)
            return (f"transition {parent[5:15]} → {cand[5:15]} · {a['provider']}/{a['model']}@{a['modelVersion']}",
                    {"repo_id": _0x(a["repoId"]), "parent": parent, "candidate": cand, "attestor_key_id": a["attestorKeyId"],
                     "model": f"{a['provider']}/{a['model']}@{a['modelVersion']}", "approved": a["approved"],
                     "expires_at": a["expiresAt"], "_fmt": fmt})
        if name == "publishSnapshot":
            fmt = "sha1" if bytes(args["head"])[20:] == b"\0" * 12 else "sha256"
            head = oid_from_bytes32(args["head"], fmt)
            return (f"snapshot {head[5:15]} → {args['cid'][:20]}…",
                    {"repo_id": _0x(args["repoId"]), "head": head, "cid": args["cid"], "_fmt": fmt})
        return name, {}


# ---------------------------------------------------------------- deployment


def deploy(rpc_url: str | None = None, private_key: str | None = None) -> dict:
    """Deploy a fresh DOSRRegistry; returns {contract, tx_hash, block}."""
    chain = EvmChain(rpc_url, private_key=private_key)
    receipt = chain._send(bytes.fromhex(load_artifact()["bytecode"][2:]), "deploy", to=None)
    return {"contract": to_checksum_address(receipt["contractAddress"]), "tx_hash": receipt["transactionHash"],
            "block": int(receipt["blockNumber"], 16), "chain_id": chain.chain_id, "rpc_url": chain.rpc_url,
            "deployer": chain.account.address}


def has_contract(rpc_url: str, contract: str) -> bool:
    try:
        code = Rpc(rpc_url, timeout=3)("eth_getCode", to_checksum_address(contract), "latest")
    except (ChainError, RpcError):
        return False
    return code not in ("0x", "0x0", None)


def ensure_deployed(rpc_url: str | None = None, contract: str | None = None, private_key: str | None = None) -> dict:
    """The contract at `contract` (default: the dev address) if it has code, else a new deployment."""
    rpc_url = rpc_url or DEFAULT_RPC_URL
    contract = contract or DEV_CONTRACT_ADDRESS
    if has_contract(rpc_url, contract):
        return {"contract": to_checksum_address(contract), "deployed": False, "rpc_url": rpc_url}
    return {**deploy(rpc_url, private_key), "deployed": True}
