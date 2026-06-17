# Power_curve_V2：2020–2024 省级负荷校准、冷热/EV分解与基础负荷提取执行方案

> 面向 Codex 的项目级执行文档。请在新文件夹 `D:/codeenv/pycharmproject/National_RL/Power_curve_V2` 中工作，不要复用旧版 `Power_curve` 运行目录。本文档强调“历史期总负荷校准与分解质量控制”，暂不开展未来 2030–2050 外推计算；未来外推只保留接口和参数说明。

---

## 0. 项目定位与核心逻辑

本项目的目标不是重新从零预测中国省级逐小时总负荷，而是在论文公开的 2020–2024 年省级逐小时负荷数据基础上，完成四件事：

1. **春节偏差校准**：对论文公开的 2020–2024 省级小时负荷曲线中春节所在月份进行偏差修正，并将修正差额按规则分配到同年其他小时，保证省级年度总电量严格不变。
2. **冷热负荷分解**：严格复现论文的天气敏感负荷方法，使用 BAIT/HDD/CDD、省级 HDD/CDD 阈值和论文给出的 `Power coefficient for heating/cooling`，计算逐省逐小时供暖与制冷负荷。空间气象加权在本项目主线中采用“城市月度用电权重”，但必须同时保留“论文人口加权口径”作为可选 baseline 或偏离说明。
3. **EV 负荷分解**：严格按照 `methodology_v1.md` 中广州实测校准逻辑，采用广州五类行为群体、六个环形高斯成分、96 点概率曲线、逐月单车日均充电电量和省级新能源汽车保有量，生成 2020–2024 省级 EV 充电负荷。EV 不得叠加到历史总负荷上，只作为总负荷内部拆分项。
4. **基础残值负荷提取**：利用春节校准后的总负荷减去冷热负荷和 EV 负荷，得到 2020–2024 的基础残值负荷，并提取可用于未来 2030–2050 负荷模拟的 8760 小时基础负荷模板。

历史期总量关系定义为：

```text
spring_adjusted_total_load_mw
= heating_load_mw
+ cooling_load_mw
+ ev_load_mw
+ base_residual_load_mw_raw
```

其中：

```text
base_residual_load_mw_raw
= spring_adjusted_total_load_mw
  - heating_load_mw
  - cooling_load_mw
  - ev_load_mw
```

必须同时保留论文原始总负荷：

```text
author_total_load_mw
```

并且永远不得被覆盖、修改或用校准值替代。

---

## 1. 工作根目录与输入数据

### 1.1 根目录

新项目根目录：

```text
D:/codeenv/pycharmproject/National_RL/Power_curve_V2
```

建议新增结构：

```text
Power_curve_V2/
  scripts/
  config/
  outputs/
  reports/
  README_codex_plan.md
```

所有脚本写入：

```text
D:/codeenv/pycharmproject/National_RL/Power_curve_V2/scripts
```

所有运行结果写入：

```text
D:/codeenv/pycharmproject/National_RL/Power_curve_V2/outputs/run_<timestamp>/
```

### 1.2 当前可见输入数据

根目录下已有以下数据或文件夹：

```text
2015_2024各省负荷_论文/
各省冷热系数/
省级矢量边界/
实际统调负荷_海南广东/
新能源汽车保有量/
城市月度省内用电占比_2022.xlsx
春节月份分省用电偏差分析_2010-2020.xlsx
```

Codex 首先必须完成数据清单，不得假定内部字段名称完全正确。

---

## 2. 方法边界：论文复现与本项目改进的关系

### 2.1 论文原始方法的关键点

论文构建了 2015–2024 年中国 31 个省级区域逐小时负荷曲线。其核心方法包括：

- 使用年度电量、小时气象变量和典型日负荷信息重构逐小时总负荷；
- 气象变量包括温度、风速、太阳辐射、相对湿度；
- 使用 BAIT（building-adjusted internal temperature）构造建筑感知温度；
- 通过 HDD/CDD 和供暖/制冷功率系数表示天气敏感负荷；
- 省级气象变量采用人口加权聚合；
- 跨年扩展时可使用年度用电量、空调拥有量和对应小时气象输入。

本项目对论文的处理原则是：

```text
总负荷：直接采用论文公开的 2020–2024 省级小时总负荷作为历史锚点。
冷热负荷：严格复现论文的 BAIT/HDD/CDD + Power coefficient 计算逻辑。
空间加权：主线采用城市月度用电权重；需同时标记这是对论文人口加权气象聚合方式的改进/替代，而不是完全一致复现。
```

### 2.2 是否需要使用各省空调保有量？

