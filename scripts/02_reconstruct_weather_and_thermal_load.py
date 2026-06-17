# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import gc
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from power_curve_common import (
    EXPECTED_PROVINCES,
    YEARS,
    PROJECT_ROOT,
    add_qc,
    get_input_paths,
    init_context,
    module_exit,
    normalize_city,
    normalize_province,
    read_power_coefficients,
    read_required_table,
    write_df,
    write_markdown,
)


MODULE = "02_reconstruct_weather_and_thermal_load"

ERA5_SPECS: dict[str, dict[str, str]] = {
    "t2m": {"folder": "2m_temp", "pattern": "t2m_{year}.nc", "output": "temperature_c", "unit_transform": "kelvin_to_c"},
    "d2m": {"folder": "2m_dewpoint", "pattern": "dt2m_{year}.nc", "output": "dewpoint_c", "unit_transform": "kelvin_to_c"},
    "u10": {"folder": "10mu", "pattern": "u_{year}.nc", "output": "u10_ms", "unit_transform": "none"},
    "v10": {"folder": "10mv", "pattern": "v_{year}.nc", "output": "v10_ms", "unit_transform": "none"},
    "ssrd": {"folder": "surface_solar_radi", "pattern": "ssrd_{year}.nc", "output": "solar_wm2", "unit_transform": "j_m2_to_w_m2"},
    "sp": {"folder": "surface_press", "pattern": "sp_{year}.nc", "output": "surface_pressure_pa", "unit_transform": "none"},
}

BAIT_PARAMS = {
    "x": 0.012,
    "y": 0.2,
    "z": 0.05,
    "t_star_c": 16.0,
    "smooth_lambda": 0.10232,
    "smooth_window_hours": 48,
    "blend_lower_c": 15.0,
    "blend_upper_c": 23.0,
}

REQUIRED_WEATHER_COLS = ["temperature_c", "dewpoint_c", "u10_ms", "v10_ms", "solar_wm2", "surface_pressure_pa"]

NORTH_HEATING_PROVINCES = {
    "北京",
    "甘肃",
    "河北",
    "河南",
    "黑龙江",
    "吉林",
    "辽宁",
    "内蒙古",
    "宁夏",
    "青海",
    "陕西",
    "山东",
    "山西",
    "天津",
    "新疆",
    "西藏",
}

THRESHOLD_VALUES = {
    "north": {"heat_threshold_c": 14.713, "cool_threshold_c": 22.253},
    "south": {"heat_threshold_c": 16.818, "cool_threshold_c": 22.631},
}


def parse_module_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Module 02: city-weighted ERA5 exposure and BAIT/HDD/CDD reconstruction")
    parser.add_argument("--run-dir", type=str, default=None)
    parser.add_argument("--config", type=str, default=str(PROJECT_ROOT / "config" / "run_config.yaml"))
    parser.add_argument("--only-weights", action="store_true", help="Build city-ERA5 mapping and point weights only")
    parser.add_argument("--smoke-province", type=str, default=None, help="Optional single province for smoke extraction")
    parser.add_argument("--smoke-year", type=int, default=None, help="Optional target Beijing year for smoke extraction")
    parser.add_argument("--smoke-month", type=int, default=None, help="Optional target Beijing month for smoke extraction")
    parser.add_argument("--smoke-variable", choices=sorted(ERA5_SPECS), default=None, help="Optional single ERA5 variable smoke test")
    return parser.parse_args()


def current_memory_mb() -> float | None:
    try:
        import psutil  # type: ignore

        return float(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)
    except Exception:
        return None


def era5_file(weather_root: Path, var: str, year: int) -> Path:
    spec = ERA5_SPECS[var]
    return weather_root / spec["folder"] / spec["pattern"].format(year=year)


def required_era5_files(weather_root: Path) -> tuple[pd.DataFrame, list[Path], list[Path]]:
    rows: list[dict[str, Any]] = []
    missing_core: list[Path] = []
    missing_boundary_2019: list[Path] = []
    for year in [2019] + YEARS:
        for var in ERA5_SPECS:
            path = era5_file(weather_root, var, year)
            row = {
                "year": year,
                "variable": var,
                "path": str(path),
                "exists": path.exists(),
                "purpose": "2019_boundary_hours" if year == 2019 else "target_2020_2024",
            }
            rows.append(row)
            if not path.exists() and year == 2019:
                missing_boundary_2019.append(path)
            elif not path.exists():
                missing_core.append(path)
    return pd.DataFrame(rows), missing_core, missing_boundary_2019


def read_city_shapefile(path: Path):
    import geopandas as gpd

    last_exc: Exception | None = None
    for encoding in ["gbk", "gb18030"]:
        try:
            return gpd.read_file(path, encoding=encoding)
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return gpd.read_file(path)


def read_sample_era5_grid(ctx) -> tuple[np.ndarray, np.ndarray]:
    import xarray as xr

    sample_path = era5_file(Path(str(ctx.config["weather_root"])), "t2m", 2020)
    ds = xr.open_dataset(sample_path)
    try:
        lat_values = np.asarray(ds["latitude"].values)
        lon_values = np.asarray(ds["longitude"].values)
    finally:
        ds.close()
    return lat_values, lon_values


def load_city_weights(ctx, flags: list[dict]) -> pd.DataFrame | None:
    path = get_input_paths(ctx.config)["city_weights"]
    try:
        weights = pd.read_excel(path, sheet_name="城市月度占比_长表")
    except Exception as exc:
        add_qc(flags, MODULE, "SOFT_FAIL", "city_weight_read", "城市月度用电权重读取失败", repr(exc), blocking=True)
        return None

    required = ["省份中文", "城市中文", "月份", "城市月度用电量", "城市省内占比"]
    missing_cols = [col for col in required if col not in weights.columns]
    if missing_cols:
        add_qc(flags, MODULE, "SOFT_FAIL", "city_weight_columns", "城市月度权重表缺少必要字段", missing_cols, blocking=True)
        return None

    out = pd.DataFrame(
        {
            "province_cn": weights["省份中文"].map(normalize_province),
            "city_cn": weights["城市中文"].map(normalize_city),
            "month": pd.to_numeric(weights["月份"], errors="coerce"),
            "city_power_mwh": pd.to_numeric(weights["城市月度用电量"], errors="coerce"),
            "city_weight_in_province": pd.to_numeric(weights["城市省内占比"], errors="coerce"),
        }
    ).dropna(subset=["province_cn", "city_cn", "month", "city_weight_in_province"])
    out["month"] = out["month"].astype(int)
    out = out[(out["month"] >= 1) & (out["month"] <= 12)].copy()
    out.sort_values(["province_cn", "city_cn", "month"], inplace=True)
    write_df(out, ctx.tables_dir / "city_monthly_power_weight.csv")

    closure = out.groupby(["province_cn", "month"], as_index=False).agg(
        city_count=("city_cn", "nunique"),
        city_weight_sum=("city_weight_in_province", "sum"),
        city_power_mwh_sum=("city_power_mwh", "sum"),
    )
    closure["abs_error"] = closure["city_weight_sum"].sub(1.0).abs()
    write_df(closure, ctx.tables_dir / "city_monthly_power_weight_qc.csv")
    bad = closure[closure["abs_error"] > 1e-6]
    if not bad.empty:
        add_qc(
            flags,
            MODULE,
            "SOFT_FAIL",
            "city_weight_closure",
            "城市月度用电权重在省-月层面不闭合，停止后续 ERA5 抽取",
            bad.to_dict("records"),
            blocking=True,
        )
        return None
    add_qc(flags, MODULE, "INFO", "city_weight_closure", "城市月度用电权重省-月闭合通过", {"province_months": int(len(closure))})
    return out


