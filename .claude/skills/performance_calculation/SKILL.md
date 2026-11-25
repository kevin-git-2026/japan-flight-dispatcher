---
name: performance_calculation
description: >-
  飞行性能与飞行计划计算的领域知识与对照工具：
  ISA 与速度换算；质点能量模型（升力、阻力、推力、燃油流量）与比航程；
  成本指数（Airbus kg/min、Boeing 100 lb/h）与 ECON 速度；OPT / MAX / REC MAX 巡航高度、阶梯爬升、日本巡航高度层、风–高度权衡；
  航程油分段积分；ICAO / EASA / 日本（航空法施行規則第153条、告示第319号）燃油政策。
  另含 OpenAP 2.6.2 与 opentop 2.7.0（按成本指数优化航迹、求航程油）的接口、机型覆盖、许可边界，
  以及对照 Airbus 公开例子与 EEA 表的实测偏差；附开发用 venv 里的对照脚本。
  当开发或调试本项目的 ETA 与巡航高度（dispatcher/eta.py、aircrafts.json 的 perf）、
  SimBrief 预填的高度 / CI / 燃油参数，或要按给定成本指数估算最优巡航高度与航程油耗时，使用本 skill。
---

# 飞行性能计算 (performance_calculation)

> 本文件给出计算链、决策流程、避坑与索引；细则按需读 `reference/` 下对应文件。
> 约定：
> - 正文中文，航空术语保留英文缩写（CI / ECON / OPT / SR / TOC …）；
> - 方法以 Airbus、Boeing 公开资料与 ICAO / EASA / 日本法规为准，OpenAP / opentop 作为参照实现；
> - **实测数值只写在 [reference/validation.md](reference/validation.md)**，其他文件按用例编号（E·T·B）引用。

## 一、计算链（7 环节）

| 环节 | 做什么 | 细则 |
| --- | --- | --- |
| ① 输入与重量 | ZFW、TOW、LW；MZFW / MTOW / MLW / MFC；重量迭代 | [fuel_policy.md](reference/fuel_policy.md) 第二节、[trip_fuel.md](reference/trip_fuel.md) 第二节 |
| ② 大气与速度 | ISA、dT、气压高度、CAS / TAS / Mach、风三角、**单位换算** | [atmosphere_speeds.md](reference/atmosphere_speeds.md) |
| ③ 性能模型 | 质点能量方程、阻力极曲线与波阻、推力、燃油流量、比航程、W/δ | [aircraft_model.md](reference/aircraft_model.md) |
| ④ 速度 | 成本指数、ECON、MRC / LRC、风对 ECON 的影响 | [cost_index.md](reference/cost_index.md) |
| ⑤ 高度 | OPT / MAX / REC MAX、1.3 g、阶梯爬升、日本高度层、风–高度、短程 | [cruise_altitude.md](reference/cruise_altitude.md) |
| ⑥ 航程油 | 分段积分、正推 / 反推迭代、最优控制、粗查表 | [trip_fuel.md](reference/trip_fuel.md) |
| ⑦ 燃油政策 | ICAO / EASA / 日本各分项、SimBrief 参数 | [fuel_policy.md](reference/fuel_policy.md) |

| 工具与对照 | 细则 |
| --- | --- |
| OpenAP 核心库：接口、单位、覆盖、坑、许可 | [openap.md](reference/openap.md) |
| opentop 优化器：接口、CI 换算、约束、局限 | [opentop.md](reference/opentop.md) |
| 本项目现状与约束（不含功能方案） | [project_state.md](reference/project_state.md) |
| 实测记录（环境、命令、结果、对照） | [validation.md](reference/validation.md) |
| 对照脚本与其依赖版本清单 | [scripts/openap_check.py](scripts/openap_check.py)、[scripts/requirements-lock.txt](scripts/requirements-lock.txt) |

## 二、决策流程：给定 CI，求最优巡航高度与航程油

