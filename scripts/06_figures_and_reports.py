# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from power_curve_common import add_qc, init_context, module_exit, parse_args, read_required_table, write_df, write_markdown


MODULE = "06_figures_and_reports"

CJK_FONT_CANDIDATES = [
    ("SimHei", Path("C:/Windows/Fonts/simhei.ttf")),
    ("Microsoft YaHei", Path("C:/Windows/Fonts/msyh.ttc")),
    ("SimSun", Path("C:/Windows/Fonts/simsun.ttc")),
    ("Noto Sans SC", Path("C:/Windows/Fonts/NotoSansSC-VF.ttf")),
]


def configure_matplotlib_fonts(flags: list[dict]) -> None:
    for font_name, font_path in CJK_FONT_CANDIDATES:
        if font_path.exists():
            fm.fontManager.addfont(str(font_path))
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            plt.rcParams["pdf.fonttype"] = 42
            plt.rcParams["ps.fonttype"] = 42
            add_qc(
                flags,
                MODULE,
                "INFO",
                "figure_font_configured",
                "Module 06 figures use an explicit CJK-capable font",
                {"font": font_name, "path": str(font_path)},
                blocking=False,
            )
            return
    plt.rcParams["axes.unicode_minus"] = False
    add_qc(
        flags,
        MODULE,
        "WARN",
        "figure_cjk_font_missing",
        "No configured CJK font was found; figure labels may miss Chinese glyphs",
        {"candidates": [str(path) for _, path in CJK_FONT_CANDIDATES]},
        blocking=False,
    )


def load_optional(ctx, filename: str, **kwargs) -> pd.DataFrame | None:
    path = ctx.tables_dir / filename
    if not path.exists():
        return None
    if filename.endswith(".gz"):
        return pd.read_csv(path, compression="gzip", **kwargs)
    return pd.read_csv(path, **kwargs)


def save_figure(ctx, name: str, fig, data: pd.DataFrame) -> None:
    png = ctx.figures_dir / f"{name}.png"
    pdf = ctx.figures_dir / f"{name}.pdf"
    csv_path = ctx.figures_dir / f"{name}_plot_data.csv"
    fig.tight_layout()
    fig.savefig(png, dpi=220)
    fig.savefig(pdf)
    plt.close(fig)
    write_df(data, csv_path)


def simple_bar(ctx, name: str, data: pd.DataFrame, x: str, y: str, title: str, ylabel: str) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.8))
    plot = data.copy()
    ax.bar(plot[x].astype(str), plot[y].astype(float), color="#3B82F6")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", labelrotation=45)
    save_figure(ctx, name, fig, plot)


def add_assessment(rows: list[dict], stage: str, metric: str, value, reference: str, decision: str, note: str) -> None:
    rows.append(
        {
            "stage": stage,
            "metric": metric,
            "value": value,
            "reference": reference,
            "decision": decision,
            "note": note,
        }
    )


