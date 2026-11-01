---
name: data-diff-lean-r023
description: >
  Compare two databases or tables for exact row diffs
  (missing/extra/value_diff/dup_pk) with the rust-lean-r0.23 Mach-O champion.
  Use when the user gives two DB connection infos
  (MySQL/MariaDB/Doris/Postgres): probe both, match table pairs, run rust lean,
  report differing tables/columns/rows. Prefer this over data-diff-lean-r12 when
  running on Apple arm64 Studio with the r0.23 binary. Do not ask the user to
  export CSV.
compatibility: >
  Requires arm64 lean_rs binary (bundled) or LEAN_RS_BIN; Python 3; DuckDB for
  staging/report; network to both DBs. Sibling skill data-diff-lean-r12 (DuckDB)
  stays installed — do not delete it.
metadata:
  author: coxon
  version: "1.0"
  champion: rust-lean-r0.23
  geo_wall_s: "1.281574"
  geo_tte_s: "1.160322"
  binary_sha256: "456b7bfc5749b73845adf80b4e07825783d26ed77d6bbd475f98e369343971ab"
---

# data-diff lean rust-lean-r0.23

Exact row-level diffs between two databases. The **diff engine is rust-lean-r0.23**
(`bin/lean_rs` + thin adapter `methods/lean_rs.py`) — Merkle / pk-range
reconciliation in Rust. This skill **wires two DBs into that champion and reports
its results**, not a different strategy (no ad-hoc `LIMIT`, no sample-and-hope,
no custom SQL diffs).

Sibling skill **[data-diff-lean-r12](sand-workflow:data-diff-lean-r12)** remains
the DuckDB R12 champion path. Do not delete or overwrite it.

## Non-negotiable: keep rust-lean core

r0.23 already handles scale by summarizing pk-range leaves (~4096 rows), Merkle
reconciling, then fetching only mismatched ranges. That logic must stay intact:

1. Typed leaf summary over pk-range buckets (`--rows-per-leaf`, default 4096).
2. Merkle / range reconciliation (fanout 16).
3. `--raw-first`: only raw-mismatched leaves re-summarized with Spec canonicalize.
4. `--subleaf`: descend to fine granules.
5. Dense path + dup guard.
6. Exact classify → `missing` / `extra` / `value_diff` / `dup_pk`.

**Do not** replace steps 1–6 with:
- `SELECT … LIMIT N` as the “diff”
- Random sampling / hash of a subset presented as equality
- Hand-rolled `EXCEPT` / `NOT EXISTS` instead of lean_rs
- Swapping in DuckDB `methods/lean.py` inside this skill’s two-DB path

`tools/db_to_r023.py` sets champion env before each pair:
`LEAN_PROBE=1 LEAN_RAW_FIRST=1 LEAN_SUBLEAF=1 LEAN_THREADS=16`
(S2 off, mmap off). Leave those as CURRENT_BEST.

Staging (`a.parquet` / `b.parquet` under `doris_runs/`) is only the **input feed**
into lean_rs. Prefer DuckDB ATTACH export; Doris dirty types may fall back to
pymysql CAST. Staging must still carry **all rows the user asked to compare**.
If disk is short: free space — **never silently truncate**.

## When to use

- User gives two DB connections and wants exact diffs on **arm64** (Studio).
- Engines: MySQL / MariaDB / **Doris** (MySQL FE, often `:9030`) / Postgres.
- Single pair (`--table-a` / `--table-b`) or discover pairs (`--all-pairs`).
- Prefer this skill when the goal is the **rust-lean-r0.23** champion (geo wall
  1.281574s / geo tte 1.160322s on locked 100M core-6). Use R12 skill when only
  DuckDB is available (e.g. non-arm64 box without the Mach-O binary).

## Do not use / out of scope

- Approximate / fuzzy matching.
- Schema migration / DDL.
- Asking the user to export CSV (staging is agent-internal).
- Echoing passwords.
- Using `LIMIT` / `WHERE` subsets **unless the user explicitly scoped** the
  comparison, or for a labeled skill self-test smoke.
- Full 100M bench as part of ordinary two-DB work.

## Install

```bash
# On Studio (arm64): skill already under ~/.codex/skills/data-diff-lean-r023/
# Or point at the datadiff checkout:
export DATADIFF_ROOT="$HOME/datadiff"
# Ensure bin/lean_rs is rust-lean-r0.23 (sha256 456b7bfc…971ab)
```

