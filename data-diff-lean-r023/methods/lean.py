"""Stub only — data-diff-lean-r023 uses methods.lean_rs (rust-lean-r0.23).

Present so tools/db_to_r12.py root checks succeed when this skill is ROOT.
Do not call run() from this module.
"""


def run(*_a, **_k):
    raise RuntimeError(
        "data-diff-lean-r023 must use methods.lean_rs (rust-lean-r0.23), "
        "not methods.lean. Invoke tools/db_to_r023.py"
    )