1. **查机型覆盖**（[openap.md](reference/openap.md) 第四节，或脚本 `fleet`）：
   - 没有数据的机型（DH8D、CRJ7 等）：只能用本项目的 `eta` 剖面或 SimBrief；
   - 靠同义映射或通用油耗曲线的机型（A359、B772、A20N…）：结果只看量级。
2. **定重量**：
   - TOW = ZFW + 起飞油，油量又由燃油政策决定，所以要迭代；
   - 检查 MTOW / MLW / MZFW。国内短程宽体机常被 MLW 卡住。
3. **统一 CI 单位**为 kg/min：Boeing 的值 × 0.756。用 opentop 时再换成 `ci:N`，N = 100 × CI / (CI + 25.625)。
4. **求高度与速度**：
   - a. **网格**（脚本 `grid`）：在合法高度层 × Mach 上找每海里成本 (FF[kg/h] + 60 × CI) / GS 最小的点。
     - 约束：Mach ≤ MMO − 0.02；剩余爬升率 ≥ 300 fpm；不超过 ceiling（近似 REC MAX）。
     - 有风时用地速。
   - b. **最优控制**（脚本 `flight`，即 opentop `CompleteFlight`）：高度是连续量，要用 `--snap` 贴到合法层再带上限重解。
   - c. **短程**：全程最优高度低于按比航程求出的 OPT；opentop 的燃油最优会飞得又低又慢。
5. **航程油**：分段积分（重量递减），或取 opentop 的 `fuel`。再按燃油政策加应急油、备降油、最后储备油（日本：5% 应急；有备降场时在备降场上空待机 30 分钟，无备降场时在目的地上空待机 45 分钟）。
6. **对照**：与本项目 `eta`（时间、高度）、EEA 表（量级）、Airbus 公开例子比较。已知偏差见 [validation.md](reference/validation.md)。

## 三、OpenAP / opentop 能做与不能做

| | 能做 | 不能做 / 要小心 |
| --- | --- | --- |
| **OpenAP 核心库**（LGPL，数据 GPL v3） | ISA 与速度换算、推力、阻力极曲线、燃油流量、WRAP 的 ADS-B 统计 | **没有** OPT、CI、比航程、航程油的函数，要自己组合。油耗不随 Mach 变，窄体机 MRC / ECON 偏快；A320 的 OPT 偏高 3,200–4,600 ft，比航程约低 10%；宽体机推力偏紧，OPT 被压低 |
| **opentop**（MIT） | 按 `fuel` / `time` / `ci:N` 优化整条航迹，给出航程油、时间、高度与速度剖面 | `ci:N` 是 0–100 的权重，不是航司 CI。高度连续，不守高度层，无抖振约束；横向航迹自由；不含储备油；payload 模式落地时油量为零；起飞太重时可能给出靠烧油凑 MLW 的病态解；宽体机 Mach 偏低 |
| **本项目怎么用** | 只在开发用 venv 里对照、看趋势与量级 | `dispatcher/` 不导入，exe 不打包，代码与数据不复制进仓库 |

## 四、本项目现状（摘要，详见 [project_state.md](reference/project_state.md)）

- **没有质量、阻力、推力、油耗，也没有 CI**。`aircrafts.json` 的 `perf` 是运动学剖面（升降速度、升降率、巡航 Mach / TAS、典型高度、ceiling）。
- **巡航高度**是经验式 `eta.cruise_fl`（喷气 100 + 0.6 × NM，不超过 `perf.fl`）+ AIP 高度条件 + 东单西双，不是最优高度。
- **`eta.estimate`** 只积分时间（按 ISA，用 Open-Meteo 的 850–200 hPa 高空风）。对照 FlightAware 校准后中位误差 +1.3 分钟；A320 的空中时间与 opentop 相差约 1 分钟以内。
- **SimBrief 链接**不发 `fl`、`civalue` 与任何燃油、重量参数。
- **约束**：逻辑层只用标准库；OpenAP 只作对照；OpenAP 数据（GPL）、EEA 表（© EUROCONTROL）不入库；性能数据只写在 `aircrafts.json` 的 `perf` 里（用户定）。