历史 2020–2024 主线计算中，**不要再额外使用各省空调保有量去修正冷热负荷**，原因如下：

1. 本项目直接使用论文 `Power coefficient for heating/cooling`。
2. 这些逐省逐年系数已经是论文根据历史负荷、气象关系和跨年空调拥有量变化得到的天气敏感负荷强度参数。
3. 如果在 2020–2024 计算中再次乘以空调保有量，会形成重复修正。

因此历史期计算公式为：

```text
heating_load_mw = p_heat_gwh_per_degree_day * hdd_hour * 1000
cooling_load_mw = p_cool_gwh_per_degree_day * cdd_hour * 1000
```

其中 `p_heat_gwh_per_degree_day` 和 `p_cool_gwh_per_degree_day` 直接来自论文系数表。

各省空调保有量只在未来外推时使用，例如将 2024 年系数外推到 2030–2050：

```text
p_cool_future = p_cool_2024 * (AC_ownership_future / AC_ownership_2024) ** eta
```

未来外推不是本轮执行主线，只需在报告中保留接口说明。

---

## 3. 总体模块顺序

请按以下模块顺序执行。每个模块完成后必须输出模块级 QC。出现 `HARD_FAIL` 必须停止。

```text
00_inventory_and_schema_check.py
01_read_and_spring_adjust_author_load.py
02_reconstruct_weather_and_thermal_load.py
03_reconstruct_ev_load.py
04_extract_base_residual_and_template.py
05_actual_load_validation_gd_hainan.py
06_figures_and_reports.py
```

模块之间的数据流：

```text
论文原始总负荷
  -> 春节偏差校准
  -> spring_adjusted_total_load_mw
  -> 减去 heating/cooling/EV
  -> base_residual_load_mw_raw
  -> 8760 基础负荷模板
```

---

## 4. QC 严重等级

所有 QC 标志必须分为三类：

| 等级 | 含义 | 是否阻断 |
|---|---|---:|
| `HARD_FAIL` | 会导致结果不可用或违反核心约束 | 是 |
| `SOFT_FAIL` | 可修复异常，修复后可继续 | 修复前阻断 |
| `WARN` | 不阻断，但必须写入报告 | 否 |

### 4.1 必须 HARD_FAIL 的情形

包括但不限于：

- 论文总负荷文件无法读取；
- 2020–2024 年任一年、任一省小时数无法解释；
- 省份映射失败或省份重复；
- 春节校准后省级年度总电量不闭合；
- `author_total_load_mw` 被覆盖或修改；
- `Power coefficient for heating/cooling` 缺失；
- BAIT/HDD/CDD 计算无法完成；
- EV 96 点权重严重不闭合且无法归一化；
- 冷热/EV/基础残值逐小时闭合误差超过阈值；
- 广东/海南真实负荷被错误用于校准全国负荷。

### 4.2 可 WARN 的情形

- EV 权重和为 `0.9999998` 这类浮点误差，归一化后记录 WARN；
- EV 保有量字段名为 `province` 而非 `province_cn`，可通过别名修复；
- 广东真实负荷存在少量非正值或缺失值；
- 城市月度用电权重覆盖率不足但有合理 fallback；
- 城市权重无法严格复现论文人口权重，应作为方法差异写入报告。

---

## 5. 模块 00：数据清单、字段识别与路径检查

脚本：

```text
scripts/00_inventory_and_schema_check.py
```

### 5.1 任务

1. 扫描 `Power_curve_V2` 根目录。
2. 识别各数据文件、sheet、字段、行数、年份、省份范围。
3. 建立统一省份名称映射表。
4. 检查广东/海南真实负荷文件格式。
5. 检查 EV 行为画像与省级保有量数据。
6. 检查城市月度用电权重文件字段。
7. 检查春节偏差系数文件字段。

### 5.2 输出

```text
outputs/run_<timestamp>/source_data_inventory.csv
outputs/run_<timestamp>/tables/schema_check_report.csv
outputs/run_<timestamp>/tables/province_name_mapping_report.csv
outputs/run_<timestamp>/tables/qc_flags_module00.csv
outputs/run_<timestamp>/path_check_report.md
```

### 5.3 字段别名规则

省份字段候选：

```text
province_cn, province, 省份, 省, 地区, region, Province
```

年份字段候选：

```text
year, 年份, Year
```

月份字段候选：

```text
month, 月份, Month
```

负荷字段候选：

```text
load, Load, electricity, Electricity, 用电量, 负荷, demand
```

严禁因为字段名不完全一致就直接失败；能确定语义时应标准化并记录。

