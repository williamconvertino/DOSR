"""Model version constraints for `policy.review.approved_models[].version`.

A policy can pin an exact version or allow a range, npm/Cargo style:

    "1.7.1"            exactly 1.7.1
    "^1.7.1"           >=1.7.1 <2.0.0   (minimum version, same major)
    "~1.7.1"           >=1.7.1 <1.8.0   (same minor)
    ">=1.7.1"          minimum version, no upper bound
    ">=1.7.1 <1.9"     several comparators, all must hold
    "1.x" / "1.7.*"    wildcards
    "*"                any version

Dated model snapshots compare as dates, so a policy can require a minimum snapshot:

    ">=2024-08-06"     OpenAI-style snapshot 2024-08-06 or newer
    ">=20250929"       Anthropic-style snapshot 20250929 or newer

A review request always names one concrete version (`review.model_version`), and
that exact string is what the attestor signs. Other versions that are not dotted
numbers (e.g. Zhipu's "api-v4", "stable") can only be pinned exactly.

Shared by the client (fail fast) and the attestor (authoritative check).
"""

import re

_NUM = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CMP = re.compile(r"^(>=|<=|>|<|=)?\s*(.+)$")


class VersionError(ValueError):
    pass


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`1`, `1.7`, `1.7.1`, `v1.7.1` -> (1, 7, 1); `2024-08-06` -> (2024, 8, 6);
    `20250929` -> (20250929, 0, 0); anything else -> None."""
    m = _NUM.match(text.strip()) or _DATE.match(text.strip())
    if not m:
        return None
    return tuple(int(g) if g is not None else 0 for g in m.groups())


def is_range(spec: str) -> bool:
    s = spec.strip()
    return s == "*" or s[:1] in "^~<>=" or " " in s or bool(re.search(r"(^|\.)[x*X](\.|$)", s))


def _bounds(spec: str) -> list[tuple[str, tuple[int, int, int]]]:
    """A range as a list of (operator, version) comparators that must all hold."""
    s = spec.strip()
    if s == "*":
        return []
    out = []
    for part in s.split():
        if part[0] in "^~":
            v = parse_version(part[1:])
            if v is None:
                raise VersionError(f"cannot parse version range {spec!r}")
            given = part[1:].lstrip("v").count(".") + 1
            if part[0] == "^":
                # ^1.7.1 <2.0.0, ^0.7.1 <0.8.0, ^0.0.3 <0.0.4 (npm semantics)
                if v[0] or given == 1:
                    upper = (v[0] + 1, 0, 0)
                elif v[1] or given == 2:
                    upper = (0, v[1] + 1, 0)
                else:
                    upper = (0, 0, v[2] + 1)
            else:
                upper = (v[0] + 1, 0, 0) if given == 1 else (v[0], v[1] + 1, 0)
            out += [(">=", v), ("<", upper)]
        elif re.search(r"(^|\.)[x*X](\.|$)", part):
            nums = []
            for piece in part.lstrip("v").split("."):
                if piece in ("x", "X", "*"):
                    break
                if not piece.isdigit():
                    raise VersionError(f"cannot parse version range {spec!r}")
                nums.append(int(piece))
            if not nums:
                continue
            lower = tuple(nums + [0] * (3 - len(nums)))
            upper = list(lower)
            upper[len(nums) - 1] += 1
            out += [(">=", lower), ("<", tuple(upper))]
        else:
            op, rest = _CMP.match(part).groups()
            v = parse_version(rest)
            if v is None:
                raise VersionError(f"cannot parse version range {spec!r}")
            out.append((op or "=", v))
    return out


def _is_snapshot(text: str) -> bool:
    return bool(_DATE.match(text) or re.fullmatch(r"\d{8}", text))


def validate_spec(spec: str) -> None:
    """Raises VersionError for a malformed range. Non-numeric exact pins are fine."""
    if not isinstance(spec, str) or not spec.strip():
        raise VersionError("model version must be a non-empty string")
    if is_range(spec):
        _bounds(spec)


def version_satisfies(spec: str, version: str) -> bool:
    """Whether the concrete `version` is allowed by the policy's `spec`."""
    if not is_range(spec):
        if spec == version:
            return True
        a, b = parse_version(spec), parse_version(version)
        # "1.7" == "1.7.0", but only when both are plain dotted numbers.
        return a is not None and b is not None and a == b and not is_range(version)
    if is_range(version):
        return False  # a request must name one concrete version
    v = parse_version(version)
    if v is None:
        return False
    try:
        bounds = _bounds(spec)
    except VersionError:
        return False
    ops = {">=": v.__ge__, ">": v.__gt__, "<=": v.__le__, "<": v.__lt__, "=": v.__eq__}
    return all(ops[op](bound) for op, bound in bounds)


def lowest_version(spec: str) -> str:
    """The smallest concrete version a spec allows (what the client asks for when the
    user did not pick one). Exact pins are returned unchanged."""
    if not is_range(spec):
        return spec
    lower = [b for op, b in _bounds(spec) if op in (">=", "=")]
    strict = [b for op, b in _bounds(spec) if op == ">"]
    if lower:
        v = max(lower)
        # Keep a dated snapshot in its own spelling ("2024-08-06", not "2024.8.6").
        for part in spec.split():
            text = _CMP.match(part).group(2)
            if _is_snapshot(text) and parse_version(text) == v:
                return text
    elif strict:
        b = max(strict)
        if any(_is_snapshot(_CMP.match(p).group(2)) for p in spec.split()):
            raise VersionError(f"version range {spec!r} has no lowest snapshot; use >= instead of >")
        v = (b[0], b[1], b[2] + 1)
    else:
        v = (0, 0, 0)
    out = f"{v[0]}.{v[1]}.{v[2]}"
    if not version_satisfies(spec, out):
        raise VersionError(f"version range {spec!r} allows no version")
    return out


def find_approved_model(approved_models: list[dict], provider: str, model: str, version: str) -> dict | None:
    """The policy entry that approves `provider/model@version`, or None."""
    for m in approved_models:
        if m["provider"] == provider and m["model"] == model and version_satisfies(m["version"], version):
            return m
    return None


def explain_unapproved(approved_models: list[dict], provider: str, model: str, version: str) -> str:
    """Human-readable reason `provider/model@version` is not approved."""
    same = [m for m in approved_models if m["provider"] == provider and m["model"] == model]
    if not same:
        return f"model {provider}/{model}@{version} is not approved by the policy"
    allowed = ", ".join(repr(m["version"]) for m in same)
    return f"model {provider}/{model}@{version} is not approved by the policy (allowed versions: {allowed})"
