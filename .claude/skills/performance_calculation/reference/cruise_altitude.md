# 巡航高度（cruise_altitude）

> 上游：[../SKILL.md](../SKILL.md)。
> 来源：
> - Airbus《Getting to Grips with Aircraft Performance》（2002），以下简称 GTG：p.133–147；
> - Airbus Safety First #21（2016 年 1 月）「Control your speed… in cruise」，p.5；
> - Boeing AERO 2007 Q2；
> - 航空法施行規則第177条（e-Gov 法令 API）。
> 访问 2026-11-25。

## 一、几个高度的定义

| 名称 | 定义 | 随什么变 |
| --- | --- | --- |
| **OPT（最优高度）** | 给定重量与 Mach 时比航程最大的高度（GTG p.133）。CI > 0 时取每海里成本最小的高度（见 [cost_index.md](cost_index.md)） | 重量减轻则升高（W/δ 关系，见 [aircraft_model.md](aircraft_model.md)） |
| **MAX（最大巡航高度）** | 用最大巡航推力能保持该 Mach 平飞的最高高度（GTG p.138） | 重量减轻、气温降低则升高 |
| **Service ceiling** | 发动机还能以恒定 Mach 保持 **+300 ft/min** 爬升率的高度（Safety First #21 p.5） | 同上 |
| **抖振高度（buffet ceiling）** | 1.3 g 抖振边界对应的高度，约等于平飞中 40° 坡度时的载荷（GTG p.145；Safety First #21）。1.3 g 是**运行限制，不是法规限制**（GTG p.145） | 重量减轻则升高 |
| **证书最大高度** | 固定值 | — |
| **REC MAX（推荐最大高度）** | Safety First #21：REC MAX = min（service ceiling，气动（1.3 g）升限，证书最大高度）。GTG p.145：FMGS 的推荐最大高度取证书最大高度、最大巡航高度、1.3 g 抖振高度、爬升升限中的最低者 | — |

- 选高度的顺序：先满足 REC MAX，再按 CI 找成本最低的合法高度层。
- OPT 高于 REC MAX 时只能飞 REC MAX 以下。重量大、气温高时可能出现这种情况，这时只能先在较低的高度层巡航。

## 二、阶梯爬升

- 巡航中油烧掉、重量减轻，OPT 随之升高；理想状态是一直跟着 OPT 走（GTG p.147）。
- 只要待在 OPT ±2,000 ft 以内，SR 就 ≥ 最大值的 99%，所以实际做法是分段爬升（GTG p.147）。
- 台阶大小（GTG p.147 图）：FL290 以下 2,000 ft；FL290 以上 4,000 ft，RVSM 空域 2,000 ft。台阶由半圆规则决定。

## 三、日本的巡航高度层（航空法施行規則第177条）

计器飞行（IFR）、获准 RVSM 运行的航空机：

| 磁方位 | 41,000 ft 以下 | 41,000 ft 以上 |
| --- | --- | --- |
| 0° 以上、180° 未满 | 1,000 ft 的奇数倍（如 FL270、FL290、FL310、FL410） | 45,000 ft + 4,000 ft 的倍数 |
| 180° 以上、360° 未满 | 1,000 ft 的偶数倍（如 FL280、FL300、FL400） | 43,000 ft + 4,000 ft 的倍数 |

- 非 RVSM 航空机：29,000 ft 未满时规则同上；41,000 ft 以上时同上。
- 本项目 `dispatcher/eta.py` 的 `_level_ok` 即此规则。磁差按 8°W 近似（`MAG_VAR_W`）。

## 四、风–高度权衡

- 低于 OPT 时空中 SR 下降，但如果低高度的顺风更大，**地面** SR 可能反而更好，这时应在较低高度巡航（GTG p.136）。
- FCOM 有专门的「风–高度权衡」图表。GTG p.137 的例子：
  - 68 t、FL350 有 10 kt 逆风；
  - 降到 FL310 的最小风差是 23 kt，所以 FL310 的顺风要超过 13 kt 才值得降。
- 计算上，用地速算每海里成本（[cost_index.md](cost_index.md) 第二节）、逐高度比较即可。
- 对照脚本：`grid --wind 'FL:kt,…'` 输入一条风廓线。

## 五、短程

- 航程短时，爬升与下降占了大部分距离，飞到 OPT 不划算。**全程最优高度低于按比航程求出的 OPT**：
  - A320 羽田→福冈（476 NM），opentop 的燃油最优巡航在 FL335–350（T3 B1）；
  - 同一重量下按比航程求出的 OPT 是 FL395（T2）。
- 爬升 + 下降的水平距离超过航程时，必须降低巡航高度。本项目 `eta.estimate` 的做法是逐层降低，直到放得下。
- 日本国内的 AIP 航路常带高度条件（`routes.csv` 的 Altitude 列，如 `FL260+`）。本项目在 `timed.alt_matches` / `eta.cruise_fl` 里把它当作上下限。

## 六、用 OpenAP 求高度时的偏差（实测，详见 [validation.md](validation.md)）

- **A320 偏高**：按比航程求的 OPT 比 GTG p.148 FCOM 巡航表的最佳层高 3,200–4,600 ft；比 OpenAP 自己的 ADS-B 统计（WRAP 巡航高度众数 FL358）也高（T2、T4）。
- **宽体机偏低**：参考 Mach 下 OPT 被推力限住（T2）：
  - B772 0.70 MTOW 只到 FL319；
  - A359 0.70 MTOW、M0.85 时没有满足 300 fpm 剩余爬升率的高度；
  - 不计推力约束时，两者的 SR 都一直升到 ceiling。
- **opentop 的高度是连续量**，不取整到合法高度层，也不考虑抖振。要得到可飞的高度层，需贴到合法层后带上限重解（脚本 `flight --snap`；T3 B5 记录了限高的油耗代价）。