---

## 6. 模块 01：论文负荷读取与春节偏差校准

脚本：

```text
scripts/01_read_and_spring_adjust_author_load.py
```

### 6.1 输入

```text
2015_2024各省负荷_论文/
春节月份分省用电偏差分析_2010-2020.xlsx
```

只使用 2020–2024 年。

### 6.2 读取论文总负荷

标准字段：

```text
province_cn
year
month
date_bj
hour_bj
datetime_bj
author_total_load_mw
```

如果论文数据单位为 GWh/h，则转换：

```text
author_total_load_mw = figshare_gwh * 1000
```

如果数据已经是 MW，必须通过字段名、数值量级和年度电量反推确认，不得盲目乘以 1000。

### 6.3 春节月份识别

春节日期：

| 年份 | 春节日期 | 春节月份 |
|---:|---|---:|
| 2020 | 2020-01-25 | 1 |
| 2021 | 2021-02-12 | 2 |
| 2022 | 2022-02-01 | 2 |
| 2023 | 2023-01-22 | 1 |
| 2024 | 2024-02-10 | 2 |

主线采用“春节所在月份”校准，而不是仅校准春节当天或春节周。

### 6.4 春节偏差系数解释

从 `春节月份分省用电偏差分析_2010-2020.xlsx` 中读取省级春节月份偏差系数。

Codex 必须先识别该文件中系数的语义，不得直接假定。

可接受的字段语义：

1. `spring_factor`：直接乘数，例如 `0.88` 表示春节月负荷乘以 0.88；
2. `bias_ratio`：偏差比例，例如 `-0.12` 表示春节月负荷下降 12%，转换为 `spring_factor=1+bias_ratio`；
3. `drop_ratio`：下降比例，例如 `0.12` 表示下降 12%，转换为 `spring_factor=1-drop_ratio`；
4. 若文件已给出分省、分月份、分年份系数，优先使用最细粒度；否则使用省级多年平均系数。

系数合理范围建议：

```text
0.60 <= spring_factor <= 1.10
```

超出范围为 `SOFT_FAIL`，需要输出人工检查表；明显荒谬则 `HARD_FAIL`。

### 6.5 春节月乘系数并保持全年总量不变

对省份 `p`、年份 `y`：

1. 原始年度电量：

```text
E_original = sum(author_total_load_mw over all hours in year)
```

2. 春节月份集合：

```text
S = hours where month == spring_month(y)
```

3. 非春节月份集合：

```text
R = all other hours in same province-year
```

4. 春节月校准：

```text
L_spring_adjusted[t] = author_total_load_mw[t] * spring_factor[p, y]
for t in S
```

5. 春节月校准造成的电量差额：

```text
delta_E = sum(author_total_load_mw[t] for t in S)
          - sum(L_spring_adjusted[t] for t in S)
```

若 `spring_factor < 1`，则 `delta_E > 0`，表示春节月被下调，需要把差额分配到其他小时。

6. 将差额按非春节小时原负荷比例分配：

```text
redistribution_factor = 1 + delta_E / sum(author_total_load_mw[t] for t in R)
L_spring_adjusted[t] = author_total_load_mw[t] * redistribution_factor
for t in R
```

7. 最终字段：

```text
spring_adjusted_total_load_mw = L_spring_adjusted
```

8. 年度闭合检查：

```text
abs(sum(spring_adjusted_total_load_mw) - sum(author_total_load_mw)) < 1e-6 * annual_peak_or_energy_scale
```

建议同时使用相对误差：

```text
relative_annual_energy_error < 1e-10
```

### 6.6 春节校准后的真实负荷对比

模块 01 结束后，必须立即生成广东、海南对比的第一版结果：

```text
author_total_load_mw vs actual_load_mw
spring_adjusted_total_load_mw vs actual_load_mw
```

指标：

```text
bias_mw
MAE
RMSE
NRMSE
MAPE
corr
coverage
peak_bias_mw
peak_time_error_hour
monthly_energy_error_pct
spring_month_error_pct
```

注意：

- 广东、海南真实负荷只用于验证；
- 不允许反向修改全国省级总负荷；
- 如果春节校准反而使广东/海南误差变大，也必须如实报告。

### 6.7 输出

```text
tables/author_load_2020_2024_long.csv.gz
tables/spring_adjustment_coefficients.csv
tables/spring_adjustment_energy_closure.csv
tables/actual_comparison_after_spring_adjustment.csv
tables/qc_flags_module01.csv
```

---

## 7. 模块 02：冷热负荷模拟

脚本：

