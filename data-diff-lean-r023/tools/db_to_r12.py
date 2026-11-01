#!/usr/bin/env python3
"""Two-DB probe → R12 lean champion.

User gives connection info for side A and side B. Agent probes both
(information_schema), matches candidate table pairs (name + schema
similarity), stages internally, runs R12 lean, and reports differing
tables / columns / rows.

Do NOT ask the user to export. Staging under doris_runs/ is internal only.
Secrets: never print passwords; prefer ROOT/.secrets/*.env mode 600.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote

ROOT = Path(os.environ.get("DATADIFF_ROOT", Path.home() / "datadiff")).resolve()
NOISE = re.compile(
    r"^(etl_time|etl_date|_etl_|dw_|ods_|binlog_|_load_time|load_time|"
    r"insert_time|datax_|sync_time|op_ts)$",
    re.I,
)
INT_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
    "int2", "int4", "int8", "serial", "bigserial",
}
# name-suffix heuristics for A/B pairing when names differ
SUFFIX_PAIRS = (
    ("_a", "_b"),
    ("_src", "_dst"),
    ("_source", "_target"),
    ("_old", "_new"),
    ("_left", "_right"),
    ("_prod", "_stg"),
    ("_prod", "_stage"),
)


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_dsn(dsn: str) -> dict[str, str]:
    """mysql://user:pass@host:port/db  or  postgres://..."""
    u = urlparse(dsn)
    scheme = (u.scheme or "mysql").lower()
    engine = "postgres" if scheme.startswith("postgres") else "mysql"
    if scheme in ("doris", "jdbc"):
        engine = "mysql"
    return {
        "engine": engine,
        "host": u.hostname or "",
        "port": str(u.port or (5432 if engine == "postgres" else 3306)),
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": (u.path or "/").lstrip("/").split("?")[0],
    }


def side_cfg(prefix: str, env_file: Path | None, dsn: str | None) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if env_file:
        raw = load_env_file(env_file)
        aliases = [
            ("HOST", ["HOST", "DORIS_HOST", "MYSQL_HOST", "PGHOST", f"{prefix}_HOST"]),
            ("PORT", ["PORT", "DORIS_PORT", "MYSQL_PORT", "PGPORT", f"{prefix}_PORT"]),
            ("USER", ["USER", "DORIS_USER", "MYSQL_USER", "PGUSER", f"{prefix}_USER"]),
            ("PASSWORD", ["PASSWORD", "DORIS_PASSWORD", "MYSQL_PASSWORD", "PGPASSWORD", f"{prefix}_PASSWORD"]),
            ("DATABASE", ["DATABASE", "DB", "DORIS_DB", "MYSQL_DATABASE", "PGDATABASE", f"{prefix}_DB"]),
            ("ENGINE", ["ENGINE", "DORIS_ENGINE", f"{prefix}_ENGINE"]),
        ]
        for dest, keys in aliases:
            for k in keys:
                if k in raw and raw[k]:
                    cfg[dest] = raw[k]
                    break
        if "ENGINE" not in cfg:
            if any(k.startswith("DORIS_") for k in raw):
                cfg["ENGINE"] = "mysql"
    if dsn:
        p = parse_dsn(dsn)
        cfg = {
            "ENGINE": p["engine"],
            "HOST": p["host"],
            "PORT": p["port"],
            "USER": p["user"],
            "PASSWORD": p["password"],
            "DATABASE": p["database"],
        }
    for key in ("ENGINE", "HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
        for envk in (f"{prefix}_{key}", f"{prefix}_{key.replace('DATABASE', 'DB')}"):
            if os.environ.get(envk):
                cfg[key if not envk.endswith("_DB") else "DATABASE"] = os.environ[envk]
    if not cfg.get("HOST") or not cfg.get("DATABASE"):
        die(f"{prefix}: need HOST+DATABASE (env file, DSN, or {prefix}_* env)")
    cfg.setdefault("ENGINE", "mysql")
    cfg.setdefault("PORT", "5432" if cfg["ENGINE"] == "postgres" else "3306")
    cfg.setdefault("USER", "")
    cfg.setdefault("PASSWORD", "")
    return cfg


def redact(cfg: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if k == "PASSWORD" else v) for k, v in cfg.items()}


def connect_pymysql(cfg: dict[str, str]):
    import pymysql
    return pymysql.connect(
        host=cfg["HOST"], port=int(cfg["PORT"]), user=cfg["USER"],
        password=cfg["PASSWORD"], database=cfg["DATABASE"],
        connect_timeout=30, read_timeout=3600, charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
    )


def connect_psycopg(cfg: dict[str, str]):
    import psycopg2
    return psycopg2.connect(
        host=cfg["HOST"], port=int(cfg["PORT"]), user=cfg["USER"],
        password=cfg["PASSWORD"], dbname=cfg["DATABASE"],
        connect_timeout=30,
    )


