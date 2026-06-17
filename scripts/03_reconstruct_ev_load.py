# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np
import pandas as pd

from power_curve_common import (
    EXPECTED_PROVINCES,
    YEARS,
    add_qc,
    get_input_paths,
    init_context,
    module_exit,
    normalize_province,
    parse_args,
    read_required_table,
    rel,
    write_df,
)


MODULE = "03_reconstruct_ev_load"

MONTHLY_KWH = {
    1: (4.9674, "observed"),
    2: (5.3994, "observed"),
    3: (6.0624, "interpolated"),
    4: (6.7253, "observed"),
    5: (6.9293, "observed"),
    6: (7.0477, "observed"),
    7: (7.0151, "observed"),
    8: (6.7304, "observed"),
    9: (7.2206, "observed"),
    10: (7.6859, "observed"),
    11: (7.3551, "observed"),
    12: (6.9834, "observed"),
}

BEHAVIOR_GROUPS = [
    ("distributed_background", 0.242782, 8.708092),
    ("night_concentrated", 0.361089, 1.911315),
    ("daytime_topup", 0.105582, 12.886760),
    ("morning_topup", 0.164519, 6.967729),
    ("evening_return", 0.126028, 20.134170),
]

COMPONENTS = [
    ("night_concentrated", "night_sharp", 0.069839, 0.526263, 0.376192),
    ("night_concentrated", "night_extended", 0.291249, 1.911315, 1.527093),
    ("morning_topup", "morning_topup", 0.164519, 6.967729, 1.046837),
    ("daytime_topup", "midday_topup", 0.105582, 12.886760, 0.733610),
    ("evening_return", "evening_return", 0.126028, 20.134170, 1.194739),
    ("distributed_background", "distributed_background", 0.242782, 8.708092, 7.968137),
]


