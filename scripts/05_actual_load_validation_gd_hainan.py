# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np
import pandas as pd

from power_curve_common import add_qc, compare_to_actual, init_context, module_exit, parse_args, read_actual_validation_loads, read_required_table, write_df


MODULE = "05_actual_load_validation_gd_hainan"


def main() -> None:
    args = parse_args("Module 05: Guangdong and Hainan actual load validation")
    ctx = init_context(args, MODULE)
    flags: list[dict] = []
    try:
        components = read_required_table(ctx, "hourly_province_load_components_2020_2024.csv.gz", compression="gzip")
        components["datetime_bj"] = pd.to_datetime(components["datetime_bj"], errors="coerce")
        actual = read_actual_validation_loads(ctx)
        components["reconstructed_total_load_mw"] = (
            components["base_residual_load_mw_raw"] + components["heating_load_mw"] + components["cooling_load_mw"] + components["ev_load_mw"]
        )
        model_cols = [
            "author_total_load_mw",
            "spring_adjusted_total_load_mw",
            "reconstructed_total_load_mw",
            "base_residual_load_mw_raw",
        ]
        comparison = compare_to_actual(components, actual, model_cols, MODULE)
        write_df(comparison, ctx.tables_dir / "actual_load_comparison_guangdong_hainan.csv")

        merged = components[["province_cn", "datetime_bj", "year", "month"] + model_cols].merge(actual, on=["province_cn", "datetime_bj"], how="inner")
        monthly_rows = []
        peak_rows = []
        shape_rows = []
        for (province, year, month), sub in merged.groupby(["province_cn", "year", "month"]):
            for col in model_cols:
                valid = sub[col].notna() & sub["actual_load_mw"].notna()
                if valid.sum() == 0:
                    continue
                s = sub.loc[valid]
                actual_energy = s["actual_load_mw"].sum()
                monthly_rows.append(
                    {
                        "province_cn": province,
                        "year": int(year),
                        "month": int(month),
                        "model_col": col,
                        "valid_hours": int(valid.sum()),
                        "model_energy_mwh": float(s[col].sum()),
                        "actual_energy_mwh": float(actual_energy),
                        "monthly_energy_error_pct": float((s[col].sum() - actual_energy) / actual_energy * 100.0) if actual_energy else np.nan,
                    }
                )
                corr = s[col].corr(s["actual_load_mw"]) if s[col].std() and s["actual_load_mw"].std() else np.nan
                shape_rows.append({"province_cn": province, "year": int(year), "month": int(month), "model_col": col, "hourly_shape_corr_by_month": corr})
        for province, sub in merged.groupby("province_cn"):
            for col in model_cols:
                valid = sub[col].notna() & sub["actual_load_mw"].notna()
                if valid.sum() == 0:
                    continue
                s = sub.loc[valid]
                peak_rows.append(
                    {
                        "province_cn": province,
                        "model_col": col,
                        "model_peak_mw": float(s[col].max()),
                        "actual_peak_mw": float(s["actual_load_mw"].max()),
                        "peak_load_error_pct": float((s[col].max() - s["actual_load_mw"].max()) / s["actual_load_mw"].max() * 100.0),
                    }
                )
        write_df(pd.DataFrame(monthly_rows), ctx.tables_dir / "actual_load_monthly_comparison_guangdong_hainan.csv")
        write_df(pd.DataFrame(peak_rows), ctx.tables_dir / "actual_load_peak_comparison_guangdong_hainan.csv")
        write_df(pd.DataFrame(shape_rows), ctx.tables_dir / "actual_load_shape_corr_by_month_guangdong_hainan.csv")
        add_qc(flags, MODULE, "INFO", "validation_only", "广东/海南真实统调负荷仅用于验证，未用于校准全国负荷", {"comparison_rows": int(len(comparison))}, blocking=False)
    except FileNotFoundError as exc:
        add_qc(flags, MODULE, "SOFT_FAIL", "upstream_missing", "模块 05 缺少上游组件表，阻断执行", repr(exc), blocking=True)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "module05_exception", "模块 05 执行异常", repr(exc))
    module_exit(flags, ctx, "05", MODULE)


if __name__ == "__main__":
    main()
