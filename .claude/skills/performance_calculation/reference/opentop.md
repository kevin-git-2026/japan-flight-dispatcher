# opentop 轨迹优化器（opentop）

> 上游：[../SKILL.md](../SKILL.md)。
> 来源：opentop 2.7.0 源码（本机 venv 实测，2026-11-25）；PyPI；Sun, J. (2022) OpenAP.top, *Aerospace* 9(7):383（CC BY 4.0，https://doi.org/10.3390/aerospace9070383）。
> 数值见 [validation.md](validation.md)。

## 一、包与许可

| 项 | 情况 |
| --- | --- |
| 名称 | PyPI 上的 `opentop`（2.7.0，`import opentop`），即原 `openap-top` 的 v2。旧包 `openap-top` 停在 1.11.0，写法是 `from openap import top` |
| 仓库 | `github.com/junzis/opentop`（在维护）；`github.com/TUDelft-CNS-ATM/openap-top` 已停更 |
| 许可 | **MIT**（2.6.0 起；此前为 LGPL-3.0） |
| 依赖 | openap ≥ 2.6、casadi ≥ 3.7（自带 IPOPT）、cartopy、matplotlib、scikit-learn、xarray、pyproj、pyarrow；Python ≥ 3.11 |
| 导入 | `import opentop` 会连带导入 matplotlib 与 cartopy（`vis` 模块）。本机冷启动约 9 秒，要设 `MPLBACKEND=Agg` |

## 二、方法

- 把整条航迹当最优控制问题求解（CasADi Opti + IPOPT，Legendre 配点，多项式 3 阶）。
  - 状态：x、y、h、m、t；控制：Mach、垂直速度、航向。
- 节点数缺省为航程 ÷ 50 km，限在 20–120 之间（`setup(nodes=…)` 可指定）。
- IPOPT 缺省 tol 1e-6、acceptable_tol 1e-4、max_iter 3000，输出已静音。
- 本机实测每次 4–25 秒（A320 日本国内航线约 10 秒）。

## 三、接口（2.7.0 代码，`inspect.signature` 核对）

```
CompleteFlight(actype, origin, destination, m0=0.85, engine=None, use_synonym=False,
               dT=0.0, performance_model="openap", bada_path=None, payload=None)
  .setup(nodes=None, polydeg=3, debug=False, max_nodes=120, max_iter=3000, tol=1e-6, acceptable_tol=1e-4, ipopt_kwargs=None)
  .trajectory(objective="fuel", *, max_fuel=None, return_failed=False, initial_guess=None, …, result_object=False)
```

- `origin` / `destination`：ICAO 码（查 OpenAP 自带机场表）或 `(lat, lon)`。
- `Cruise` 另有 `h_min` / `h_max`，以及 `fix_cruise_altitude()`、`fix_mach_number()`、`allow_cruise_descent()`、`follow_track()`。
- 还有 `Climb`、`Descent`、`MultiAircraft`，以及航路网络优化（`RouteNetwork` 等），本 skill 不涉及。
- **`result_object=True`** 返回 `TrajectoryResult`，字段为 `df`、`success`、`status`、`objective`、`iters`、`fuel`、`grid_cost`、`stats`。
- **结果 DataFrame 的列与单位**：
  - `ts`（秒）；`altitude`（ft）；`h`（m）；`tas`（kt）；`mach`；`vertical_rate`（ft/min）；
  - `mass`（kg）；`fuel_cost`（该段用油 kg）；`fuelflow`（kg/s）；`heading`；`latitude` / `longitude`；`x` / `y`（m）；`grid_cost`。
  - `fuel` = 起始质量 − 落地质量 = Σ `fuel_cost`。
- **与 README 的差异**：README 写的 `step_climb`、`h_max`、`physical_validation` 等参数，2.7.0 的 `CompleteFlight.trajectory()` 没有。
  - `h_max` 由 `init_conditions(**kwargs)` 读取，缺省为机型 ceiling，但 `trajectory()` 不转交这个参数。
  - 对照脚本用子类覆盖 `init_conditions` 来设上限。

## 四、质量与约束（2.7.0 `base.py`、`full.py`）

