# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from power_curve_common import (
    NORTH_PROVINCES,
    YEARS,
    add_qc,
    get_input_paths,
    init_context,
    module_exit,
    normalize_city,
    normalize_province,
    parse_args,
    read_power_coefficients,
    read_required_table,
    rel,
    write_df,
    write_markdown,
)


MODULE = "02_reconstruct_weather_and_thermal_load"

ERA5_PATTERNS = {
    "t2m": ("2m_temp", "t2m_{year}.nc"),
    "d2m": ("2m_dewpoint", "dt2m_{year}.nc"),
    "u10": ("10mu", "u_{year}.nc"),
    "v10": ("10mv", "v_{year}.nc"),
    "ssrd": ("surface_solar_radi", "ssrd_{year}.nc"),
    "sp": ("surface_press", "sp_{year}.nc"),
}


def _extract_era5_province_task(task: dict) -> tuple[pd.DataFrame, dict]:
    """Worker: extract one variable-year file and aggregate ERA5 grid points to province-hour."""
    import xarray as xr

    var = task["var"]
    ds_path = task["ds_path"]
    year_times = pd.DataFrame(task["year_times"])
    point_weights = pd.DataFrame(task["point_weights"])
    source_year = int(task["source_year"])
    unit_transform = task["unit_transform"]

    ds = xr.open_dataset(ds_path)
    try:
        unique_points = (
            point_weights[["lat_idx", "lon_idx"]]
            .drop_duplicates()
            .sort_values(["lat_idx", "lon_idx"])
            .reset_index(drop=True)
        )
        unique_points["point_id"] = np.arange(len(unique_points))
        point_weights = point_weights.merge(unique_points, on=["lat_idx", "lon_idx"], how="left")
        da = ds[var].isel(
            latitude=xr.DataArray(unique_points["lat_idx"].astype(int).to_numpy(), dims="point"),
            longitude=xr.DataArray(unique_points["lon_idx"].astype(int).to_numpy(), dims="point"),
        )
        time_index = pd.DatetimeIndex(pd.to_datetime(ds["valid_time"].values))
        source_dt = pd.to_datetime(year_times["source_datetime"])
        indexer = time_index.get_indexer(source_dt)
        if (indexer < 0).any():
            missing_times = year_times.loc[indexer < 0, ["datetime_bj", "source_datetime"]].head(20).astype(str).to_dict("records")
            raise ValueError(f"ERA5 time coverage missing for {var} {source_year}: {missing_times}")
        year_times = year_times.reset_index(drop=True)
        year_times["time_indexer"] = indexer
        result_parts: list[pd.DataFrame] = []
        for month, time_sub in year_times.groupby("month", sort=True):
            month_indexer = time_sub["time_indexer"].to_numpy(dtype=int)
            values = np.asarray(da.isel(valid_time=xr.DataArray(month_indexer, dims="time")).load().values, dtype=np.float32)
            if unit_transform == "kelvin_to_c":
                values = values - np.float32(273.15)
            elif unit_transform == "j_m2_to_w_m2":
                values = values / np.float32(3600.0)
            weights_month = point_weights[point_weights["month"].astype(int).eq(int(month))]
            for province, weights_p in weights_month.groupby("province_cn", sort=True):
                point_ids = weights_p["point_id"].astype(int).to_numpy()
                weights = weights_p["point_weight"].to_numpy(dtype=np.float32)
                weights = weights / weights.sum()
                result_parts.append(
                    pd.DataFrame(
                        {
                            "province_cn": province,
                            "datetime_bj": time_sub["datetime_bj"].to_numpy(),
                            "time_alignment_method": time_sub["time_alignment_method"].to_numpy(),
                            var: values[:, point_ids].dot(weights).astype(np.float32),
                        }
                    )
                )
            del values
    finally:
        ds.close()
    out = pd.concat(result_parts, ignore_index=True)
    qc = {
        "source_year": source_year,
        "variable": var,
        "target_rows": int(len(year_times)),
        "unique_grid_points": int(len(unique_points)),
        "aggregated_rows": int(len(out)),
        "file": ds_path,
    }
    return out, qc


def required_era5_files(weather_root: Path) -> tuple[list[Path], list[Path]]:
    required: list[Path] = []
    missing: list[Path] = []
    # Strict BJ target -> UTC source requires 2019-2024 for the first eight BJ hours of 2020.
    for year in range(2019, 2025):
        for folder, pattern in ERA5_PATTERNS.values():
            path = weather_root / folder / pattern.format(year=year)
            required.append(path)
            if not path.exists():
                missing.append(path)
    return required, missing


