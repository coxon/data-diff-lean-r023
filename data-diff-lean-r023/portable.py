"""Claude R14 portable canonical encoding + SHA-256/8.

Per compare column (order fixed by caller), length-prefixed:
  NULL  -> u32be(0xFFFFFFFF)
  value -> u32be(n) || utf-8(canonical text)

Canonical text (matches Spec-v3 D3–D6 intent):
  int           -> decimal digits (no leading +, no leading zeros except 0)
  decimal(18,2) -> scaled int text (HALF_UP once ×100)
  double        -> str(round(x*1000)) with banker's round (half-even) as int
  string        -> NFC then strip \\s/U+3000/U+00A0 ends; empty stays empty (≠ NULL)
  timestamp     -> epoch-µs decimal text (naive=UTC; NS truncated)

Row digest = SHA-256(concat encodings)[:8] as uint64 big-endian.
Available in DuckDB (sha256), Postgres, Oracle STANDARD_HASH, Hive sha2.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN

NULL_LEN = 0xFFFFFFFF
_WS = re.compile(r"^[\s\u00a0\u3000]+|[\s\u00a0\u3000]+$", re.UNICODE)


def _strip_ws(s: str) -> str:
    return _WS.sub("", s)


def _enc_bytes(payload: bytes | None) -> bytes:
    if payload is None:
        return struct.pack(">I", NULL_LEN)
    return struct.pack(">I", len(payload)) + payload


def canon_text(col: str, value) -> str | None:
    """Return canonical UTF-8 text for a non-null value, or None for NULL."""
    if value is None:
        return None
    if col == "amount":
        d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return str(int(d * 100))
    if col == "measure":
        # half-even on x*1000 as int
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return "nan" if math.isnan(x) else ("inf" if x > 0 else "-inf")
        scaled = Decimal(str(x)) * Decimal(1000)
        return str(int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN)))
    if col == "int_value":
        return str(int(value))
    if col in ("name", "code", "nullable_a", "nullable_b"):
        s = unicodedata.normalize("NFC", str(value))
        return _strip_ws(s)
    if col in ("event_time", "etl_time"):
        # accept datetime or string; produce epoch µs (naive=UTC; NS truncated)
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            us = int(dt.timestamp() * 1_000_000)
            return str(us)
        s = str(value).strip()
        # Try fromisoformat-ish: 'YYYY-MM-DD HH:MM:SS[.fraction][+HH[:MM]|Z]'
        s2 = s.replace('Z', '+00:00')
        # '+08' / '-0500' → ISO offset
        s2 = re.sub(r'([+-]\d{2})$', r'\1:00', s2)
        s2 = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', s2)
        if ' ' in s2 and 'T' not in s2:
            s2 = s2.replace(' ', 'T', 1)
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            # last resort: already epoch text
            return s
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        us = int(dt.timestamp() * 1_000_000)
        return str(us)
    return str(value)


def encode_value(col: str, value) -> bytes:
    text = canon_text(col, value)
    if text is None:
        return _enc_bytes(None)
    return _enc_bytes(text.encode("utf-8"))


def encode_row(cols: list[str], values: dict) -> bytes:
    return b"".join(encode_value(c, values.get(c)) for c in cols)


def row_hash_u64(cols: list[str], values: dict) -> int:
    digest = hashlib.sha256(encode_row(cols, values)).digest()[:8]
    return int.from_bytes(digest, "big")


def row_hash_hex8(cols: list[str], values: dict) -> str:
    return hashlib.sha256(encode_row(cols, values)).hexdigest()[:16]


def duckdb_encode_expr(col: str, typ_hint: str | None = None, alias: str = "") -> str:
    """SQL expression returning VARCHAR canonical text or NULL (DuckDB)."""
    v = f"{alias}.{col}" if alias else col
    if col == "amount":
        return f"cast(cast(cast({v} as decimal(18,2)) * 100 as bigint) as VARCHAR)"
    if col == "measure":
        # Claude R14: round(x·1000) half-even as int text; NaN/±Inf named.
        # DuckDB round() is half-up; emulate half-even when fractional part is exactly 0.5.
        s = f"({v} * 1000)"
        he = (f"(CASE WHEN abs({s} - trunc({s})) = 0.5 "
              f"THEN (CASE WHEN cast(trunc({s}) as bigint) % 2 = 0 THEN cast(trunc({s}) as bigint) "
              f"ELSE cast(round({s}, 0) as bigint) END) "
              f"ELSE cast(round({s}, 0) as bigint) END)")
        return (f"(CASE WHEN isnan({v}) THEN 'nan' WHEN isinf({v}) AND {v}>0 THEN 'inf' "
                f"WHEN isinf({v}) THEN '-inf' ELSE cast(({he}) as VARCHAR) END)")
    if col == "int_value":
        return f"cast({v} as VARCHAR)"
    if col in ("name", "code", "nullable_a", "nullable_b"):
        from common import TRIM_RE
        return f"regexp_replace(nfc_normalize({v}), {TRIM_RE}, '', 'g')"
    if col in ("event_time", "etl_time"):
        micros = (f"(CASE WHEN typeof({v})::VARCHAR LIKE '%NS%' THEN epoch_ns({v}) // 1000 "
                  f"ELSE epoch_us({v}) END)")
        return f"cast({micros} as VARCHAR)"
    return f"cast({v} as VARCHAR)"


def duckdb_row_hash_sql(cols: list[str], alias: str = "") -> str:
    """SQL: sha256(portable blob) hex prefix 16 (= first 8 bytes)."""
    blob_parts = []
    for c in cols:
        tex = duckdb_encode_expr(c, alias=alias)
        # Use || not concat(): concat() coerces BLOB→VARCHAR in DuckDB.
        blob_parts.append(
            f"(CASE WHEN ({tex}) IS NULL THEN from_hex('ffffffff') "
            f"ELSE from_hex(lpad(to_hex(strlen({tex})::BIGINT), 8, '0')) || encode({tex}) END)"
        )
    concat = " || ".join(blob_parts)
    return f"substr(sha256({concat}), 1, 16)"


def duckdb_fp_contrib_sql(hex16_expr: str) -> str:
    """Claude R14: leaf contrib = u32_hi + u32_lo as BIGINT (no u64 overflow semantics)."""
    return (f"(('0x'||substr(({hex16_expr}),1,8))::UINTEGER::BIGINT"
            f"+('0x'||substr(({hex16_expr}),9,8))::UINTEGER::BIGINT)")


def duckdb_row_fp_sql(cols: list[str], alias: str = "") -> str:
    """SQL expression: portable SHA-256/8 as two-half BIGINT contrib (one sha256)."""
    # Nested SELECT so sha256 runs once; caller must place this where a scalar subquery is legal,
    # or prefer duckdb_row_hash_sql + duckdb_fp_contrib_sql over a precomputed hex column.
    return duckdb_fp_contrib_sql(duckdb_row_hash_sql(cols, alias=alias))