def compact_value(value) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def markdown_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(row[col]).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def write_error_assessment(
    ctx,
    current_flags: list[dict],
    actual: pd.DataFrame | None,
    actual_monthly: pd.DataFrame | None,
    actual_shape: pd.DataFrame | None,
    actual_peak: pd.DataFrame | None,
    base_qc: pd.DataFrame | None,
    template: pd.DataFrame | None,
) -> None:
    rows: list[dict] = []
    for module_number in range(7):
        if module_number == 6:
            qc = pd.DataFrame(current_flags)
        else:
            path = ctx.tables_dir / f"qc_flags_module{module_number:02d}.csv"
            if not path.exists():
                add_assessment(rows, f"Module {module_number:02d}", "QC file", "missing", "QC file must exist", "FAIL", "Module output QC is absent")
                continue
            qc = pd.read_csv(path)
        if qc.empty:
            add_assessment(rows, f"Module {module_number:02d}", "QC file", "missing", "QC file must exist", "FAIL", "Module output QC is absent")
            continue
        counts = qc["severity"].astype(str).str.upper().value_counts().to_dict()
        if counts.get("HARD_FAIL", 0) > 0:
            decision = "FAIL"
        elif counts.get("SOFT_FAIL", 0) > 0 or counts.get("WARN", 0) > 0:
            decision = "PASS_WITH_WARN"
        else:
            decision = "PASS"
        add_assessment(rows, f"Module {module_number:02d}", "QC severity counts", str(counts), "HARD_FAIL must be 0", decision, "Module-level QC gate")

    if base_qc is not None and not base_qc.empty:
        negative_hours = int(base_qc["negative_hours"].sum()) if "negative_hours" in base_qc.columns else 0
        min_base = float(base_qc["min_base_residual_mw"].min()) if "min_base_residual_mw" in base_qc.columns else np.nan
        add_assessment(
            rows,
            "Module 04",
            "base residual negative hours",
            negative_hours,
            "0 preferred; negatives require diagnosis",
            "PASS" if negative_hours == 0 else "PASS_WITH_WARN",
            f"Minimum raw base residual MW = {compact_value(min_base)}",
        )
    if template is not None and not template.empty and "base_template_share" in template.columns:
        sums = template.groupby("province_cn")["base_template_share"].sum()
        max_dev = float((sums - 1.0).abs().max())
        add_assessment(
            rows,
            "Module 04",
            "8760 template share closure",
            max_dev,
            "absolute deviation <= 1e-9",
            "PASS" if max_dev <= 1e-9 else "FAIL",
            "Mean base residual template must close to one by province",
        )

    if actual is not None and not actual.empty:
        focus = actual[actual["model_col"].eq("spring_adjusted_total_load_mw")].copy()
        for _, row in focus.iterrows():
            province = row["province_cn"]
            coverage = float(row["coverage"])
            corr = float(row["corr"])
            mape = float(row["MAPE"])
            peak_error_pct = np.nan
            if actual_peak is not None and not actual_peak.empty:
                peak = actual_peak[
                    actual_peak["province_cn"].eq(province)
                    & actual_peak["model_col"].eq("spring_adjusted_total_load_mw")
                ]
                if not peak.empty:
                    peak_error_pct = float(peak.iloc[0]["peak_load_error_pct"])
            monthly_max_abs = np.nan
            if actual_monthly is not None and not actual_monthly.empty:
                monthly = actual_monthly[
                    actual_monthly["province_cn"].eq(province)
                    & actual_monthly["model_col"].eq("spring_adjusted_total_load_mw")
                ].copy()
                if not monthly.empty:
                    monthly_max_abs = float(monthly["monthly_energy_error_pct"].abs().max())
            shape_min = np.nan
            if actual_shape is not None and not actual_shape.empty:
                shape = actual_shape[
                    actual_shape["province_cn"].eq(province)
                    & actual_shape["model_col"].eq("spring_adjusted_total_load_mw")
                ]
                if not shape.empty:
                    shape_min = float(shape["hourly_shape_corr_by_month"].min())

            add_assessment(rows, f"Module 05 {province}", "actual coverage", coverage, ">= 0.98", "PASS" if coverage >= 0.98 else "PASS_WITH_WARN", "Actual dispatch-load validation coverage")
            add_assessment(rows, f"Module 05 {province}", "overall MAPE pct", mape, "<= 10 good; 10-20 warning; >20 fail", "PASS" if mape <= 10 else ("PASS_WITH_WARN" if mape <= 20 else "FAIL"), "Energy/load magnitude error against actual dispatch load")
            add_assessment(rows, f"Module 05 {province}", "overall correlation", corr, ">= 0.8", "PASS" if corr >= 0.8 else "PASS_WITH_WARN", "Hourly shape agreement")
            add_assessment(rows, f"Module 05 {province}", "peak load error pct", peak_error_pct, "absolute <= 5", "PASS" if pd.notna(peak_error_pct) and abs(peak_error_pct) <= 5 else "PASS_WITH_WARN", "Peak magnitude validation")
            add_assessment(rows, f"Module 05 {province}", "worst monthly energy error pct", monthly_max_abs, "<= 10 preferred; >20 high", "PASS" if pd.notna(monthly_max_abs) and monthly_max_abs <= 10 else ("PASS_WITH_WARN" if pd.notna(monthly_max_abs) and monthly_max_abs <= 30 else "FAIL"), "Worst month highlights口径/春节/actual coverage risk")
            add_assessment(rows, f"Module 05 {province}", "minimum monthly shape corr", shape_min, ">= 0.5 preferred", "PASS" if pd.notna(shape_min) and shape_min >= 0.5 else "PASS_WITH_WARN", "Worst monthly hourly-shape correlation")

    assessment = pd.DataFrame(rows)
    write_df(assessment, ctx.tables_dir / "module_error_assessment.csv")
    report = "# Module Error Assessment\n\n"
    report += "This report summarizes QC gates and validation errors. Guangdong and Hainan actual dispatch loads remain validation-only and are not used for national calibration.\n\n"
    report += markdown_table(assessment)
    report += "\n"
    write_markdown(ctx.reports_dir / "05_module_error_assessment_report.md", report)


