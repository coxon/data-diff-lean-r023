"""Fixed data contract and local machine limits. All artifacts stay in this repo."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent  # skill install root (or override via DATADIFF_ROOT in wrappers)
TMP = ROOT / 'tmp'
TMP.mkdir(exist_ok=True)
os.environ['TMPDIR'] = str(TMP)
os.environ['SPARK_LOCAL_DIRS'] = str(TMP / 'spark')
os.environ['SPARK_LOCAL_IP'] = '127.0.0.1'
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
COLUMNS = ['int_value', 'amount', 'measure', 'name', 'code', 'event_time', 'nullable_a', 'nullable_b']
NUMERIC = ['int_value', 'amount', 'measure']
SCENARIOS = ['identical', 'sparse_clustered', 'sparse_scattered', 'medium_0_01pct', 'medium_0_1pct', 'medium_1pct',
             'systematic', 'missing_extra', 'swapped', 'duplicate_pk', 'null_swap', 'normalization', 'timezone',
             'forgotten_exclude', 'combined']
SCALES = {'1M': 1_000_000, '10M': 10_000_000, '100M': 100_000_000, '1B': 1_000_000_000}
SEED = 20261031
FLOAT_EPS = 0.001
CONTRACT = {'version': 'spec-v3-D17', 'seed': SEED, 'key': 'pk', 'decimal': '18,2',
            'float_tolerance': FLOAT_EPS, 'strings': 'NFC then strip Unicode whitespace (\\s + U+3000 + U+00A0); empty != NULL',
            'timestamp': 'UTC microseconds; naive = UTC; sub-us ignored',
            'null_pk': 'DQ section counts only; excluded from diff (not a null_pk row type)',
            'duplicate_policy': 'dup_pk whenever either side count>1 (even identical); columns = a:N,b:M',
            'decimal_policy': 'compare as scaled int (x100); HALF_UP once at read on both sides',
            'default_exclude': ['etl_time'],
            'sampling': 'if >5% leaf mismatch, hash(pk) % max(1,n//256) = 0'}


def connect(path=':memory:'):
    import duckdb
    c = duckdb.connect(str(path))
    c.execute("SET memory_limit='20GB'")
    c.execute('SET threads=12')
    c.execute("SET max_temp_directory_size='50GB'")
    c.execute(f"SET temp_directory='{TMP}'")
    c.execute("SET TimeZone='UTC'")
    c.execute('SET preserve_insertion_order=false')
    return c


def quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def bucket_shape(n):
    if n <= 1_000_000:
        return 128, 1
    if n <= 10_000_000:
        return 1024, 1
    n2 = 8
    while n / (1024 * n2) > 20_000:
        n2 *= 2
    return 1024, n2


def json_write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')


def preflight(label, projected_bytes=0):
    vm = subprocess.check_output(['vm_stat'], text=True)
    disk = shutil.disk_usage(ROOT)
    # Count allocated benchmark files including spill and results, not the read-only upstream or venv.
    used = sum(p.stat().st_size for d in ['data', 'tmp', 'results']
               for p in (ROOT / d).rglob('*') if p.is_file())
    if used + projected_bytes > 350_000_000_000 or disk.free - projected_bytes < 30_000_000_000:
        raise RuntimeError('Disk guard: data+spill must stay <=350 GB and >=30 GB free')
    receipt = {'at': time.time(), 'label': label, 'free_disk_bytes': disk.free,
               'task_disk_bytes': used, 'projected_bytes': projected_bytes, 'vm_stat': vm}
    with (ROOT / 'results' / 'preflight.jsonl').open('a') as f:
        f.write(json.dumps(receipt) + '\n')
    print(json.dumps({k: v for k, v in receipt.items() if k != 'vm_stat'}), flush=True)
    return receipt


# Spec-v3 / D5: NFC then strip Unicode whitespace (\\s) plus U+3000 and U+00A0 at both ends.
TRIM_CHARS = "(' ' || chr(9) || chr(10) || chr(13) || chr(160) || chr(12288))"
# Fast end-trim; DuckDB trim(x, chr-charset) is ~10× slower at 100M. \\s covers ASCII WS; explicit NBSP/U+3000.
TRIM_RE = r"'^[\s\x{00A0}\x{3000}]+|[\s\x{00A0}\x{3000}]+$'"


def norm(col, engine='duckdb', alias=''):
    v = f'{alias}.{col}' if alias else col
    text_type = 'STRING' if engine == 'spark' else 'VARCHAR'
    if col == 'amount':
        # D4: HALF_UP once at read via cast to decimal(18,2); compare as scaled int text
        v = f'cast(cast({v} as decimal(18,2)) * 100 as bigint)'
    elif col == 'measure':
        # D3 hashing proxy; equality uses abs<=eps (see equal())
        v = f'round({v},3)'
    elif col in ['name', 'code', 'nullable_a', 'nullable_b']:
        # D5: NFC then strip ends; empty string stays distinct from NULL (coalesce below)
        if engine == 'spark':
            v = f"regexp_replace(normalize({v}, 'NFC'), {TRIM_RE}, '', 'g')"
        else:
            v = f"regexp_replace(nfc_normalize({v}), {TRIM_RE}, '', 'g')"
    elif col in ['event_time', 'etl_time']:
        # D6: naive=UTC; µs precision. TIMESTAMP_NS truncates via epoch_ns//1000; others already µs (epoch_us).
        if engine == 'spark':
            v = f"date_format(cast({v} as timestamp), 'yyyy-MM-dd HH:mm:ss.SSSSSS')"
        else:
            micros = (f"(CASE WHEN typeof({v})::VARCHAR LIKE '%NS%' THEN epoch_ns({v}) // 1000 "
                      f"ELSE epoch_us({v}) END)")
            v = f"strftime(make_timestamp({micros}), '%Y-%m-%d %H:%M:%S.%f')"
    # Separate null and value namespaces (upstream protocol principle), not an ambiguous literal sentinel.
    return f"coalesce(concat('V:',cast({v} as {text_type})), 'N:')" if engine == 'spark' else f"coalesce('V:' || cast({v} as {text_type}), 'N:')"


def _ts_micros(alias_col):
    """D6 µs instant as bigint; NS truncates, US/TZ use epoch_us."""
    return (f"(CASE WHEN typeof({alias_col})::VARCHAR LIKE '%NS%' THEN epoch_ns({alias_col}) // 1000 "
            f"ELSE epoch_us({alias_col}) END)")


def equal(col, engine='duckdb'):
    if col == 'measure':
        # D3: NULL=NULL, NaN=NaN, -0=0, ±Inf only equals self (IS NOT DISTINCT), else |a-b|<=1e-3
        return f'(a.measure IS NOT DISTINCT FROM b.measure OR coalesce(abs(a.measure-b.measure)<={FLOAT_EPS}, false))'
    if col in ('event_time', 'etl_time') and engine == 'duckdb':
        return f'({_ts_micros(f"a.{col}")} IS NOT DISTINCT FROM {_ts_micros(f"b.{col}")})'
    return f'({norm(col, engine, "a")} = {norm(col, engine, "b")})'
