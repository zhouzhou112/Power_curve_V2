# Power_curve_V2 Review Status

This repository contains code and execution plans only. Raw input data, ERA5 files,
shapefiles, Excel workbooks, run outputs, and generated reports are intentionally
excluded from Git.

## Research Objective

Reconstruct and decompose China provincial hourly electricity load for historical
years 2020-2024, using the paper-provided provincial hourly load as the immutable
anchor.

Core invariants:

- Preserve `author_total_load_mw`; never overwrite it.
- Spring Festival calibration may only create `spring_adjusted_total_load_mw`.
- Heating, cooling, and EV loads are internal decompositions of historical total
  load; they must not be added on top of the historical total load.
- Guangdong and Hainan actual dispatch loads are validation-only datasets and
  must not calibrate the national/provincial load anchor.

## Execution Order

Run from `D:/codeenv/pycharmproject/National_RL/Power_curve_V2`:

```powershell
& "C:\Users\ZZ\.conda\envs\RL\python.exe" -X utf8 scripts\run_pipeline.py
```

The pipeline executes:

1. `00_inventory_and_schema_check.py`
2. `01_read_and_spring_adjust_author_load.py`
3. `02_reconstruct_weather_and_thermal_load.py`
4. `03_reconstruct_ev_load.py`
5. `04_extract_base_residual_and_template.py`
6. `05_actual_load_validation_gd_hainan.py`
7. `06_figures_and_reports.py`

Each module writes QC rows and stops the pipeline immediately on `HARD_FAIL`.

## Current Implementation State

Implemented files:

- `config/run_config.yaml`
- `scripts/power_curve_common.py`
- `scripts/run_pipeline.py`
- `scripts/00_inventory_and_schema_check.py`
- `scripts/01_read_and_spring_adjust_author_load.py`
- `scripts/02_reconstruct_weather_and_thermal_load.py`
- `scripts/03_reconstruct_ev_load.py`
- `scripts/04_extract_base_residual_and_template.py`
- `scripts/05_actual_load_validation_gd_hainan.py`
- `scripts/06_figures_and_reports.py`
- `codex_power_curve_v2_reset_plan.md`
- `MODULE02_CITY_ERA5_BAIT_REVISION.md`

Latest local syntax check:

```powershell
& "C:\Users\ZZ\.conda\envs\RL\python.exe" -X utf8 -c "from pathlib import Path; files=sorted(Path('scripts').glob('*.py')); [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in files]; print('syntax ok', len(files))"
```

Result: `syntax ok 9`.

## Important Known Decisions

- Module 01 uses the national Spring Festival coefficient as a recorded `WARN`
  fallback when a provincial coefficient is missing, currently needed for
  `Inner Mongolia` in the available workbook.
- Module 02 was corrected after user feedback: it should not use a single
  representative weather point per province. It maps ERA5 grid centers covered
  by city polygons, assigns each city's monthly electricity weight to its covered
  ERA5 points, then normalizes to province-month weights for hourly feature
  extraction.
- Module 02 now uses a two-stage flow: first write city/ERA5 mapping and
  province-month point weights, then stream ERA5 by target year-month-variable.
  It no longer holds all 5 years x 6 variables in a `results` dictionary.
- Module 02 defaults to single-worker ERA5 reading, capped at 2 if parallel
  reading is enabled later. This avoids local Windows NetCDF I/O contention.
- Strict UTC-to-Beijing alignment ideally requires 2019 year-end ERA5 files for
  the first eight Beijing-time hours of 2020. The current implementation allows
  fallback only when `allow_2019_boundary_fallback: true`; the fallback uses the
  next-day same-local-hour substitute, i.e. Beijing-time 2020-01-02 00:00-07:00
  for target 2020-01-01 00:00-07:00.
- BAIT is now implemented from the specified Eq.(4)-Eq.(11) structure, including
  specific humidity from dewpoint plus surface pressure and a finite 48-hour
  exponential window. The older transparent approximation is no longer used as
  the main result.
- Module 02 uses the requested central-heating north/south HDD/CDD thresholds:
  north is the 16 concentrated-heating provinces, south is the remaining 15
  provinces. The thresholds are `north: 14.713/22.253` and
  `south: 16.818/22.631` for heat/cool respectively.
- Latest Module 02 gate status after the threshold correction:
  `--only-weights` passed, and national
  `--smoke-year 2020 --smoke-month 1` passed with no `HARD_FAIL`. The January
  2020 smoke used 9,263 ERA5 grid points, produced 23,064 province-hour rows,
  and retained only the configured 2019-boundary fallback `WARN`.
  Full national 2020-2024 Module 02 has not been run in this turn because the
  smoke runtime implies a multi-hour full extraction.

## Review Priorities

Please review these points first:

- Whether `scripts/02_reconstruct_weather_and_thermal_load.py` correctly assigns
  city monthly electricity weights to all ERA5 points covered by each city and
  closes province-month weights to 1.
- Whether the Module 02 memory/performance strategy is sufficient for national
  hourly extraction without excessive RAM use.
- Whether the Spring Festival fallback in Module 01 is acceptable or should
  remain a blocking data issue.
- Whether Module 04's hourly identity check enforces:

```text
spring_adjusted_total_load_mw
= heating_load_mw + cooling_load_mw + ev_load_mw + base_residual_load_mw_raw
```

- Whether validation-only actual loads for Guangdong and Hainan are kept out of
  calibration paths.

## Excluded From Repository

Not uploaded:

- Raw author load workbooks.
- Spring Festival, EV stock, city weight, and power coefficient workbooks.
- ERA5 NetCDF files.
- Province/city shapefiles.
- Actual Guangdong/Hainan load workbooks.
- `outputs/` and `reports/` run products.

These files are required locally to execute the full pipeline, but not required
for code review.