def era5_file(weather_root: Path, var: str, year: int) -> Path:
    folder, pattern = ERA5_PATTERNS[var]
    return weather_root / folder / pattern.format(year=year)


def load_city_grid_points(ctx, flags: list[dict]) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    try:
        import geopandas as gpd
        from shapely.geometry import Point

        paths = get_input_paths(ctx.config)
        sample_ds_path = era5_file(Path(str(ctx.config["weather_root"])), "t2m", 2020)
        import xarray as xr

        ds = xr.open_dataset(sample_ds_path)
        lat_values = ds["latitude"].values
        lon_values = ds["longitude"].values
        ds.close()
        lon_grid, lat_grid = np.meshgrid(lon_values, lat_values)
        flat = pd.DataFrame(
            {
                "lat_idx": np.repeat(np.arange(len(lat_values)), len(lon_values)),
                "lon_idx": np.tile(np.arange(len(lon_values)), len(lat_values)),
                "era5_latitude": lat_grid.ravel(),
                "era5_longitude": lon_grid.ravel(),
            }
        )
        points = gpd.GeoDataFrame(
            flat,
            geometry=gpd.points_from_xy(flat["era5_longitude"], flat["era5_latitude"]),
            crs="EPSG:4326",
        )
        city = gpd.read_file(paths["city_shapefile"], encoding="gbk")
        if city.crs is None:
            add_qc(flags, MODULE, "SOFT_FAIL", "city_shapefile_crs", "市级边界缺少 CRS，不能安全匹配 ERA5 经纬度", blocking=True)
            return None, None
        city = city.to_crs("EPSG:4326")
        city["province_cn"] = city["省"].map(normalize_province)
        city["city_cn"] = city["name"].map(normalize_city)
        weights = load_city_weights(ctx, flags)
        if weights is None:
            return None, None
        needed = weights[["province_cn", "city_cn"]].drop_duplicates()
        city = city.merge(needed, on=["province_cn", "city_cn"], how="inner")
        if city.empty:
            add_qc(flags, MODULE, "HARD_FAIL", "city_shape_weight_match_empty", "城市权重无法匹配任何市级边界")
            return None, None
        bbox = city.total_bounds
        points = points[
            points["era5_longitude"].between(bbox[0] - 0.25, bbox[2] + 0.25)
            & points["era5_latitude"].between(bbox[1] - 0.25, bbox[3] + 0.25)
        ].copy()
        joined = gpd.sjoin(
            points,
            city[["province_cn", "city_cn", "geometry"]],
            how="inner",
            predicate="within",
        ).drop(columns=["index_right"], errors="ignore")
        mapping = pd.DataFrame(joined.drop(columns="geometry"))
        fallback_rows = []
        counts = mapping.groupby(["province_cn", "city_cn"]).size().rename("era5_point_count").reset_index()
        missing = needed.merge(counts[["province_cn", "city_cn"]], on=["province_cn", "city_cn"], how="left", indicator=True)
        missing = missing[missing["_merge"] == "left_only"][["province_cn", "city_cn"]]
        if not missing.empty:
            city_for_fallback = city.merge(missing, on=["province_cn", "city_cn"], how="inner")
            reps = city_for_fallback.geometry.representative_point()
            for (_, row), point in zip(city_for_fallback.iterrows(), reps):
                lat_idx = int(np.abs(lat_values - point.y).argmin())
                lon_idx = int(np.abs(lon_values - point.x).argmin())
                fallback_rows.append(
                    {
                        "lat_idx": lat_idx,
                        "lon_idx": lon_idx,
                        "era5_latitude": float(lat_values[lat_idx]),
                        "era5_longitude": float(lon_values[lon_idx]),
                        "province_cn": row["province_cn"],
                        "city_cn": row["city_cn"],
                        "mapping_method": "nearest_representative_for_city_without_grid_center",
                    }
                )
            add_qc(
                flags,
                MODULE,
                "WARN",
                "city_without_era5_center_fallback",
                "部分城市边界内没有 ERA5 网格中心，使用城市代表点最近 ERA5 点 fallback",
                missing.to_dict("records"),
                blocking=False,
            )
        mapping["mapping_method"] = "grid_center_within_city_boundary"
        if fallback_rows:
            mapping = pd.concat([mapping, pd.DataFrame(fallback_rows)], ignore_index=True)
        dup = mapping.groupby(["lat_idx", "lon_idx"]).size().reset_index(name="assignments")
        dup = dup[dup["assignments"] > 1]
        if not dup.empty:
            add_qc(
                flags,
                MODULE,
                "WARN",
                "era5_point_multiple_city_assignments",
                "部分 ERA5 点被多个城市边界覆盖；权重将在省月点层面合并",
                {"duplicate_point_count": int(len(dup))},
                blocking=False,
            )
        counts = mapping.groupby(["province_cn", "city_cn"]).size().rename("era5_point_count").reset_index()
        mapping = mapping.merge(counts, on=["province_cn", "city_cn"], how="left")
        point_weights = weights.merge(mapping, on=["province_cn", "city_cn"], how="left")
        if point_weights[["lat_idx", "lon_idx"]].isna().any().any():
            add_qc(flags, MODULE, "HARD_FAIL", "city_weight_grid_mapping_missing", "城市月度权重仍有城市无法映射到 ERA5 点")
            return None, None
        point_weights["point_weight_raw"] = point_weights["city_weight"] / point_weights["era5_point_count"]
        point_weights = (
            point_weights.groupby(["province_cn", "month", "lat_idx", "lon_idx", "era5_latitude", "era5_longitude"], as_index=False)
            .agg(point_weight_raw=("point_weight_raw", "sum"), contributing_city_count=("city_cn", "nunique"))
        )
        sums = point_weights.groupby(["province_cn", "month"])["point_weight_raw"].sum().rename("province_month_weight_sum").reset_index()
        point_weights = point_weights.merge(sums, on=["province_cn", "month"], how="left")
        point_weights["point_weight"] = point_weights["point_weight_raw"] / point_weights["province_month_weight_sum"]
        bad = sums[sums["province_month_weight_sum"].sub(1.0).abs() > 1e-6]
        if not bad.empty:
            add_qc(flags, MODULE, "WARN", "era5_point_weight_renormalized", "省月 ERA5 点权重和非 1，已重归一", bad.head(50).to_dict("records"), blocking=False)
        mapping = mapping[["province_cn", "city_cn", "lat_idx", "lon_idx", "era5_latitude", "era5_longitude", "era5_point_count", "mapping_method"]].drop_duplicates()
        return mapping, point_weights
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "city_grid_point_mapping", "市级边界到 ERA5 网格点映射失败", repr(exc))
        return None, None