def list_tables_mysql(cfg: dict[str, str]) -> list[str]:
    conn = connect_pymysql(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY 1",
            (cfg["DATABASE"],),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def list_tables_pg(cfg: dict[str, str]) -> list[str]:
    conn = connect_psycopg(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def columns_mysql(cfg: dict[str, str], table: str) -> list[dict[str, Any]]:
    conn = connect_pymysql(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_KEY, EXTRA "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "ORDER BY ORDINAL_POSITION",
            (cfg["DATABASE"], table),
        )
        return [{"name": r[0], "type": r[1], "key": r[2], "extra": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()


def columns_pg(cfg: dict[str, str], table: str) -> list[dict[str, Any]]:
    conn = connect_psycopg(cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.column_name, c.data_type,
                   CASE WHEN tc.constraint_type='PRIMARY KEY' THEN 'PRI' ELSE '' END
            FROM information_schema.columns c
            LEFT JOIN information_schema.key_column_usage kcu
              ON c.table_schema=kcu.table_schema AND c.table_name=kcu.table_name
             AND c.column_name=kcu.column_name
            LEFT JOIN information_schema.table_constraints tc
              ON kcu.constraint_name=tc.constraint_name AND tc.constraint_type='PRIMARY KEY'
            WHERE c.table_schema='public' AND c.table_name=%s
            ORDER BY c.ordinal_position
            """,
            (table,),
        )
        return [{"name": r[0], "type": r[1], "key": r[2] or "", "extra": ""} for r in cur.fetchall()]
    finally:
        conn.close()


def list_tables(cfg: dict[str, str]) -> list[str]:
    return list_tables_pg(cfg) if cfg["ENGINE"] == "postgres" else list_tables_mysql(cfg)


def columns_of(cfg: dict[str, str], table: str) -> list[dict[str, Any]]:
    return columns_pg(cfg, table) if cfg["ENGINE"] == "postgres" else columns_mysql(cfg, table)


def infer_pk(cols: list[dict[str, Any]], hint: str | None = None) -> str:
    names = {c["name"]: c for c in cols}
    if hint:
        if hint not in names:
            die(f"pk hint {hint!r} not in columns: {sorted(names)}")
        return hint
    pri = [c for c in cols if c.get("key") == "PRI"]
    if len(pri) == 1:
        return pri[0]["name"]
    if len(pri) > 1:
        for c in pri:
            if c["type"].lower().split("(")[0] in INT_TYPES:
                print(f"WARN: composite PRIMARY KEY; using integer part {c['name']}", file=sys.stderr)
                return c["name"]
        print(f"WARN: composite PRIMARY KEY; using {pri[0]['name']}", file=sys.stderr)
        return pri[0]["name"]
    for cand in ("id", "pk", "uid"):
        if cand in names and names[cand]["type"].lower().split("(")[0] in INT_TYPES:
            return cand
    idish = [
        c for c in cols
        if c["name"].endswith("_id") and c["type"].lower().split("(")[0] in INT_TYPES
    ]
    if len(idish) == 1:
        return idish[0]["name"]
    raise ValueError("cannot infer pk; pass --pk. columns=" + ",".join(names))


def try_infer_pk(cols: list[dict[str, Any]], hint: str | None = None) -> str | None:
    try:
        return infer_pk(cols, hint)
    except (ValueError, SystemExit):
        return None


def pick_compare(
    cols: list[dict[str, Any]],
    pk: str,
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[str]:
    names = [c["name"] for c in cols if c["name"] != pk]
    if include:
        missing = [c for c in include if c not in names and c != pk]
        if missing:
            die(f"compare columns missing: {missing}")
        return [c for c in include if c != pk]
    excl = set(exclude or [])
    out = []
    for n in names:
        if n in excl:
            continue
        if NOISE.match(n) or n.lower().startswith("etl_"):
            continue
        out.append(n)
    if not out:
        die("no compare columns left after noise filter")
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    if not u:
        return 0.0
    return len(a & b) / len(u)


def suffix_partner(name: str) -> str | None:
    for sa, sb in SUFFIX_PAIRS:
        if name.endswith(sa):
            return name[: -len(sa)] + sb
        if name.endswith(sb):
            return name[: -len(sb)] + sa
    return None


def normalize_base(name: str) -> str:
    for sa, sb in SUFFIX_PAIRS:
        if name.endswith(sa):
            return name[: -len(sa)]
        if name.endswith(sb):
            return name[: -len(sb)]
    return name


def build_candidate_pairs(
    tables_a: list[str],
    tables_b: list[str],
    meta_a: dict[str, list[dict[str, Any]]] | None = None,
    meta_b: dict[str, list[dict[str, Any]]] | None = None,
    min_jaccard: float = 0.5,
) -> list[dict[str, Any]]:
    """Match A/B tables by exact name, suffix heuristics, then schema similarity."""
    set_b = set(tables_b)
    used_b: set[str] = set()
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(ta: str, tb: str, match: str, score: float, shared: list[str] | None = None) -> None:
        key = (ta, tb)
        if key in seen:
            return
        seen.add(key)
        used_b.add(tb)
        entry: dict[str, Any] = {
            "table_a": ta,
            "table_b": tb,
            "match": match,
            "col_jaccard": round(score, 4),
        }
        if shared is not None:
            entry["shared_cols"] = shared
        if meta_a and ta in meta_a:
            entry["pk_a"] = try_infer_pk(meta_a[ta])
            entry["n_cols_a"] = len(meta_a[ta])
        if meta_b and tb in meta_b:
            entry["pk_b"] = try_infer_pk(meta_b[tb])
            entry["n_cols_b"] = len(meta_b[tb])
        pairs.append(entry)

    # 1) exact name
    for t in tables_a:
        if t in set_b:
            score = 1.0
            shared = None
            if meta_a and meta_b and t in meta_a and t in meta_b:
                na = {c["name"] for c in meta_a[t]}
                nb = {c["name"] for c in meta_b[t]}
                score = jaccard(na, nb)
                shared = sorted(na & nb)
            add(t, t, "exact", score, shared)

    # 2) suffix heuristics (_a/_b etc.)
    for t in tables_a:
        partner = suffix_partner(t)
        if partner and partner in set_b:
            score = 0.95
            shared = None
            if meta_a and meta_b and t in meta_a and partner in meta_b:
                na = {c["name"] for c in meta_a[t]}
                nb = {c["name"] for c in meta_b[partner]}
                score = max(0.95, jaccard(na, nb))
                shared = sorted(na & nb)
            add(t, partner, "suffix", score, shared)

    # 3) schema similarity for leftovers (same base or high jaccard)
    if meta_a and meta_b:
        leftover_a = [t for t in tables_a if all(p["table_a"] != t for p in pairs)]
        leftover_b = [t for t in tables_b if t not in used_b]
        for ta in leftover_a:
            if ta not in meta_a:
                continue
            na = {c["name"] for c in meta_a[ta]}
            best = None
            best_score = 0.0
            for tb in leftover_b:
                if tb not in meta_b:
                    continue
                nb = {c["name"] for c in meta_b[tb]}
                score = jaccard(na, nb)
                # boost same normalized base
                if normalize_base(ta) == normalize_base(tb):
                    score = max(score, 0.85)
                if score >= min_jaccard and score > best_score:
                    best_score = score
                    best = (tb, sorted(na & nb))
            if best:
                add(ta, best[0], "schema", best_score, best[1])
                leftover_b = [t for t in leftover_b if t != best[0]]

    pairs.sort(key=lambda p: (-p["col_jaccard"], p["table_a"], p["table_b"]))
    return pairs


def pair_tables(
    tables_a: list[str],
    tables_b: list[str],
    hint_a: str | None,
    hint_b: str | None,
) -> tuple[str, str]:
    if hint_a and hint_b:
        return hint_a, hint_b
    if hint_a and not hint_b:
        if hint_a in tables_b:
            return hint_a, hint_a
        partner = suffix_partner(hint_a)
        if partner and partner in tables_b:
            return hint_a, partner
        die(f"table B not found for A={hint_a}; candidates B={tables_b[:40]}")
    pairs = build_candidate_pairs(tables_a, tables_b)
    if len(pairs) == 1:
        return pairs[0]["table_a"], pairs[0]["table_b"]
    if not pairs:
        die(f"no table pair found. A={tables_a[:40]} B={tables_b[:40]}; pass --table-a/--table-b")
    print("CANDIDATE_PAIRS " + json.dumps(pairs[:50]), flush=True)
    die(f"{len(pairs)} candidate pairs; pass --table-a/--table-b, --tables, or --all-pairs")


def duckdb_attach_export(
    cfg: dict[str, str], table: str, pk: str, cols: list[str], out: Path, sql: str | None
) -> dict[str, Any]:
    import duckdb
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    c = duckdb.connect()
    c.execute("SET threads=8")
    engine = cfg["ENGINE"].lower()
    t0 = time.perf_counter()
    if engine in ("mysql", "doris", "mariadb"):
        c.execute("INSTALL mysql; LOAD mysql;")
        pw = cfg["PASSWORD"].replace("'", "''")
        user = cfg["USER"].replace("'", "''")
        c.execute(
            f"ATTACH 'host={cfg['HOST']} port={cfg['PORT']} user={user} "
            f"password={pw} database={cfg['DATABASE']}' AS src (TYPE MYSQL);"
        )
        src = f"src.{cfg['DATABASE']}.{table}"
        select = sql or (f"SELECT {pk} AS pk, " + ", ".join(cols) + f" FROM {src}")
        c.execute(f"COPY ({select}) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    elif engine in ("postgres", "postgresql"):
        c.execute("INSTALL postgres; LOAD postgres;")
        pw = cfg["PASSWORD"].replace("'", "''")
        user = cfg["USER"].replace("'", "''")
        c.execute(
            f"ATTACH 'dbname={cfg['DATABASE']} user={user} password={pw} "
            f"host={cfg['HOST']} port={cfg['PORT']}' AS src (TYPE POSTGRES);"
        )
        src = f"src.public.{table}"
        select = sql or f'SELECT "{pk}" AS pk, ' + ", ".join(f'"{x}"' for x in cols) + f" FROM {src}"
        c.execute(f"COPY ({select}) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    else:
        die(f"unsupported engine {engine}")
    n = c.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    wall = time.perf_counter() - t0
    c.close()
    return {"rows": int(n), "bytes": out.stat().st_size, "wall_s": round(wall, 3), "path": str(out)}


def pymysql_export_cast(
    cfg: dict[str, str], table: str, pk: str, cols: list[str], out: Path,
    sql: str | None = None,
) -> dict[str, Any]:
    """Fallback when DuckDB mysql ATTACH chokes on dirty Doris types."""
    import pymysql
    import pyarrow as pa
    import pyarrow.parquet as pq

    out.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the caller's subset (including ORDER BY / LIMIT) through both
    # CAST attempts; never fall back to the full source table when SQL is given.
    source = f"({sql.strip().rstrip(';')}) AS r12_subset" if sql else f"`{table}`"
    sel = (
        f"SELECT `{pk}` AS pk, "
        + ", ".join(f"CAST(`{c}` AS VARCHAR) AS `{c}`" for c in cols)
        + f" FROM {source}"
    )
    schema = pa.schema([("pk", pa.int64())] + [(c, pa.string()) for c in cols])
    t0 = time.perf_counter()
    conn = pymysql.connect(
        host=cfg["HOST"], port=int(cfg["PORT"]), user=cfg["USER"],
        password=cfg["PASSWORD"], database=cfg["DATABASE"],
        connect_timeout=30, read_timeout=3600, charset="utf8mb4",
        cursorclass=pymysql.cursors.SSCursor,
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute(sel)
        except Exception:
            sel = (
                f"SELECT `{pk}` AS pk, "
                + ", ".join(f"CAST(`{c}` AS CHAR) AS `{c}`" for c in cols)
                + f" FROM {source}"
            )
            cur.execute(sel)
            schema = pa.schema([("pk", pa.int64())] + [(c, pa.string()) for c in cols])
        tmp = out.with_suffix(".tmp")
        if tmp.exists():
            tmp.unlink()
        w = pq.ParquetWriter(tmp, schema, compression="zstd")
        n = 0
        while True:
            rows = cur.fetchmany(100_000)
            if not rows:
                break
            cols_t = list(zip(*rows))
            arrays = []
            for j, field in enumerate(schema):
                try:
                    arrays.append(pa.array(cols_t[j], type=field.type))
                except Exception:
                    arrays.append(
                        pa.array(
                            [None if v is None else str(v) for v in cols_t[j]],
                            type=pa.string(),
                        )
                    )
            try:
                w.write_table(pa.Table.from_arrays(arrays, schema=schema), row_group_size=1_000_000)
            except Exception:
                w.close()
                tmp.unlink(missing_ok=True)
                schema = pa.schema([("pk", pa.string())] + [(c, pa.string()) for c in cols])
                w = pq.ParquetWriter(tmp, schema, compression="zstd")
                arrays = [
                    pa.array(
                        [None if v is None else str(v) for v in cols_t[j]],
                        type=pa.string(),
                    )
                    for j in range(len(schema))
                ]
                w.write_table(pa.Table.from_arrays(arrays, schema=schema), row_group_size=1_000_000)
            n += len(rows)
        w.close()
        tmp.rename(out)
    finally:
        conn.close()
    return {
        "rows": n,
        "bytes": out.stat().st_size,
        "wall_s": round(time.perf_counter() - t0, 3),
        "path": str(out),
        "mode": "pymysql_cast",
    }


def export_side(
    cfg: dict[str, str],
    table: str,
    pk: str,
    cols: list[str],
    out: Path,
    sql: str | None,
    prefer: str,
) -> dict[str, Any]:
    print(f"EXPORT start table={table} -> {out.name} cfg={redact(cfg)}", flush=True)
    if prefer == "pymysql" or (cfg["ENGINE"] in ("mysql", "doris") and prefer == "auto"):
        try:
            if prefer == "pymysql":
                return pymysql_export_cast(cfg, table, pk, cols, out, sql)
            return duckdb_attach_export(cfg, table, pk, cols, out, sql)
        except Exception as e:
            print(f"EXPORT fallback after {type(e).__name__}: {str(e)[:200]}", flush=True)
            if cfg["ENGINE"] in ("mysql", "doris", "mariadb"):
                return pymysql_export_cast(cfg, table, pk, cols, out, sql)
            raise
    return duckdb_attach_export(cfg, table, pk, cols, out, sql)


def run_lean(
    a_rel: str, b_rel: str, n: int, compare: list[str], work: Path, root: Path
) -> dict[str, Any]:
    sys.path.insert(0, str(root))
    os.chdir(root)
    for k in ("LEAN_ADAPTIVE", "LEAN_PERCOL", "LEAN_PORTABLE"):
        os.environ.pop(k, None)
    os.environ["LEAN_PROBE"] = "1"
    os.environ["LEAN_RAW_FIRST"] = "1"
    os.environ["LEAN_DENSE_DUPCHECK"] = "1"
    os.environ["LEAN_SUBLEAF"] = "1"
    from methods.lean import run as lean_run
    man = {"a": a_rel, "b": b_rel, "n": n, "compare_columns": compare}
    work.mkdir(parents=True, exist_ok=True)
    (work / "manifest.json").write_text(json.dumps(man, indent=2))
    t0 = time.perf_counter()
    out = lean_run(man, work)
    out = dict(out)
    out["lean_wall_s"] = round(time.perf_counter() - t0, 3)
    return out


def inventory_side(
    cfg: dict[str, str], tables: list[str], with_columns: bool = True
) -> list[dict[str, Any]]:
    inv = []
    for t in tables:
        entry: dict[str, Any] = {"table": t}
        if with_columns:
            cols = columns_of(cfg, t)
            entry["n_cols"] = len(cols)
            entry["columns"] = [
                {"name": c["name"], "type": c["type"], "key": c.get("key", "")} for c in cols
            ]
            entry["pk_guess"] = try_infer_pk(cols)
        inv.append(entry)
    return inv


def probe_report(
    cfg_a: dict[str, str],
    cfg_b: dict[str, str],
    with_columns: bool = True,
    min_jaccard: float = 0.5,
) -> dict[str, Any]:
    ta_list = list_tables(cfg_a)
    tb_list = list_tables(cfg_b)
    meta_a: dict[str, list[dict[str, Any]]] = {}
    meta_b: dict[str, list[dict[str, Any]]] = {}
    inv_a = inventory_side(cfg_a, ta_list, with_columns=with_columns)
    inv_b = inventory_side(cfg_b, tb_list, with_columns=with_columns)
    if with_columns:
        for e in inv_a:
            meta_a[e["table"]] = [
                {"name": c["name"], "type": c["type"], "key": c.get("key", ""), "extra": ""}
                for c in e["columns"]
            ]
        for e in inv_b:
            meta_b[e["table"]] = [
                {"name": c["name"], "type": c["type"], "key": c.get("key", ""), "extra": ""}
                for c in e["columns"]
            ]
    candidates = build_candidate_pairs(
        ta_list, tb_list,
        meta_a if with_columns else None,
        meta_b if with_columns else None,
        min_jaccard=min_jaccard,
    )
    unmatched_a = [t for t in ta_list if all(p["table_a"] != t for p in candidates)]
    unmatched_b = [t for t in tb_list if all(p["table_b"] != t for p in candidates)]
    return {
        "a": redact(cfg_a),
        "b": redact(cfg_b),
        "n_tables_a": len(ta_list),
        "n_tables_b": len(tb_list),
        "inventory_a": inv_a,
        "inventory_b": inv_b,
        "candidate_pairs": candidates,
        "unmatched_a": unmatched_a,
        "unmatched_b": unmatched_b,
    }


def parse_tables_filter(raw: str | None) -> list[tuple[str | None, str | None]] | None:
    """Parse --tables: 'foo,bar' or 'a1:b1,a2:b2' → list of (table_a, table_b)."""
    if not raw:
        return None
    out: list[tuple[str | None, str | None]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            a, b = part.split(":", 1)
            out.append((a.strip() or None, b.strip() or None))
        else:
            out.append((part, part))
    return out


def filter_pairs(
    candidates: list[dict[str, Any]],
    tables_filter: list[tuple[str | None, str | None]] | None,
    max_pairs: int | None,
) -> list[dict[str, Any]]:
    selected = candidates
    if tables_filter:
        wanted = set()
        for a, b in tables_filter:
            wanted.add((a, b))
        filtered = []
        for p in candidates:
            ta, tb = p["table_a"], p["table_b"]
            if (ta, tb) in wanted or (ta, ta) in wanted or (tb, tb) in wanted:
                filtered.append(p)
                continue
            # base-name match: filter "orders" hits orders_a/orders_b
            for fa, fb in tables_filter:
                base = fa or fb
                if base and (normalize_base(ta) == base or normalize_base(tb) == base or ta == base or tb == base):
                    filtered.append(p)
                    break
        selected = filtered
    if max_pairs is not None and max_pairs > 0:
        selected = selected[:max_pairs]
    return selected


def column_diff_summary(row_diffs_path: str) -> dict[str, Any]:
    import duckdb
    c = duckdb.connect()
    try:
        type_counts = dict(
            c.execute(
                f"SELECT diff_type, count(*) FROM read_parquet('{row_diffs_path}') GROUP BY 1"
            ).fetchall()
        )
        # explode columns for value_diff
        top_cols: list[tuple[str, int]] = []
        try:
            top_cols = c.execute(
                f"""
                SELECT col, count(*) AS n
                FROM (
                  SELECT UNNEST(columns) AS col
                  FROM read_parquet('{row_diffs_path}')
                  WHERE diff_type = 'value_diff' AND columns IS NOT NULL
                )
                GROUP BY 1 ORDER BY n DESC LIMIT 30
                """
            ).fetchall()
        except Exception:
            # columns may be varchar/json depending on lean version
            try:
                top_cols = c.execute(
                    f"""
                    SELECT columns AS col, count(*) AS n
                    FROM read_parquet('{row_diffs_path}')
                    WHERE diff_type = 'value_diff' AND columns IS NOT NULL
                    GROUP BY 1 ORDER BY n DESC LIMIT 30
                    """
                ).fetchall()
            except Exception:
                top_cols = []
        sample = c.execute(
            f"SELECT pk, diff_type, columns FROM read_parquet('{row_diffs_path}') ORDER BY pk LIMIT 20"
        ).fetchall()
    finally:
        c.close()
    return {
        "type_counts": {str(k): int(v) for k, v in type_counts.items()},
        "top_changed_columns": [{"column": str(r[0]), "n": int(r[1])} for r in top_cols],
        "sample": [{"pk": r[0], "diff_type": r[1], "columns": r[2]} for r in sample],
        "differs": any(int(v) > 0 for v in type_counts.values()),
    }


def run_one_pair(
    cfg_a: dict[str, str],
    cfg_b: dict[str, str],
    table_a: str,
    table_b: str,
    root: Path,
    run_dir: Path,
    pk_hint: str | None,
    compare_hint: list[str] | None,
    exclude: list[str] | None,
    prefer: str,
    sql_a: str | None,
    sql_b: str | None,
    export_only: bool,
) -> dict[str, Any]:
    meta_a = columns_of(cfg_a, table_a)
    meta_b = columns_of(cfg_b, table_b)
    try:
        pk = infer_pk(meta_a, pk_hint)
    except (ValueError, SystemExit) as e:
        return {
            "table_a": table_a,
            "table_b": table_b,
            "status": "error",
            "error": f"pk: {e}",
        }
    if pk not in {c["name"] for c in meta_b}:
        return {
            "table_a": table_a,
            "table_b": table_b,
            "status": "error",
            "error": f"pk {pk} missing on side B",
        }
    compare = pick_compare(meta_a, pk, compare_hint, exclude)
    bnames = {c["name"] for c in meta_b}
    compare = [c for c in compare if c in bnames]
    only_a = sorted({c["name"] for c in meta_a} - bnames - {pk})
    only_b = sorted(bnames - {c["name"] for c in meta_a} - {pk})
    if not compare:
        return {
            "table_a": table_a,
            "table_b": table_b,
            "status": "error",
            "error": "no shared compare columns",
            "columns_only_a": only_a,
            "columns_only_b": only_b,
        }

    pair_dir = run_dir / f"{table_a}__vs__{table_b}".replace("/", "_")
    pair_dir.mkdir(parents=True, exist_ok=True)
    a_path = pair_dir / "a.parquet"
    b_path = pair_dir / "b.parquet"
    exp_a = export_side(cfg_a, table_a, pk, compare, a_path, sql_a, prefer)
    exp_b = export_side(cfg_b, table_b, pk, compare, b_path, sql_b, prefer)
    n = max(exp_a["rows"], exp_b["rows"])
    discovery = {
        "table_a": table_a,
        "table_b": table_b,
        "pk_src": pk,
        "pk_out": "pk",
        "compare_columns": compare,
        "columns_only_a": only_a,
        "columns_only_b": only_b,
        "n": n,
        "side_a": redact(cfg_a),
        "side_b": redact(cfg_b),
        "export_a": exp_a,
        "export_b": exp_b,
    }
    (pair_dir / "discovery.json").write_text(json.dumps(discovery, indent=2))
    print(
        "DISCOVERY",
        json.dumps({k: discovery[k] for k in ("table_a", "table_b", "pk_src", "n", "compare_columns")}),
        flush=True,
    )
    if export_only:
        return {
            "table_a": table_a,
            "table_b": table_b,
            "status": "export_only",
            "pair_dir": str(pair_dir),
            "discovery": discovery,
            "schema_differs": bool(only_a or only_b),
        }

    a_rel = str(a_path.relative_to(root))
    b_rel = str(b_path.relative_to(root))
    work = pair_dir / "work"
    outcome = run_lean(a_rel, b_rel, n, compare, work, root)
    colsum = column_diff_summary(outcome["row_diffs"])
    report = {
        "table_a": table_a,
        "table_b": table_b,
        "status": "ok",
        "pair_dir": str(pair_dir),
        "discovery": discovery,
        "type_counts": colsum["type_counts"],
        "top_changed_columns": colsum["top_changed_columns"],
        "differs": colsum["differs"] or bool(only_a or only_b),
        "schema_differs": bool(only_a or only_b),
        "columns_only_a": only_a,
        "columns_only_b": only_b,
        "detected_rows": outcome.get("detected_rows"),
        "lean_wall_s": outcome.get("lean_wall_s"),
        "bytes_exchanged": outcome.get("bytes_exchanged"),
        "row_diffs": outcome.get("row_diffs"),
        "method_json": str(work / "method.json"),
        "sample": colsum["sample"],
        "knobs": {
            "LEAN_PROBE": 1,
            "LEAN_RAW_FIRST": 1,
            "LEAN_DENSE_DUPCHECK": 1,
            "LEAN_SUBLEAF": 1,
        },
    }
    (pair_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(
        "PAIR_DONE",
        json.dumps(
            {
                "table_a": table_a,
                "table_b": table_b,
                "type_counts": colsum["type_counts"],
                "differs": report["differs"],
                "lean_wall_s": outcome.get("lean_wall_s"),
            },
            default=str,
        ),
        flush=True,
    )
    return report


def run_pairs(
    cfg_a: dict[str, str],
    cfg_b: dict[str, str],
    pairs: list[dict[str, Any]],
    root: Path,
    run_id: str,
    pk_hint: str | None,
    compare_hint: list[str] | None,
    exclude: list[str] | None,
    prefer: str,
    sql_a: str | None,
    sql_b: str | None,
    export_only: bool,
    concurrency: int,
) -> dict[str, Any]:
    run_dir = root / "doris_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    def _one(p: dict[str, Any]) -> dict[str, Any]:
        try:
            return run_one_pair(
                cfg_a, cfg_b, p["table_a"], p["table_b"], root, run_dir,
                pk_hint, compare_hint, exclude, prefer, sql_a, sql_b, export_only,
            )
        except Exception as e:
            return {
                "table_a": p["table_a"],
                "table_b": p["table_b"],
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
            }

    concurrency = max(1, min(concurrency, 4))  # hard cap: protect RSS / DB
    if concurrency == 1 or len(pairs) == 1:
        for p in pairs:
            results.append(_one(p))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(_one, p): p for p in pairs}
            for fut in as_completed(futs):
                results.append(fut.result())

    differing = [r for r in results if r.get("differs")]
    identical = [r for r in results if r.get("status") == "ok" and not r.get("differs")]
    errors = [r for r in results if r.get("status") == "error"]
    summary = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "n_pairs": len(pairs),
        "n_differs": len(differing),
        "n_identical": len(identical),
        "n_errors": len(errors),
        "differing_tables": [
            {
                "table_a": r["table_a"],
                "table_b": r["table_b"],
                "type_counts": r.get("type_counts"),
                "top_changed_columns": r.get("top_changed_columns"),
                "columns_only_a": r.get("columns_only_a"),
                "columns_only_b": r.get("columns_only_b"),
                "row_diffs": r.get("row_diffs"),
                "pair_dir": r.get("pair_dir"),
            }
            for r in differing
        ],
        "identical_tables": [
            {"table_a": r["table_a"], "table_b": r["table_b"]} for r in identical
        ],
        "errors": [
            {"table_a": r["table_a"], "table_b": r["table_b"], "error": r.get("error")}
            for r in errors
        ],
        "pairs": results,
        "side_a": redact(cfg_a),
        "side_b": redact(cfg_b),
    }
    (run_dir / "pair_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (run_dir / "probe_pairs.json").write_text(json.dumps(pairs, indent=2))
    print(
        "SUMMARY",
        json.dumps(
            {
                "run_dir": str(run_dir),
                "n_pairs": summary["n_pairs"],
                "n_differs": summary["n_differs"],
                "n_identical": summary["n_identical"],
                "n_errors": summary["n_errors"],
                "differing_tables": [
                    f"{x['table_a']}__vs__{x['table_b']}" for x in summary["differing_tables"]
                ],
            }
        ),
        flush=True,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Two DB connections → probe → R12 lean (tables/columns/rows)"
    )
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--run-id", default=time.strftime("db_r12_%Y%m%d%H%M%S"))
    ap.add_argument("--env-a", help="side A secrets env file (mode 600)")
    ap.add_argument("--env-b", help="side B secrets env file; default same as A")
    ap.add_argument("--dsn-a")
    ap.add_argument("--dsn-b")
    ap.add_argument("--table-a")
    ap.add_argument("--table-b")
    ap.add_argument(
        "--tables",
        help="filter pairs: foo,bar or a1:b1,a2:b2 (base name or A:B)",
    )
    ap.add_argument("--sql-a", help="optional full SELECT for A (must alias pk)")
    ap.add_argument("--sql-b")
    ap.add_argument("--pk")
    ap.add_argument("--compare", help="comma-separated compare columns")
    ap.add_argument("--exclude", help="comma-separated extra excludes")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--prefer-export", choices=["auto", "duckdb", "pymysql"], default="auto")
    ap.add_argument(
        "--list-only",
        action="store_true",
        help="probe both DBs: inventory + candidate pairs, then exit",
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help="alias of --list-only (full inventory + schema-aware pairs)",
    )
    ap.add_argument(
        "--names-only",
        action="store_true",
        help="with --list-only: skip per-table column fetch (faster)",
    )
    ap.add_argument("--all-pairs", action="store_true", help="run R12 on all candidate pairs")
    ap.add_argument("--max-pairs", type=int, default=0, help="cap pairs (0 = no cap)")
    ap.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="pair concurrency (hard-capped at 4; default 1)",
    )
    ap.add_argument(
        "--min-jaccard",
        type=float,
        default=0.5,
        help="min column-name Jaccard for schema-similarity pairs",
    )
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not (root / "methods" / "lean.py").exists():
        die(f"ROOT {root} missing methods/lean.py")

    env_a = Path(args.env_a).expanduser() if args.env_a else root / ".secrets" / "doris-aidatadb.env"
    env_b = Path(args.env_b).expanduser() if args.env_b else env_a
    cfg_a = side_cfg("SIDEA", env_a if env_a.exists() else None, args.dsn_a)
    cfg_b = side_cfg("SIDEB", env_b if env_b.exists() else None, args.dsn_b)

    list_only = args.list_only or args.probe
    if list_only:
        report = probe_report(
            cfg_a, cfg_b,
            with_columns=not args.names_only,
            min_jaccard=args.min_jaccard,
        )
        print(json.dumps(report, indent=2, default=str))
        return 0

    compare_hint = args.compare.split(",") if args.compare else None
    exclude = args.exclude.split(",") if args.exclude else None
    tables_filter = parse_tables_filter(args.tables)

    # Multi-pair path: --all-pairs or --tables with >1 or no explicit single pair
    want_multi = args.all_pairs or (tables_filter is not None and not (args.table_a or args.table_b))
    if want_multi or (not args.table_a and not args.table_b and args.all_pairs):
        ta_list = list_tables(cfg_a)
        tb_list = list_tables(cfg_b)
        # column meta for better matching
        meta_a = {t: columns_of(cfg_a, t) for t in ta_list}
        meta_b = {t: columns_of(cfg_b, t) for t in tb_list}
        candidates = build_candidate_pairs(
            ta_list, tb_list, meta_a, meta_b, min_jaccard=args.min_jaccard
        )
        max_pairs = args.max_pairs if args.max_pairs > 0 else None
        selected = filter_pairs(candidates, tables_filter, max_pairs)
        if not selected:
            die(
                "no pairs selected after filter; run --list-only to see candidates. "
                f"candidates={len(candidates)}"
            )
        if not args.all_pairs and tables_filter is None and len(selected) > 1:
            print("CANDIDATE_PAIRS " + json.dumps(selected[:50]), flush=True)
            die(f"{len(selected)} candidate pairs; pass --all-pairs, --tables, or --table-a/--table-b")
        summary = run_pairs(
            cfg_a, cfg_b, selected, root, args.run_id,
            args.pk, compare_hint, exclude, args.prefer_export,
            args.sql_a, args.sql_b, args.export_only, args.concurrency,
        )
        # non-zero if any pair errored hard? keep 0 when diffs found (diffs are success)
        return 1 if summary["n_errors"] and not summary["n_differs"] and not summary["n_identical"] else 0

    # Single-pair path (legacy / explicit)
    ta_list = list_tables(cfg_a)
    tb_list = list_tables(cfg_b)
    if not args.table_a and not args.table_b:
        # auto unique pair, else ask for --all-pairs
        candidates = build_candidate_pairs(ta_list, tb_list)
        if len(candidates) == 1:
            args.table_a = candidates[0]["table_a"]
            args.table_b = candidates[0]["table_b"]
        elif len(candidates) == 0:
            die(f"no table pair found. A={ta_list[:40]} B={tb_list[:40]}")
        else:
            print("CANDIDATE_PAIRS " + json.dumps(candidates[:50]), flush=True)
            die(
                f"{len(candidates)} candidate pairs; pass --all-pairs to compare all, "
                "or --table-a/--table-b / --tables to choose"
            )

    table_a, table_b = pair_tables(ta_list, tb_list, args.table_a, args.table_b)
    run_dir = root / "doris_runs" / args.run_id
    summary = run_pairs(
        cfg_a, cfg_b, [{"table_a": table_a, "table_b": table_b}], root, args.run_id,
        args.pk, compare_hint, exclude, args.prefer_export,
        args.sql_a, args.sql_b, args.export_only, 1,
    )
    result = summary["pairs"][0]
    # also write top-level report.json for single-pair compat
    (run_dir / "report.json").write_text(json.dumps(result, indent=2, default=str))
    if result.get("status") == "ok":
        print(
            "DONE",
            json.dumps(
                {
                    "type_counts": result.get("type_counts"),
                    "detected_rows": result.get("detected_rows"),
                    "lean_wall_s": result.get("lean_wall_s"),
                    "top_changed_columns": result.get("top_changed_columns"),
                    "run_dir": str(run_dir),
                },
                default=str,
            ),
            flush=True,
        )
        return 0
    if result.get("status") == "export_only":
        print("EXPORT_ONLY done", run_dir)
        return 0
    print("ERROR_PAIR", json.dumps(result, default=str), flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