def build_probability_profiles(flags: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    group_df = pd.DataFrame(BEHAVIOR_GROUPS, columns=["behavior_group", "energy_share", "main_peak_hour"])
    component_df = pd.DataFrame(COMPONENTS, columns=["behavior_group", "component_name", "component_weight", "center_hour", "sigma_hour"])
    hours_15 = np.arange(96) / 4.0
    prob = np.zeros_like(hours_15, dtype=float)
    rows = []
    for group, name, weight, center, sigma in COMPONENTS:
        dist = np.minimum(np.abs(hours_15 - center), 24.0 - np.abs(hours_15 - center))
        raw = np.exp(-0.5 * np.square(dist / sigma))
        conditional = raw / raw.sum()
        weighted = conditional * weight
        prob += weighted
        for i, hour in enumerate(hours_15):
            rows.append(
                {
                    "slot_15min": i,
                    "hour_float": hour,
                    "hour_int": int(np.floor(hour)),
                    "behavior_group": group,
                    "component_name": name,
                    "conditional_probability": conditional[i],
                    "weighted_probability": weighted[i],
                }
            )
    total = prob.sum()
    if abs(total - 1.0) > 1e-6:
        add_qc(flags, MODULE, "WARN", "ev_96_probability_normalized", "EV 96 点权重存在浮点偏差，已归一化", {"raw_sum": float(total)}, blocking=False)
        prob = prob / total
    probability_96 = pd.DataFrame({"slot_15min": np.arange(96), "hour_float": hours_15, "hour_int": np.floor(hours_15).astype(int), "ev_15min_weight": prob})
    hourly = probability_96.groupby("hour_int", as_index=False)["ev_15min_weight"].sum().rename(columns={"hour_int": "hour_bj", "ev_15min_weight": "ev_hour_weight"})
    if abs(hourly["ev_hour_weight"].sum() - 1.0) > 1e-10:
        add_qc(flags, MODULE, "HARD_FAIL", "ev_hour_weight_sum", "EV 24 小时权重无法闭合", {"sum": float(hourly["ev_hour_weight"].sum())})
    return group_df, component_df, pd.DataFrame(rows), hourly


def read_ev_stock(ctx, flags: list[dict]) -> pd.DataFrame:
    path = get_input_paths(ctx.config)["ev_stock"]
    raw = pd.read_excel(path)
    raw["province_cn"] = raw["地区"].map(normalize_province)
    raw["year"] = pd.to_numeric(raw["年份"], errors="coerce").astype("Int64")
    raw["nev_stock"] = pd.to_numeric(raw["新能源汽车保有量（辆）"], errors="coerce")
    cleaned = raw[raw["province_cn"].isin(EXPECTED_PROVINCES)][["province_cn", "year", "nev_stock"]].copy()
    cleaned["ev_stock_source"] = "observed"
    out_rows = []
    for province in EXPECTED_PROVINCES:
        p = cleaned[cleaned["province_cn"] == province].copy()
        for year in range(2020, 2024):
            row = p[p["year"] == year]
            if row.empty:
                add_qc(flags, MODULE, "HARD_FAIL", f"ev_stock_missing_{province}_{year}", "EV 保有量历史年份缺失")
            else:
                out_rows.append(row.iloc[0].to_dict())
        v2021 = p.loc[p["year"] == 2021, "nev_stock"]
        v2023 = p.loc[p["year"] == 2023, "nev_stock"]
        if not v2021.empty and not v2023.empty and float(v2021.iloc[0]) > 0:
            cagr = (float(v2023.iloc[0]) / float(v2021.iloc[0])) ** 0.5 - 1.0
            stock_2024 = float(v2023.iloc[0]) * (1.0 + cagr)
            source = "2021_2023_cagr_extrapolated"
        elif not v2023.empty:
            stock_2024 = float(v2023.iloc[0])
            source = "2023_flat_fallback"
            add_qc(flags, MODULE, "WARN", f"ev_stock_2024_flat_{province}", "2024 EV 保有量无法计算 CAGR，使用 2023 持平", blocking=False)
        else:
            stock_2024 = np.nan
            source = "missing"
            add_qc(flags, MODULE, "HARD_FAIL", f"ev_stock_2024_missing_{province}", "2024 EV 保有量无法外推")
        out_rows.append({"province_cn": province, "year": 2024, "nev_stock": stock_2024, "ev_stock_source": source})
    out = pd.DataFrame(out_rows)
    return out


def main() -> None:
    args = parse_args("Module 03: reconstruct EV charging load")
    ctx = init_context(args, MODULE)
    flags: list[dict] = []
    try:
        author = read_required_table(ctx, "author_load_2020_2024_long.csv.gz", compression="gzip", usecols=["province_cn", "year", "month", "date_bj", "hour_bj", "datetime_bj"])
        group_df, component_df, prob_96, hourly = build_probability_profiles(flags)
        monthly = pd.DataFrame(
            [
                {"month": month, "ev_kwh_per_vehicle_day": value, "ev_energy_month_source": source, "guangzhou_nev_stock_base": 1450000}
                for month, (value, source) in MONTHLY_KWH.items()
            ]
        )
        stock = read_ev_stock(ctx, flags)
        if not any(f["severity"] == "HARD_FAIL" for f in flags):
            ev = author.merge(stock, on=["province_cn", "year"], how="left").merge(monthly, on="month", how="left").merge(hourly, on="hour_bj", how="left")
            ev["ev_daily_kwh"] = ev["nev_stock"] * ev["ev_kwh_per_vehicle_day"]
            ev["ev_load_mw"] = ev["ev_daily_kwh"] * ev["ev_hour_weight"] / 1000.0
            if ev[["nev_stock", "ev_kwh_per_vehicle_day", "ev_hour_weight", "ev_load_mw"]].isna().any().any():
                add_qc(flags, MODULE, "HARD_FAIL", "ev_load_missing_values", "EV 负荷计算存在缺失字段")
            summary = ev.groupby(["province_cn", "year"], as_index=False).agg(
                ev_annual_twh=("ev_load_mw", lambda s: s.sum() / 1e6),
                ev_mean_mw=("ev_load_mw", "mean"),
                ev_peak_mw=("ev_load_mw", "max"),
                nev_stock=("nev_stock", "first"),
                ev_kwh_per_vehicle_day_mean=("ev_kwh_per_vehicle_day", "mean"),
            )
            evening = ev[(ev["hour_bj"] >= 18) & (ev["hour_bj"] <= 22)].groupby(["province_cn", "year"], as_index=False).agg(
                evening_ev_mean_mw=("ev_load_mw", "mean"),
                evening_ev_peak_mw=("ev_load_mw", "max"),
            )
            summary = summary.merge(evening, on=["province_cn", "year"], how="left")
            write_df(ev[["province_cn", "year", "month", "date_bj", "hour_bj", "datetime_bj", "nev_stock", "ev_stock_source", "ev_kwh_per_vehicle_day", "ev_energy_month_source", "ev_hour_weight", "ev_load_mw"]], ctx.tables_dir / "ev_load_2020_2024.csv.gz")
            write_df(summary, ctx.tables_dir / "ev_load_parameters_and_qc.csv")
        write_df(group_df, ctx.tables_dir / "ev_behavior_group_parameters.csv")
        write_df(component_df, ctx.tables_dir / "ev_behavior_component_parameters.csv")
        write_df(prob_96, ctx.tables_dir / "ev_behavior_probability_96.csv")
        write_df(hourly, ctx.tables_dir / "ev_behavior_probability_hourly.csv")
        write_df(monthly, ctx.tables_dir / "ev_monthly_energy_parameters.csv")
        write_df(stock, ctx.tables_dir / "ev_stock_cleaned_2020_2024.csv")
        add_qc(flags, MODULE, "INFO", "ev_internal_split_only", "EV 负荷仅作为历史总负荷内部拆分项，未叠加到总负荷", blocking=False)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "module03_exception", "模块 03 执行异常", repr(exc))
    module_exit(flags, ctx, "03", MODULE)


if __name__ == "__main__":
    main()