def load_city_weights(ctx, flags: list[dict]) -> pd.DataFrame | None:
    try:
        path = get_input_paths(ctx.config)["city_weights"]
        weights = pd.read_excel(path, sheet_name="城市月度占比_长表")
        weights["province_cn"] = weights["省份中文"].map(normalize_province)
        weights["city_cn"] = weights["城市中文"].map(normalize_city)
        weights["month"] = pd.to_numeric(weights["月份"], errors="coerce").astype("Int64")
        weights["city_weight"] = pd.to_numeric(weights["城市省内占比"], errors="coerce")
        closure = weights.groupby(["province_cn", "month"])["city_weight"].sum().reset_index()
        if (closure["city_weight"].sub(1.0).abs() > 1e-6).any():
            add_qc(flags, MODULE, "SOFT_FAIL", "city_weight_closure", "城市月度用电权重不闭合", closure.to_dict("records"), blocking=True)
        return weights[["province_cn", "city_cn", "month", "city_weight"]].dropna()
    except Exception as exc:
        add_qc(flags, MODULE, "SOFT_FAIL", "city_weight_read", "城市月度用电权重读取失败", repr(exc), blocking=True)
        return None


def write_blocker(ctx, missing: list[Path], flags: list[dict]) -> None:
    text = "# Module 02 Blocker: ERA5 Time Alignment\n\n"
    text += "The configured policy is strict UTC-to-Beijing alignment. For Beijing-time 2020-01-01 00:00 to 07:00, ERA5 source hours must come from 2019-12-31 16:00 to 23:00 UTC.\n\n"
    text += "Missing required files:\n\n"
    for path in missing:
        text += f"- `{path}`\n"
    text += "\nNo boundary-hour filling or direct Beijing-time reinterpretation was applied.\n"
    write_markdown(ctx.reports_dir / "city_weather_weighting_blocker_report.md", text)
    write_markdown(ctx.run_dir / "city_weather_weighting_blocker_report.md", text)
    add_qc(
        flags,
        MODULE,
        "SOFT_FAIL",
        "era5_2019_boundary_missing",
        "严格 UTC->北京时间对齐缺少 2019 年末边界小时文件，模块 02 阻断",
        [str(p) for p in missing],
        blocking=True,
    )


