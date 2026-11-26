"""Hardening defaults (RC-9): weak hashes are declared non-cryptographic.

SHA-1 and MD5 are used in `app/` only for cache keys and fingerprints (the
taxonomy fingerprint, the binder's state hashes, the scale cache key). Each
call carries `usedforsecurity=False`: the digest is unchanged, so stored
signatures stay valid, and the declaration is what tells a reader and the
scanners that no security property rests on the hash. The toolkit's Semgrep
rule accepts the flag; this test is the regression net that runs without
Semgrep, so the flag cannot be dropped from one site unnoticed.

Found by mutation testing (`security/mutations.yaml`,
`P9-sha1-without-usedforsecurity`): the SAST job was the only thing holding
this, and it does not run in `tests/security`.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

WEAK = {"sha1", "md5"}


def _weak_hash_calls(tree):
    """Every `hashlib.sha1(...)` / `hashlib.md5(...)` / `hashlib.new("sha1", ...)`
    call in `tree`, as (call node, usedforsecurity keyword value or None)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = None
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "hashlib":
            if fn.attr in WEAK:
                name = fn.attr
            elif fn.attr == "new" and node.args and isinstance(node.args[0], ast.Constant) \
                    and str(node.args[0].value).lower().replace("-", "") in WEAK:
                name = str(node.args[0].value)
        elif isinstance(fn, ast.Name) and fn.id in WEAK:
            name = fn.id
        if name is None:
            continue
        flag = next((kw.value for kw in node.keywords if kw.arg == "usedforsecurity"), None)
        yield node, flag


def _sites():
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node, flag in _weak_hash_calls(tree):
            yield path.relative_to(APP.parent), node.lineno, flag


def test_every_weak_hash_call_declares_usedforsecurity_false():
    sites = list(_sites())
    assert sites, "no SHA-1/MD5 call found under app/ — the walker is looking in the wrong place"

    offenders = [
        f"{path}:{line}"
        for path, line, flag in sites
        if not (isinstance(flag, ast.Constant) and flag.value is False)
    ]
    assert not offenders, (
        "weak hash used without usedforsecurity=False (a cache key or fingerprint "
        "must say so; anything else must use SHA-256): " + ", ".join(offenders)
    )


def test_the_known_fingerprint_sites_are_still_covered():
    """The four sites the RC-9 pass marked. If one moves, update this list;
    if one is deleted, the walker above still guards whatever remains."""
    covered = {str(path) for path, _line, _flag in _sites()}
    assert {"app/autofill/__init__.py", "app/autofill/jev_binder.py", "app/autofill/scale.py"} <= covered
