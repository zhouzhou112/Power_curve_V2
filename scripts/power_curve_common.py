# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
START_YEAR = 2020
END_YEAR = 2024
YEARS = list(range(START_YEAR, END_YEAR + 1))

EXPECTED_PROVINCES = [
    "北京",
    "天津",
    "河北",
    "山西",
    "内蒙古",
    "辽宁",
    "吉林",
    "黑龙江",
    "上海",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "重庆",
    "四川",
    "贵州",
    "云南",
    "西藏",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
]

NORTH_PROVINCES = {
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

SPRING_MONTH = {2020: 1, 2021: 2, 2022: 2, 2023: 1, 2024: 2}
SPRING_DATE = {
    2020: "2020-01-25",
    2021: "2021-02-12",
    2022: "2022-02-01",
    2023: "2023-01-22",
    2024: "2024-02-10",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "project_root": str(PROJECT_ROOT),
    "output_root": "outputs",
    "report_root": "reports",
    "start_year": START_YEAR,
    "end_year": END_YEAR,
    "weather_root": "D:/National_model/Data/ERA5/original_data",
    "era5_time_policy": "strict_utc_to_bj",
    "author_load_workbook": "2015_2024各省负荷_论文/Data output.xlsx",
    "power_coefficient_workbook": "各省冷热系数/Power coefficient.xlsx",
    "spring_bias_workbook": "春节月份分省用电偏差分析_2010-2020.xlsx",
    "city_weight_workbook": "城市月度省内用电占比_2022.xlsx",
    "ev_stock_workbook": "新能源汽车保有量/17-23年新能源汽车保有量.xlsx",
    "actual_hainan_workbook": "实际统调负荷_海南广东/2020-2024海南统调负荷.xls",
    "actual_guangdong_workbook": "实际统调负荷_海南广东/20200101-20241231广东省负荷_t.xlsx",
    "actual_guangdong_legacy_csv": "实际统调负荷_海南广东/广东省负荷(2018-2023).csv",
    "province_shapefile": "省级矢量边界/中国_省_市_县shp格式/中国_省.shp",
    "city_shapefile": "省级矢量边界/中国_省_市_县shp格式/中国_市级行政区.shp",
}


@dataclass
class RunContext:
    root: Path
    run_dir: Path
    tables_dir: Path
    figures_dir: Path
    reports_dir: Path
    config: dict[str, Any]


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--run-dir", type=str, default=None)
    parser.add_argument("--config", type=str, default=str(PROJECT_ROOT / "config" / "run_config.yaml"))
    return parser.parse_args()


def _coerce_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
        if re.fullmatch(r"[-+]?\d+\.\d+", value):
            return float(value)
    except Exception:
        pass
    return value.strip("'\"")


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    path = Path(config_path) if config_path else PROJECT_ROOT / "config" / "run_config.yaml"
    if not path.exists():
        return config
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("run_config.yaml must be a mapping")
        config.update(data)
    except Exception:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            config[key.strip()] = _coerce_scalar(value)
    return config


def resolve_path(config: dict[str, Any], key: str) -> Path:
    value = str(config[key])
    path = Path(value)
    if not path.is_absolute():
        path = Path(config["project_root"]) / value
    return path


def init_context(args: argparse.Namespace | None = None, module_name: str | None = None) -> RunContext:
    config = load_config(getattr(args, "config", None))
    root = Path(config["project_root"])
    if getattr(args, "run_dir", None):
        run_dir = Path(args.run_dir)
        if not run_dir.is_absolute():
            run_dir = root / run_dir
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = root / str(config.get("output_root", "outputs")) / f"run_{stamp}"
    tables_dir = run_dir / "tables"
    figures_dir = run_dir / "figures"
    reports_dir = run_dir / "reports"
    for path in [run_dir, tables_dir, figures_dir, reports_dir]:
        path.mkdir(parents=True, exist_ok=True)
    if module_name:
        append_manifest(run_dir, {"event": "module_start", "module": module_name, "time": now_iso()})
    return RunContext(root=root, run_dir=run_dir, tables_dir=tables_dir, figures_dir=figures_dir, reports_dir=reports_dir, config=config)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append_manifest(run_dir: Path, payload: dict[str, Any]) -> None:
    path = run_dir / "run_manifest.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def rel(path: Path, root: Path = PROJECT_ROOT) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def add_qc(
    flags: list[dict[str, Any]],
    module: str,
    severity: str,
    check_id: str,
    message: str,
    details: Any = "",
    blocking: bool | None = None,
) -> None:
    severity = severity.upper()
    if blocking is None:
        blocking = severity == "HARD_FAIL"
    flags.append(
        {
            "module": module,
            "severity": severity,
            "check_id": check_id,
            "blocking": bool(blocking),
            "message": message,
            "details": json.dumps(details, ensure_ascii=False) if isinstance(details, (dict, list)) else str(details),
            "timestamp": now_iso(),
        }
    )