```text
scripts/02_reconstruct_weather_and_thermal_load.py
```

### 7.1 输入

```text
各省冷热系数/
城市月度省内用电占比_2022.xlsx
省级矢量边界/
ERA5 或项目中已准备的小时气象数据
```

如果 ERA5 不在 `Power_curve_V2` 根目录下，应在 `config/run_config.yaml` 中显式配置路径。例如：

```yaml
weather_root: D:/National_model/Data/ERA5/original_data
```

### 7.2 空间加权：论文 baseline 与项目主线

论文原始口径：

```text
省级气象 = 省内人口加权的小时气象平均值
```

本项目主线：

```text
省级气象 = 省内城市月度用电权重加权的小时气象平均值
```

实现要求：

1. 若具备城市坐标或城市边界，则按城市逐小时气象加权；
2. 城市权重按月份变化，来自 `城市月度省内用电占比_2022.xlsx`；
3. 对每个省、每个月，城市权重必须满足：

```text
sum(city_weight within province-month) = 1
```

4. 若缺少城市坐标/边界，则不得假装完成城市加权。必须：
   - 输出 `city_weather_weighting_blocker_report.md`；
   - 若存在可用 fallback，则运行 fallback；
   - 在报告中明确“主线无法执行城市气象加权，已使用 fallback”。

推荐 fallback 顺序：

```text
城市用电权重 + 城市坐标/边界
> 省内人口网格权重（论文口径）
> 省域面积权重
> 省会/代表城市气象（仅作调试，不作正式主结果）
```

如数据允许，建议同时输出两套气象聚合结果：

```text
weather_weight_method = city_monthly_electricity
weather_weight_method = population_weighted_paper_baseline
```

正式主结果使用城市月度用电权重；论文复现对照使用人口权重。

### 7.3 ERA5变量转换

使用变量：

```text
t2m: 2m temperature, K
d2m: 2m dew point temperature, K
u10: 10m u wind, m/s
v10: 10m v wind, m/s
ssrd: surface solar radiation downwards, J/m2
sp: surface pressure, Pa, optional QC
```

转换：

```text
temperature_c = t2m - 273.15
dewpoint_c = d2m - 273.15
wind_speed_ms = sqrt(u10^2 + v10^2)
solar_wm2 = ssrd / 3600
```

相对湿度：

```text
relative_humidity_pct = 100 * exp(
    17.625 * dewpoint_c / (243.04 + dewpoint_c)
    - 17.625 * temperature_c / (243.04 + temperature_c)
)
```

截断：

```text
0 <= relative_humidity_pct <= 100
```

### 7.4 BAIT构造

必须尽量严格复现论文 BAIT 思路。保留所有中间字段。

原始 BAIT 由温度、太阳辐射、风速、湿度构造：

```text
bait_raw_c = f(temperature_c, solar_wm2, wind_speed_ms, specific_humidity_gkg)
```

根据论文，需考虑：

- 太阳辐射增加室内热感；
- 风速降低室内热感；
- 湿度在热天增强闷热感，在冷天增强寒冷感；
- 建筑热惯性通过 48 小时指数平滑表示；
- 高温条件下 BAIT 与室外温度按 sigmoid 混合。

若因论文公式细节无法完全复刻，Codex 必须：

1. 在代码中用函数名和注释明确当前公式；
2. 在 `method_report.md` 中列出与论文可能存在的差异；
3. 输出 BAIT 与温度的分布、相关性、极值 QC。

必须保留字段：

```text
temperature_c
dewpoint_c
relative_humidity_pct
specific_humidity_gkg
wind_speed_ms
solar_wm2
bait_raw_c
bait_smoothed_c
bait_c
```

### 7.5 HDD/CDD阈值

HDD/CDD 阈值不得再在代码中使用南北两个固定值。必须从 `各省冷热系数/Power coefficient.xlsx` 读取省级阈值字段：

```text
province_cn
heat_threshold_c
cool_threshold_c
```

允许字段别名包括但不限于：

```text
T_heat / t_heat_c / heat_threshold_c / heating_threshold_c / HDD阈值 / 供暖阈值 / 采暖阈值
T_cool / t_cool_c / cool_threshold_c / cooling_threshold_c / CDD阈值 / 制冷阈值
```

若 workbook 中没有可识别的省级阈值表，或 31 省覆盖不完整，模块 02 必须 `HARD_FAIL`，不得回退使用南北固定阈值。

阈值 QC 输出：

```text
hdd_cdd_threshold_workbook_inspection.csv
hdd_cdd_thresholds_by_province.csv
```

