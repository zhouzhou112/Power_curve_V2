# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np
import pandas as pd

from power_curve_common import add_qc, init_context, module_exit, parse_args, read_required_table, write_df


MODULE = "04_extract_base_residual_and_template"


def make_template(df: pd.DataFrame, value_col: str, output_col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = df[~df["is_feb29"].astype(bool)].copy()
    base["template_key"] = pd.to_datetime(base["datetime_bj"]).dt.strftime("%m-%d %H")
    by_year = base.groupby(["province_cn", "year"], as_index=False)[value_col].sum().rename(columns={value_col: "annual_base_energy_mwh"})
    base = base.merge(by_year, on=["province_cn", "year"], how="left")
    base[output_col] = base[value_col] / base["annual_base_energy_mwh"]
    by_year_template = base[["province_cn", "year", "template_key", "datetime_bj", output_col]].copy()
    mean_template = by_year_template.groupby(["province_cn", "template_key"], as_index=False)[output_col].mean()
    sums = mean_template.groupby("province_cn")[output_col].sum().rename("sum_share")
    mean_template = mean_template.merge(sums, on="province_cn", how="left")
    mean_template[output_col] = mean_template[output_col] / mean_template["sum_share"]
    mean_template.drop(columns=["sum_share"], inplace=True)
    return by_year_template, mean_template


def main() -> None:
    args = parse_args("Module 04: extract base residual load and 8760 template")
    ctx = init_context(args, MODULE)
    flags: list[dict] = []
    try:
        author = read_required_table(ctx, "author_load_2020_2024_long.csv.gz", compression="gzip")
        thermal = read_required_table(ctx, "heating_cooling_load_2020_2024.csv.gz", compression="gzip")
        ev = read_required_table(ctx, "ev_load_2020_2024.csv.gz", compression="gzip")
        for table in [author, thermal, ev]:
            table["datetime_bj"] = pd.to_datetime(table["datetime_bj"], errors="coerce")
        keep_thermal = [
            "province_cn",
            "datetime_bj",
            "temperature_c",
            "dewpoint_c",
            "relative_humidity_pct",
            "wind_speed_ms",
            "solar_wm2",
            "bait_raw_c",
            "bait_smoothed_c",
            "bait_c",
            "heat_threshold_c",
            "cool_threshold_c",
            "hdd_hour",
            "cdd_hour",
            "p_heat_gwh_per_degree_day",
            "p_cool_gwh_per_degree_day",
            "heating_load_mw",
            "cooling_load_mw",
            "weather_weight_method",
            "time_alignment_method",
        ]
        if "weather_weight_method" not in thermal.columns:
            thermal["weather_weight_method"] = "city_monthly_electricity"
        if "time_alignment_method" not in thermal.columns:
            thermal["time_alignment_method"] = "not_recorded"
        main = author.merge(thermal[keep_thermal], on=["province_cn", "datetime_bj"], how="left").merge(
            ev[["province_cn", "datetime_bj", "nev_stock", "ev_stock_source", "ev_kwh_per_vehicle_day", "ev_energy_month_source", "ev_hour_weight", "ev_load_mw"]],
            on=["province_cn", "datetime_bj"],
            how="left",
        )
        required_cols = ["spring_adjusted_total_load_mw", "heating_load_mw", "cooling_load_mw", "ev_load_mw"]
        if main[required_cols].isna().any().any():
            missing_counts = main[required_cols].isna().sum().to_dict()
            add_qc(flags, MODULE, "HARD_FAIL", "component_missing_values", "冷热/EV/总负荷组件存在缺失，不能提取基础残值", missing_counts)
        else:
            main["base_residual_load_mw_raw"] = main["spring_adjusted_total_load_mw"] - main["heating_load_mw"] - main["cooling_load_mw"] - main["ev_load_mw"]
            main["base_residual_load_mw_clipped"] = main["base_residual_load_mw_raw"].clip(lower=0)
            main["closure_error_mw"] = main["spring_adjusted_total_load_mw"] - (
                main["heating_load_mw"] + main["cooling_load_mw"] + main["ev_load_mw"] + main["base_residual_load_mw_raw"]
            )
            max_error = float(main["closure_error_mw"].abs().max())
            if max_error >= 1e-6:
                add_qc(flags, MODULE, "HARD_FAIL", "component_hourly_closure", "逐小时组件闭合误差超过 1e-6 MW", {"max_abs_error_mw": max_error})
            negative = main[main["base_residual_load_mw_raw"] < 0].copy()
            neg_summary = main.groupby(["province_cn", "year"], as_index=False).agg(
                total_energy_mwh=("spring_adjusted_total_load_mw", "sum"),
                base_raw_energy_mwh=("base_residual_load_mw_raw", "sum"),
                base_clipped_energy_mwh=("base_residual_load_mw_clipped", "sum"),
                negative_hours=("base_residual_load_mw_raw", lambda s: int((s < 0).sum())),
                negative_energy_mwh=("base_residual_load_mw_raw", lambda s: float(-s[s < 0].sum())),
                min_base_residual_mw=("base_residual_load_mw_raw", "min"),
            )
            neg_summary["negative_energy_share_pct"] = neg_summary["negative_energy_mwh"] / neg_summary["total_energy_mwh"] * 100.0
            if (neg_summary["negative_hours"] > 0).any():
                add_qc(flags, MODULE, "WARN", "base_residual_negative_values", "基础残值负荷存在负值，已输出诊断；raw 未裁剪", neg_summary[neg_summary["negative_hours"] > 0].head(20).to_dict("records"), blocking=False)
            component_summary = main.groupby(["province_cn", "year"], as_index=False).agg(
                spring_adjusted_energy_mwh=("spring_adjusted_total_load_mw", "sum"),
                heating_energy_mwh=("heating_load_mw", "sum"),
                cooling_energy_mwh=("cooling_load_mw", "sum"),
                ev_energy_mwh=("ev_load_mw", "sum"),
                base_residual_raw_energy_mwh=("base_residual_load_mw_raw", "sum"),
                peak_total_mw=("spring_adjusted_total_load_mw", "max"),
                peak_heating_mw=("heating_load_mw", "max"),
                peak_cooling_mw=("cooling_load_mw", "max"),
                peak_ev_mw=("ev_load_mw", "max"),
            )
            by_year, mean_template = make_template(main, "base_residual_load_mw_raw", "base_template_share")
            _, clipped_template = make_template(main, "base_residual_load_mw_clipped", "base_template_share_clipped_mean")
            write_df(main, ctx.tables_dir / "hourly_province_load_components_2020_2024.csv.gz")
            write_df(neg_summary, ctx.tables_dir / "base_residual_qc.csv")
            write_df(by_year, ctx.tables_dir / "base_template_8760_by_year.csv.gz")
            write_df(mean_template, ctx.tables_dir / "base_template_8760_mean_2020_2024.csv.gz")
            write_df(clipped_template, ctx.tables_dir / "base_template_8760_clipped_sensitivity.csv.gz")
            write_df(component_summary, ctx.tables_dir / "component_energy_summary_by_province_year.csv")
            add_qc(flags, MODULE, "INFO", "module04_outputs", "模块 04 组件闭合与基础负荷模板输出完成", {"rows": int(len(main)), "max_closure_error_mw": max_error}, blocking=False)
    except FileNotFoundError as exc:
        add_qc(flags, MODULE, "SOFT_FAIL", "upstream_missing", "模块 04 缺少上游输出，阻断执行", repr(exc), blocking=True)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "module04_exception", "模块 04 执行异常", repr(exc))
    module_exit(flags, ctx, "04", MODULE)


if __name__ == "__main__":
    main()