def build_city_era5_mapping(ctx, city_weights: pd.DataFrame, flags: list[dict]) -> pd.DataFrame | None:
    try:
        import geopandas as gpd

        paths = get_input_paths(ctx.config)
        lat_values, lon_values = read_sample_era5_grid(ctx)
        lon_grid, lat_grid = np.meshgrid(lon_values, lat_values)
        grid = pd.DataFrame(
            {
                "lat_idx": np.repeat(np.arange(len(lat_values)), len(lon_values)).astype(int),
                "lon_idx": np.tile(np.arange(len(lon_values)), len(lat_values)).astype(int),
                "era5_latitude": lat_grid.ravel().astype(float),
                "era5_longitude": lon_grid.ravel().astype(float),
            }
        )
        points = gpd.GeoDataFrame(
            grid,
            geometry=gpd.points_from_xy(grid["era5_longitude"], grid["era5_latitude"]),
            crs="EPSG:4326",
        )
        city = read_city_shapefile(paths["city_shapefile"])
        if city.crs is None:
            add_qc(flags, MODULE, "SOFT_FAIL", "city_shapefile_crs", "市级边界缺少 CRS，不能安全匹配 ERA5 经纬度", blocking=True)
            return None
        city = city.to_crs("EPSG:4326")
        if "省" not in city.columns or "name" not in city.columns:
            add_qc(flags, MODULE, "SOFT_FAIL", "city_shapefile_columns", "市级边界缺少 `省` 或 `name` 字段", list(city.columns), blocking=True)
            return None
        city["province_cn"] = city["省"].map(normalize_province)
        city["city_cn"] = city["name"].map(normalize_city)
        needed = city_weights[["province_cn", "city_cn"]].drop_duplicates()
        city_match = city.merge(needed, on=["province_cn", "city_cn"], how="inner")
        matched = city_match[["province_cn", "city_cn"]].drop_duplicates()
        missing_city = needed.merge(matched, on=["province_cn", "city_cn"], how="left", indicator=True)
        missing_city = missing_city[missing_city["_merge"].eq("left_only")][["province_cn", "city_cn"]]
        if not missing_city.empty:
            write_df(missing_city, ctx.tables_dir / "city_weight_without_shapefile_match.csv")
            add_qc(
                flags,
                MODULE,
                "SOFT_FAIL",
                "city_weight_shapefile_match",
                "部分城市权重无法匹配市级边界，不能静默丢弃",
                missing_city.head(100).to_dict("records"),
                blocking=True,
            )
            return None
        if city_match.empty:
            add_qc(flags, MODULE, "HARD_FAIL", "city_shape_weight_match_empty", "城市权重无法匹配任何市级边界")
            return None

        bbox = city_match.total_bounds
        points = points[
            points["era5_longitude"].between(bbox[0] - 0.25, bbox[2] + 0.25)
            & points["era5_latitude"].between(bbox[1] - 0.25, bbox[3] + 0.25)
        ].copy()
        join_cols = ["province_cn", "city_cn", "geometry"]
        try:
            joined = gpd.sjoin(points, city_match[join_cols], how="inner", predicate="within")
        except TypeError:
            joined = gpd.sjoin(points, city_match[join_cols], how="inner", op="within")
        mapping = pd.DataFrame(joined.drop(columns=["geometry", "index_right"], errors="ignore"))
        mapping["mapping_method"] = "grid_center_within_city_boundary"

        city_counts = (
            mapping[["province_cn", "city_cn", "lat_idx", "lon_idx"]]
            .drop_duplicates()
            .groupby(["province_cn", "city_cn"], as_index=False)
            .size()
            .rename(columns={"size": "city_era5_point_count"})
        )
        missing_points = needed.merge(city_counts[["province_cn", "city_cn"]], on=["province_cn", "city_cn"], how="left", indicator=True)
        missing_points = missing_points[missing_points["_merge"].eq("left_only")][["province_cn", "city_cn"]]
        fallback_rows: list[dict[str, Any]] = []
        if not missing_points.empty:
            city_fallback = city_match.merge(missing_points, on=["province_cn", "city_cn"], how="inner")
            reps = city_fallback.geometry.representative_point()
            for (_, row), point in zip(city_fallback.iterrows(), reps):
                lat_idx = int(np.abs(lat_values - point.y).argmin())
                lon_idx = int(np.abs(lon_values - point.x).argmin())
                fallback_rows.append(
                    {
                        "province_cn": row["province_cn"],
                        "city_cn": row["city_cn"],
                        "lat_idx": lat_idx,
                        "lon_idx": lon_idx,
                        "era5_latitude": float(lat_values[lat_idx]),
                        "era5_longitude": float(lon_values[lon_idx]),
                        "mapping_method": "nearest_representative_for_city_without_grid_center",
                    }
                )
            write_df(pd.DataFrame(fallback_rows), ctx.tables_dir / "fallback_city_era5_mapping.csv")
            add_qc(
                flags,
                MODULE,
                "WARN",
                "city_without_era5_center_fallback",
                "部分城市边界内没有 ERA5 网格中心，使用城市 representative point 最近 ERA5 点",
                missing_points.to_dict("records"),
                blocking=False,
            )
        if fallback_rows:
            mapping = pd.concat([mapping, pd.DataFrame(fallback_rows)], ignore_index=True)
        mapping = mapping[["province_cn", "city_cn", "lat_idx", "lon_idx", "era5_latitude", "era5_longitude", "mapping_method"]].drop_duplicates()
        mapping["lat_idx"] = mapping["lat_idx"].astype(int)
        mapping["lon_idx"] = mapping["lon_idx"].astype(int)

        city_counts = (
            mapping[["province_cn", "city_cn", "lat_idx", "lon_idx"]]
            .drop_duplicates()
            .groupby(["province_cn", "city_cn"], as_index=False)
            .size()
            .rename(columns={"size": "city_era5_point_count"})
        )
        mapping = mapping.merge(city_counts, on=["province_cn", "city_cn"], how="left")
        contributing = (
            mapping.groupby(["lat_idx", "lon_idx"], as_index=False)
            .agg(contributing_city_count=("city_cn", "nunique"))
        )
        mapping = mapping.merge(contributing, on=["lat_idx", "lon_idx"], how="left")
        overlaps = mapping[mapping["contributing_city_count"].gt(1)].copy()
        if not overlaps.empty:
            write_df(overlaps, ctx.tables_dir / "overlapping_era5_city_assignments.csv")
            add_qc(
                flags,
                MODULE,
                "WARN",
                "era5_point_multiple_city_assignments",
                "部分 ERA5 点被多个城市覆盖；后续按省-月-点层面汇总权重",
                {"overlap_rows": int(len(overlaps)), "overlap_points": int(overlaps[["lat_idx", "lon_idx"]].drop_duplicates().shape[0])},
                blocking=False,
            )

        count_qc = city_counts.sort_values(["province_cn", "city_cn"]).copy()
        write_df(count_qc, ctx.tables_dir / "city_era5_point_count_qc.csv")
        write_df(
            mapping.sort_values(["province_cn", "city_cn", "lat_idx", "lon_idx"]),
            ctx.tables_dir / "era5_city_grid_point_mapping.csv",
        )
        return mapping
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "city_grid_point_mapping", "市级边界到 ERA5 网格点映射失败", repr(exc))
        return None