小时 HDD/CDD：

```text
hdd_hour = max(heat_threshold_c - bait_c, 0) / 24
cdd_hour = max(bait_c - cool_threshold_c, 0) / 24
```

### 7.5.1 2019 年末 ERA5 边界小时 fallback

ERA5 `valid_time` 视为 UTC。严格北京时间对齐下，目标北京时间 `2020-01-01 00:00—07:00` 需要 UTC `2019-12-31 16:00—23:00`。若本地缺少 2019 年 ERA5 文件：

```yaml
allow_2019_boundary_fallback: false
```

时必须 `HARD_FAIL`。

仅当配置显式设置为：

```yaml
allow_2019_boundary_fallback: true
era5_2019_boundary_fallback_method: next_day_same_local_hour
```

才允许 fallback。fallback 规则为：目标北京时间 `2020-01-01 00:00—07:00` 使用北京时间 `2020-01-02 00:00—07:00` 的 ERA5 气象值，即 UTC `2020-01-01 16:00—23:00`。该策略保持本地日内时钟一致，避免用 UTC `2020-01-01 00:00—07:00` 即北京时间 `08:00—15:00` 替代凌晨小时。受影响记录仅 8 小时，必须在 `weather_time_alignment_qc.csv` 与 method report 中显式标记。

年初 BAIT 平滑使用可用历史窗口，并按实际可用权重重新归一化。

### 7.5.2 完整 Module 02 运行前门控

在完整运行全国 2020—2024 前，必须先执行：

```powershell
& "C:\Users\ZZ\.conda\envs\RL\python.exe" -X utf8 scripts\02_reconstruct_weather_and_thermal_load.py --only-weights
& "C:\Users\ZZ\.conda\envs\RL\python.exe" -X utf8 scripts\02_reconstruct_weather_and_thermal_load.py --smoke-year 2020 --smoke-month 1
```

仅当上述两个检查均无 `HARD_FAIL` 时，才允许运行全国 2020—2024 完整 Module 02。

### 7.6 Power coefficients

从 `各省冷热系数/` 读取论文拟合好的逐省逐年系数。

标准字段：

```text
province_cn
year
p_heat_gwh_per_degree_day
p_cool_gwh_per_degree_day
```

不得在主线重新拟合这些系数。

冷热负荷：

```text
heating_load_mw = p_heat_gwh_per_degree_day * hdd_hour * 1000
cooling_load_mw = p_cool_gwh_per_degree_day * cdd_hour * 1000
```

解释：

- 系数单位为 `GWh / degree C / Day`；
- `hdd_hour` 和 `cdd_hour` 为小时 degree-day 分量；
- 乘以 1000 将 GWh/h 转为 MW。

### 7.7 输出

```text
tables/weather_features_hourly_province_2020_2024.csv.gz
tables/weather_weighting_qc.csv
tables/power_coefficients_long.csv
tables/heating_cooling_load_2020_2024.csv.gz
tables/heating_cooling_summary_by_province_year.csv
tables/qc_flags_module02.csv
```

---

## 8. 模块 03：EV负荷模拟

脚本：

```text
scripts/03_reconstruct_ev_load.py
```

### 8.1 方法来源

EV 模块必须严格按照 `methodology_v1.md` 的新版方法执行：

- 广州 15 分钟实测充电负荷校准；
- 5 类行为群体；
- 6 个环形高斯成分；
- 96 个 15 分钟概率点；
- 聚合为 24 小时电量权重；
- 使用广州逐月单车日均充电电量；
- 使用省级新能源汽车保有量；
- 历史期只采用 `current_pattern`，不引入行为演化情景。

### 8.2 EV行为群体

五类行为群体：

| 行为类型 | 充电电量贡献占比 | 主要峰值时刻 |
|---|---:|---:|
| 分散持续补能型 | 0.242782 | 8.708092 |
| 夜间集中充电型 | 0.361089 | 1.911315 |
| 日间补能型 | 0.105582 | 12.886760 |
| 早间补能型 | 0.164519 | 6.967729 |
| 晚间回程充电型 | 0.126028 | 20.134170 |

必须在报告中说明当前强假设：

```text
等效车辆占比 = 充电电量贡献占比
```

原因：当前没有分行为群体的单车日均充电电量。

### 8.3 六个环形高斯成分

