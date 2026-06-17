# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np
import pandas as pd

from power_curve_common import (
    EXPECTED_PROVINCES,
    SPRING_MONTH,
    YEARS,
    add_qc,
    author_load_hash,
    compare_to_actual,
    has_blocking_soft_fail,
    init_context,
    module_exit,
    parse_args,
    read_actual_validation_loads,
    read_author_load_long,
    read_spring_coefficients,
    rel,
    write_df,
)


MODULE = "01_read_and_spring_adjust_author_load"


def apply_spring_adjustment(author: pd.DataFrame, coeff: pd.DataFrame, flags: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = author.merge(coeff[["province_cn", "drop_ratio", "spring_factor", "spring_factor_source"]], on="province_cn", how="left")
    missing = sorted(df.loc[df["spring_factor"].isna(), "province_cn"].dropna().unique().tolist())
    if missing:
        national = coeff.loc[coeff["province_cn"].eq("全国") & coeff["spring_factor"].notna()]
        if national.empty:
            add_qc(flags, MODULE, "HARD_FAIL", "spring_factor_missing_no_fallback", "部分省份缺少春节校准系数，且全国 fallback 不可用", missing)
            df["is_spring_month"] = False
            df["spring_month"] = df["year"].map(SPRING_MONTH)
            df["spring_redistribution_factor"] = np.nan
            df["spring_adjusted_total_load_mw"] = np.nan
            return df, pd.DataFrame()
        fallback_factor = float(national["spring_factor"].iloc[0])
        fallback_drop = float(national["drop_ratio"].iloc[0])
        mask = df["province_cn"].isin(missing)
        df.loc[mask, "spring_factor"] = fallback_factor
        df.loc[mask, "drop_ratio"] = fallback_drop
        df.loc[mask, "spring_factor_source"] = "national_mean_fallback_missing_province"
        add_qc(
            flags,
            MODULE,
            "WARN",
            "spring_factor_missing_fallback",
            "部分省份缺少春节校准系数，已使用全国平均降幅 fallback；该处理必须在报告中说明",
            {"provinces": missing, "fallback_spring_factor": fallback_factor, "fallback_drop_ratio": fallback_drop},
            blocking=False,
        )
    if (df["spring_factor"] < 0.60).any() or (df["spring_factor"] > 1.10).any():
        add_qc(
            flags,
            MODULE,
            "SOFT_FAIL",
            "spring_factor_range",
            "春节校准系数超出建议范围 0.60-1.10",
            {"min": float(df["spring_factor"].min()), "max": float(df["spring_factor"].max())},
            blocking=True,
        )

    adjusted_parts: list[pd.DataFrame] = []
    closure_rows: list[dict] = []
    for (province, year), group in df.groupby(["province_cn", "year"], sort=True):
        group = group.sort_values("datetime_bj").copy()
        spring_month = SPRING_MONTH[int(year)]
        factor = float(group["spring_factor"].iloc[0])
        spring_mask = group["month"].eq(spring_month)
        rest_mask = ~spring_mask
        original = group["author_total_load_mw"].astype(float)
        adjusted = original.copy()
        adjusted.loc[spring_mask] = original.loc[spring_mask] * factor
        delta_e = original.loc[spring_mask].sum() - adjusted.loc[spring_mask].sum()
        rest_energy = original.loc[rest_mask].sum()
        redistribution_factor = 1.0 + delta_e / rest_energy if rest_energy else np.nan
        adjusted.loc[rest_mask] = original.loc[rest_mask] * redistribution_factor
        group["is_spring_month"] = spring_mask
        group["spring_month"] = spring_month
        group["spring_factor"] = factor
        group["spring_factor_source"] = group["spring_factor_source"].iloc[0]
        group["spring_redistribution_factor"] = redistribution_factor
        group["spring_adjusted_total_load_mw"] = adjusted
        original_energy = original.sum()
        adjusted_energy = adjusted.sum()
        abs_error = adjusted_energy - original_energy
        rel_error = abs(abs_error) / abs(original_energy) if original_energy else np.nan
        closure_rows.append(
            {
                "province_cn": province,
                "year": int(year),
                "spring_month": spring_month,
                "spring_factor": factor,
                "spring_factor_source": group["spring_factor_source"].iloc[0],
                "spring_redistribution_factor": redistribution_factor,
                "original_energy_mwh": original_energy,
                "spring_adjusted_energy_mwh": adjusted_energy,
                "delta_e_mwh": delta_e,
                "annual_energy_abs_error_mwh": abs_error,
                "relative_annual_energy_error": rel_error,
                "status": "OK" if rel_error < 1e-10 else "HARD_FAIL",
            }
        )
        adjusted_parts.append(group)
    out = pd.concat(adjusted_parts, ignore_index=True)
    closure = pd.DataFrame(closure_rows)
    bad = closure["relative_annual_energy_error"].fillna(np.inf) >= 1e-10
    if bad.any():
        add_qc(flags, MODULE, "HARD_FAIL", "spring_annual_energy_closure", "春节校准后省级年度总电量未闭合", closure.loc[bad].to_dict("records"))
    return out, closure


def validate_author_structure(author: pd.DataFrame, flags: list[dict]) -> None:
    for year in YEARS:
        expected_hours = 8784 if year in {2020, 2024} else 8760
        sub = author[author["year"] == year]
        counts = sub.groupby("province_cn")["datetime_bj"].nunique()
        missing = [p for p in EXPECTED_PROVINCES if p not in counts.index]
        bad_counts = counts[counts != expected_hours]
        if missing or not bad_counts.empty:
            add_qc(
                flags,
                MODULE,
                "HARD_FAIL",
                f"author_hours_{year}",
                f"{year} 论文负荷小时数或省份覆盖异常",
                {"missing": missing, "bad_counts": bad_counts.to_dict(), "expected_hours": expected_hours},
            )
    if author["author_total_load_mw"].isna().any() or (author["author_total_load_mw"] <= 0).any():
        add_qc(flags, MODULE, "HARD_FAIL", "author_load_values", "论文原始负荷存在缺失或非正值")


def main() -> None:
    args = parse_args("Module 01: read paper load and apply spring adjustment")
    ctx = init_context(args, MODULE)
    flags: list[dict] = []

    try:
        author = read_author_load_long(ctx, YEARS)
        validate_author_structure(author, flags)
        raw_hash_before = author_load_hash(author)
        coeff = read_spring_coefficients(ctx)
        write_df(coeff, ctx.tables_dir / "spring_adjustment_coefficients.csv")
        adjusted, closure = apply_spring_adjustment(author, coeff, flags)
        raw_hash_after = author_load_hash(adjusted.rename(columns={"author_total_load_mw": "author_total_load_mw"}))
        if raw_hash_before != raw_hash_after:
            add_qc(flags, MODULE, "HARD_FAIL", "author_load_hash_preserved", "author_total_load_mw 在模块 01 中发生变化")
        else:
            add_qc(flags, MODULE, "INFO", "author_load_hash_preserved", "author_total_load_mw 原始值哈希保持不变", raw_hash_before, blocking=False)
        export_cols = [
            "province_cn",
            "year",
            "month",
            "date_bj",
            "hour_bj",
            "datetime_bj",
            "is_leap_year",
            "is_feb29",
            "is_spring_month",
            "spring_factor",
            "spring_factor_source",
            "spring_redistribution_factor",
            "author_total_load_mw",
            "spring_adjusted_total_load_mw",
        ]
        write_df(adjusted[export_cols], ctx.tables_dir / "author_load_2020_2024_long.csv.gz")
        write_df(closure, ctx.tables_dir / "spring_adjustment_energy_closure.csv")
        if not any(f["severity"] == "HARD_FAIL" for f in flags) and not has_blocking_soft_fail(flags):
            try:
                actual = read_actual_validation_loads(ctx)
                comparison = compare_to_actual(
                    adjusted,
                    actual,
                    ["author_total_load_mw", "spring_adjusted_total_load_mw"],
                    MODULE,
                )
                write_df(comparison, ctx.tables_dir / "actual_comparison_after_spring_adjustment.csv")
            except Exception as exc:
                add_qc(flags, MODULE, "WARN", "actual_comparison_after_spring", "广东/海南真实负荷初版验证失败；不用于校准", repr(exc), blocking=False)
        add_qc(
            flags,
            MODULE,
            "INFO",
            "module01_outputs",
            "模块 01 输出完成",
            {
                "author_rows": int(len(author)),
                "closure_rows": int(len(closure)),
                "run_dir": rel(ctx.run_dir, ctx.root),
            },
            blocking=False,
        )
    except SystemExit:
        raise
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "module01_exception", "模块 01 执行异常", repr(exc))
    module_exit(flags, ctx, "01", MODULE)


if __name__ == "__main__":
    main()
