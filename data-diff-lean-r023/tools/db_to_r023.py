#!/usr/bin/env python3
"""Two-DB probe → rust-lean-r0.23 champion.

Thin wrapper: reuses tools/db_to_r12.py for secrets/probe/staging/pair_summary,
but runs methods.lean_rs (Mach-O bin/lean_rs) instead of methods.lean.

Champion defaults (CURRENT_BEST): 16 threads, S2 off, mmap off.
Do NOT replace the rust core with LIMIT-as-diff.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db_to_r12 as core  # noqa: E402

CHAMPION_REV = "rust-lean-r0.23"
EXPECTED_SHA256 = (
    "456b7bfc5749b73845adf80b4e07825783d26ed77d6bbd475f98e369343971ab"
)


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_lean_rs_bin(root: Path) -> Path:
    env = os.environ.get("LEAN_RS_BIN")
    candidates = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            root / "bin" / "lean_rs",
            Path.home() / ".codex/skills/data-diff-lean-r023/bin/lean_rs",
            Path.home() / "datadiff/bin/lean_rs",
            Path.home() / "datadiff/deliver/revisions/rust-lean-r0.23-bin",
        ]
    )
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c.resolve()
    core.die(
        "lean_rs binary not found (arm64 Mach-O rust-lean-r0.23). "
        "Set LEAN_RS_BIN or install under $ROOT/bin/lean_rs"
    )
    raise AssertionError("unreachable")


def run_lean(
    a_rel: str, b_rel: str, n: int, compare: list[str], work: Path, root: Path
) -> dict[str, Any]:
    """Champion path: rust-lean-r0.23 via methods.lean_rs."""
    sys.path.insert(0, str(root))
    os.chdir(root)
    for k in (
        "LEAN_ADAPTIVE",
        "LEAN_PERCOL",
        "LEAN_PORTABLE",
        "LEAN_MMAP",
        "LEAN_S2",
        "LEAN_ENABLE_S2",
        "LEAN_USE_MMAP",
    ):
        os.environ.pop(k, None)
    os.environ["LEAN_PROBE"] = "1"
    os.environ["LEAN_RAW_FIRST"] = "1"
    os.environ["LEAN_SUBLEAF"] = "1"
    os.environ.setdefault("LEAN_DUP_GUARD", "1")
    os.environ.setdefault("LEAN_THREADS", "16")
    os.environ["RAYON_NUM_THREADS"] = os.environ["LEAN_THREADS"]

    bin_path = resolve_lean_rs_bin(root)
    os.environ["LEAN_RS_BIN"] = str(bin_path)
    digest = _sha256(bin_path)
    if digest != EXPECTED_SHA256:
        print(
            f"WARN: lean_rs sha256={digest} expected={EXPECTED_SHA256} "
            f"(continuing; verify CURRENT_BEST)",
            flush=True,
        )

    from methods.lean_rs import REV, run as lean_run

    if REV != CHAMPION_REV:
        core.die(f"methods.lean_rs REV={REV!r} expected {CHAMPION_REV!r}")

    man = {"a": a_rel, "b": b_rel, "n": n, "compare_columns": compare}
    work.mkdir(parents=True, exist_ok=True)
    (work / "manifest.json").write_text(json.dumps(man, indent=2))
    t0 = time.perf_counter()
    out = lean_run(man, work)
    out = dict(out)
    out["lean_wall_s"] = round(time.perf_counter() - t0, 3)
    out.setdefault("method_revision", REV)
    out.setdefault("engine", "rust")
    out["champion"] = CHAMPION_REV
    out["lean_rs_bin"] = str(bin_path)
    out["lean_rs_sha256"] = digest
    out["knobs"] = {
        "LEAN_PROBE": 1,
        "LEAN_RAW_FIRST": 1,
        "LEAN_SUBLEAF": 1,
        "LEAN_THREADS": int(os.environ["LEAN_THREADS"]),
        "S2": 0,
        "mmap": 0,
    }
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a == "--run-id" or a.startswith("--run-id=") for a in argv):
        argv = ["--run-id", time.strftime("db_r023_%Y%m%d%H%M%S"), *argv]

    core.run_lean = run_lean
    # Prefer skill/datadiff root that has lean_rs when --root not passed
    if not any(a == "--root" or a.startswith("--root=") for a in argv):
        skill = Path.home() / ".codex/skills/data-diff-lean-r023"
        env = os.environ.get("DATADIFF_ROOT")
        for c in ([Path(env).expanduser()] if env else []) + [
            skill,
            Path.home() / "datadiff",
        ]:
            if (c / "methods" / "lean_rs.py").is_file() and (
                (c / "bin" / "lean_rs").is_file()
                or os.environ.get("LEAN_RS_BIN")
            ):
                # ensure lean.py stub or real exists for core.main check
                if not (c / "methods" / "lean.py").is_file():
                    stub = c / "methods" / "lean.py"
                    stub.write_text(
                        'def run(*a, **k):\n'
                        '    raise RuntimeError("use lean_rs via db_to_r023")\n'
                    )
                argv = ["--root", str(c.resolve()), *argv]
                break
    return core.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
