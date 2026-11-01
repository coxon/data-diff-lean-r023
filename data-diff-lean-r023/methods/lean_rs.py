"""Thin harness adapter: invoke rust lean_rs binary. No algorithm in Python."""
import json
import os
import subprocess
import time
from pathlib import Path

REV = "rust-lean-r0.23"
ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("LEAN_RS_BIN", ROOT / "bin" / "lean_rs"))


def run(manifest, work, engine="rust", phase_callback=None):
    from common import ROOT as CROOT, json_write
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if phase_callback:
        phase_callback("rust_lean")
    cols_list = manifest["compare_columns"]
    cols = ",".join(cols_list)
    n = manifest["n"]
    a = CROOT / manifest["a"]
    b = CROOT / manifest["b"]
    cmd = [
        str(BIN),
        "--a", str(a),
        "--b", str(b),
        "--out-dir", str(work),
        "--n", str(n),
        "--cols", cols,
        "--rows-per-leaf", os.environ.get("LEAN_ROWS_PER_LEAF", "4096"),
        "--dense", os.environ.get("LEAN_DENSE", "0.5"),
        "--fanout", os.environ.get("LEAN_FANOUT", "16"),
        "--partitions", os.environ.get("LEAN_PARTITIONS", "64"),
        "--revision", REV,
    ]
    if os.environ.get("LEAN_PROBE", "0") == "1":
        cmd.append("--probe")
    if os.environ.get("LEAN_RAW_FIRST", "0") == "1":
        cmd.append("--raw-first")
    if os.environ.get("LEAN_SUBLEAF", "0") == "1":
        cmd.append("--subleaf")
    if os.environ.get("LEAN_DUP_GUARD", "1") != "1":
        cmd.append("--no-dup-guard")
    t0 = time.perf_counter()
    env = os.environ.copy()
    # rayon threads
    if os.environ.get("LEAN_THREADS"):
        env["RAYON_NUM_THREADS"] = os.environ["LEAN_THREADS"]
    else:
        env.setdefault("RAYON_NUM_THREADS", "16")
    print("PHASE rust_exec", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(CROOT), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        (work / "lean_rs.stderr").write_text(proc.stderr or "")
        (work / "lean_rs.stdout").write_text(proc.stdout or "")
        raise RuntimeError(f"lean_rs exit={proc.returncode}: {proc.stderr[-2000:]}")
    (work / "lean_rs.stderr").write_text(proc.stderr or "")
    method_path = work / "method.json"
    if not method_path.exists():
        # parse last json line from stdout
        out = proc.stdout.strip().splitlines()
        method_path.write_text(out[-1] + "\n" if out else "{}")
    outcome = json.loads(method_path.read_text())
    # harness expects these keys
    outcome.setdefault("method_revision", REV)
    outcome.setdefault("engine", "rust")
    # wall already inside binary; keep
    outcome["_adapter_wall_s"] = time.perf_counter() - t0
    outcome["_compare_columns"] = cols_list
    json_write(work / "method.json", outcome)
    return outcome