def build_point_weights(ctx, city_weights: pd.DataFrame, mapping: pd.DataFrame, flags: list[dict]) -> pd.DataFrame | None:
    expanded = city_weights.merge(mapping, on=["province_cn", "city_cn"], how="left")
    if expanded[["lat_idx", "lon_idx", "city_era5_point_count"]].isna().any().any():
        missing = expanded[expanded[["lat_idx", "lon_idx", "city_era5_point_count"]].isna().any(axis=1)]
        add_qc(
            flags,
            MODULE,
            "HARD_FAIL",
            "city_weight_grid_mapping_missing",
            "城市月度权重仍有城市无法映射到 ERA5 点",
            missing[["province_cn", "city_cn"]].drop_duplicates().to_dict("records"),
        )
        return None
    expanded["city_point_weight"] = expanded["city_weight_in_province"] / expanded["city_era5_point_count"]
    city_equal_qc = expanded.groupby(["province_cn", "city_cn", "month"], as_index=False).agg(
        city_era5_point_count=("city_era5_point_count", "first"),
        city_weight_in_province=("city_weight_in_province", "first"),
        point_rows=("city_point_weight", "size"),
        point_weight_min=("city_point_weight", "min"),
        point_weight_max=("city_point_weight", "max"),
    )
    city_equal_qc["expected_point_weight"] = city_equal_qc["city_weight_in_province"] / city_equal_qc["city_era5_point_count"]
    city_equal_qc["max_abs_error"] = np.maximum(
        (city_equal_qc["point_weight_min"] - city_equal_qc["expected_point_weight"]).abs(),
        (city_equal_qc["point_weight_max"] - city_equal_qc["expected_point_weight"]).abs(),
    )
    write_df(city_equal_qc, ctx.tables_dir / "city_era5_point_weight_qc.csv")
    bad_city = city_equal_qc[city_equal_qc["max_abs_error"] > 1e-12]
    if not bad_city.empty:
        add_qc(flags, MODULE, "HARD_FAIL", "city_point_weight_not_equal", "同一城市内部 ERA5 点权重不一致", bad_city.head(50).to_dict("records"))
        return None

    point_weights = (
        expanded.groupby(["province_cn", "month", "lat_idx", "lon_idx", "era5_latitude", "era5_longitude"], as_index=False)
        .agg(
            point_weight_raw=("city_point_weight", "sum"),
            contributing_city_count=("city_cn", "nunique"),
            contributing_cities=("city_cn", lambda s: ";".join(sorted(set(map(str, s))))),
        )
    )
    closure = point_weights.groupby(["province_cn", "month"], as_index=False).agg(
        point_count=("point_weight_raw", "size"),
        point_weight_raw_sum=("point_weight_raw", "sum"),
    )
    unique_point_count = (
        point_weights[["province_cn", "month", "lat_idx", "lon_idx"]]
        .drop_duplicates()
        .groupby(["province_cn", "month"], as_index=False)
        .size()
        .rename(columns={"size": "contributing_grid_points"})
    )
    closure = closure.merge(unique_point_count, on=["province_cn", "month"], how="left")
    closure["raw_abs_error"] = closure["point_weight_raw_sum"].sub(1.0).abs()
    if (closure["point_weight_raw_sum"] <= 0).any():
        bad = closure[closure["point_weight_raw_sum"] <= 0]
        add_qc(flags, MODULE, "HARD_FAIL", "era5_point_weight_nonpositive_sum", "省-月 ERA5 点原始权重和非正", bad.to_dict("records"))
        return None
    point_weights = point_weights.merge(closure[["province_cn", "month", "point_weight_raw_sum"]], on=["province_cn", "month"], how="left")
    point_weights["point_weight"] = point_weights["point_weight_raw"] / point_weights["point_weight_raw_sum"]
    final_closure = point_weights.groupby(["province_cn", "month"], as_index=False).agg(
        point_count=("point_weight", "size"),
        point_weight_sum=("point_weight", "sum"),
        point_weight_raw_sum=("point_weight_raw", "sum"),
        max_contributing_city_count=("contributing_city_count", "max"),
    )
    final_closure = final_closure.merge(unique_point_count, on=["province_cn", "month"], how="left")
    final_closure["final_abs_error"] = final_closure["point_weight_sum"].sub(1.0).abs()
    final_closure["renormalized"] = final_closure["point_weight_raw_sum"].sub(1.0).abs() > 1e-6
    write_df(final_closure, ctx.tables_dir / "era5_grid_point_weight_qc.csv")
    renorm = final_closure[final_closure["renormalized"]]
    if not renorm.empty:
        add_qc(
            flags,
            MODULE,
            "WARN",
            "era5_point_weight_renormalized",
            "省-月 ERA5 点原始权重和偏离 1，已记录并进行显式重归一化",
            renorm.to_dict("records"),
            blocking=False,
        )
    bad_final = final_closure[final_closure["final_abs_error"] > 1e-6]
    if not bad_final.empty:
        add_qc(flags, MODULE, "HARD_FAIL", "era5_point_weight_not_closed", "省-月 ERA5 最终点权重未闭合", bad_final.to_dict("records"))
        return None
    point_weights = point_weights[
        [
            "province_cn",
            "month",
            "lat_idx",
            "lon_idx",
            "era5_latitude",
            "era5_longitude",
            "point_weight_raw",
            "point_weight",
            "contributing_city_count",
            "contributing_cities",
            "point_weight_raw_sum",
        ]
    ].sort_values(["province_cn", "month", "lat_idx", "lon_idx"])
    write_df(point_weights, ctx.tables_dir / "era5_grid_point_weights_by_month.csv")
    add_qc(flags, MODULE, "INFO", "era5_point_weight_closure", "省-月 ERA5 点权重闭合通过", {"province_months": int(len(final_closure))})
    return point_weights


