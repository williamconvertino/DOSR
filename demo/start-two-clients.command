#!/bin/bash
# IPFS demo on ONE computer: two clients (Laptop A + Laptop B), each with its own workspace,
# attestor and IPFS node, already peered, one browser tab each. Downloads IPFS on first use.
# (macOS; also works on Linux via `./demo/start-two-clients.command`.) Close this window (or Ctrl+C) to stop.
cd "$(dirname "$0")/.." || exit 1

fail() {
    echo
    echo "The demo failed to start. Make sure Python 3.10+ and git are installed,"
    echo "and that ports 8080-8081, 8765-8766, 8790-8791, 5001-5002 and 4001-4002 are free."
    read -r -p "Press Enter to close..."
    exit 1
}

VENV_PY="demo/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "First run: setting up demo/.venv (this takes a minute)..."
    command -v python3 >/dev/null 2>&1 || { echo "python3 not found (install from python.org or 'brew install python')"; fail; }
    python3 demo/setup_env.py || fail
fi

"$VENV_PY" demo/demo.py pair || fail
