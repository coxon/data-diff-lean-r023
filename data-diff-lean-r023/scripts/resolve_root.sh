#!/usr/bin/env bash
set -euo pipefail
SKILL="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$SKILL/methods/lean_rs.py" && -x "$SKILL/bin/lean_rs" && -f "$SKILL/tools/db_to_r023.py" ]]; then
  printf '%s\n' "$SKILL"; exit 0
fi
if [[ -n "${DATADIFF_ROOT:-}" && -f "${DATADIFF_ROOT}/methods/lean_rs.py" ]]; then
  printf '%s\n' "$(cd "$DATADIFF_ROOT" && pwd)"; exit 0
fi
if [[ -f "$HOME/datadiff/methods/lean_rs.py" && -x "$HOME/datadiff/bin/lean_rs" ]]; then
  printf '%s\n' "$HOME/datadiff"; exit 0
fi
echo "ERROR: rust-lean-r0.23 skill root not found (need methods/lean_rs.py + bin/lean_rs)" >&2
exit 2
