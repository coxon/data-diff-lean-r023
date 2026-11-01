#!/usr/bin/env python3
"""Skill entrypoint: run bundled tools/db_to_r023.py (rust-lean-r0.23)."""
from __future__ import annotations
import os, runpy, sys
from pathlib import Path

def resolve_root() -> Path:
    skill = Path(__file__).resolve().parents[1]
    if (skill / "methods" / "lean_rs.py").is_file() and (skill / "tools" / "db_to_r023.py").is_file():
        return skill
    env = os.environ.get("DATADIFF_ROOT")
    for c in ([Path(env).expanduser()] if env else []) + [Path.home() / "datadiff"]:
        if (c / "methods" / "lean_rs.py").is_file() and (c / "tools" / "db_to_r023.py").is_file():
            return c.resolve()
    print("ERROR: lean_rs skill root not found", file=sys.stderr)
    raise SystemExit(2)

def main() -> int:
    root = resolve_root()
    os.environ["DATADIFF_ROOT"] = str(root)
    if not os.environ.get("LEAN_RS_BIN") and (root / "bin" / "lean_rs").is_file():
        os.environ["LEAN_RS_BIN"] = str(root / "bin" / "lean_rs")
    tool = root / "tools" / "db_to_r023.py"
    sys.argv = [str(tool), *sys.argv[1:]]
    sys.path.insert(0, str(root / "tools"))
    sys.path.insert(0, str(root))
    runpy.run_path(str(tool), run_name="__main__")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
