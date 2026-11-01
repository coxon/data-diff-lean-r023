#!/usr/bin/env python3
"""Convenience entry: two DB connections → probe (+ optional --all-pairs R12).

Thin wrapper around tools/db_to_r12.py so agent recipes can say:
  python tools/db_pair_probe.py --env-a … --env-b …           # probe only
  python tools/db_pair_probe.py --env-a … --env-b … --all-pairs
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure sibling import works when invoked as a script
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db_to_r12 as core  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to probe when neither --all-pairs nor explicit table flags present
    has_action = any(
        a in argv
        for a in (
            "--all-pairs",
            "--list-only",
            "--probe",
            "--table-a",
            "--tables",
            "--export-only",
        )
    )
    if not has_action:
        argv = ["--probe", *argv]
    elif "--all-pairs" in argv and "--probe" not in argv and "--list-only" not in argv:
        # all-pairs already runs diffs; leave as-is
        pass
    return core.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
