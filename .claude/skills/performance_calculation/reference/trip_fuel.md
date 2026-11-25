# 航程油耗（trip_fuel）

> 上游：[../SKILL.md](../SKILL.md)。
> 来源：
> - EASA AMC1 CAT.OP.MPA.181(b)（航程油的组成）；
> - Airbus《Getting to Grips with Aircraft Performance》（2002）第 F–H 章，以下简称 GTG；
> - Boeing AERO 2009 Q3「Fuel Conservation Strategies」（Jeppesen，Altus）；
> - EMEP/EEA 排放清单指南 2023，1.A.3.a 航空附件（Annex 1 master emissions calculator v1.5，© EUROCONTROL）。
> 访问 2026-11-25。数值见 [validation.md](validation.md)。

## 一、分段积分

航程油 = 起飞爬升（到 TOC）+ 巡航（TOC → TOD，含阶梯爬升）+ 下降（TOD → 进近开始）+ 进近着陆（EASA AMC1 CAT.OP.MPA.181(b)）。

每一段按小步积分，**每步按当时的重量**计算：

```
重量 m、高度 h、速度（CAS / Mach 计划）、风、温度
  → TAS、地速、所需推力、燃油流量 FF
  → dt = 步长 / 爬升率（升降段）或 步长 / 地速（巡航段）
  → 燃油 += FF · dt；重量 −= FF · dt
```

- **重量**：越重越耗油。AERO 2009 Q3 指出飞机的状态取决于它怎么飞到这一点，一次计划要做成千上万次计算。
- **风与温度**：按航段取预报风（地速）与气温（推力、TAS）。
- **爬升 / 下降**：FCOM 用表直接给出从松刹车到 TOC 的时间、距离、油量（GTG p.158 例子），以及从 TOD 到 1,500 ft 的对应数值（GTG p.167）。
- **TOD**：从终点往回推。下降距离由下降速度计划与下降率决定，本项目 `eta._descent` 也是从终点倒推。

## 二、重量的迭代

应急油与备降油取决于航程油，航程油又取决于起飞重量，所以要迭代：

1. **由落地重量反推（常用）**：
   - LW = ZFW + 备降油 + 最后储备油（+ 未用的应急油）；
   - 从目的地往回积分，得到 TOW 与航程油。
2. **由起飞重量正推**：先估 TOW，正向积分得航程油，再按燃油政策（[fuel_policy.md](fuel_policy.md)）算出总油量，修正 TOW，重复到收敛。

无论哪种，都要检查 MTOW、MLW、MZFW、MFC。

## 三、另一条路：最优控制

- opentop 把整条航迹（高度、速度、航向随时间变化）作为一个最优控制问题求解，目标可以是燃油、时间、成本指数等。见 [opentop.md](opentop.md)。
- 与分段积分的区别：
  - 高度是连续量（巡航中可以连续爬升）；
  - 只算空中段（100 ft → 100 ft），不含滑行油与各项储备；
  - 横向航迹也被优化，不沿实际航路。

## 四、粗查表

- **EMEP/EEA 2023 航空附件的 master emissions calculator**（数据 © EUROCONTROL），按机型给出：
  - **CCD**：3,000 ft 以上的爬升、巡航、下降，按大圆距离列出，附所用巡航层；
  - **LTO**：ICAO 标准起降循环，含滑行 26 分钟。
- **与 opentop 对比时的口径**：CCD + LTO 中的空中部分（起飞 0.7 分 + 爬升 2.2 分 + 进近 4.0 分，按 ICAO 发动机数据），再按大圆距离插值。
- 该表的重量、业载假设不明，只适合看量级。不要把表内容复制进仓库。

## 五、实测结论（详见 [validation.md](validation.md)）

- opentop 的燃油最优航程油与 EEA 表（同机型、同大圆距离）相比见 T4。
- 本项目 `eta` 的空中时间与 opentop 的结果对比见 T3。
- 与 GTG 爬升例子对比（T4）：
  - 本项目的 A320 爬升剖面（`perf.roc` 三段，与重量无关）爬到 FL330 只用 17 分钟、105 NM；
  - Airbus 例子（74 t 松刹车）是 23 分钟、146 NM，本项目在重量大时爬得过快。