def main() -> None:
    args = parse_args("Module 06: figures and reports")
    ctx = init_context(args, MODULE)
    flags: list[dict] = []
    configure_matplotlib_fonts(flags)
    try:
        components = read_required_table(ctx, "hourly_province_load_components_2020_2024.csv.gz", compression="gzip")
        summary = read_required_table(ctx, "component_energy_summary_by_province_year.csv")
        closure = load_optional(ctx, "spring_adjustment_energy_closure.csv")
        coeff = load_optional(ctx, "power_coefficients_long.csv")
        ev_hourly = load_optional(ctx, "ev_behavior_probability_hourly.csv")
        ev_monthly = load_optional(ctx, "ev_monthly_energy_parameters.csv")
        ev_qc = load_optional(ctx, "ev_load_parameters_and_qc.csv")
        base_qc = load_optional(ctx, "base_residual_qc.csv")
        template = load_optional(ctx, "base_template_8760_mean_2020_2024.csv.gz")
        actual = load_optional(ctx, "actual_load_comparison_guangdong_hainan.csv")
        actual_monthly = load_optional(ctx, "actual_load_monthly_comparison_guangdong_hainan.csv")
        actual_shape = load_optional(ctx, "actual_load_shape_corr_by_month_guangdong_hainan.csv")
        actual_peak = load_optional(ctx, "actual_load_peak_comparison_guangdong_hainan.csv")
        spring_actual = load_optional(ctx, "actual_comparison_after_spring_adjustment.csv")

        flow = pd.DataFrame(
            {
                "step": ["author load", "spring adjustment", "weather thermal split", "EV split", "base residual", "validation/report"],
                "order": [1, 2, 3, 4, 5, 6],
            }
        )
        fig, ax = plt.subplots(figsize=(9, 2.6))
        ax.plot(flow["order"], [1] * len(flow), marker="o", color="#2563EB")
        for _, row in flow.iterrows():
            ax.text(row["order"], 1.03, row["step"], ha="center", va="bottom", fontsize=9)
        ax.set_axis_off()
        ax.set_title("Method Flow")
        save_figure(ctx, "fig_01_method_flow", fig, flow)

        if closure is not None:
            data = closure.groupby("year", as_index=False)["relative_annual_energy_error"].max()
            simple_bar(ctx, "fig_02_spring_adjustment_energy_conservation", data, "year", "relative_annual_energy_error", "Spring Adjustment Energy Closure", "max relative error")

        weather_method = pd.DataFrame({"weather_weight_method": ["city_monthly_electricity"], "used_as_mainline": [1], "population_baseline_available": [0]})
        simple_bar(ctx, "fig_03_weather_weighting_city_vs_population", weather_method, "weather_weight_method", "used_as_mainline", "Weather Weighting Method", "indicator")

        if coeff is not None and {"p_heat_gwh_per_degree_day", "p_cool_gwh_per_degree_day"}.issubset(coeff.columns):
            coef_plot = coeff[coeff["year"].between(2020, 2024)].groupby("year", as_index=False)[["p_heat_gwh_per_degree_day", "p_cool_gwh_per_degree_day"]].mean()
            fig, ax = plt.subplots(figsize=(7, 4.5))
            ax.plot(coef_plot["year"], coef_plot["p_heat_gwh_per_degree_day"], marker="o", label="Heating")
            ax.plot(coef_plot["year"], coef_plot["p_cool_gwh_per_degree_day"], marker="o", label="Cooling")
            ax.set_title("Power Coefficients by Year")
            ax.set_ylabel("GWh/degree C/day")
            ax.legend()
            save_figure(ctx, "fig_04_power_coefficients_by_province_year", fig, coef_plot)

        if ev_hourly is not None:
            fig, ax = plt.subplots(figsize=(7, 4.5))
            ax.plot(ev_hourly["hour_bj"], ev_hourly["ev_hour_weight"], marker="o", color="#0F766E")
            ax.set_title("EV Hourly Probability Profile")
            ax.set_xlabel("Hour Beijing Time")
            ax.set_ylabel("Hourly weight")
            save_figure(ctx, "fig_05_ev_behavior_probability_profile", fig, ev_hourly)

        if ev_monthly is not None:
            simple_bar(ctx, "fig_06_ev_monthly_kwh_per_vehicle", ev_monthly, "month", "ev_kwh_per_vehicle_day", "EV Monthly kWh per Vehicle Day", "kWh/vehicle/day")

        if spring_actual is not None and not spring_actual.empty:
            data = spring_actual.pivot_table(index="province_cn", columns="model_col", values="MAE").reset_index()
            fig, ax = plt.subplots(figsize=(7, 4.5))
            width = 0.35
            x = np.arange(len(data))
            for i, col in enumerate([c for c in data.columns if c != "province_cn"]):
                ax.bar(x + (i - 0.5) * width, data[col], width, label=col)
            ax.set_xticks(x)
            ax.set_xticklabels(data["province_cn"])
            ax.set_ylabel("MAE MW")
            ax.set_title("Author vs Spring Adjusted Validation")
            ax.legend(fontsize=8)
            save_figure(ctx, "fig_07_author_vs_spring_adjusted_gd_hainan", fig, data)

        national = summary.groupby("year", as_index=False)[["heating_energy_mwh", "cooling_energy_mwh", "ev_energy_mwh", "base_residual_raw_energy_mwh"]].sum()
        fig, ax = plt.subplots(figsize=(8, 4.8))
        bottom = np.zeros(len(national))
        for col in ["base_residual_raw_energy_mwh", "heating_energy_mwh", "cooling_energy_mwh", "ev_energy_mwh"]:
            ax.bar(national["year"], national[col] / 1e6, bottom=bottom / 1e6, label=col)
            bottom += national[col].to_numpy()
        ax.set_title("National Component Stack")
        ax.set_ylabel("TWh")
        ax.legend(fontsize=8)
        save_figure(ctx, "fig_08_national_component_stack", fig, national)

        examples = summary[summary["province_cn"].isin(["广东", "海南", "浙江", "北京"])].copy()
        if not examples.empty:
            fig, ax = plt.subplots(figsize=(8, 4.8))
            data = examples.groupby("province_cn")[["heating_energy_mwh", "cooling_energy_mwh", "ev_energy_mwh"]].sum().reset_index()
            x = np.arange(len(data))
            bottom = np.zeros(len(data))
            for col in ["heating_energy_mwh", "cooling_energy_mwh", "ev_energy_mwh"]:
                ax.bar(x, data[col] / 1e6, bottom=bottom / 1e6, label=col)
                bottom += data[col].to_numpy()
            ax.set_xticks(x)
            ax.set_xticklabels(data["province_cn"])
            ax.set_title("Province Component Stack Examples")
            ax.set_ylabel("TWh")
            ax.legend(fontsize=8)
            save_figure(ctx, "fig_09_province_component_stack_examples", fig, data)

        rank = summary.groupby("province_cn", as_index=False)[["heating_energy_mwh", "cooling_energy_mwh"]].sum()
        rank["thermal_energy_mwh"] = rank["heating_energy_mwh"] + rank["cooling_energy_mwh"]
        simple_bar(ctx, "fig_10_heating_cooling_rankings", rank.nlargest(15, "thermal_energy_mwh"), "province_cn", "thermal_energy_mwh", "Heating + Cooling Rankings", "MWh")

        if ev_qc is not None:
            ev_plot = ev_qc.groupby("province_cn", as_index=False)["ev_peak_mw"].max().nlargest(15, "ev_peak_mw")
            simple_bar(ctx, "fig_11_ev_peak_share_by_province", ev_plot, "province_cn", "ev_peak_mw", "EV Peak Load by Province", "MW")

        if base_qc is not None:
            neg = base_qc.groupby("province_cn", as_index=False)["negative_hours"].sum().nlargest(15, "negative_hours")
            simple_bar(ctx, "fig_12_base_residual_negative_qc", neg, "province_cn", "negative_hours", "Base Residual Negative Hours", "hours")

        if template is not None:
            tmp = template[template["province_cn"].isin(["广东", "海南", "北京", "浙江"])].copy()
            tmp["hour_index"] = tmp.groupby("province_cn").cumcount()
            fig, ax = plt.subplots(figsize=(9, 4.8))
            for province, sub in tmp.groupby("province_cn"):
                ax.plot(sub["hour_index"], sub["base_template_share"], label=province)
            ax.set_title("Base Template 8760 Examples")
            ax.set_xlabel("Hour index")
            ax.set_ylabel("share")
            ax.legend(fontsize=8)
            save_figure(ctx, "fig_13_base_template_8760_examples", fig, tmp)

        if actual is not None and not actual.empty:
            simple_bar(ctx, "fig_14_actual_validation_error_summary", actual, "model_col", "RMSE", "Actual Validation RMSE Summary", "RMSE MW")

        if spring_actual is not None and not spring_actual.empty:
            pivot = spring_actual.pivot_table(index="province_cn", columns="model_col", values="spring_month_error_pct").reset_index()
            save_cols = [c for c in pivot.columns if c != "province_cn"]
            if len(save_cols) >= 2:
                pivot["spring_adjustment_improvement_pct_point"] = pivot[save_cols[0]].abs() - pivot[save_cols[1]].abs()
                simple_bar(ctx, "fig_15_spring_month_validation_improvement", pivot, "province_cn", "spring_adjustment_improvement_pct_point", "Spring Month Validation Improvement", "abs error reduction pct point")

        report1 = """# Method Process Report

- The paper `author_total_load_mw` is preserved as the original historical total load.
- Spring adjustment creates `spring_adjusted_total_load_mw` and conserves annual provincial energy.
- Heating/cooling and EV loads are internal split components, not additions to historical total load.
- Main weather aggregation uses city monthly electricity weights. Population-weighted paper baseline is documented as a method difference when not available.
"""
        report2 = "# Quality Control Report\n\nSee module-level `qc_flags_module*.csv` files and generated diagnostic tables in this run directory.\n"
        report3 = "# Actual Load Validation Report\n\nGuangdong and Hainan actual dispatch loads are validation-only and are not used for national load calibration.\n"
        report4 = "# Future Template Description\n\nThe exported 8760 base residual templates can be used for future annual base energy allocation. This run does not execute 2030-2050 extrapolation.\n"
        write_markdown(ctx.reports_dir / "01_method_process_report.md", report1)
        write_markdown(ctx.reports_dir / "02_quality_control_report.md", report2)
        write_markdown(ctx.reports_dir / "03_actual_load_validation_report.md", report3)
        write_markdown(ctx.reports_dir / "04_future_template_description.md", report4)
        add_qc(flags, MODULE, "INFO", "error_assessment_written", "Module-level error assessment report written", blocking=False)
        add_qc(flags, MODULE, "INFO", "figures_reports_written", "模块 06 图件与报告输出完成", blocking=False)
        write_error_assessment(ctx, flags, actual, actual_monthly, actual_shape, actual_peak, base_qc, template)
    except FileNotFoundError as exc:
        add_qc(flags, MODULE, "SOFT_FAIL", "upstream_missing", "模块 06 缺少上游结果，阻断图件和报告生成", repr(exc), blocking=True)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "module06_exception", "模块 06 执行异常", repr(exc))
    module_exit(flags, ctx, "06", MODULE)


if __name__ == "__main__":
    main()
