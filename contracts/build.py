"""Compile contracts/src/DOSRRegistry.sol into packages/chain/dosr_chain/contract/DOSRRegistry.json.

    python contracts/build.py           # download solc once (checked against its sha256), compile
    python contracts/build.py --check   # fail if the committed artifact is stale (CI / tests)

The artifact (ABI + bytecode + the sha256 of the source it came from) is committed, so the
demo and the Python adapter never need a Solidity compiler. Re-run this after editing the
contract. solc is cached in contracts/.solc/ (git-ignored).
"""

import argparse
import hashlib
import json
import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "contracts" / "src" / "DOSRRegistry.sol"
OUT = ROOT / "packages" / "chain" / "dosr_chain" / "contract" / "DOSRRegistry.json"
SOLC_DIR = ROOT / "contracts" / ".solc"
SOLC_VERSION = "0.8.38"
BINARIES = "https://binaries.soliditylang.org"


def _platform() -> str:
    if sys.platform == "win32":
        return "windows-amd64"
    if sys.platform == "darwin":
        return "macosx-amd64"  # universal binary on recent releases
    if platform.machine().lower() in ("aarch64", "arm64"):
        raise SystemExit("no official solc binary for linux-arm64; install solc 0.8.38 and pass --solc")
    return "linux-amd64"


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dosr-contracts-build"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def solc_path() -> Path:
    plat = _platform()
    exe = SOLC_DIR / (f"solc-{SOLC_VERSION}.exe" if plat.startswith("windows") else f"solc-{SOLC_VERSION}")
    if exe.exists():
        return exe
    listing = json.loads(_get(f"{BINARIES}/{plat}/list.json", 60))
    build = next((b for b in listing["builds"] if b["version"] == SOLC_VERSION), None)
    if not build:
        raise SystemExit(f"solc {SOLC_VERSION} not found for {plat}")
    url = f"{BINARIES}/{plat}/{build['path']}"
    print(f"downloading {url}")
    data = _get(url, 300)
    if "0x" + hashlib.sha256(data).hexdigest() != build["sha256"]:
        raise SystemExit("downloaded solc does not match its published sha256; not using it")
    SOLC_DIR.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(data)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def source_sha256() -> str:
    # Normalize line endings so a Windows checkout (CRLF) builds the same artifact.
    return hashlib.sha256(SRC.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def compile_contract(solc: Path) -> dict:
    std_in = {
        "language": "Solidity",
        "sources": {"DOSRRegistry.sol": {"content": SRC.read_text("utf-8").replace("\r\n", "\n")}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": True,
            "evmVersion": "cancun",
            "outputSelection": {"*": {"DOSRRegistry": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"]}},
        },
    }
    proc = subprocess.run([str(solc), "--standard-json"], input=json.dumps(std_in), capture_output=True,
                          text=True, check=True)
    out = json.loads(proc.stdout)
    errors = [e for e in out.get("errors", []) if e["severity"] == "error"]
    for e in out.get("errors", []):
        print(e["formattedMessage"], file=sys.stderr)
    if errors:
        raise SystemExit("compilation failed")
    c = out["contracts"]["DOSRRegistry.sol"]["DOSRRegistry"]
    return {
        "contract": "DOSRRegistry",
        "solc": SOLC_VERSION,
        "settings": {k: std_in["settings"][k] for k in ("optimizer", "viaIR", "evmVersion")},
        "source": "contracts/src/DOSRRegistry.sol",
        "source_sha256": source_sha256(),
        "abi": c["abi"],
        "bytecode": "0x" + c["evm"]["bytecode"]["object"],
        "deployed_bytecode": "0x" + c["evm"]["deployedBytecode"]["object"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only check that the artifact matches the source")
    ap.add_argument("--solc", help="use this solc binary instead of downloading one")
    args = ap.parse_args()
    if args.check:
        current = json.loads(OUT.read_text("utf-8")).get("source_sha256") if OUT.exists() else None
        if current != source_sha256():
            print(f"{OUT.relative_to(ROOT)} is stale: run python contracts/build.py", file=sys.stderr)
            return 1
        print("artifact is up to date")
        return 0
    artifact = compile_contract(Path(args.solc) if args.solc else solc_path())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    size = (len(artifact["deployed_bytecode"]) - 2) // 2
    print(f"wrote {OUT.relative_to(ROOT)} ({size:,} bytes deployed, limit 24,576)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
