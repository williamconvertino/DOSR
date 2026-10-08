"""Create demo/.venv and install the attestor + client packages into it.

    python demo/setup_env.py            # create/update the venv
    python demo/setup_env.py --recreate # delete and rebuild it

Works on Windows, macOS, and Linux. Requires Python 3.10+ and git on PATH.
"""

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / "demo" / ".venv"
EDITABLE = ["packages/protocol", "packages/git-storage", "packages/chain", "apps/client[dev]"]


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recreate", action="store_true", help="delete demo/.venv first")
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        print("Python 3.10+ is required", file=sys.stderr)
        return 1
    if shutil.which("git") is None:
        print("git must be installed and on PATH", file=sys.stderr)
        return 1

    if args.recreate and VENV.exists():
        shutil.rmtree(VENV)
    if not venv_python().exists():
        print(f"creating virtualenv at {VENV}")
        venv.create(VENV, with_pip=True)

    py = str(venv_python())
    pip = [py, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    print("installing attestor requirements...")
    subprocess.check_call([*pip, "-r", str(ROOT / "services/attestor/requirements.txt")])
    print("installing DOSR packages (editable)...")
    cmd = list(pip)
    for pkg in EDITABLE:
        cmd += ["-e", str(ROOT / pkg)]
    subprocess.check_call(cmd)

    print("\nDone. Next steps:")
    print(f"  {py} demo/demo.py attestor   # terminal 1: start the attestor")
    print(f"  {py} demo/demo.py gui        # terminal 2: start the client GUI")
    print(f"  {py} demo/demo.py run        # or: scripted end-to-end CLI demo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
