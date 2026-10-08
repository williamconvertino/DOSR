import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
# Import the real attestor app so these tests catch client/server wire drift.
sys.path.insert(0, str(ROOT / "services" / "attestor"))

from fastapi.testclient import TestClient  # noqa: E402

from attestor.main import app as attestor_app  # noqa: E402
from dosr_chain import MockChain  # noqa: E402
from dosr_client.attestor import AttestorClient  # noqa: E402
from dosr_client.config import ClientProfile  # noqa: E402


@pytest.fixture
def chain(tmp_path):
    return MockChain(tmp_path / "chain" / "state.json")


@pytest.fixture
def attestor():
    return AttestorClient("http://testserver", http=TestClient(attestor_app))


@pytest.fixture
def alice():
    return ClientProfile(id="alice", name="Alice Example", email="alice@dosr.demo")


@pytest.fixture
def mallory():
    return ClientProfile(
        id="mallory",
        name="Mallory",
        email="mallory@dosr.demo",
        review_model={"provider": "mock", "model": "always-approve", "version": "1"},
    )