## Prerequisites

- **arm64** `bin/lean_rs` (bundled Mach-O) or `LEAN_RS_BIN` override.
- Python 3 + DuckDB (+ PyArrow; pymysql/psycopg for MySQL/Doris/Postgres) for
  staging and `pair_summary` post-process.
- Network to both DBs.
- Secrets mode `600` under `$ROOT/.secrets/`, or DSNs / `SIDEA_*`/`SIDEB_*`.
  Never print passwords.

Details: [references/engines.md](references/engines.md),
[references/r023-knobs.md](references/r023-knobs.md).

## Inputs

| Flag | Meaning |
|------|---------|
| `--env-a` / `--env-b` or `--dsn-a` / `--dsn-b` | Connections |
| `--table-a` / `--table-b` / `--tables` / `--all-pairs` | Which pairs |
| `--pk` | **Prefer integer single-column PK** |
| `--compare` / `--exclude` | Columns |
| `--max-pairs` / `--concurrency` | Caps (concurrency ≤ 4) |
| `--list-only` / `--probe` | Inventory only |
| `--sql-a` / `--sql-b` | **Only** when user scoped the set, or agent smoke |

## Steps

### 1. Secrets (no echo)

```bash
ROOT="${DATADIFF_ROOT:-$HOME/.codex/skills/data-diff-lean-r023}"
mkdir -p "$ROOT/.secrets" && chmod 600 "$ROOT/.secrets/"*.env 2>/dev/null || true
```

### 2. Probe

```bash
cd "$ROOT"
python3 tools/db_pair_probe.py --env-a .secrets/side-a.env --env-b .secrets/side-b.env
# or: ~/.codex/skills/data-diff-lean-r023/scripts/db_pair_probe.py …
```

### 3. Run rust-lean-r0.23 (champion path)

```bash
cd "$ROOT"
# Explicit pair — full tables; champion knobs set inside db_to_r023
python3 tools/db_to_r023.py \
  --env-a .secrets/side-a.env --env-b .secrets/side-b.env \
  --table-a orders --table-b orders --pk id

# Many pairs
python3 tools/db_to_r023.py \
  --env-a .secrets/side-a.env --env-b .secrets/side-b.env \
  --all-pairs --max-pairs 20 --concurrency 1
```

Doris: always pass explicit `--pk`. Prefer `--prefer-export auto`.
Do not invent LIMIT for Doris size.

### 4. Report from artifacts

Primary: `doris_runs/<id>/pair_summary.json`
(schema: [references/report-schema.md](references/report-schema.md);
run_id prefix `db_r023_…`).

Tell the user: differing tables, per-table `type_counts`, top columns, sample
keys, `lean_wall_s`, `method_revision=rust-lean-r0.23`. Deliverable is the
**diff summary**, not Parquet dumps.

## Success criteria

- Diffs come from **lean_rs / rust-lean-r0.23**, not a hand-rolled query.
- Adapter `REV` and preferably binary sha256 match CURRENT_BEST.
- Full scope the user asked for (no silent row truncation).
- Champion knobs: 16 threads, S2 off, mmap off.
- No passwords in logs/chat.
- R12 skill left intact.

## Edge cases

| Case | Action |
|------|--------|
| Cannot infer pk | Pass `--pk`; integer pk for range mode |
| Doris UNI keys | Explicit `--pk` |
| Non-arm64 host | Use R12 skill or cross-compile; this binary is Mach-O arm64 |
| Unsupported Arrow Utf8 / dirty Doris types | Prefer R12 skill, or stage with typed CAST parquet lean_rs accepts |
| Disk for staging | Free space; do not LIMIT |
| User-scoped subset | Then `--sql-a`/`--sql-b` — say so in the report |
| Agent smoke only | LIMIT / tiny parquet allowed **only** for skill self-test, labeled smoke |
| Schema-only cols | `columns_only_a` / `columns_only_b` |

## Scripts

- [scripts/db_pair_probe.py](scripts/db_pair_probe.py)
- [scripts/db_to_r023.py](scripts/db_to_r023.py) — sets r0.23 champion env, stages, calls lean_rs
- [scripts/resolve_root.sh](scripts/resolve_root.sh)

Bundles: `bin/lean_rs`, `methods/lean_rs.py`, staging tools (`db_to_r12.py` reused
internally). Optional: `DATADIFF_ROOT` / `LEAN_RS_BIN` overrides.