def write_qc(flags: list[dict[str, Any]], ctx: RunContext, module_number: str) -> Path:
    path = ctx.tables_dir / f"qc_flags_module{module_number}.csv"
    df = pd.DataFrame(flags)
    if df.empty:
        df = pd.DataFrame(columns=["module", "severity", "check_id", "blocking", "message", "details", "timestamp"])
    df.to_csv(path, index=False, encoding="utf-8-sig")
    append_manifest(
        ctx.run_dir,
        {
            "event": "qc_written",
            "module": module_number,
            "path": rel(path, ctx.root),
            "hard_fail": has_hard_fail(flags),
            "blocking_soft_fail": has_blocking_soft_fail(flags),
        },
    )
    return path


def has_hard_fail(flags: Iterable[dict[str, Any]]) -> bool:
    return any(str(row.get("severity", "")).upper() == "HARD_FAIL" for row in flags)


def has_blocking_soft_fail(flags: Iterable[dict[str, Any]]) -> bool:
    return any(str(row.get("severity", "")).upper() == "SOFT_FAIL" and bool(row.get("blocking", False)) for row in flags)


def fail_if_hard(flags: list[dict[str, Any]], ctx: RunContext, module_number: str) -> None:
    write_qc(flags, ctx, module_number)
    if has_hard_fail(flags):
        raise SystemExit(1)


def write_df(df: pd.DataFrame, path: Path, **kwargs: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        df.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip", **kwargs)
    else:
        df.to_csv(path, index=False, encoding="utf-8-sig", **kwargs)
    return path


def read_csv_smart(path: Path, **kwargs: Any) -> pd.DataFrame:
    for encoding in ["utf-8-sig", "utf-8", "gb18030", "gbk", "cp936"]:
        try:
            return pd.read_csv(path, encoding=encoding, **kwargs)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, **kwargs)