def build_spatial_weights(ctx, flags: list[dict]) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
    city_weights = load_city_weights(ctx, flags)
    if city_weights is None:
        return None, None, None
    mapping = build_city_era5_mapping(ctx, city_weights, flags)
    if mapping is None:
        return city_weights, None, None
    point_weights = build_point_weights(ctx, city_weights, mapping, flags)
    return city_weights, mapping, point_weights


def write_boundary_fallback_report(ctx, missing: list[Path], flags: list[dict]) -> None:
    text = "# Module 02 ERA5 Boundary Fallback\n\n"
    text += "ERA5 `valid_time` is interpreted as UTC. Strict Beijing-time alignment maps 2020-01-01 00:00-07:00 Beijing time to 2019-12-31 16:00-23:00 UTC.\n\n"
    text += "The following 2019 boundary files are missing. Because `allow_2019_boundary_fallback: true`, the affected eight target hours use the next-day same-local-hour substitute: Beijing time 2020-01-02 00:00-07:00, which maps to UTC 2020-01-01 16:00-23:00. This preserves the local diurnal phase and avoids replacing early-morning hours with UTC 2020-01-01 00:00-07:00, i.e. Beijing 08:00-15:00.\n\n"
    for path in missing:
        text += f"- `{path}`\n"
    write_markdown(ctx.reports_dir / "era5_boundary_fallback_report.md", text)
    add_qc(
        flags,
        MODULE,
        "WARN",
        "era5_2019_boundary_fallback",
        "缺少 2019 年末 ERA5 边界小时；按配置使用次日同本地小时替代，即北京时间 2020-01-02 00:00-07:00 对应的 UTC 2020-01-01 16:00-23:00",
        {"missing_files": [str(p) for p in missing], "fallback_method": "next_day_same_local_hour"},
        blocking=False,
    )


def build_target_times(ctx, smoke_year: int | None, smoke_month: int | None, use_boundary_fallback: bool) -> pd.DataFrame:
    if smoke_year and smoke_month:
        start = pd.Timestamp(year=int(smoke_year), month=int(smoke_month), day=1)
        end = start + pd.DateOffset(months=1)
        times = pd.DataFrame({"datetime_bj": pd.date_range(start, end - pd.Timedelta(hours=1), freq="h")})
    else:
        author = read_required_table(ctx, "author_load_2020_2024_long.csv.gz", compression="gzip")
        author["datetime_bj"] = pd.to_datetime(author["datetime_bj"])
        times = pd.DataFrame({"datetime_bj": sorted(author["datetime_bj"].unique())})
    times["target_year"] = times["datetime_bj"].dt.year
    times["target_month"] = times["datetime_bj"].dt.month
    times["source_datetime"] = times["datetime_bj"] - pd.Timedelta(hours=8)
    times["source_year"] = times["source_datetime"].dt.year
    times["time_alignment_method"] = "strict_utc_to_bj"
    times["fallback_reference_datetime_bj"] = pd.NaT
    boundary_mask = times["source_year"].lt(2020)
    if boundary_mask.any() and use_boundary_fallback:
        fallback_reference = times.loc[boundary_mask, "datetime_bj"] + pd.Timedelta(days=1)
        times.loc[boundary_mask, "fallback_reference_datetime_bj"] = fallback_reference
        times.loc[boundary_mask, "source_datetime"] = fallback_reference - pd.Timedelta(hours=8)
        times.loc[boundary_mask, "source_year"] = 2020
        times.loc[boundary_mask, "time_alignment_method"] = "fallback_next_day_same_local_hour_for_missing_2019_boundary"
    times["source_year"] = times["source_year"].astype(int)
    return times


def write_time_alignment_qc(ctx, times: pd.DataFrame) -> None:
    qc = times.groupby(["time_alignment_method", "target_year", "target_month", "source_year"], as_index=False).agg(
        rows=("datetime_bj", "count"),
        fallback_rows=("fallback_reference_datetime_bj", lambda s: int(s.notna().sum())),
        min_datetime_bj=("datetime_bj", "min"),
        max_datetime_bj=("datetime_bj", "max"),
        min_source_datetime=("source_datetime", "min"),
        max_source_datetime=("source_datetime", "max"),
        min_fallback_reference_datetime_bj=("fallback_reference_datetime_bj", "min"),
        max_fallback_reference_datetime_bj=("fallback_reference_datetime_bj", "max"),
    )
    write_df(qc, ctx.tables_dir / "weather_time_alignment_qc.csv")


def transform_values(values: np.ndarray, transform: str) -> np.ndarray:
    values = values.astype(np.float32, copy=False)
    if transform == "kelvin_to_c":
        return values - np.float32(273.15)
    if transform == "j_m2_to_w_m2":
        return values / np.float32(3600.0)
    return values


