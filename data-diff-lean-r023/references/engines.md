# Engines and staging

## Supported engines

| Engine | Wire | Preferred path | Fallback |
|--------|------|----------------|----------|
| MySQL / MariaDB | mysql | DuckDB `ATTACH … (TYPE MYSQL)` then lean | `pymysql` SSCursor |
| Doris (MySQL FE, often `:9030`) | mysql | same | `pymysql` + `CAST(col AS CHAR/VARCHAR)` for dirty types |
| Postgres | postgres | DuckDB `ATTACH … (TYPE POSTGRES)` then lean | `psycopg2` if needed |

Set `ENGINE=mysql` for Doris. Port default: MySQL/Doris `3306` (Doris FE query often `9030`), Postgres `5432`.

## Connection config

Accepted via env file, DSN, or process env (`SIDEA_*` / `SIDEB_*`):

- `HOST` `PORT` `USER` `PASSWORD` `DATABASE` `ENGINE`
- Aliases: `DORIS_HOST`, `DORIS_PORT`, `DORIS_USER`, `DORIS_PASSWORD`, `DORIS_DB`, `MYSQL_*`, `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE`
- DSN: `mysql://user:pass@host:port/db`, `postgres://…`, `doris://…` (treated as mysql)

Never print `PASSWORD`. Redacted configs appear in `discovery.json` / `pair_summary.json`.

## Dirty-type notes (Doris / MySQL)

DuckDB MySQL ATTACH can fail on Doris dates, arrays, decimals, or odd column types. The tool:

1. Tries DuckDB ATTACH when `--prefer-export auto` (default) or `duckdb`.
2. On failure (or when `prefer=pymysql`), runs `pymysql` streaming export with `CAST(col AS CHAR)` / `VARCHAR` for compare columns.
3. Stages Parquet under `doris_runs/<id>/…` internally — do not ask the user to export.

Force fallback: `--prefer-export pymysql`.

## PK and compare-column inference

1. `--pk` hint if given.
2. Else single-column `PRIMARY KEY`; if composite, prefer integer part.
3. Else integer column named `id` / `pk` / `uid`.
4. Else sole integer `*_id`.
5. Else fail — pass `--pk`.

Compare columns = shared non-pk names minus noise (`etl_time`, `etl_date`, `load_time`, `binlog_*`, `dw_*`, `ods_*`, …) and `--exclude`. Schema-only columns land in `columns_only_a` / `columns_only_b`.

## Pair matching

- `exact` — same table name on both sides
- `suffix` — `_a`/`_b`, `_src`/`_dst`, `_source`/`_target`, `_old`/`_new`, `_left`/`_right`, `_prod`/`_stg|stage`
- `schema` — column-name Jaccard ≥ `--min-jaccard` (default 0.5)

Unmatched tables are listed; they are not auto-diffed.