- **`m0`**：起飞质量占 MTOW 的比例，固定不变。
- **`payload=`（kg）**：
  - 起飞质量变成优化变量，取值在 OEW + 业载 到 min(MTOW, OEW + 业载 + MFC) 之间；
  - 落地质量下限为 OEW + 业载。
  - 实测落地质量**正好等于 OEW + 业载**，即不留任何储备油（T3 B6）。要留储备，就把储备油加进 `payload`。
- **高度**：两端为 100 ft，上限为 ceiling（OpenAP 数据）或 `h_max`。
- **Mach**：上限为 MMO（不是 MMO − 0.02）。
- **垂直速度**：上限 2,500 ft/min。
- **分段**：节点按爬升 / 巡航 / 下降分段（节点 ≤ 40 或航程 ≤ 800 km 时各占三分之一）。巡航节点要求 |VS| ≤ 500 fpm 且不低于 FL150；爬升节点 VS ≥ 0，下降节点 VS ≤ 0。另有推力与能量约束。
- **解后检查**：最高高度不到 5,000 ft，或落地质量低于下限、高于 MLW，返回 `None` 并发警告。
  - **但并不总是如此**：B772 以 0.85 MTOW 飞羽田→福冈，优化器把 39.7 t 油用低空慢飞烧掉，刚好落在 MLW 之下，状态仍是「成功」（T3 B7）。
  - 对照脚本把落地质量 ≥ 0.995 MLW 的解标为病态解。
- **无抖振约束**。

## 五、目标函数

| `objective` | 含义 |
| --- | --- |
| `"fuel"` | 燃油（kg） |
| `"time"` | 时间（s） |
| `"ci:N"` | 时间成本与燃油成本的加权，N 为 0–100 |
| `"gwp100"` 等 | GWP20/50/100、GTP20/50/100 气候指标 |
| 可调用对象 | `f(x, u, dt, **kw)` 返回每段的代价 |

### `ci:N` 与航司 CI 的换算

- 每段代价（欧元）= (N/100) × (dt/60) × time_price + (1 − N/100) × 段用油 × fuel_price / 0.82。
  - 缺省 time_price = 25 EUR/min，fuel_price = 0.8 EUR/L。
  - 2022 年论文用的是 20 EUR/min 与 1 EUR/kg，与代码不同。
- 最小化它等价于最小化「燃油 + CI_eq × 分钟」，即 **CI_eq = 25.625 × N / (100 − N) kg/min**，反过来 **N = 100 × CI / (CI + 25.625)**（E11）。
  - Airbus CI 10 / 30 / 60 / 100 kg/min → ci:28.1 / 53.9 / 70.1 / 79.6；
  - ci:30 / 50 / 90 → 11.0 / 25.6 / 230.6 kg/min；
  - Boeing CI 先乘 0.756 换成 kg/min。
- N ≥ 100 时燃油权重为 0 或负，不要用；要最短时间就用 `"time"`。
- 用自定义目标「燃油 + 30 × 分钟」重跑，结果与 ci:53.93 完全相同（T3 B2c），证明换算正确。

## 六、局限

- **高度是连续量**：巡航中可以连续爬升（TOC 后仍以 ≤ 500 fpm 慢慢爬），不取整到合法高度层，也不守东单西双。
  - 对照脚本 `flight --snap` 取不高于巡航最高点、最接近巡航均高的合法层，以它为上限重解。
  - 按 TOC 高度贴层会偏低，多耗的油明显更多（[validation.md](validation.md)「贴层规则」）。
- **横向航迹自由**：只给起讫点时大致沿大圆，不走实际航路；给 `waypoints` 可约束。
- **气象**：温度只有一个标量 `dT`。风要用 `enable_wind(DataFrame)` 输入格点风，内部做多项式拟合。本 skill 没有接本项目的 Open-Meteo 风：沿一条线取的风点不足以拟合格点风场，而且它会去优化横向航迹。
- **继承 OpenAP 的偏差**：
  - 宽体机的燃油最优 Mach 偏低：B789 M0.76、A359 M0.73、B772 M0.72（T3 B4）；
  - 宽体机航程油比 EEA 表高 7–40%（T4）。
- **短程**：燃油最优会飞得又低又慢。A320 羽田→伊丹的巡航约 FL200、M0.59（T3 B1）。
