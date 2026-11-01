# Report artifacts

Run directory: `$ROOT/doris_runs/<run_id>/` (default `run_id` = `db_r023_YYYYMMDDHHMMSS`).

## `pair_summary.json` (primary)

```json
{
  "run_id": "…",
  "run_dir": "…",
  "n_pairs": 0,
  "n_differs": 0,
  "n_identical": 0,
  "n_errors": 0,
  "differing_tables": [
    {
      "table_a": "orders",
      "table_b": "orders",
      "type_counts": {"missing": 0, "extra": 0, "value_diff": 0, "dup_pk": 0},
      "top_changed_columns": [{"column": "amount", "n": 12}],
      "columns_only_a": [],
      "columns_only_b": [],
      "row_diffs": "…/work/row_diffs.parquet",
      "pair_dir": "…"
    }
  ],
  "identical_tables": [{"table_a": "…", "table_b": "…"}],
  "errors": [{"table_a": "…", "table_b": "…", "error": "…"}],
  "pairs": [],
  "side_a": {"HOST": "…", "DATABASE": "…"},
  "side_b": {"HOST": "…", "DATABASE": "…"}
}
```

`side_*` are redacted (no password).

## Per-pair files

| Path | Contents |
|------|----------|
| `<A>__vs__<B>/work/row_diffs.parquet` | columns: `pk`, `diff_type`, `columns` |
| `<A>__vs__<B>/work/method.json` | timings, bytes, knobs |
| `<A>__vs__<B>/discovery.json` | pk, compare_columns, schema-only cols, redacted cfg |
| `<A>__vs__<B>/report.json` | type counts + sample keys |
| `probe_pairs.json` | candidate pairs used for the run |

## Diff types

| `diff_type` | Meaning |
|-------------|---------|
| `missing` | pk present on A, absent on B |
| `extra` | pk present on B, absent on A |
| `value_diff` | same pk, one or more compare columns differ |
| `dup_pk` | duplicate primary key on a side |

## Agent report checklist

1. Summarize `n_differs` / `n_identical` / `n_errors`.
2. For each differing table: type counts + top changed columns.
3. Mention schema-only columns if any.
4. Offer a few sample pks (from `sample` or parquet), not the full dump.
5. Confirm secrets stayed out of logs.
