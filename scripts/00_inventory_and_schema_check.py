# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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
    province_status,
    read_csv_smart,
    read_power_coefficients,
    rel,
    write_df,
    write_markdown,
    year_sheet_name,
)


MODULE = "00_inventory_and_schema_check"


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value)


def list_source_files(root: Path) -> pd.DataFrame:
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in {"outputs", "reports", "__pycache__"} for part in path.relative_to(root).parts):
            continue
        rows.append(
            {
                "relative_path": rel(path, root),
                "suffix": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
                "last_write_time": pd.Timestamp(path.stat().st_mtime, unit="s").isoformat(),
            }
        )
    return pd.DataFrame(rows)


def workbook_summaries(path: Path, root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        xl = pd.ExcelFile(path)
    except Exception as exc:
        return [{"relative_path": rel(path, root), "source_type": "workbook", "read_status": "ERROR", "error": repr(exc)}]
    for sheet in xl.sheet_names:
        row: dict[str, Any] = {
            "relative_path": rel(path, root),
            "source_type": "workbook",
            "sheet": sheet,
            "read_status": "OK",
        }
        try:
            sample = pd.read_excel(path, sheet_name=sheet, header=None, nrows=8)
            row["sample_rows"] = int(sample.shape[0])
            row["sample_columns"] = int(sample.shape[1])
            row["first_row_values"] = json.dumps([clean_cell(v) for v in sample.iloc[0].tolist()[:20]], ensure_ascii=False)
        except Exception as exc:
            row["read_status"] = "ERROR"
            row["error"] = repr(exc)
        rows.append(row)
    return rows


def csv_summary(path: Path, root: Path) -> dict[str, Any]:
    row: dict[str, Any] = {"relative_path": rel(path, root), "source_type": "csv", "read_status": "OK"}
    try:
        sample = read_csv_smart(path, nrows=8)
        row["sample_rows"] = int(sample.shape[0])
        row["sample_columns"] = int(sample.shape[1])
        row["columns"] = json.dumps(list(map(str, sample.columns)), ensure_ascii=False)
    except Exception as exc:
        row["read_status"] = "ERROR"
        row["error"] = repr(exc)
    return row


def inspect_author_load(path: Path, flags: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    schema_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    try:
        xl = pd.ExcelFile(path)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "author_workbook_readable", "论文总负荷文件无法读取", repr(exc))
        return schema_rows, mapping_rows
    for year in YEARS:
        sheet = year_sheet_name(year)
        if sheet not in xl.sheet_names:
            add_qc(flags, MODULE, "HARD_FAIL", f"author_sheet_{year}", f"论文负荷缺少 sheet: {sheet}")
            continue
        try:
            raw = pd.read_excel(path, sheet_name=sheet, header=None)
            header = raw.iloc[0].tolist()
            dt = pd.to_datetime(raw.iloc[1:, 0], errors="coerce")
            provinces = [normalize_province(v) for v in header[1:] if normalize_province(v) in EXPECTED_PROVINCES]
            status = province_status(header[1:])
            expected_hours = 8784 if year in {2020, 2024} else 8760
            schema_rows.append(
                {
                    "source": "author_load",
                    "relative_path": rel(path),
                    "sheet": sheet,
                    "year": year,
                    "rows_after_header": int(raw.shape[0] - 1),
                    "expected_hours": expected_hours,
                    "valid_datetime_rows": int(dt.notna().sum()),
                    "province_count": len(provinces),
                    "unit_hint": clean_cell(header[0]),
                    "field_interpretation": "Unit: GWh; convert to author_total_load_mw by multiplying 1000",
                    "status": "OK",
                }
            )
            for raw_label in header[1:]:
                norm = normalize_province(raw_label)
                if norm:
                    mapping_rows.append({"source": "author_load", "raw_label": clean_cell(raw_label), "province_cn": norm, "status": "OK" if norm in EXPECTED_PROVINCES else "EXTRA"})
            if raw.shape[0] - 1 != expected_hours or dt.notna().sum() != expected_hours:
                add_qc(flags, MODULE, "HARD_FAIL", f"author_hours_{year}", f"{year} 论文负荷小时数不能解释", schema_rows[-1])
            if len(provinces) != 31 or status["missing"] or status["duplicated"]:
                add_qc(flags, MODULE, "HARD_FAIL", f"author_provinces_{year}", f"{year} 论文负荷省份映射失败或重复", status)
        except Exception as exc:
            add_qc(flags, MODULE, "HARD_FAIL", f"author_sheet_read_{year}", f"{sheet} 读取失败", repr(exc))
    return schema_rows, mapping_rows


def inspect_spring(path: Path, flags: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    schema_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    try:
        df = pd.read_excel(path, sheet_name="省级汇总")
        ratio = pd.to_numeric(df["平均较相邻月均值低幅"], errors="coerce")
        factors = 1.0 - ratio
        schema_rows.append(
            {
                "source": "spring_bias",
                "relative_path": rel(path),
                "sheet": "省级汇总",
                "rows": int(len(df)),
                "province_count_including_national": int(df["省份"].nunique()),
                "factor_min": float(factors.min()),
                "factor_max": float(factors.max()),
                "field_interpretation": "平均较相邻月均值低幅 is drop_ratio; spring_factor = 1 - drop_ratio",
                "status": "OK",
            }
        )
        for raw_label in df["省份"]:
            norm = normalize_province(raw_label)
            if norm:
                mapping_rows.append({"source": "spring_bias", "raw_label": clean_cell(raw_label), "province_cn": norm, "status": "OK" if norm in EXPECTED_PROVINCES or norm == "全国" else "EXTRA"})
        status = province_status(df["省份"])
        if status["missing"]:
            add_qc(flags, MODULE, "HARD_FAIL", "spring_province_coverage", "春节偏差表缺少省份", status)
        missing_ratio = df.loc[df["省份"].map(normalize_province).isin(EXPECTED_PROVINCES) & ratio.isna(), "省份"].astype(str).tolist()
        if missing_ratio:
            add_qc(flags, MODULE, "WARN", "spring_factor_missing_values", "春节偏差表存在省份但缺少可用降幅系数；模块 01 将阻断校准", missing_ratio, blocking=False)
        if (factors < 0.60).any() or (factors > 1.10).any():
            add_qc(flags, MODULE, "SOFT_FAIL", "spring_factor_range", "春节系数超出 0.60-1.10 建议范围", {"min": float(factors.min()), "max": float(factors.max())}, blocking=True)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "spring_bias_readable", "春节偏差文件无法读取或字段缺失", repr(exc))
    return schema_rows, mapping_rows


def inspect_power_coefficients(ctx, flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        coeff = read_power_coefficients(ctx)
        subset = coeff[coeff["year"].isin(YEARS)]
        heat_missing = subset["p_heat_gwh_per_degree_day"].isna().sum()
        cool_missing = subset["p_cool_gwh_per_degree_day"].isna().sum()
        rows.append(
            {
                "source": "power_coefficients",
                "relative_path": rel(get_input_paths(ctx.config)["power_coefficients"]),
                "rows_2020_2024": int(len(subset)),
                "heat_missing": int(heat_missing),
                "cool_missing": int(cool_missing),
                "province_year_expected": 31 * len(YEARS),
                "field_interpretation": "GWh/degree C/day; hourly load MW = coefficient * degree_day_hour * 1000",
                "status": "OK" if heat_missing == 0 and cool_missing == 0 and len(subset) >= 31 * len(YEARS) else "CHECK",
            }
        )
        if heat_missing or cool_missing or len(subset) < 31 * len(YEARS):
            add_qc(flags, MODULE, "HARD_FAIL", "power_coefficients_coverage", "Power coefficient for heating/cooling 缺失", rows[-1])
        dedicated = get_input_paths(ctx.config)["power_coefficients"]
        try:
            cooling_sheet = pd.read_excel(dedicated, sheet_name="Power coefficient for cooling", header=None, nrows=2)
            years = [int(round(float(v))) for v in cooling_sheet.iloc[1].tolist()[1:] if pd.notna(v)]
            if 2024 not in years:
                add_qc(flags, MODULE, "WARN", "cooling_standalone_missing_2024", "单独 cooling sheet 缺少 2024；已计划从 combined 区块补齐", {"cooling_sheet_years": years}, blocking=False)
        except Exception:
            pass
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "power_coefficients_readable", "冷热系数无法解析", repr(exc))
    return rows


def inspect_city_weights(path: Path, flags: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    schema_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    try:
        df = pd.read_excel(path, sheet_name="城市月度占比_长表")
        province_col = "省份中文"
        month_col = "月份"
        weight_col = "城市省内占比"
        df["province_cn"] = df[province_col].map(normalize_province)
        grouped = df.groupby(["province_cn", month_col], dropna=False)[weight_col].sum().reset_index(name="weight_sum")
        schema_rows.append(
            {
                "source": "city_weights",
                "relative_path": rel(path),
                "sheet": "城市月度占比_长表",
                "rows": int(len(df)),
                "province_count": int(df["province_cn"].nunique()),
                "province_month_count": int(len(grouped)),
                "weight_sum_min": float(grouped["weight_sum"].min()),
                "weight_sum_max": float(grouped["weight_sum"].max()),
                "status": "OK",
            }
        )
        for raw_label in df[province_col].drop_duplicates():
            norm = normalize_province(raw_label)
            mapping_rows.append({"source": "city_weights", "raw_label": clean_cell(raw_label), "province_cn": norm, "status": "OK" if norm in EXPECTED_PROVINCES else "EXTRA"})
        if df["province_cn"].nunique() != 31 or (grouped["weight_sum"].sub(1.0).abs() > 1e-6).any():
            add_qc(flags, MODULE, "SOFT_FAIL", "city_weight_closure", "城市月度省内权重覆盖或闭合异常", schema_rows[-1], blocking=True)
    except Exception as exc:
        add_qc(flags, MODULE, "SOFT_FAIL", "city_weights_readable", "城市月度权重无法读取", repr(exc), blocking=True)
    return schema_rows, mapping_rows


def inspect_ev_stock(path: Path, flags: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    try:
        df = pd.read_excel(path)
        df["province_cn"] = df["地区"].map(normalize_province)
        df["年份"] = pd.to_numeric(df["年份"], errors="coerce").astype("Int64")
        counts = df.groupby("年份")["province_cn"].nunique().to_dict()
        rows.append(
            {
                "source": "ev_stock",
                "relative_path": rel(path),
                "rows": int(len(df)),
                "year_min": int(df["年份"].min()),
                "year_max": int(df["年份"].max()),
                "province_count": int(df["province_cn"].nunique()),
                "rows_by_year": json.dumps({str(k): int(v) for k, v in counts.items()}, ensure_ascii=False),
                "field_interpretation": "新能源汽车保有量（辆） -> nev_stock; 2024 uses 2021-2023 CAGR extrapolation",
                "status": "OK",
            }
        )
        for raw_label in df["地区"].drop_duplicates():
            norm = normalize_province(raw_label)
            mapping_rows.append({"source": "ev_stock", "raw_label": clean_cell(raw_label), "province_cn": norm, "status": "OK" if norm in EXPECTED_PROVINCES else "EXTRA"})
        for year in range(2017, 2024):
            if counts.get(year, 0) != 31:
                add_qc(flags, MODULE, "HARD_FAIL", f"ev_stock_{year}", f"EV 保有量 {year} 年未覆盖 31 省", counts)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "ev_stock_readable", "EV 保有量文件无法读取", repr(exc))
    return rows, mapping_rows


def inspect_actual_loads(paths: dict[str, Path], flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        hainan = pd.read_excel(paths["actual_hainan"])
        hdt = pd.to_datetime(hainan.iloc[:, 0], errors="coerce")
        hval = pd.to_numeric(hainan.iloc[:, 1], errors="coerce")
        rows.append(
            {
                "source": "actual_hainan",
                "relative_path": rel(paths["actual_hainan"]),
                "rows": int(len(hainan)),
                "valid_datetime_rows": int(hdt.notna().sum()),
                "min_datetime": str(hdt.min()),
                "max_datetime": str(hdt.max()),
                "non_positive_values": int((hval <= 0).sum()),
                "unit_interpretation": "MW",
                "status": "OK",
            }
        )
        if hdt.notna().sum() < 43848:
            add_qc(flags, MODULE, "WARN", "actual_hainan_coverage", "海南真实负荷有效小时少于 2020-2024 应有小时数", rows[-1])
    except Exception as exc:
        add_qc(flags, MODULE, "WARN", "actual_hainan_readable", "海南真实负荷无法读取；不影响校准，只影响验证", repr(exc))
    try:
        gd = pd.read_excel(paths["actual_guangdong"], sheet_name=0, header=None)
        body = gd.iloc[2:, :]
        dates = pd.to_datetime(body.iloc[:, 1], errors="coerce")
        values = body.iloc[:, 2:98].apply(pd.to_numeric, errors="coerce")
        rows.append(
            {
                "source": "actual_guangdong",
                "relative_path": rel(paths["actual_guangdong"]),
                "rows_days": int(len(body)),
                "valid_dates": int(dates.notna().sum()),
                "quarter_hour_columns": int(values.shape[1]),
                "min_date": str(dates.min()),
                "max_date": str(dates.max()),
                "non_positive_cells": int((values <= 0).sum().sum()),
                "unit_interpretation": "万千瓦 -> MW by multiplying 10",
                "status": "OK",
            }
        )
        if int((values <= 0).sum().sum()) > 0:
            add_qc(flags, MODULE, "WARN", "actual_guangdong_non_positive", "广东真实负荷存在非正值；验证时剔除，不用于校准", rows[-1])
    except Exception as exc:
        add_qc(flags, MODULE, "WARN", "actual_guangdong_readable", "广东真实负荷无法读取；不影响校准，只影响验证", repr(exc))
    return rows


def inspect_weather_and_shapes(paths: dict[str, Path], flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    weather_root = paths["weather_root"]
    var_patterns = {
        "t2m": ("2m_temp", "t2m_{year}.nc"),
        "d2m": ("2m_dewpoint", "dt2m_{year}.nc"),
        "u10": ("10mu", "u_{year}.nc"),
        "v10": ("10mv", "v_{year}.nc"),
        "ssrd": ("surface_solar_radi", "ssrd_{year}.nc"),
        "sp": ("surface_press", "sp_{year}.nc"),
    }
    for var, (folder, pattern) in var_patterns.items():
        missing = []
        present = []
        for year in YEARS:
            path = weather_root / folder / pattern.format(year=year)
            (present if path.exists() else missing).append(year)
        rows.append({"source": "era5", "variable": var, "folder": str(weather_root / folder), "present_years": json.dumps(present), "missing_years": json.dumps(missing), "status": "OK" if not missing else "CHECK"})
        if missing:
            add_qc(flags, MODULE, "SOFT_FAIL", f"era5_{var}_years", f"ERA5 {var} 缺少年份文件", {"missing": missing}, blocking=True)
    missing_2019 = []
    for var, (folder, pattern) in var_patterns.items():
        path = weather_root / folder / pattern.format(year=2019)
        if not path.exists():
            missing_2019.append(str(path))
    if missing_2019:
        add_qc(flags, MODULE, "WARN", "era5_2019_boundary_missing", "严格 UTC->北京时间对齐时，模块 02 需要 2019 年末 8 小时；当前缺少 2019 文件，将在模块 02 阻断", missing_2019, blocking=False)
    try:
        import geopandas as gpd

        prov = gpd.read_file(paths["province_shapefile"])
        city = gpd.read_file(paths["city_shapefile"], encoding="gbk")
        rows.append({"source": "province_shapefile", "relative_path": rel(paths["province_shapefile"]), "features": int(len(prov)), "crs": str(prov.crs), "encoding": "default/utf-8", "status": "OK"})
        rows.append({"source": "city_shapefile", "relative_path": rel(paths["city_shapefile"]), "features": int(len(city)), "crs": str(city.crs), "encoding": "gbk", "status": "OK"})
    except Exception as exc:
        add_qc(flags, MODULE, "SOFT_FAIL", "shapefile_readable", "省/市矢量边界无法按约定编码读取", repr(exc), blocking=True)
    return rows


def main() -> None:
    args = parse_args("Module 00: inventory, schema check, province mapping and QC")
    ctx = init_context(args, MODULE)
    paths = get_input_paths(ctx.config)
    flags: list[dict[str, Any]] = []

    inventory = list_source_files(ctx.root)
    write_df(inventory, ctx.run_dir / "source_data_inventory.csv")

    schema_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    for key, path in paths.items():
        if key == "weather_root":
            if not path.exists():
                add_qc(flags, MODULE, "SOFT_FAIL", "weather_root_exists", "ERA5 根目录不存在", str(path), blocking=True)
            continue
        if not path.exists():
            severity = "HARD_FAIL" if key in {"author_load", "spring_bias", "power_coefficients", "city_weights", "ev_stock"} else "WARN"
            add_qc(flags, MODULE, severity, f"path_exists_{key}", "输入路径不存在", str(path), blocking=severity == "HARD_FAIL")
    for path in [p for p in paths.values() if isinstance(p, Path) and p.exists() and p.suffix.lower() in {".xlsx", ".xls"}]:
        schema_rows.extend(workbook_summaries(path, ctx.root))
    for path in [p for p in paths.values() if isinstance(p, Path) and p.exists() and p.suffix.lower() == ".csv"]:
        schema_rows.append(csv_summary(path, ctx.root))

    author_schema, author_mapping = inspect_author_load(paths["author_load"], flags)
    schema_rows.extend(author_schema)
    mapping_rows.extend(author_mapping)
    spring_schema, spring_mapping = inspect_spring(paths["spring_bias"], flags)
    schema_rows.extend(spring_schema)
    mapping_rows.extend(spring_mapping)
    schema_rows.extend(inspect_power_coefficients(ctx, flags))
    city_schema, city_mapping = inspect_city_weights(paths["city_weights"], flags)
    schema_rows.extend(city_schema)
    mapping_rows.extend(city_mapping)
    ev_schema, ev_mapping = inspect_ev_stock(paths["ev_stock"], flags)
    schema_rows.extend(ev_schema)
    mapping_rows.extend(ev_mapping)
    schema_rows.extend(inspect_actual_loads(paths, flags))
    schema_rows.extend(inspect_weather_and_shapes(paths, flags))

    mapping_df = pd.DataFrame(mapping_rows).drop_duplicates()
    schema_df = pd.DataFrame(schema_rows)
    write_df(schema_df, ctx.tables_dir / "schema_check_report.csv")
    write_df(mapping_df, ctx.tables_dir / "province_name_mapping_report.csv")

    hard_count = sum(1 for f in flags if f["severity"] == "HARD_FAIL")
    soft_count = sum(1 for f in flags if f["severity"] == "SOFT_FAIL")
    warn_count = sum(1 for f in flags if f["severity"] == "WARN")
    report = f"""# Module 00 Path and Schema Check

Run directory: `{ctx.run_dir}`

## Summary
- Source files scanned: {len(inventory)}
- Schema rows: {len(schema_df)}
- Province mapping rows: {len(mapping_df)}
- QC counts: HARD_FAIL={hard_count}, SOFT_FAIL={soft_count}, WARN={warn_count}

## Important Notes
- `author_total_load_mw` is derived only from the paper workbook and preserves the original author load as a separate field.
- Spring bias workbook field `平均较相邻月均值低幅` is interpreted as `drop_ratio`; `spring_factor = 1 - drop_ratio`.
- Guangdong/Hainan actual load files are marked validation-only and must not be used for calibration.
- ERA5 strict UTC-to-Beijing alignment is enforced in module 02; missing 2019 boundary files are recorded here as a warning and will block module 02.
"""
    write_markdown(ctx.run_dir / "path_check_report.md", report)
    module_exit(flags, ctx, "00", MODULE)


if __name__ == "__main__":
    main()