def write_boundary_fallback_report(ctx, missing: list[Path], flags: list[dict]) -> None:
    text = "# Module 02 ERA5 Boundary Fallback\n\n"
    text += "The preferred policy is strict UTC-to-Beijing alignment. ERA5 2019 files are missing, so Beijing-time 2020-01-01 00:00 to 07:00 cannot be mapped to 2019-12-31 16:00 to 23:00 UTC.\n\n"
    text += "Fallback used for those eight hours only: use the first eight available 2020 ERA5 hours with the same Beijing clock labels. All other hours keep UTC-to-Beijing alignment.\n\n"
    text += "Missing files recorded:\n\n"
    for path in missing:
        text += f"- `{path}`\n"
    write_markdown(ctx.reports_dir / "era5_boundary_fallback_report.md", text)
    add_qc(
        flags,
        MODULE,
        "WARN",
        "era5_2019_boundary_fallback",
        "缺少 2019 年末 ERA5 边界小时；2020 年前 8 个北京时间小时使用 2020 年首 8 个 ERA5 小时 fallback",
        [str(p) for p in missing],
        blocking=False,
    )


def build_weather_if_unblocked(ctx, flags: list[dict]) -> None:
    """Compute province weather using city-covered ERA5 grid points and monthly city weights."""
    author = read_required_table(ctx, "author_load_2020_2024_long.csv.gz", compression="gzip")
    author["datetime_bj"] = pd.to_datetime(author["datetime_bj"])
    times = pd.DataFrame({"datetime_bj": sorted(author["datetime_bj"].unique())})
    times["datetime_utc"] = times["datetime_bj"] - pd.Timedelta(hours=8)
    times["utc_year"] = times["datetime_utc"].dt.year
    times["month"] = times["datetime_bj"].dt.month
    times["source_datetime"] = times["datetime_utc"]
    times["source_year"] = times["utc_year"]
    boundary_mask = times["source_year"].lt(2020)
    if boundary_mask.any():
        times.loc[boundary_mask, "source_datetime"] = times.loc[boundary_mask, "datetime_bj"]
        times.loc[boundary_mask, "source_year"] = 2020
        times.loc[boundary_mask, "time_alignment_method"] = "fallback_2020_first_hours_for_missing_2019_boundary"
        times.loc[~boundary_mask, "time_alignment_method"] = "strict_utc_to_bj"
    else:
        times["time_alignment_method"] = "strict_utc_to_bj"
    write_df(
        times.groupby(["time_alignment_method", "source_year"], as_index=False).agg(
            rows=("datetime_bj", "count"),
            min_datetime_bj=("datetime_bj", "min"),
            max_datetime_bj=("datetime_bj", "max"),
            min_source_datetime=("source_datetime", "min"),
            max_source_datetime=("source_datetime", "max"),
        ),
        ctx.tables_dir / "weather_time_alignment_qc.csv",
    )
    grid_mapping, point_weights = load_city_grid_points(ctx, flags)
    if grid_mapping is None or point_weights is None:
        return
    write_df(grid_mapping, ctx.tables_dir / "era5_city_grid_point_mapping.csv")
    write_df(point_weights, ctx.tables_dir / "era5_grid_point_weights_by_month.csv")
    weight_qc = point_weights.groupby(["province_cn", "month"], as_index=False).agg(
        point_count=("point_weight", "size"),
        weight_sum=("point_weight", "sum"),
        contributing_grid_points=("lat_idx", "nunique"),
    )
    write_df(weight_qc, ctx.tables_dir / "era5_grid_point_weight_qc.csv")
    if (weight_qc["weight_sum"].sub(1.0).abs() > 1e-6).any():
        add_qc(flags, MODULE, "HARD_FAIL", "era5_point_weight_not_closed", "省月 ERA5 点权重未闭合")
        return

    weather_root = Path(str(ctx.config["weather_root"]))
    unit_transform = {
        "t2m": "kelvin_to_c",
        "d2m": "kelvin_to_c",
        "u10": "none",
        "v10": "none",
        "ssrd": "j_m2_to_w_m2",
        "sp": "none",
    }
    tasks: list[dict] = []
    for source_year, year_times in times.groupby("source_year", sort=True):
        year_times = year_times.sort_values("datetime_bj").copy()
        source_year_int = int(source_year)
        if source_year_int < 2020 or source_year_int > 2024:
            add_qc(flags, MODULE, "HARD_FAIL", "era5_source_year_out_of_range", "fallback 后仍出现不可读取 ERA5 年份", {"source_year": source_year_int})
            return
        for var in ERA5_PATTERNS:
            ds_path = era5_file(weather_root, var, source_year_int)
            if not ds_path.exists():
                add_qc(flags, MODULE, "HARD_FAIL", f"era5_missing_{var}_{source_year_int}", "ERA5 文件缺失，无法继续", str(ds_path))
                return
            tasks.append(
                {
                    "source_year": source_year_int,
                    "var": var,
                    "ds_path": str(ds_path),
                    "year_times": year_times[["datetime_bj", "source_datetime", "month", "time_alignment_method"]].copy(),
                    "point_weights": point_weights[["province_cn", "month", "lat_idx", "lon_idx", "point_weight"]].copy(),
                    "unit_transform": unit_transform[var],
                }
            )

    max_workers = int(ctx.config.get("weather_parallel_workers", 6))
    max_workers = max(1, min(max_workers, len(tasks), os.cpu_count() or max_workers))
    add_qc(
        flags,
        MODULE,
        "INFO",
        "weather_parallel_execution",
        "ERA5 变量-年份任务采用线程池并行执行；当前 Windows 沙箱不允许 multiprocessing Pipe",
        {"tasks": len(tasks), "thread_workers": max_workers},
        blocking=False,
    )
    results: dict[tuple[int, str], pd.DataFrame] = {}
    extraction_qc: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_extract_era5_province_task, task): (task["source_year"], task["var"]) for task in tasks}
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                frame, qc_row = future.result()
            except Exception as exc:
                add_qc(flags, MODULE, "HARD_FAIL", "era5_parallel_task_failed", "ERA5 并行抽取任务失败", {"task": key, "error": repr(exc)})
                return
            results[key] = frame
            extraction_qc.append(qc_row)
    write_df(pd.DataFrame(extraction_qc), ctx.tables_dir / "weather_extraction_qc.csv")

    weather_years: list[pd.DataFrame] = []
    for source_year, year_times in times.groupby("source_year", sort=True):
        source_year_int = int(source_year)
        base = results[(source_year_int, "t2m")].rename(columns={"t2m": "temperature_c"})
        for var, out_col in [
            ("d2m", "dewpoint_c"),
            ("u10", "u10_ms"),
            ("v10", "v10_ms"),
            ("ssrd", "solar_wm2"),
            ("sp", "surface_pressure_pa"),
        ]:
            part = results[(source_year_int, var)].rename(columns={var: out_col})
            base = base.merge(part[["province_cn", "datetime_bj", out_col]], on=["province_cn", "datetime_bj"], how="left")
        weather_years.append(base)
    weather = pd.concat(weather_years, ignore_index=True)
    weather["weather_weight_method"] = "city_monthly_electricity_era5_grid_points"
    weather["relative_humidity_pct"] = 100.0 * np.exp(
        17.625 * weather["dewpoint_c"] / (243.04 + weather["dewpoint_c"])
        - 17.625 * weather["temperature_c"] / (243.04 + weather["temperature_c"])
    )
    weather["relative_humidity_pct"] = weather["relative_humidity_pct"].clip(0, 100)
    weather["wind_speed_ms"] = np.sqrt(weather["u10_ms"] ** 2 + weather["v10_ms"] ** 2)
    # Transparent BAIT approximation; method report documents that exact paper coefficients were unavailable.
    weather["bait_raw_c"] = weather["temperature_c"] + 0.002 * weather["solar_wm2"] - 0.7 * weather["wind_speed_ms"] + 0.03 * (weather["relative_humidity_pct"] - 50.0)
    weather.sort_values(["province_cn", "datetime_bj"], inplace=True)
    alpha = 1.0 - np.exp(-1.0 / 48.0)
    weather["bait_smoothed_c"] = weather.groupby("province_cn")["bait_raw_c"].transform(lambda s: s.ewm(alpha=alpha, adjust=False).mean())
    blend = 1.0 / (1.0 + np.exp(-(weather["temperature_c"] - 26.0) / 2.0))
    weather["bait_c"] = (1.0 - blend) * weather["bait_smoothed_c"] + blend * (0.5 * weather["bait_smoothed_c"] + 0.5 * weather["temperature_c"])
    weather["region"] = np.where(weather["province_cn"].isin(NORTH_PROVINCES), "north", "south")
    weather["heat_threshold_c"] = np.where(weather["region"].eq("north"), 14.713, 16.818)
    weather["cool_threshold_c"] = np.where(weather["region"].eq("north"), 22.253, 22.631)
    weather["hdd_hour"] = np.maximum(weather["heat_threshold_c"] - weather["bait_c"], 0.0) / 24.0
    weather["cdd_hour"] = np.maximum(weather["bait_c"] - weather["cool_threshold_c"], 0.0) / 24.0
    weather["year"] = weather["datetime_bj"].dt.year
    weather["month"] = weather["datetime_bj"].dt.month
    coeff = read_power_coefficients(ctx)
    thermal = weather.merge(coeff[["province_cn", "year", "p_heat_gwh_per_degree_day", "p_cool_gwh_per_degree_day"]], on=["province_cn", "year"], how="left")
    if thermal[["p_heat_gwh_per_degree_day", "p_cool_gwh_per_degree_day"]].isna().any().any():
        add_qc(flags, MODULE, "HARD_FAIL", "thermal_coeff_missing", "冷热系数无法覆盖天气特征表")
        return
    thermal["heating_load_mw"] = thermal["p_heat_gwh_per_degree_day"] * thermal["hdd_hour"] * 1000.0
    thermal["cooling_load_mw"] = thermal["p_cool_gwh_per_degree_day"] * thermal["cdd_hour"] * 1000.0
    write_df(
        weather[
            [
                "province_cn",
                "datetime_bj",
                "year",
                "month",
                "time_alignment_method",
                "weather_weight_method",
                "temperature_c",
                "dewpoint_c",
                "relative_humidity_pct",
                "wind_speed_ms",
                "solar_wm2",
                "surface_pressure_pa",
                "bait_raw_c",
                "bait_smoothed_c",
                "bait_c",
                "heat_threshold_c",
                "cool_threshold_c",
                "hdd_hour",
                "cdd_hour",
            ]
        ],
        ctx.tables_dir / "weather_features_hourly_province_2020_2024.csv.gz",
    )
    write_df(thermal, ctx.tables_dir / "heating_cooling_load_2020_2024.csv.gz")
    summary = thermal.groupby(["province_cn", "year"], as_index=False).agg(
        heating_energy_mwh=("heating_load_mw", "sum"),
        cooling_energy_mwh=("cooling_load_mw", "sum"),
        heating_peak_mw=("heating_load_mw", "max"),
        cooling_peak_mw=("cooling_load_mw", "max"),
        bait_min_c=("bait_c", "min"),
        bait_max_c=("bait_c", "max"),
    )
    write_df(summary, ctx.tables_dir / "heating_cooling_summary_by_province_year.csv")
    qc = thermal.groupby(["province_cn", "year"], as_index=False).agg(rh_min=("relative_humidity_pct", "min"), rh_max=("relative_humidity_pct", "max"))
    write_df(qc, ctx.tables_dir / "weather_weighting_qc.csv")
    write_df(coeff, ctx.tables_dir / "power_coefficients_long.csv")
    write_markdown(
        ctx.reports_dir / "method_report_module02.md",
        "# Module 02 Method Note\n\nBAIT is implemented as a transparent approximation using temperature, solar radiation, wind speed, humidity, 48-hour exponential smoothing, and high-temperature sigmoid blending. Exact paper-side fitted BAIT coefficients were not available in the input files, so this difference is reported explicitly.\n",
    )
    add_qc(flags, MODULE, "INFO", "module02_outputs", "模块 02 天气和冷热负荷输出完成", {"weather_rows": int(len(weather)), "thermal_rows": int(len(thermal))}, blocking=False)


def main() -> None:
    args = parse_args("Module 02: reconstruct weather features and thermal load")
    ctx = init_context(args, MODULE)
    flags: list[dict] = []
    paths = get_input_paths(ctx.config)
    try:
        required, missing = required_era5_files(paths["weather_root"])
        coverage = pd.DataFrame(
            [
                {"path": str(path), "exists": path.exists(), "required_for_strict_utc_to_bj": True}
                for path in required
            ]
        )
        write_df(coverage, ctx.tables_dir / "weather_file_coverage_module02.csv")
        if missing:
            write_boundary_fallback_report(ctx, missing, flags)
        build_weather_if_unblocked(ctx, flags)
    except Exception as exc:
        add_qc(flags, MODULE, "HARD_FAIL", "module02_exception", "模块 02 执行异常", repr(exc))
    module_exit(flags, ctx, "02", MODULE)


if __name__ == "__main__":
    main()
