# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from power_curve_common import PROJECT_ROOT, append_manifest, init_context, load_config, rel


MODULES = [
    ("00", "00_inventory_and_schema_check.py"),
    ("01", "01_read_and_spring_adjust_author_load.py"),
    ("02", "02_reconstruct_weather_and_thermal_load.py"),
    ("03", "03_reconstruct_ev_load.py"),
    ("04", "04_extract_base_residual_and_template.py"),
    ("05", "05_actual_load_validation_gd_hainan.py"),
    ("06", "06_figures_and_reports.py"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Power_curve_V2 modules in order with QC gating")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "run_config.yaml"))
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--through", default="06", choices=[m[0] for m in MODULES])
    parser.add_argument("--ignore-blocking-soft", action="store_true")
    return parser.parse_args()


def qc_state(qc_path: Path) -> dict:
    if not qc_path.exists():
        return {"exists": False, "hard_fail": True, "blocking_soft_fail": False, "message": "QC file missing"}
    df = pd.read_csv(qc_path)
    severity = df.get("severity", pd.Series(dtype=str)).astype(str).str.upper()
    blocking = df.get("blocking", pd.Series([False] * len(df))).astype(str).str.lower().isin(["true", "1", "yes"])
    hard = bool((severity == "HARD_FAIL").any())
    soft_block = bool(((severity == "SOFT_FAIL") & blocking).any())
    return {
        "exists": True,
        "hard_fail": hard,
        "blocking_soft_fail": soft_block,
        "rows": int(len(df)),
        "severity_counts": severity.value_counts().to_dict(),
    }


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ctx_args = argparse.Namespace(config=args.config, run_dir=args.run_dir)
    ctx = init_context(ctx_args, "run_pipeline")
    append_manifest(ctx.run_dir, {"event": "pipeline_start", "time": datetime.now().isoformat(timespec="seconds"), "run_dir": rel(ctx.run_dir, ctx.root)})
    stop_after_index = [m[0] for m in MODULES].index(args.through)
    results = []
    for module_number, script in MODULES[: stop_after_index + 1]:
        script_path = Path(__file__).resolve().parent / script
        cmd = [sys.executable, str(script_path), "--config", str(args.config), "--run-dir", str(ctx.run_dir)]
        print(f"[pipeline] running module {module_number}: {script}", flush=True)
        proc = subprocess.run(cmd, cwd=str(ctx.root), text=True)
        qc_path = ctx.tables_dir / f"qc_flags_module{module_number}.csv"
        state = qc_state(qc_path)
        result = {"module": module_number, "script": script, "returncode": proc.returncode, "qc": state}
        results.append(result)
        append_manifest(ctx.run_dir, {"event": "pipeline_module_result", **result})
        if proc.returncode != 0:
            print(f"[pipeline] stopped after module {module_number}: return code {proc.returncode}", flush=True)
            break
        if state["hard_fail"]:
            print(f"[pipeline] stopped after module {module_number}: HARD_FAIL in QC", flush=True)
            break
        if state["blocking_soft_fail"] and not args.ignore_blocking_soft:
            print(f"[pipeline] stopped after module {module_number}: blocking SOFT_FAIL in QC", flush=True)
            break
    summary_path = ctx.run_dir / "pipeline_summary.json"
    summary_path.write_text(json.dumps({"run_dir": str(ctx.run_dir), "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    append_manifest(ctx.run_dir, {"event": "pipeline_end", "time": datetime.now().isoformat(timespec="seconds"), "summary_path": rel(summary_path, ctx.root)})
    print(f"[pipeline] summary: {summary_path}", flush=True)


if __name__ == "__main__":
    main()