## 五、对照脚本

只在开发用的独立 venv 里运行，不要装进打包 exe 用的全局 Python：

```
python -m venv <venv>
<venv>\Scripts\python -m pip install --only-binary=:all: -r .claude/skills/performance_calculation/scripts/requirements-lock.txt
<venv>\Scripts\python -B .claude/skills/performance_calculation/scripts/openap_check.py env
```

- [requirements-lock.txt](scripts/requirements-lock.txt) 是 [validation.md](reference/validation.md) 实测时的精确版本。
- 只装 `openap==2.6.2 opentop==2.7.0` 也能运行，但依赖的版本可能不同，结果不保证逐字一致。

| 子命令 | 用途 |
| --- | --- |
| `env [--sig]` | 版本与 API 冒烟检查（E01–E13）；`--sig` 打印签名 |
| `fleet [--types …]` | 日本机队在 OpenAP 里的数据覆盖 |
| `grid TYPE --mass KG \| --mass-frac F \| --mass-sweep a:b:n` | 核心模型网格：OPT、MAX、MRC / LRC、各 CI 的 ECON；可选 `--wind`、`--track`、`--h-max`、`--cl-max` |
| `flight TYPE ORIG DEST` | opentop 一次：`--m0` / `--tow` / `--payload`；`--ci-airbus` / `--ci-boeing` / `--objective`；`--h-max`、`--snap`、`--compare-eta` |
| `matrix [--case T0…T4] [--only B1…] [--eea xlsx]` | 重跑 [validation.md](reference/validation.md) 的全部用例并输出其表格 |

- 脚本只读地导入本项目的 `dispatcher.eta` / `dispatcher.aircraft`。
- 除 `--json` 指定的路径外不写文件；用全局 Python 运行时以退出码 2 结束，并打印上面的配置方法。

## 六、必读避坑

1. **CI 单位**：Airbus 用 kg/min，Boeing 用 100 lb/h（× 0.756 换成 kg/min）；opentop 的 `ci:N` 是 0–100 权重（[opentop.md](reference/opentop.md)）。
2. **OPT 离不开重量与 Mach**（W/δ）。没有重量就谈不上最优高度。重量大、气温高时，OPT 可能高于 REC MAX，这时只能先在较低高度巡航。
3. **高度上限**：REC MAX = min（以 +300 fpm 定的 service ceiling，1.3 g 抖振高度，证书最大高度）。OpenAP 没有抖振数据。
4. **有风时比较地面比航程**：每海里成本用地速算。LRC 不随风调整，ECON 会。
5. **日本高度层**：施行規則第177条，按磁航迹东单西双（FL410 以上每 4,000 ft）。opentop 不守这条。
6. **OpenAP 单位混用**：
   - `aero` 用 SI；`prop` 的 ceiling 是 m；WRAP 是 km；其余模型用 kt / ft / fpm / kg；`FuelFlow` 返回 kg/s（全机）。
   - **数组只传一维**，(N, 1) 形状会静默算错。
7. **`use_synonym`**：`FuelFlow` 内部会建 WRAP 并转交这个参数。A359、B772、A20N 等不加 `use_synonym=True` 就建不起来。
8. **opentop 2.7.0 与 README 不同**：
   - `trajectory()` 没有 `step_climb` 与 `h_max`；`m0` 是 MTOW 的比例；
   - payload 模式落地时没有余油；落地质量贴着 MLW 的解是病态解。
9. **模型偏差有方向**：
   - 窄体机：OpenAP 的 OPT 偏高、ECON 偏快；
   - 宽体机：OPT 被推力压低，opentop 的 Mach 偏低；
   - 只拿来比较同一模型内的相对差别最稳妥。
10. **许可**：
    - OpenAP 数据是 GPL v3，EEA 表 © EUROCONTROL，都不进仓库或随包数据；Airbus / Boeing 资料只短引；
    - OpenAP 只装在开发 venv，因为全局 Python 是 `flet pack` 的打包解释器。