| 行为类型 | 成分名称 | 权重 | 峰值时刻 | sigma(h) |
|---|---|---:|---:|---:|
| 夜间集中充电型 | 夜间尖峰 | 0.069839 | 0.526263 | 0.376192 |
| 夜间集中充电型 | 夜间延续 | 0.291249 | 1.911315 | 1.527093 |
| 早间补能型 | 早间补能 | 0.164519 | 6.967729 | 1.046837 |
| 日间补能型 | 午间补能 | 0.105582 | 12.886760 | 0.733610 |
| 晚间回程充电型 | 晚间回程 | 0.126028 | 20.134170 | 1.194739 |
| 分散持续补能型 | 分散背景 | 0.242782 | 8.708092 | 7.968137 |

环形距离：

```text
circular_distance = min(abs(hour - center), 24 - abs(hour - center))
```

每个成分先生成 96 个 15 分钟条件概率，再乘以成分权重得到 `weighted_probability`。所有成分相加得到 96 点总体概率。

### 8.4 96点到24小时权重

```text
f_15min_total(hour) = sum(weighted_probability over all components)
hour_int = floor(hour)
ev_hour_weight[hour_int] = sum(f_15min_total within same hour_int)
```

约束：

```text
sum(ev_hour_weight) = 1
```

若 96 点权重和与 1 的误差小于 `1e-6`，可归一化并记录 WARN。

### 8.5 逐月单车日均充电电量

主线不使用固定 `6.825408 kWh/辆·日`，改用广州逐月实测反推参数。

广州新能源汽车保有量基准：

```text
guangzhou_nev_stock_base = 1_450_000 vehicles
```

逐月参数：

| month | ev_kwh_per_vehicle_day | 来源 |
|---:|---:|---|
| 1 | 4.9674 | observed |
| 2 | 5.3994 | observed |
| 3 | 6.0624 | interpolated |
| 4 | 6.7253 | observed |
| 5 | 6.9293 | observed |
| 6 | 7.0477 | observed |
| 7 | 7.0151 | observed |
| 8 | 6.7304 | observed |
| 9 | 7.2206 | observed |
| 10 | 7.6859 | observed |
| 11 | 7.3551 | observed |
| 12 | 6.9834 | observed |

必须输出：

```text
tables/ev_monthly_energy_parameters.csv
```

### 8.6 省级新能源汽车保有量

输入：

```text
新能源汽车保有量/
```

规则：

- 2020–2023 优先使用实测或已整理省级数据；
- 2024 若缺失，使用 2021–2023 CAGR 外推；
- 若 CAGR 不可计算，回退为 2023 持平值；
- 字段名可用别名标准化，如 `province` -> `province_cn`。

输出字段：

```text
province_cn
year
nev_stock
ev_stock_source
```

### 8.7 省级历史EV负荷

逐省逐日 EV 电量：

```text
ev_daily_kwh = nev_stock * ev_kwh_per_vehicle_day[month_bj]
```

逐小时 EV 负荷：

```text
ev_load_mw = ev_daily_kwh * ev_hour_weight[hour_bj] / 1000
```

注意：

```text
EV负荷不得加到 spring_adjusted_total_load_mw 上。
EV负荷只是总负荷内部拆分项。
```

### 8.8 EV QC

输出指标：

```text
ev_annual_twh
ev_mean_mw
ev_peak_mw
ev_peak_hour_bj
ev_peak_share_pct
evening_ev_mean_mw
evening_ev_peak_mw
evening_ev_share_pct
nev_stock
ev_kwh_per_vehicle_day
ev_energy_month_source
ev_stock_source
```

晚峰窗口：

```text
18:00 <= hour_bj <= 22:00
```

输出：

```text
tables/ev_behavior_group_parameters.csv
tables/ev_behavior_component_parameters.csv
tables/ev_behavior_probability_96.csv
tables/ev_behavior_probability_hourly.csv
tables/ev_monthly_energy_parameters.csv
tables/ev_stock_cleaned_2020_2024.csv
tables/ev_load_2020_2024.csv.gz
tables/ev_load_parameters_and_qc.csv
tables/qc_flags_module03.csv
```

---

## 9. 模块 04：基础残值负荷与未来模板提取

脚本：

```text
scripts/04_extract_base_residual_and_template.py
```

### 9.1 基础残值定义

主定义：

```text
base_residual_load_mw_raw
= spring_adjusted_total_load_mw
  - heating_load_mw
  - cooling_load_mw
  - ev_load_mw
```

绘图字段：

```text
base_residual_load_mw_clipped = max(base_residual_load_mw_raw, 0)
```

注意：

- `raw` 必须保留；
- `clipped` 只能用于绘图或未来模板的敏感性版本；
- 如果出现大量负值，不能静默裁剪，必须诊断来源。

### 9.2 闭合测试

逐小时闭合：

