# rust-lean-r0.23 champion knobs

Champion: **rust-lean-r0.23** (`bin/lean_rs` + `methods/lean_rs.py`).
`db_to_r023.py` sets these before each pair run.

## Always-on (CURRENT_BEST)

```bash
export LEAN_PROBE=1
export LEAN_RAW_FIRST=1
export LEAN_SUBLEAF=1
export LEAN_THREADS=16          # → RAYON_NUM_THREADS
# S2 off (do not set LEAN_S2 / LEAN_ENABLE_S2)
# mmap off (do not set LEAN_MMAP / LEAN_USE_MMAP)
```

Binary sha256 (authoritative):
`456b7bfc5749b73845adf80b4e07825783d26ed77d6bbd475f98e369343971ab`

Bench evidence: geo wall **1.281574s**, geo tte **1.160322s**, exact **6/6**.

## Optional

| Knob | When |
|------|------|
| `LEAN_PARTITIONS=N` | Passed to binary `--partitions` (default 64) |
| `LEAN_ROWS_PER_LEAF` | Default 4096 |
| `LEAN_DUP_GUARD=0` | Disable dup guard (`--no-dup-guard`) |
| `LEAN_RS_BIN=/path` | Override binary path |

## Do not

- Use `LIMIT` / sample SQL as the “diff” (unless user scoped / labeled smoke).
- Enable S2 or mmap for champion reproduction unless a new CURRENT_BEST says so.
- Replace rust core with DuckDB `methods/lean.py` inside this skill (that is R12).

## Pipeline (rust)

1. Leaf summary over pk-range buckets (~4096).
2. Merkle / range reconciliation (fanout 16).
3. Raw-first + subleaf descent.
4. Dense path + exact classify → `missing` / `extra` / `value_diff` / `dup_pk`.

## Types

`lean_rs` expects typed Parquet columns compatible with the Spec contract
(bench schema: int/decimal/string-as-supported/timestamp/nullables). Arbitrary
Arrow `Utf8` from dirty Doris exports may error (`unsupported column …: Utf8`);
fall back to **data-diff-lean-r12** for those tables or fix staging CAST.