def normalize_province(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    paren = re.search(r"\(([^)]+)\)", s)
    if paren:
        s = paren.group(1)
    s = re.sub(r"\s+", "", s)
    s = s.replace("　", "")
    replacements = [
        ("维吾尔自治区", ""),
        ("壮族自治区", ""),
        ("回族自治区", ""),
        ("自治区", ""),
        ("特别行政区", ""),
        ("省", ""),
        ("市", ""),
    ]
    for old, new in replacements:
        s = s.replace(old, new)
    aliases = {
        "全国": "全国",
        "内蒙": "内蒙古",
        "广西壮族": "广西",
        "宁夏回族": "宁夏",
        "新疆维吾尔": "新疆",
        "西藏自治区": "西藏",
        "黑龍江": "黑龙江",
        "陝西": "陕西",
        "香港": "香港",
        "澳门": "澳门",
        "臺灣": "台湾",
        "台湾": "台湾",
    }
    if s in aliases:
        return aliases[s]
    english_aliases = {
        "Beijing": "北京",
        "Tianjin": "天津",
        "Hebei": "河北",
        "Shanxi": "山西",
        "InnerMongolia": "内蒙古",
        "Liaoning": "辽宁",
        "Jilin": "吉林",
        "Heilongjiang": "黑龙江",
        "Shanghai": "上海",
        "Jiangsu": "江苏",
        "Zhejiang": "浙江",
        "Anhui": "安徽",
        "Fujian": "福建",
        "Jiangxi": "江西",
        "Shandong": "山东",
        "Henan": "河南",
        "Hubei": "湖北",
        "Hunan": "湖南",
        "Guangdong": "广东",
        "Guangxi": "广西",
        "Hainan": "海南",
        "Chongqing": "重庆",
        "Sichuan": "四川",
        "Guizhou": "贵州",
        "Yunnan": "云南",
        "Tibet": "西藏",
        "Shaanxi": "陕西",
        "Gansu": "甘肃",
        "Qinghai": "青海",
        "Ningxia": "宁夏",
        "Xinjiang": "新疆",
    }
    compact_ascii = re.sub(r"[^A-Za-z]", "", s)
    if compact_ascii in english_aliases:
        return english_aliases[compact_ascii]
    if s in EXPECTED_PROVINCES:
        return s
    return s


def normalize_city(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    paren = re.search(r"\(([^)]+)\)", s)
    if paren:
        s = paren.group(1)
    s = re.sub(r"\s+", "", s)
    for suffix in ["市", "地区", "盟", "自治州", "藏族自治州", "回族自治州", "蒙古自治州", "哈萨克自治州"]:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def province_status(values: Iterable[Any]) -> dict[str, Any]:
    normalized = [normalize_province(v) for v in values]
    normalized = [v for v in normalized if v]
    unique = sorted(set(normalized))
    missing = [p for p in EXPECTED_PROVINCES if p not in unique]
    extra = [p for p in unique if p not in EXPECTED_PROVINCES and p != "全国"]
    duplicated = sorted({p for p in normalized if normalized.count(p) > 1})
    return {"count": len(unique), "missing": missing, "extra": extra, "duplicated": duplicated, "unique": unique}


def get_input_paths(config: dict[str, Any]) -> dict[str, Path]:
    return {
        "author_load": resolve_path(config, "author_load_workbook"),
        "power_coefficients": resolve_path(config, "power_coefficient_workbook"),
        "spring_bias": resolve_path(config, "spring_bias_workbook"),
        "city_weights": resolve_path(config, "city_weight_workbook"),
        "ev_stock": resolve_path(config, "ev_stock_workbook"),
        "actual_hainan": resolve_path(config, "actual_hainan_workbook"),
        "actual_guangdong": resolve_path(config, "actual_guangdong_workbook"),
        "actual_guangdong_legacy_csv": resolve_path(config, "actual_guangdong_legacy_csv"),
        "province_shapefile": resolve_path(config, "province_shapefile"),
        "city_shapefile": resolve_path(config, "city_shapefile"),
        "weather_root": Path(str(config["weather_root"])),
    }


def year_sheet_name(year: int) -> str:
    return f"{year} load curve"


def read_author_load_long(ctx: RunContext, years: Iterable[int] = YEARS) -> pd.DataFrame:
    paths = get_input_paths(ctx.config)
    workbook = paths["author_load"]
    frames: list[pd.DataFrame] = []
    for year in years:
        sheet = year_sheet_name(year)
        wide = pd.read_excel(workbook, sheet_name=sheet)
        first_col = wide.columns[0]
        unit = str(first_col)
        dt = pd.to_datetime(wide[first_col], errors="coerce")
        if dt.isna().any():
            bad = wide.loc[dt.isna(), first_col].head(5).astype(str).tolist()
            raise ValueError(f"{sheet} has invalid datetime rows: {bad}")
        wide = wide.copy()
        wide.rename(columns={first_col: "datetime_bj"}, inplace=True)
        value_cols = [c for c in wide.columns if c != "datetime_bj" and normalize_province(c) in EXPECTED_PROVINCES]
        if len(value_cols) != 31:
            raise ValueError(f"{sheet} expected 31 province columns, got {len(value_cols)}")
        long = wide[["datetime_bj"] + value_cols].melt(id_vars="datetime_bj", var_name="province_raw", value_name="author_load_raw")
        long["province_cn"] = long["province_raw"].map(normalize_province)
        long["author_load_raw"] = pd.to_numeric(long["author_load_raw"], errors="coerce")
        if unit.lower().find("gwh") >= 0:
            long["author_total_load_mw"] = long["author_load_raw"] * 1000.0
            unit_out = "GWh_per_hour_to_MW"
        elif unit.lower().find("mw") >= 0:
            long["author_total_load_mw"] = long["author_load_raw"]
            unit_out = "MW"
        else:
            median_value = float(long["author_load_raw"].median())
            if median_value < 500:
                long["author_total_load_mw"] = long["author_load_raw"] * 1000.0
                unit_out = "inferred_GWh_per_hour_to_MW"
            else:
                long["author_total_load_mw"] = long["author_load_raw"]
                unit_out = "inferred_MW"
        long["author_load_unit_interpretation"] = unit_out
        long["datetime_bj"] = pd.to_datetime(long["datetime_bj"])
        long["year"] = long["datetime_bj"].dt.year
        long["month"] = long["datetime_bj"].dt.month
        long["date_bj"] = long["datetime_bj"].dt.strftime("%Y-%m-%d")
        long["hour_bj"] = long["datetime_bj"].dt.hour
        long["is_leap_year"] = long["datetime_bj"].dt.is_leap_year
        long["is_feb29"] = (long["datetime_bj"].dt.month == 2) & (long["datetime_bj"].dt.day == 29)
        frames.append(
            long[
                [
                    "province_cn",
                    "year",
                    "month",
                    "date_bj",
                    "hour_bj",
                    "datetime_bj",
                    "is_leap_year",
                    "is_feb29",
                    "province_raw",
                    "author_load_unit_interpretation",
                    "author_total_load_mw",
                ]
            ]
        )
    result = pd.concat(frames, ignore_index=True)
    result.sort_values(["province_cn", "datetime_bj"], inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result


def author_load_hash(df: pd.DataFrame) -> str:
    cols = ["province_cn", "datetime_bj", "author_total_load_mw"]
    tmp = df[cols].copy()
    tmp["datetime_bj"] = pd.to_datetime(tmp["datetime_bj"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
    payload = tmp.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_spring_coefficients(ctx: RunContext) -> pd.DataFrame:
    path = get_input_paths(ctx.config)["spring_bias"]
    df = pd.read_excel(path, sheet_name="省级汇总")
    province_col = "省份"
    ratio_col = "平均较相邻月均值低幅"
    if province_col not in df.columns or ratio_col not in df.columns:
        raise ValueError(f"spring workbook missing required columns: {province_col}, {ratio_col}")
    out = df[[province_col, ratio_col, "可计算年份数", "缺失年份"]].copy()
    out["province_cn"] = out[province_col].map(normalize_province)
    out = out[out["province_cn"].isin(EXPECTED_PROVINCES + ["全国"])].copy()
    out.rename(columns={ratio_col: "drop_ratio"}, inplace=True)
    out["drop_ratio"] = pd.to_numeric(out["drop_ratio"], errors="coerce")
    out["spring_factor"] = 1.0 - out["drop_ratio"]
    out["spring_factor_source"] = "spring_bias_summary_mean_drop_ratio"
    return out[["province_cn", "drop_ratio", "spring_factor", "可计算年份数", "缺失年份", "spring_factor_source"]]


def parse_power_coefficient_sections(path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    xl = pd.ExcelFile(path)
    for sheet in xl.sheet_names:
        raw = pd.read_excel(path, sheet_name=sheet, header=None)
        for kind, field in [("heating", "p_heat_gwh_per_degree_day"), ("cooling", "p_cool_gwh_per_degree_day")]:
            hit: tuple[int, int] | None = None
            for r in range(min(3, raw.shape[0])):
                for c in range(raw.shape[1]):
                    val = raw.iat[r, c]
                    if isinstance(val, str) and kind in val.lower():
                        hit = (r, c)
                        break
                if hit:
                    break
            if not hit:
                continue
            label_row, start_col = hit
            year_row = label_row + 1
            province_col = max(0, start_col - 1)
            for row_idx in range(year_row + 1, raw.shape[0]):
                prov = normalize_province(raw.iat[row_idx, province_col])
                if prov not in EXPECTED_PROVINCES:
                    continue
                for c in range(start_col, raw.shape[1]):
                    y_raw = raw.iat[year_row, c]
                    try:
                        year = int(round(float(y_raw)))
                    except Exception:
                        continue
                    if year < 2010 or year > 2030:
                        continue
                    value = pd.to_numeric(raw.iat[row_idx, c], errors="coerce")
                    if pd.isna(value):
                        continue
                    rows.append(
                        {
                            "province_cn": prov,
                            "year": year,
                            "coefficient_type": kind,
                            field: float(value),
                            "source_file": rel(path),
                            "source_sheet": sheet,
                        }
                    )
    if not rows:
        raise ValueError(f"no power coefficient sections detected in {path}")
    df = pd.DataFrame(rows)
    heat = df[df["coefficient_type"] == "heating"][["province_cn", "year", "p_heat_gwh_per_degree_day", "source_file", "source_sheet"]]
    cool = df[df["coefficient_type"] == "cooling"][["province_cn", "year", "p_cool_gwh_per_degree_day", "source_file", "source_sheet"]]
    heat = heat.drop_duplicates(["province_cn", "year"], keep="first")
    cool = cool.drop_duplicates(["province_cn", "year"], keep="first")
    merged = heat.merge(cool, on=["province_cn", "year"], how="outer", suffixes=("_heat", "_cool"))
    return merged


def read_power_coefficients(ctx: RunContext) -> pd.DataFrame:
    paths = get_input_paths(ctx.config)
    candidates = [paths["power_coefficients"], paths["author_load"]]
    frames = []
    for path in candidates:
        if path.exists():
            try:
                frames.append(parse_power_coefficient_sections(path))
            except Exception:
                continue
    if not frames:
        raise ValueError("No readable power coefficients found")
    df = pd.concat(frames, ignore_index=True)
    # Prefer the dedicated workbook, then fill from the author workbook combined sheet.
    df["priority"] = np.where(df["source_file_heat"].fillna(df["source_file_cool"]).str.contains("各省冷热系数", regex=False), 0, 1)
    df.sort_values(["province_cn", "year", "priority"], inplace=True)
    df = df.groupby(["province_cn", "year"], as_index=False).first()
    return df.drop(columns=["priority"], errors="ignore")


def read_actual_hainan(ctx: RunContext) -> pd.DataFrame:
    path = get_input_paths(ctx.config)["actual_hainan"]
    df = pd.read_excel(path)
    dt_col, val_col = df.columns[:2]
    out = pd.DataFrame(
        {
            "province_cn": "海南",
            "datetime_bj": pd.to_datetime(df[dt_col], errors="coerce"),
            "actual_load_mw": pd.to_numeric(df[val_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["datetime_bj"])
    out.loc[out["actual_load_mw"] <= 0, "actual_load_mw"] = np.nan
    out["actual_load_source"] = rel(path, ctx.root)
    return out


def read_actual_guangdong(ctx: RunContext) -> pd.DataFrame:
    path = get_input_paths(ctx.config)["actual_guangdong"]
    raw = pd.read_excel(path, sheet_name=0, header=None)
    header = raw.iloc[1].tolist()
    body = raw.iloc[2:].copy()
    date_values = pd.to_datetime(body.iloc[:, 1], errors="coerce")
    value_cols = list(range(2, min(98, raw.shape[1])))
    records: list[pd.DataFrame] = []
    for col_idx in value_cols:
        time_label = header[col_idx]
        if pd.isna(time_label):
            continue
        time_str = str(time_label)
        td = pd.to_timedelta(time_str, errors="coerce")
        if pd.isna(td):
            continue
        values = pd.to_numeric(body.iloc[:, col_idx], errors="coerce") * 10.0
        dt = date_values + td
        part = pd.DataFrame({"datetime_bj": dt, "actual_load_mw": values})
        records.append(part)
    out = pd.concat(records, ignore_index=True)
    out = out.dropna(subset=["datetime_bj"])
    out.loc[out["actual_load_mw"] <= 0, "actual_load_mw"] = np.nan
    out["datetime_bj"] = pd.to_datetime(out["datetime_bj"]).dt.floor("15min")
    hourly = out.set_index("datetime_bj").resample("h")["actual_load_mw"].mean().reset_index()
    hourly["province_cn"] = "广东"
    hourly["actual_load_source"] = rel(path, ctx.root)
    return hourly[["province_cn", "datetime_bj", "actual_load_mw", "actual_load_source"]]


def read_actual_validation_loads(ctx: RunContext) -> pd.DataFrame:
    return pd.concat([read_actual_guangdong(ctx), read_actual_hainan(ctx)], ignore_index=True)


def safe_corr(a: pd.Series, b: pd.Series) -> float:
    valid = a.notna() & b.notna()
    if valid.sum() < 3:
        return np.nan
    if a[valid].std() == 0 or b[valid].std() == 0:
        return np.nan
    return float(a[valid].corr(b[valid]))


def compare_to_actual(
    model: pd.DataFrame,
    actual: pd.DataFrame,
    model_cols: list[str],
    module_label: str,
) -> pd.DataFrame:
    base_cols = ["province_cn", "datetime_bj", "year", "month"]
    merged = model[base_cols + model_cols].merge(actual, on=["province_cn", "datetime_bj"], how="inner")
    rows: list[dict[str, Any]] = []
    for province in ["广东", "海南"]:
        sub_p = merged[merged["province_cn"] == province].copy()
        for col in model_cols:
            valid = sub_p[col].notna() & sub_p["actual_load_mw"].notna()
            sub = sub_p.loc[valid].copy()
            if sub.empty:
                rows.append({"module": module_label, "province_cn": province, "model_col": col, "coverage": 0})
                continue
            err = sub[col] - sub["actual_load_mw"]
            actual_mean = sub["actual_load_mw"].mean()
            actual_peak = sub["actual_load_mw"].max()
            peak_model_idx = sub[col].idxmax()
            peak_actual_idx = sub["actual_load_mw"].idxmax()
            peak_time_error = abs((sub.loc[peak_model_idx, "datetime_bj"] - sub.loc[peak_actual_idx, "datetime_bj"]).total_seconds()) / 3600.0
            monthly = sub.groupby(["year", "month"])[[col, "actual_load_mw"]].sum()
            monthly_pct = (monthly[col] - monthly["actual_load_mw"]) / monthly["actual_load_mw"] * 100.0
            spring_mask = sub.apply(lambda r: int(r["month"]) == SPRING_MONTH.get(int(r["year"]), -1), axis=1)
            if spring_mask.any():
                spring_model = sub.loc[spring_mask, col].sum()
                spring_actual = sub.loc[spring_mask, "actual_load_mw"].sum()
                spring_error_pct = (spring_model - spring_actual) / spring_actual * 100.0 if spring_actual else np.nan
            else:
                spring_error_pct = np.nan
            rows.append(
                {
                    "module": module_label,
                    "province_cn": province,
                    "model_col": col,
                    "coverage": float(valid.mean()),
                    "valid_hours": int(valid.sum()),
                    "bias_mw": float(err.mean()),
                    "MAE": float(err.abs().mean()),
                    "RMSE": float(np.sqrt(np.mean(np.square(err)))),
                    "NRMSE": float(np.sqrt(np.mean(np.square(err))) / actual_peak) if actual_peak else np.nan,
                    "MAPE": float((err.abs() / sub["actual_load_mw"]).replace([np.inf, -np.inf], np.nan).mean() * 100.0),
                    "corr": safe_corr(sub[col], sub["actual_load_mw"]),
                    "peak_bias_mw": float(sub[col].max() - actual_peak),
                    "peak_time_error_hour": float(peak_time_error),
                    "monthly_energy_error_pct": float(monthly_pct.mean()),
                    "monthly_abs_energy_error_pct": float(monthly_pct.abs().mean()),
                    "spring_month_error_pct": float(spring_error_pct) if not pd.isna(spring_error_pct) else np.nan,
                    "actual_mean_mw": float(actual_mean),
                    "actual_peak_mw": float(actual_peak),
                }
            )
    return pd.DataFrame(rows)


def write_markdown(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def latest_table(ctx: RunContext, name: str) -> Path:
    return ctx.tables_dir / name


def module_exit(flags: list[dict[str, Any]], ctx: RunContext, module_number: str, module_name: str) -> None:
    qc_path = write_qc(flags, ctx, module_number)
    append_manifest(
        ctx.run_dir,
        {
            "event": "module_end",
            "module": module_name,
            "time": now_iso(),
            "qc_path": rel(qc_path, ctx.root),
            "hard_fail": has_hard_fail(flags),
            "blocking_soft_fail": has_blocking_soft_fail(flags),
        },
    )
    if has_hard_fail(flags):
        raise SystemExit(1)


def read_required_table(ctx: RunContext, filename: str, **kwargs: Any) -> pd.DataFrame:
    path = ctx.tables_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"required upstream table missing: {path}")
    if filename.endswith(".gz"):
        kwargs.setdefault("compression", "gzip")
        return pd.read_csv(path, **kwargs)
    return pd.read_csv(path, **kwargs)


def ensure_datetime(df: pd.DataFrame, col: str = "datetime_bj") -> pd.DataFrame:
    df = df.copy()
    df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


if __name__ == "__main__":
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