```text
closure_error_mw = spring_adjusted_total_load_mw
    - (heating_load_mw + cooling_load_mw + ev_load_mw + base_residual_load_mw_raw)
```

要求：

```text
abs(closure_error_mw) < 1e-6
```

若不能满足则 HARD_FAIL。

### 9.3 8760 基础负荷模板

面向未来 2030–2050，提取 8760 小时基础负荷模板。历史 2020 和 2024 为闰年时必须删除 2 月 29 日或按统一规则处理。

建议输出两类模板：

#### 模板 A：逐年归一化模板

对每个省、每年：

```text
base_template_share[p,y,h] = base_residual_load_mw_raw[p,y,h] / sum(base_residual_load_mw_raw[p,y,:])
```

删除 2 月 29 日后保证：

```text
sum(base_template_share[p,y,:]) = 1
```

#### 模板 B：2020–2024 多年平均模板

```text
base_template_share_mean[p,h] = mean(base_template_share[p,y,h] over valid years)
```

再归一化：

```text
sum(base_template_share_mean[p,:]) = 1
```

如果 `raw` 存在负值较多，则另行输出：

```text
base_template_share_clipped_mean
```

并在报告中说明差异。

### 9.4 负值诊断

必须统计：

```text
negative_hours
negative_energy_mwh
negative_energy_share_pct
negative_hours_by_month
negative_hours_by_hour
negative_hours_by_component_dominance
```

若负值集中在：

- 夏季高温：检查 CDD 与制冷系数；
- 冬季寒潮：检查 HDD 与供暖系数；
- 夜间/凌晨：检查 EV 夜间尖峰是否过强；
- 春节月份：检查春节系数是否过度下调。

### 9.5 输出

```text
tables/hourly_province_load_components_2020_2024.csv.gz
tables/base_residual_qc.csv
tables/base_template_8760_by_year.csv.gz
tables/base_template_8760_mean_2020_2024.csv.gz
tables/base_template_8760_clipped_sensitivity.csv.gz
tables/component_energy_summary_by_province_year.csv
tables/qc_flags_module04.csv
```

---

## 10. 模块 05：广东、海南真实负荷验证

脚本：

```text
scripts/05_actual_load_validation_gd_hainan.py
```

### 10.1 输入

```text
实际统调负荷_海南广东/
```

需要自动识别并优先使用：

```text
2020-2024海南统调负荷.xls
20200101-20241231广东省负荷_t.xlsx
广东省负荷(2018-2023).csv
```

如文件夹中还有广州或地市负荷，可作为附加分析，但主验证只做广东省与海南省。

### 10.2 广东处理规则

- 若为 15 分钟负荷，转为小时均值；
- 非正值视为无效；
- 缺失小时不插值进入指标计算；
- 如果为绘图连续性进行插值，必须生成独立字段：

```text
actual_load_mw_plot_interp
```

并明确不得用于误差指标。

### 10.3 海南处理规则

- 若已为小时数据，直接使用；
- 非正值视为无效；
- 覆盖率 100% 时标记为高质量验证样本。

### 10.4 对比对象

必须比较：

```text
author_total_load_mw
spring_adjusted_total_load_mw
base_residual_load_mw_raw + heating_load_mw + cooling_load_mw + ev_load_mw
base_residual_load_mw_raw
```

第一、第二项用于检验春节校准是否改善真实负荷拟合；第三项用于验证闭合；第四项用于观察基础负荷形态是否合理，不应与真实总负荷直接等同。

### 10.5 输出指标

```text
coverage
bias_mw
MAE
RMSE
NRMSE
MAPE
corr
annual_energy_error_pct
monthly_energy_error_pct
peak_load_error_pct
peak_time_error_hour
summer_peak_error_pct
spring_month_error_pct
hourly_shape_corr_by_month
```

输出：

```text
tables/actual_load_comparison_guangdong_hainan.csv
tables/actual_load_monthly_comparison_guangdong_hainan.csv
tables/actual_load_peak_comparison_guangdong_hainan.csv
tables/qc_flags_module05.csv
```

---

## 11. 模块 06：图件与报告

脚本：

```text
scripts/06_figures_and_reports.py
```

### 11.1 必须生成的图

方法图：

```text
fig_01_method_flow
fig_02_spring_adjustment_energy_conservation
fig_03_weather_weighting_city_vs_population
fig_04_power_coefficients_by_province_year
fig_05_ev_behavior_probability_profile
fig_06_ev_monthly_kwh_per_vehicle
```

结果图：

