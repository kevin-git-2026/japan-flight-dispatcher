# 本项目现状与约束（project_state）

> 上游：[../SKILL.md](../SKILL.md)。
> 按 2026-11-25 的代码（v2.2.0 之后的 main）核对。代码改了这里就会过时，改动 `dispatcher/eta.py`、`aircrafts.json` 的 `perf`、`dispatcher/weather.py`、`dispatcher/planner.py` 的 SimBrief 部分时请同步。
> 本文件只描述现状，不提功能方案。

## 一、计算链各环节在本项目里的对应

| 环节 | 本项目现状 |
| --- | --- |
| ① 输入与重量 | **没有质量**。`aircrafts.json`（212 型）每条只有 `id`、`icao`、`name`、`engines`（发动机型号，如 `CFM56-5B4/P`）、`pax`（座位数）、`cargo`、`search` 与 `perf`，没有 OEW / MZFW / MTOW / MLW / MFC，也没有业载 |
| ② 大气与速度 | `eta.isa` / `hpa_to_ft` / `tas_from_mach` / `tas_from_cas`：只用 ISA，不考虑 dT；与 OpenAP 偏差 ≤ 0.03%（E12） |
| ③ 性能模型 | **没有升力、阻力、推力、油耗模型**。`perf` 是运动学剖面，见下文 |
| ④ 速度 | 巡航为固定的 `perf.crz`（喷气为 Mach，螺旋桨为 TAS），**没有成本指数**，不随重量、风调整 |
| ⑤ 高度 | `eta.cruise_fl`：经验式 + AIP 高度条件 + `perf.ceil` + 东单西双，不是最优高度 |
| ⑥ 航程 | `eta.estimate` 只积分**时间**，不算油 |
| ⑦ 燃油政策 | 没有 |

## 二、`perf` 与 `eta` 的具体做法

**`perf` 字段**（F36，见 CLAUDE.md 的 `aircraft.py` 一行）：

| 字段 | 含义 |
| --- | --- |
| `cls` | jet / turboprop / piston |
| `clb` | [FL100 以下 CAS, FL100 以上 CAS, Mach] |
| `roc` | 三段爬升率：FL100 以下、FL100–240、FL240 以上，fpm |
| `crz` | 巡航速度：喷气为 Mach，螺旋桨为 TAS（kt） |
| `des` | [Mach, CAS, FL100 以下 CAS] |
| `rod` | 两段下降率：FL100 以上、以下，fpm |
| `fl` | 典型巡航高度 |
| `ceil` | 最高高度 |
| `app` | 最后进近速度 |
| `term` | 终端区余量，分钟 |

- 机型留空或查不到时，按 A320 估算并注明。

**`cruise_fl`**：

1. 用户给了 FL 就用它，以 `perf.ceil` 为限。
2. 否则按经验式：
   - 喷气机：min(`perf.fl`, 100 + 0.6 × 航程 NM)；
   - 涡桨：min(`perf.fl`, 50 + 0.6 × 航程)；
   - 活塞：min(`perf.fl`, 30 + 0.3 × 航程)。
3. 用 AIP 高度条件（`FL260+`、`FL180-FL230` 等）收窄上下限。
4. 不超过 `perf.ceil`，取整到 1,000 ft。
5. 按东单西双取合法层（磁差按 8°W 近似）。

**`estimate`**：

- 爬升按 1,000 ft 积分，速度为 CAS → CAS → Mach，爬升率按三段。
- 巡航按 20 NM 积分。
- 下降从终点往回积分，最后 10 NM（约 3,000 ft 起）用 `app` 速度。
- 另加 `term` 分钟的终端区余量；滑行固定 15 分钟。
- 爬升与下降放不下时，逐层降低巡航高度。
- 每一步都按当时的位置、高度、时刻取高空风，算地速。

**高空风**（`weather.fetch_winds_aloft`）：

- 来源：Open-Meteo `jma_msm`，取不到时用 `jma_gsm`。
- 层次：850 / 700 / 500 / 400 / 300 / 250 / 200 hPa，逐小时。200 hPa 约合 FL386，更高的高度用 200 hPa 的风。
- **没有取各层气温**（同一接口有这个变量）。

**校准**：F36 用 34 条 AIP 航线对照 FlightAware 排班的轮挡时间，中位误差 +1.3 分钟。`term` 与爬升剖面的偏差在这一步里被一并吸收了。

## 三、SimBrief 链接现在带什么

- `planner._build_simbrief_url` 只发：`orig`、`dest`、`type`、`airline`、`fltnum`、`route`、`deph` / `depm`（EOBT）、`origrwy` / `destrwy`。
- **不发**：`fl`、`civalue`、`units`、`pax`、`cargo`、`manualzfw`、`contpct`、`resvrule`、`taxiout` 等。SimBrief 用自己的缺省值与自己的性能数据出 OFP。
- 参数格式见仓库根目录 `simbrief_params.md`，燃油相关的对应见 [fuel_policy.md](fuel_policy.md)。

## 四、与外部结果的对照（实测，详见 [validation.md](validation.md)）

- **空中时间**：
  - A320 在 443 NM 以上的四条航线上，`eta` 与 opentop（燃油最优、无风）相差约 1 分钟以内（T3 B1）；
  - 羽田→伊丹（218 NM）上，opentop 慢飞，比 `eta` 多约 3 分钟。
- **巡航高度**：
  - A320 在 443 NM 以上时，`eta` 给 FL350–FL360；
  - opentop 燃油最优的巡航均高为 FL343–FL360；
  - EEA 表（EUROCONTROL）在该距离用 FL320–FL360（T3、T4）。
- **爬升**：
  - 与 Airbus GTG p.158 的 74 t 例子相比，`eta` 的 A320 爬升剖面到 FL330 快约 6 分钟、近约 40 NM（T4）；
  - `perf.roc` 不随重量变化，重量大时偏快。
- **宽体机**：opentop 继承 OpenAP 的偏差，燃油最优 Mach 只有 0.72–0.76，空中时间比 `eta` 长 3–11 分钟。这里不能把 opentop 当真值（T3 B4）。

## 五、约束

- **逻辑层只用标准库**：`dispatcher/` 的 27 个逻辑模块零第三方依赖。exe 用 `flet pack` 打包，并排除了 numpy 等。
- **OpenAP / opentop 只作对照**（F36 的决定，见 PRD「机型性能（用户定）」）：
  - 只在开发用的独立 venv 里运行；
  - `dispatcher/` 与 `flight_dispatcher.py` 都不导入，exe 不打包；
  - 不装进全局 Python，因为打包 exe 用的就是它。
- **数据许可**：
  - OpenAP 的数据目录是 GPL v3，**不把其系数、机型参数复制进 `aircrafts.json` 或其他随包数据**；
  - EEA / EUROCONTROL 的表不入库；
  - Airbus / Boeing 资料只在文档里短引；
  - 仓库是公开的，而且没有 LICENSE 文件。
- **用户定过的数据位置**：飞机性能数据写进 `aircrafts.json` 的 `perf`，不新建数据文件。