def extract_month_variable(
    ctx,
    target_year: int,
    target_month: int,
    var: str,
    month_times: pd.DataFrame,
    month_weights: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    import xarray as xr

    started = time.perf_counter()
    out_col = ERA5_SPECS[var]["output"]
    weather_root = Path(str(ctx.config["weather_root"]))
    month_times = month_times.sort_values("datetime_bj").reset_index(drop=True)
    point_base = (
        month_weights[["lat_idx", "lon_idx"]]
        .drop_duplicates()
        .sort_values(["lat_idx", "lon_idx"])
        .reset_index(drop=True)
    )
    point_base["point_id"] = np.arange(len(point_base), dtype=int)
    weights = month_weights.merge(point_base, on=["lat_idx", "lon_idx"], how="left")
    aggregated_parts: list[pd.DataFrame] = []
    qc_rows: list[dict[str, Any]] = []

    for source_year, time_sub in month_times.groupby("source_year", sort=True):
        source_year_int = int(source_year)
        path = era5_file(weather_root, var, source_year_int)
        if not path.exists():
            raise FileNotFoundError(f"ERA5 file missing for {var} {source_year_int}: {path}")
        ds = xr.open_dataset(path)
        try:
            data_var = var if var in ds.data_vars else list(ds.data_vars)[0]
            da = ds[data_var].isel(
                latitude=xr.DataArray(point_base["lat_idx"].astype(int).to_numpy(), dims="point"),
                longitude=xr.DataArray(point_base["lon_idx"].astype(int).to_numpy(), dims="point"),
            )
            time_index = pd.DatetimeIndex(pd.to_datetime(ds["valid_time"].values))
            source_dt = pd.to_datetime(time_sub["source_datetime"])
            indexer = time_index.get_indexer(source_dt)
            if (indexer < 0).any():
                missing = time_sub.loc[indexer < 0, ["datetime_bj", "source_datetime"]].head(20).astype(str).to_dict("records")
                raise ValueError(f"ERA5 time coverage missing for {var} {source_year_int}: {missing}")
            values = np.asarray(da.isel(valid_time=xr.DataArray(indexer.astype(int), dims="time")).load().values)
            values = transform_values(values, ERA5_SPECS[var]["unit_transform"])
        finally:
            ds.close()

        time_sub = time_sub.reset_index(drop=True)
        for province, weights_p in weights.groupby("province_cn", sort=True):
            point_ids = weights_p["point_id"].astype(int).to_numpy()
            point_w = weights_p["point_weight"].to_numpy(dtype=np.float32)
            point_w = point_w / point_w.sum()
            aggregated_parts.append(
                pd.DataFrame(
                    {
                        "province_cn": province,
                        "datetime_bj": time_sub["datetime_bj"].to_numpy(),
                        "source_datetime": time_sub["source_datetime"].to_numpy(),
                        "fallback_reference_datetime_bj": time_sub["fallback_reference_datetime_bj"].to_numpy(),
                        "source_year": source_year_int,
                        "time_alignment_method": time_sub["time_alignment_method"].to_numpy(),
                        out_col: values[:, point_ids].dot(point_w).astype(np.float32),
                    }
                )
            )
        elapsed = time.perf_counter() - started
        mem = current_memory_mb()
        row = {
            "variable": var,
            "output_column": out_col,
            "target_year": int(target_year),
            "target_month": int(target_month),
            "source_year": source_year_int,
            "selected_point_count": int(len(point_base)),
            "loaded_matrix_shape": str(tuple(values.shape)),
            "elapsed_seconds": round(elapsed, 3),
            "memory_mb": round(mem, 1) if mem is not None else np.nan,
            "file": str(path),
        }
        qc_rows.append(row)
        print(
            "[module02] extracted "
            f"var={var} target={target_year}-{target_month:02d} source_year={source_year_int} "
            f"points={len(point_base)} shape={tuple(values.shape)} elapsed={elapsed:.2f}s memory_mb={row['memory_mb']}",
            flush=True,
        )
        del values
        gc.collect()

    return pd.concat(aggregated_parts, ignore_index=True), qc_rows


def relative_humidity_from_dewpoint(temperature_c: pd.Series, dewpoint_c: pd.Series) -> pd.Series:
    rh = 100.0 * np.exp(
        17.625 * dewpoint_c / (243.04 + dewpoint_c)
        - 17.625 * temperature_c / (243.04 + temperature_c)
    )
    return rh.clip(0, 100)


def specific_humidity_gkg(dewpoint_c: pd.Series, surface_pressure_pa: pd.Series) -> pd.Series:
    vapor_pressure_pa = 611.2 * np.exp(17.67 * dewpoint_c / (dewpoint_c + 243.5))
    q = 0.622 * vapor_pressure_pa / (surface_pressure_pa - 0.378 * vapor_pressure_pa)
    return q * 1000.0


def build_hdd_cdd_thresholds(ctx, flags: list[dict]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for province in EXPECTED_PROVINCES:
        region = "north" if province in NORTH_HEATING_PROVINCES else "south"
        rows.append(
            {
                "province_cn": province,
                "thermal_region": region,
                "heat_threshold_c": THRESHOLD_VALUES[region]["heat_threshold_c"],
                "cool_threshold_c": THRESHOLD_VALUES[region]["cool_threshold_c"],
                "threshold_source_file": "codex_power_curve_v2_reset_plan.md",
                "threshold_source_sheet": "central_heating_16_province_north_south_rule",
            }
        )
    thresholds = pd.DataFrame(rows)
    write_df(thresholds, ctx.tables_dir / "hdd_cdd_thresholds_by_province.csv")
    summary = thresholds.groupby("thermal_region", as_index=False).agg(
        province_count=("province_cn", "nunique"),
        heat_threshold_c=("heat_threshold_c", "first"),
        cool_threshold_c=("cool_threshold_c", "first"),
        provinces=("province_cn", lambda s: ";".join(s)),
    )
    write_df(summary, ctx.tables_dir / "hdd_cdd_threshold_region_qc.csv")
    counts = summary.set_index("thermal_region")["province_count"].to_dict()
    if counts.get("north") != 16 or counts.get("south") != 15:
        add_qc(flags, MODULE, "HARD_FAIL", "hdd_cdd_threshold_region_count", "HDD/CDD 南北分区省份数量不符合 16/15", counts)
    else:
        add_qc(flags, MODULE, "INFO", "hdd_cdd_thresholds_loaded", "HDD/CDD 阈值按集中供暖 16 省 north / 其余 15 省 south 规则生成", counts)
    return thresholds


def finite_exp_smooth(values: np.ndarray, window_hours: int, decay_lambda: float) -> np.ndarray:
    weights = np.exp(-decay_lambda * np.arange(window_hours + 1, dtype=float))
    valid = np.isfinite(values).astype(float)
    filled = np.where(np.isfinite(values), values, 0.0)
    numerator = np.convolve(filled, weights, mode="full")[: len(values)]
    denominator = np.convolve(valid, weights, mode="full")[: len(values)]
    out = numerator / np.where(denominator == 0, np.nan, denominator)
    return out


def add_bait_hdd_cdd(weather: pd.DataFrame, thresholds: pd.DataFrame) -> pd.DataFrame:
    weather = weather.sort_values(["province_cn", "datetime_bj"]).copy()
    weather["relative_humidity_pct"] = relative_humidity_from_dewpoint(weather["temperature_c"], weather["dewpoint_c"])
    weather["specific_humidity_gkg"] = specific_humidity_gkg(weather["dewpoint_c"], weather["surface_pressure_pa"])
    weather["wind_speed_ms"] = np.sqrt(weather["u10_ms"] ** 2 + weather["v10_ms"] ** 2)
    weather["bait_k_s"] = 100.0 + 7.0 * weather["temperature_c"]
    weather["bait_k_w"] = 4.5 - 0.025 * weather["temperature_c"]
    weather["bait_k_h"] = np.exp(1.0 + 0.06 * weather["temperature_c"])
    sign_term = np.sign(weather["temperature_c"] - BAIT_PARAMS["t_star_c"])
    weather["bait_raw_c"] = (
        weather["temperature_c"]
        + BAIT_PARAMS["x"] * (weather["solar_wm2"] - weather["bait_k_s"])
        - BAIT_PARAMS["y"] * (weather["wind_speed_ms"] - weather["bait_k_w"])
        + BAIT_PARAMS["z"] * (weather["specific_humidity_gkg"] - weather["bait_k_h"]) * sign_term
    )
    smoothed = []
    for _, sub in weather.groupby("province_cn", sort=False):
        values = sub["bait_raw_c"].to_numpy(dtype=float)
        smoothed.append(
            pd.Series(
                finite_exp_smooth(values, int(BAIT_PARAMS["smooth_window_hours"]), float(BAIT_PARAMS["smooth_lambda"])),
                index=sub.index,
            )
        )
    weather["bait_smoothed_c"] = pd.concat(smoothed).sort_index()
    lower = BAIT_PARAMS["blend_lower_c"]
    upper = BAIT_PARAMS["blend_upper_c"]
    weather["bait_blend_k"] = np.where(
        weather["temperature_c"] < lower,
        -5.0,
        np.where(
            weather["temperature_c"] > upper,
            5.0,
            (weather["temperature_c"] - 0.5 * (upper + lower)) * 10.0 / (upper - lower),
        ),
    )
    weather["bait_blend_factor"] = 0.5 / (1.0 + np.exp(-weather["bait_blend_k"]))
    weather["bait_c"] = weather["bait_smoothed_c"] * (1.0 - weather["bait_blend_factor"]) + weather["temperature_c"] * weather["bait_blend_factor"]
    weather = weather.merge(
        thresholds[
            [
                "province_cn",
                "thermal_region",
                "heat_threshold_c",
                "cool_threshold_c",
                "threshold_source_file",
                "threshold_source_sheet",
            ]
        ],
        on="province_cn",
        how="left",
    )
    if weather[["heat_threshold_c", "cool_threshold_c"]].isna().any().any():
        missing = weather.loc[weather[["heat_threshold_c", "cool_threshold_c"]].isna().any(axis=1), "province_cn"].drop_duplicates().tolist()
        raise ValueError(f"weather rows missing HDD/CDD thresholds: {missing}")
    weather["hdd_hour"] = np.maximum(weather["heat_threshold_c"] - weather["bait_c"], 0.0) / 24.0
    weather["cdd_hour"] = np.maximum(weather["bait_c"] - weather["cool_threshold_c"], 0.0) / 24.0
    weather["weather_weight_method"] = "city_monthly_electricity_era5_grid_points"
    weather["humidity_method"] = "specific_humidity_gkg_from_dewpoint_and_surface_pressure"
    weather["bait_formula"] = "paper_eq4_eq11_finite_48h"
    weather["year"] = weather["datetime_bj"].dt.year
    weather["month"] = weather["datetime_bj"].dt.month
    return weather


def write_weather_qc(ctx, weather: pd.DataFrame, thermal: pd.DataFrame) -> None:
    unit_qc = pd.DataFrame(
        [
            {"paper_variable": "T", "local_column": "temperature_c", "source": "t2m", "unit_conversion": "K to C"},
            {"paper_variable": "S", "local_column": "solar_wm2", "source": "ssrd", "unit_conversion": "J/m2/hour divided by 3600 to W/m2"},
            {"paper_variable": "W", "local_column": "wind_speed_ms", "source": "u10,v10", "unit_conversion": "sqrt(u10^2+v10^2), m/s"},
            {"paper_variable": "H", "local_column": "specific_humidity_gkg", "source": "d2m,sp", "unit_conversion": "dewpoint and pressure to specific humidity, g/kg"},
        ]
    )
    write_df(unit_qc, ctx.tables_dir / "weather_variable_unit_qc.csv")
    bait_qc = weather.groupby(["province_cn", "year"], as_index=False).agg(
        heat_threshold_c=("heat_threshold_c", "first"),
        cool_threshold_c=("cool_threshold_c", "first"),
        threshold_source_sheet=("threshold_source_sheet", "first"),
        thermal_region=("thermal_region", "first"),
        temperature_min_c=("temperature_c", "min"),
        temperature_max_c=("temperature_c", "max"),
        rh_min_pct=("relative_humidity_pct", "min"),
        rh_max_pct=("relative_humidity_pct", "max"),
        specific_humidity_min_gkg=("specific_humidity_gkg", "min"),
        specific_humidity_max_gkg=("specific_humidity_gkg", "max"),
        bait_min_c=("bait_c", "min"),
        bait_p01_c=("bait_c", lambda s: s.quantile(0.01)),
        bait_p50_c=("bait_c", "median"),
        bait_p99_c=("bait_c", lambda s: s.quantile(0.99)),
        bait_max_c=("bait_c", "max"),
        hdd_nonzero_hours=("hdd_hour", lambda s: int((s > 0).sum())),
        cdd_nonzero_hours=("cdd_hour", lambda s: int((s > 0).sum())),
    )
    write_df(bait_qc, ctx.tables_dir / "bait_hdd_cdd_qc.csv")
    load_qc = thermal.groupby(["province_cn", "year"], as_index=False).agg(
        heating_energy_mwh=("heating_load_mw", "sum"),
        cooling_energy_mwh=("cooling_load_mw", "sum"),
        heating_peak_mw=("heating_load_mw", "max"),
        cooling_peak_mw=("cooling_load_mw", "max"),
        hdd_sum=("hdd_hour", "sum"),
        cdd_sum=("cdd_hour", "sum"),
    )
    write_df(load_qc, ctx.tables_dir / "heating_cooling_summary_by_province_year.csv")
    write_df(bait_qc, ctx.tables_dir / "weather_weighting_qc.csv")


def write_method_report(ctx) -> None:
    text = """# Module 02 Method Report

## Spatial Exposure

Province-level hourly ERA5 exposure is constructed from city monthly electricity
weights. For city `c` in province `p` and month `m`, the city weight
`city_weight_in_province[c,m]` is divided equally across all ERA5 grid centers
covered by the city boundary. Province-month point weights are checked and
normalized only after recording the raw closure.

## ERA5 Variables

| Paper variable | Local ERA5 input | Output column | Unit handling |
|---|---|---|---|
| T | `t2m` | `temperature_c` | K to C |
| S | `ssrd` | `solar_wm2` | J/m2/hour divided by 3600 |
| W | `u10`, `v10` | `wind_speed_ms` | sqrt(u10^2 + v10^2), m/s |
| H | `d2m`, `sp` | `specific_humidity_gkg` | dewpoint and surface pressure to g/kg |

Relative humidity is retained as `relative_humidity_pct` for QC only and is not
used as the BAIT humidity term.

## BAIT Eq.(4)-Eq.(11)

```text
oBAIT = T + 0.012*(S-kS) - 0.2*(W-kW) + 0.05*(H-kH)*sign(T-16)
kS = 100 + 7*T
kW = 4.5 - 0.025*T
kH = exp(1 + 0.06*T)
```

`sBAIT` uses the finite 48-hour exponential window
`sum(q=0..48, exp(-0.10232*q) * oBAIT[t-q]) / sum(q=0..48, exp(-0.10232*q))`,
with the denominator truncated to available prior hours at the start of each
province series.

Temperature blending uses `B = 0.5/(1+exp(-kB))`, with `B_L=15 C` and
`B_U=23 C`, then `BAIT = sBAIT*(1-B) + T*B`.

## HDD/CDD Thresholds

HDD/CDD thresholds use the central-heating north/south rule requested for this
run. The 16 north provinces are Beijing, Gansu, Hebei, Henan, Heilongjiang,
Jilin, Liaoning, Inner Mongolia, Ningxia, Qinghai, Shaanxi, Shandong, Shanxi,
Tianjin, Xinjiang, and Tibet. The remaining 15 provinces are south.

```text
north: heat_threshold_c = 14.713, cool_threshold_c = 22.253
south: heat_threshold_c = 16.818, cool_threshold_c = 22.631
```

## 2019 Boundary Fallback

When 2019 year-end ERA5 files are missing, fallback is allowed only if
`allow_2019_boundary_fallback: true` is set in `config/run_config.yaml`. The
fallback method is `next_day_same_local_hour`: target Beijing-time
2020-01-01 00:00-07:00 uses the ERA5 values for Beijing-time
2020-01-02 00:00-07:00, corresponding to UTC 2020-01-01 16:00-23:00.
"""
    write_markdown(ctx.reports_dir / "method_report_module02.md", text)


def merge_month_variable_frames(var_frames: list[pd.DataFrame]) -> pd.DataFrame:
    base = var_frames[0]
    for part in var_frames[1:]:
        value_cols = [
            col
            for col in part.columns
            if col
            not in {
                "province_cn",
                "datetime_bj",
                "source_datetime",
                "fallback_reference_datetime_bj",
                "source_year",
                "time_alignment_method",
            }
        ]
        base = base.merge(part[["province_cn", "datetime_bj"] + value_cols], on=["province_cn", "datetime_bj"], how="left")
    return base


def build_weather(ctx, point_weights: pd.DataFrame, flags: list[dict], args: argparse.Namespace) -> None:
    smoke_year = args.smoke_year
    smoke_month = args.smoke_month
    smoke_province = normalize_province(args.smoke_province) if args.smoke_province else None
    if (smoke_year is None) ^ (smoke_month is None):
        add_qc(flags, MODULE, "HARD_FAIL", "smoke_year_month_pair", "`--smoke-year` 与 `--smoke-month` 必须同时提供")
        return

    times = build_target_times(ctx, smoke_year, smoke_month, use_boundary_fallback=bool(args.use_boundary_fallback))
    if smoke_year and smoke_month:
        times = times[(times["target_year"].eq(smoke_year)) & (times["target_month"].eq(smoke_month))].copy()
    write_time_alignment_qc(ctx, times)

    if smoke_province:
        point_weights = point_weights[point_weights["province_cn"].eq(smoke_province)].copy()
        if point_weights.empty:
            add_qc(flags, MODULE, "HARD_FAIL", "smoke_province_no_weights", "smoke 省份没有 ERA5 点权重", smoke_province)
            return

    variables = [args.smoke_variable] if args.smoke_variable else list(ERA5_SPECS)
    thresholds = None
    if not args.smoke_variable:
        write_method_report(ctx)
        thresholds = build_hdd_cdd_thresholds(ctx, flags)
    max_workers = int(ctx.config.get("weather_parallel_workers", 1))
    capped_workers = max(1, min(max_workers, 2))
    add_qc(
        flags,
        MODULE,
        "INFO",
        "weather_reader_parallel_policy",
        "ERA5 读取按 year-month-variable 流式执行；默认单 worker，最多 2",
        {"configured_workers": max_workers, "capped_workers": capped_workers, "active_mode": "sequential_streaming"},
    )

    tmp_dir = ctx.run_dir / "tmp_module02_weather"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    extraction_qc: list[dict[str, Any]] = []
    tmp_files: list[Path] = []

    for (target_year, target_month), month_times in times.groupby(["target_year", "target_month"], sort=True):
        month_weights = point_weights[point_weights["month"].astype(int).eq(int(target_month))].copy()
        if month_weights.empty:
            add_qc(flags, MODULE, "HARD_FAIL", "month_point_weights_missing", "指定月份缺少 ERA5 点权重", {"month": int(target_month)})
            return
        var_frames: list[pd.DataFrame] = []
        for var in variables:
            frame, qc_rows = extract_month_variable(ctx, int(target_year), int(target_month), var, month_times, month_weights)
            var_frames.append(frame)
            extraction_qc.extend(qc_rows)
            write_df(pd.DataFrame(extraction_qc), ctx.tables_dir / "weather_extraction_qc.csv")
        month_weather = merge_month_variable_frames(var_frames)
        month_weather["target_year"] = int(target_year)
        month_weather["target_month"] = int(target_month)
        if args.smoke_variable:
            out = ctx.tables_dir / f"smoke_weather_variable_{args.smoke_variable}_{int(target_year)}_{int(target_month):02d}.csv.gz"
            write_df(month_weather, out)
            add_qc(flags, MODULE, "INFO", "module02_smoke_variable_output", "单变量 ERA5 抽取 smoke test 完成", {"path": str(out), "rows": int(len(month_weather))})
            return
        tmp_path = tmp_dir / f"tmp_weather_province_hourly_{int(target_year)}_{int(target_month):02d}.csv.gz"
        write_df(month_weather, tmp_path)
        tmp_files.append(tmp_path)
        print(f"[module02] wrote monthly weather temp file {tmp_path}", flush=True)
        del month_weather, var_frames
        gc.collect()

    raw_weather = pd.concat(
        (
            pd.read_csv(
                path,
                compression="gzip",
                encoding="utf-8-sig",
                parse_dates=["datetime_bj", "source_datetime", "fallback_reference_datetime_bj"],
            )
            for path in tmp_files
        ),
        ignore_index=True,
    )
    missing = [col for col in REQUIRED_WEATHER_COLS if col not in raw_weather.columns or raw_weather[col].isna().any()]
    if missing:
        add_qc(flags, MODULE, "HARD_FAIL", "weather_required_columns_missing", "BAIT 所需 ERA5 变量缺失或存在空值", missing)
        return
    weather = add_bait_hdd_cdd(raw_weather, thresholds)
    coeff = read_power_coefficients(ctx)
    thermal = weather.merge(
        coeff[["province_cn", "year", "p_heat_gwh_per_degree_day", "p_cool_gwh_per_degree_day"]],
        on=["province_cn", "year"],
        how="left",
    )
    if thermal[["p_heat_gwh_per_degree_day", "p_cool_gwh_per_degree_day"]].isna().any().any():
        missing_coeff = thermal.loc[
            thermal[["p_heat_gwh_per_degree_day", "p_cool_gwh_per_degree_day"]].isna().any(axis=1),
            ["province_cn", "year"],
        ].drop_duplicates()
        add_qc(flags, MODULE, "HARD_FAIL", "thermal_coeff_missing", "冷热系数无法覆盖天气特征表", missing_coeff.to_dict("records"))
        return
    thermal["heating_load_mw"] = thermal["p_heat_gwh_per_degree_day"] * thermal["hdd_hour"] * 1000.0
    thermal["cooling_load_mw"] = thermal["p_cool_gwh_per_degree_day"] * thermal["cdd_hour"] * 1000.0

    weather_cols = [
        "province_cn",
        "datetime_bj",
        "year",
        "month",
        "source_datetime",
        "fallback_reference_datetime_bj",
        "source_year",
        "time_alignment_method",
        "weather_weight_method",
        "humidity_method",
        "bait_formula",
        "thermal_region",
        "temperature_c",
        "dewpoint_c",
        "relative_humidity_pct",
        "specific_humidity_gkg",
        "u10_ms",
        "v10_ms",
        "wind_speed_ms",
        "solar_wm2",
        "surface_pressure_pa",
        "bait_k_s",
        "bait_k_w",
        "bait_k_h",
        "bait_raw_c",
        "bait_smoothed_c",
        "bait_blend_k",
        "bait_blend_factor",
        "bait_c",
        "heat_threshold_c",
        "cool_threshold_c",
        "threshold_source_file",
        "threshold_source_sheet",
        "hdd_hour",
        "cdd_hour",
    ]
    write_df(weather[weather_cols], ctx.tables_dir / "weather_features_hourly_province_2020_2024.csv.gz")
    write_df(thermal, ctx.tables_dir / "heating_cooling_load_2020_2024.csv.gz")
    write_df(coeff, ctx.tables_dir / "power_coefficients_long.csv")
    write_weather_qc(ctx, weather, thermal)
    write_method_report(ctx)
    add_qc(
        flags,
        MODULE,
        "INFO",
        "module02_outputs",
        "模块 02 城市权重 ERA5、BAIT/HDD/CDD 与冷热负荷输出完成",
        {"weather_rows": int(len(weather)), "thermal_rows": int(len(thermal)), "monthly_temp_files": len(tmp_files)},
    )


def main() -> None:
    args = parse_module_args()
    ctx = init_context(args, MODULE)
    flags: list[dict] = []
    try:
        paths = get_input_paths(ctx.config)
        coverage, missing_core, missing_boundary_2019 = required_era5_files(paths["weather_root"])
        write_df(coverage, ctx.tables_dir / "weather_file_coverage_module02.csv")
        if missing_core:
            add_qc(flags, MODULE, "HARD_FAIL", "era5_core_files_missing", "2020-2024 ERA5 核心文件缺失", [str(p) for p in missing_core])
            module_exit(flags, ctx, "02", MODULE)
            return
        allow_boundary_fallback = bool(ctx.config.get("allow_2019_boundary_fallback", False))
        setattr(args, "use_boundary_fallback", bool(missing_boundary_2019 and allow_boundary_fallback))
        if missing_boundary_2019:
            if not allow_boundary_fallback:
                add_qc(
                    flags,
                    MODULE,
                    "HARD_FAIL",
                    "era5_2019_boundary_missing_fallback_disabled",
                    "缺少 2019 年末 ERA5 边界小时，且 allow_2019_boundary_fallback=false",
                    [str(p) for p in missing_boundary_2019],
                )
                module_exit(flags, ctx, "02", MODULE)
                return
            write_boundary_fallback_report(ctx, missing_boundary_2019, flags)

        _, _, point_weights = build_spatial_weights(ctx, flags)
        if point_weights is None:
            module_exit(flags, ctx, "02", MODULE)
            return
        if args.only_weights:
            add_qc(flags, MODULE, "INFO", "module02_only_weights", "仅生成城市-ERA5 点位与省-月点权重表，未读取 ERA5 时间序列")
            module_exit(flags, ctx, "02", MODULE)
            return
        build_weather(ctx, point_weights, flags, args)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "module02_exception", "模块 02 执行异常", repr(exc))
    module_exit(flags, ctx, "02", MODULE)


if __name__ == "__main__":
    main()
