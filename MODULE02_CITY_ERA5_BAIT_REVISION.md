# Module 02 Revision: City-Weighted ERA5 Exposure and BAIT/HDD/CDD

This revision narrows Module 02 to 2020-2024 provincial hourly weather exposure,
BAIT, HDD/CDD, and heating/cooling load reconstruction.

## Method Boundary

- The module does not extend the historical load period beyond 2020-2024.
- ERA5 `valid_time` is treated as UTC and aligned to Beijing time by subtracting
  8 hours from each target Beijing timestamp.
- If 2019 year-end ERA5 boundary files are absent, fallback is allowed only when
  `allow_2019_boundary_fallback: true` is set. The fallback method is
  `next_day_same_local_hour`: Beijing-time 2020-01-01 00:00-07:00 uses ERA5
  values for Beijing-time 2020-01-02 00:00-07:00, corresponding to UTC
  2020-01-01 16:00-23:00. This avoids replacing early-morning local hours with
  UTC 2020-01-01 00:00-07:00, i.e. Beijing 08:00-15:00.
- Guangdong and Hainan actual load data remain validation-only and are not used.

## Spatial Weighting

The main weather exposure is not a clustering step. It is a city electricity
weighted exposure over ERA5 grid centers:

```text
X[p,m,t] = sum_c_in_p sum_g_in_c city_weight[c,m] / N[c] * X[g,t]
```

where `N[c]` is the number of ERA5 grid centers covered by city `c`. If a city
does not cover any ERA5 grid center, the nearest grid center to the city's
representative point is used and recorded as a fallback.

Module 02 writes:

- `city_monthly_power_weight.csv`
- `era5_city_grid_point_mapping.csv`
- `era5_grid_point_weights_by_month.csv`
- `era5_grid_point_weight_qc.csv`

Province-month final point weights must close to 1.

## ERA5 Reading

ERA5 extraction uses only `lat_idx/lon_idx` from
`era5_grid_point_weights_by_month.csv`. It processes each target
year-month-variable chunk separately and writes month-level temporary exposure
files before assembling the final 2020-2024 table. Within each target month,
ERA5 variables are extracted with configurable process parallelism. The default
is `weather_parallel_workers: 2`, with `weather_parallel_max_workers: 4` as the
memory-safety cap; `--weather-workers N` can override the configured value for a
specific run. `qc_flags_module02.csv` records configured, maximum, active worker
counts and the active execution mode.

## BAIT

The earlier transparent BAIT approximation is removed from the main result.
BAIT now follows the specified Eq.(4)-Eq.(11) structure:

```text
oBAIT = T + 0.012*(S-kS) - 0.2*(W-kW) + 0.05*(H-kH)*sign(T-16)
kS = 100 + 7*T
kW = 4.5 - 0.025*T
kH = exp(1 + 0.06*T)
```

`sBAIT` uses a finite 48-hour exponential window with weights
`exp(-0.10232*q)`, `q=0..48`. The humidity term `H` is computed from dewpoint
temperature and surface pressure as hourly specific humidity in `g/kg`; relative
humidity is retained only as a QC diagnostic.

HDD/CDD thresholds use the requested central-heating north/south rule:

```text
north: heat_threshold_c = 14.713, cool_threshold_c = 22.253
south: heat_threshold_c = 16.818, cool_threshold_c = 22.631
```

The north group contains 16 provinces: Beijing, Gansu, Hebei, Henan,
Heilongjiang, Jilin, Liaoning, Inner Mongolia, Ningxia, Qinghai, Shaanxi,
Shandong, Shanxi, Tianjin, Xinjiang, and Tibet. The south group contains the
remaining 15 provinces.

## Run Gate Before Full Module 02

Before running national 2020-2024 Module 02, execute:

```powershell
& "C:\Users\ZZ\.conda\envs\RL\python.exe" -X utf8 scripts\02_reconstruct_weather_and_thermal_load.py --only-weights
& "C:\Users\ZZ\.conda\envs\RL\python.exe" -X utf8 scripts\02_reconstruct_weather_and_thermal_load.py --smoke-year 2020 --smoke-month 1
```

Only run full Module 02 if both checks complete without `HARD_FAIL`.
