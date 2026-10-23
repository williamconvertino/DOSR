#!/bin/bash
# IPFS demo, run on EACH of two computers: IPFS node + attestor + client GUI + peer inbox.
# Downloads IPFS on first use. Allow the firewall prompts (ipfs, Python), then peer the two
# computers via the P2P button (see demo/README.md, section 3).
# (macOS; also works on Linux via `./demo/start-ipfs-demo.command`.) Close this window (or Ctrl+C) to stop.
cd "$(dirname "$0")/.." || exit 1

fail() {
    echo
    echo "The demo failed to start. Make sure Python 3.10+ and git are installed,"
    echo "and that ports 8080, 8765, 8790, 5001 and 4001 are free."
    read -r -p "Press Enter to close..."
    exit 1
}

VENV_PY="demo/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "First run: setting up demo/.venv (this takes a minute)..."
    command -v python3 >/dev/null 2>&1 || { echo "python3 not found (install from python.org or 'brew install python')"; fail; }
    python3 demo/setup_env.py || fail
fi

"$VENV_PY" demo/demo.py up || fail
