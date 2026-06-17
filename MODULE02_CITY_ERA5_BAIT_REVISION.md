# Module 02 Revision: City-Weighted ERA5 Exposure and BAIT/HDD/CDD

This revision narrows Module 02 to 2020-2024 provincial hourly weather exposure,
BAIT, HDD/CDD, and heating/cooling load reconstruction.

## Method Boundary

- The module does not extend the historical load period beyond 2020-2024.
- ERA5 `valid_time` is treated as UTC and aligned to Beijing time by subtracting
  8 hours from each target Beijing timestamp.
- If 2019 year-end ERA5 boundary files are absent, the first 8 Beijing-time
  hours of 2020 use an explicit 2020-boundary fallback and record a QC warning.
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
files before assembling the final 2020-2024 table. The default local reader is
single-worker, with an upper cap of 2 workers if parallelism is explicitly
enabled later.

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

The north/south HDD/CDD thresholds remain those documented in the reset plan:

```text
north: heat 14.713 C, cool 22.253 C
south: heat 16.818 C, cool 22.631 C
```