```text
fig_07_author_vs_spring_adjusted_gd_hainan
fig_08_national_component_stack
fig_09_province_component_stack_examples
fig_10_heating_cooling_rankings
fig_11_ev_peak_share_by_province
fig_12_base_residual_negative_qc
fig_13_base_template_8760_examples
fig_14_actual_validation_error_summary
fig_15_spring_month_validation_improvement
```

每张图必须同时输出：

```text
PNG
PDF
plot_data CSV
```

### 11.2 必须生成的报告

```text
reports/01_method_process_report.md
reports/02_quality_control_report.md
reports/03_actual_load_validation_report.md
reports/04_future_template_description.md
```

报告中必须明确：

1. 原论文使用人口加权气象，本项目主线使用城市月度用电权重，是对空间气象聚合的改进/替代；
2. 历史冷热负荷不再额外使用空调保有量，因为已直接使用论文逐省逐年 `Power coefficients`；
3. EV 负荷来自广州行为画像，是全国历史期 EV 负荷的代理分解，不是各省真实充电负荷观测；
4. 春节校准保证年度总量不变，只改变小时形态；
5. 基础残值负荷不等于真实非天气负荷，而是“春节校准总负荷扣除冷热和EV后的模型残值”；
6. 未来外推时可使用 8760 基础负荷模板、未来空调保有量修正冷热系数、未来 EV 保有量修正 EV 负荷。

---

## 12. 最终主表字段

主输出：

```text
tables/hourly_province_load_components_2020_2024.csv.gz
```

字段至少包括：

```text
province_cn
year
month
date_bj
hour_bj
datetime_bj
is_leap_year
is_feb29
is_spring_month
spring_factor
spring_redistribution_factor
author_total_load_mw
spring_adjusted_total_load_mw
temperature_c
dewpoint_c
relative_humidity_pct
specific_humidity_gkg
wind_speed_ms
solar_wm2
bait_raw_c
bait_smoothed_c
bait_c
heat_threshold_c
cool_threshold_c
threshold_source_file
threshold_source_sheet
hdd_hour
cdd_hour
p_heat_gwh_per_degree_day
p_cool_gwh_per_degree_day
heating_load_mw
cooling_load_mw
nev_stock
ev_stock_source
ev_kwh_per_vehicle_day
ev_energy_month_source
ev_hour_weight
ev_load_mw
base_residual_load_mw_raw
base_residual_load_mw_clipped
closure_error_mw
weather_weight_method
humidity_method
bait_formula
time_alignment_method
fallback_reference_datetime_bj
```

---

## 13. 未来外推接口说明，不执行

本轮不模拟 2030–2050，但需要输出未来外推可用模板和参数说明。

未来可采用：

```text
future_total_load = future_base_residual + future_heating + future_cooling + future_ev
```

其中：

```text
future_base_residual[p,y,h] = annual_base_energy[p,y] * base_template_share_mean[p,h]
future_heating/cooling = future_power_coefficient[p,y] * future_HDD/CDD[p,y,h]
future_ev = future_nev_stock[p,y] * ev_kwh_per_vehicle_day[month] * ev_hour_weight[h] / 1000
```

未来 `Power coefficients` 可由空调保有量外推：

```text
p_cool_future = p_cool_2024 * (AC_ownership_future / AC_ownership_2024) ** eta
```

但这不是本轮主线。

---

## 14. Codex执行原则

1. 先检查数据，再写模型。
2. 每个模块只做本模块任务，不提前计算下游结果。
3. 不要为了跑完而跳过异常。
4. 不要修改论文原始总负荷字段。
5. 不要把 EV/冷热负荷叠加到历史总负荷。
6. 所有修正都要生成原始字段、修正字段和修正系数。
7. 所有图必须有对应绘图数据 CSV。
8. 所有关键结果必须可追溯到输入文件、脚本、参数和运行时间。

---

## 15. 建议给 Codex 的启动指令

```text
请在新文件夹 D:/codeenv/pycharmproject/National_RL/Power_curve_V2 中，严格按照 README_codex_plan.md 执行本项目。不要复用旧版 Power_curve 的输出目录。先执行模块 00，完成数据清单、字段识别、省份映射和 QC 分级。没有 HARD_FAIL 后再自动进入模块 01。整个项目的核心约束是：论文原始 author_total_load_mw 必须保留，春节校准只能生成 spring_adjusted_total_load_mw，EV/冷热负荷只做历史总负荷内部拆分，不得叠加到历史总负荷上。广东、海南真实统调负荷只用于验证，不得用于校准全国负荷。每个模块结束后输出 QC；若出现 HARD_FAIL 立即停止。
```
