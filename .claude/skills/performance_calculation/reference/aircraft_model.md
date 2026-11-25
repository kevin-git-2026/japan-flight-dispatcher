# 飞机性能模型（aircraft_model）

> 上游：[../SKILL.md](../SKILL.md)。
> 来源：
> - Airbus《Getting to Grips with Aircraft Performance》（2002），第 F 章「Cruise」，以下简称 GTG；
> - Sun, Hoekstra & Ellerbroek, *OpenAP*, Aerospace 7(8):104, 2020（CC BY 4.0）；
> - OpenAP 2.6.2 源码（本机 venv 实测，2026-11-25）。
> 单位换算见 [atmosphere_speeds.md](atmosphere_speeds.md)；具体数值只在 [validation.md](validation.md)。

## 一、质点能量方程

把飞机当成一个质点，沿航迹的功率平衡（总能量模型，BADA 与 OpenAP 都用这一形式）：

```
(T − D) · V = m · g · dh/dt + m · V · dV/dt
L = m · g · cos γ          （定常；γ = 航迹角，sin γ = (dh/dt) / V）
```

- 平飞匀速：T = D；油耗由所需推力决定。
- 爬升：多出来的推力换成爬升率。**剩余爬升率** = (T_max − D) · V / (m g)，是 MAX 高度的判据（见 [cruise_altitude.md](cruise_altitude.md)）。
- 下降：通常慢车推力，下降率由 D 与速度计划决定。

## 二、升力与阻力

```
CL = m · g · cos γ / (½ · ρ · V² · S)
CD = CD0 + k · CL²          （阻力极曲线）
D  = ½ · ρ · V² · S · CD
```

- 同一 Mach 下有一个 L/D 最大的 CL\*（无波阻时 CL\* = √(CD0 / k)）。在最优高度巡航，就是在这个 CL 附近飞（GTG p.134）。
- **W/δ 关系**：Mach 与 CL 固定时，m / δ（δ = p / p0）为常数。所以重量减 1%，最优高度的气压也减 1%：平流层里约升高 208 ft，对流层上部也接近这个数。这就是阶梯爬升的来源。
- **波阻**：超过临界 Mach（Mcrit）后阻力急升，限制了实际可用的巡航 Mach。没有波阻项的模型会把 MRC 算得过快。

## 三、推力

- 最大爬升 / 巡航推力随高度（密度）降低而下降，也随 Mach 变化。所以重量大时可用高度受推力限制（MAX 高度）。
- 下降时为慢车推力。

## 四、燃油流量

燃油流量 FF = TSFC × T。TSFC（推力耗油率）随高度、Mach、推力等级变化。常见模型从粗到细：

| 模型 | 形式 | 说明 |
| --- | --- | --- |
| ICAO 排放数据库 4 点 | 起飞 100% / 爬升 85% / 进近 30% / 慢车 7% 推力的油耗 | 只覆盖起降循环（LTO），高空需另作修正 |
| OpenAP 1.x | 推力比的三次式 + 高度项 | 拟合 ICAO 4 点 |
| **OpenAP 2.x（现行）** | 每台发动机 FF = scale · c1 · [1 − exp(−c2 · x · e^(c3·x))]，x = 所需推力 / 最大推力 | **只与推力比有关**，没有高度项和 Mach 项。系数由 DGAC 的 Acropole 神经网络（以 QAR 数据训练）在 2022 年的 ADS-B 航迹上生成，再做拟合 |
| BADA（EUROCONTROL） | TSFC 随真空速变化 | 需许可，本项目不用 |
| FCOM 性能表 | 按重量 × 高度 × 温度查表 | 最准；属厂家资料，只能从公开示例里取少量数作对照 |

## 五、比航程

```
SR（空中）= TAS / FF        单位 NM/kg
SR（地面）= GS / FF
```

- MRC：SR 最大的速度。LRC：比 MRC 快、SR 为最大值 99% 的速度（GTG p.130）。
- Breguet 航程式 R = (V / (g · c_T)) · (L/D) · ln(W1 / W2)，只用于说明「速度 × 气动效率 / 耗油率」。实际计算用分段积分（见 [trip_fuel.md](trip_fuel.md)）。

## 六、OpenAP 模型带来的偏差（实测，详见 [validation.md](validation.md)）

- **油耗几乎不随 Mach 变化**：OpenAP 2.x 的燃油流量只取决于推力比，SR 一直随 Mach 上升，直到阻力急升。所以：
  - 开波阻时 MRC 落在 M0.79 左右（A320 FL350–370）；不开波阻时一路升到 MMO。
  - 窄体机的 ECON 在 CI ≥ 10 时几乎都顶到 MMO − 0.02，CI 对 Mach 几乎没有影响（T2）。
- **A320 巡航油耗偏高、最优高度偏高**：与 GTG p.148 的 A320 FCOM 巡航表（M.78、ISA）对比，FL350 的比航程只有表值的 0.89–0.91；按比航程求的 OPT 比表中最佳层高 3,200–4,600 ft（T4）。
- **宽体机推力偏紧**：A359、B772 在 0.70 MTOW、Mach 0.84–0.85 时，剩余爬升率 300 fpm 只够到 FL31x–FL35x；而不计推力约束时，SR 一直升到 FL429（T2）。所以宽体机的 OPT 是被推力限住的，偏低，方向与窄体机相反。
- **结论**：OpenAP 适合看趋势和量级。具体的最优高度、ECON Mach 要么先用 FCOM 类数据校准，要么只作相对比较。
