"""Git bundle creation for a parent -> candidate transition.

Modes:
- "full": every object reachable from the candidate. Self-contained, so a
  reviewer with no copy of the repository can open it. Default for v0.1 because
  the attestor has no repository cache yet.
- "thin": only objects in parent..candidate. Smaller, but the receiver must
  already have the parent commit.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

from dosr_protocol.canonical import sha256_file_qualified

from .repo import GitRepo

BUNDLE_REF = "refs/dosr/candidate"


@dataclass
class BundleInfo:
    path: str
    sha256: str
    size_bytes: int
    mode: str
    head_ref: str
    head_oid: str
    prerequisites: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def create_bundle(repo: GitRepo, candidate: str, out_path, parent: str | None = None,
                  mode: str = "full") -> BundleInfo:
    if mode not in ("full", "thin"):
        raise ValueError("bundle mode must be 'full' or 'thin'")
    if mode == "thin" and not parent:
        raise ValueError("thin bundles need a parent")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    repo.update_ref(BUNDLE_REF, candidate)
    try:
        rev = f"{parent}..{BUNDLE_REF}" if mode == "thin" else BUNDLE_REF
        repo.run("bundle", "create", "-q", str(out_path), rev)
        repo.run("bundle", "verify", "-q", str(out_path))
        heads = repo.run("bundle", "list-heads", str(out_path)).split()
    finally:
        repo.delete_ref(BUNDLE_REF)

    head_oid = heads[0] if heads else ""
    if head_oid != candidate:
        raise RuntimeError(f"bundle head {head_oid} does not match candidate {candidate}")

    return BundleInfo(
        path=str(out_path),
        sha256=sha256_file_qualified(out_path),
        size_bytes=out_path.stat().st_size,
        mode=mode,
        head_ref=BUNDLE_REF,
        head_oid=head_oid,
        prerequisites=[parent] if mode == "thin" else [],
    )
