# CIFP / ARINC 424.20 数据格式（SID/STAR/APPCH 记录）

> `NavData/CIFP/<ICAO>.dat` 里 SID/STAR/APPCH 记录的字段与码表。三类记录**同构**
> （字段定义依 ARINC 424.20，X-Plane CIFP 规范）。[sid.md](sid.md) / [star.md](star.md) / [iap.md](iap.md) 都引用本文件。
> 数据已用真实 RJTT/KSFO 记录 + Navigraph DFD + X-Plane CIFP 规范交叉验证。
> 上游：[../SKILL.md](../SKILL.md)。

## 文件基本规则
- 行码与负载用冒号 `:` 分隔（`SID:` / `STAR:` / `APPCH:` / `RWY:` / `PRDAT:`）；字段用逗号 `,` 分隔；行尾分号 `;`。
- 多字符字段的**前导/对齐空格不可压缩**（描述码 4 字符、跑道 `RW34B`、推荐导航台等都靠列宽对齐）。
- 每条 SID/STAR/APPCH 记录 = 一个程序航段(leg)，**逗号字段约 35 个**，前 12 个最关键。
- `RW34B` 的尾字母：`B`=两条平行跑道通用（34L/R）；`L/R/C` 为具体跑道。

## 字段表（以真实行 `SID:020,4,ROVE3A,RW34B,TORAM,RJ,P,C,E   ,...` 为例）
> 字段号后括注 ARINC 424.20 的 `5.x` 字段定义号。

| 字段 | 含义 | ARINC 5.x | 例值 |
| --- | --- | --- | --- |
| 1 | 序号 seq | 5.12 | 020 |
| 2 | route-type（见[表4]） | 5.7 | 4 |
| 3 | 程序标识 | 5.9/5.10 | ROVE3A |
| 4 | transition 或跑道 | 5.11 | RW34B / INUBO / BACON / 空 |
| 5 | fix 航点 | 5.13 | TORAM |
| 6 | region 区域 | 5.14 | RJ |
| 7 | **section 段码**（见[表1]） | 5.4 | P |
| 8 | **subsection 子段码**（见[表1]） | 5.5 | C |
| 9 | **航点描述码 4 字符**（见[表2]） | 5.17 | `E   ` |
| 10 | 转弯方向 | 5.20 | L/R/空 |
| 11 | RNP | 5.211 | |
| 12 | **path & terminator 航段类型**（见[表3]） | 5.21 | DF |
| 13-17 | 推荐导航台 + 其 region/section/subsection | 5.23 等 | IKL,RJ,P,I |
| 18-22 | 弧半径 / θ(theta) / ρ(rho) / 磁航迹 / 距离 | 5.204/24/25/26/27 | 0430=043.0° |
| 23-25 | 高度描述(+至少 / -至多 / B区间 / I) / 高度1 / 高度2 | 5.29/5.30 | +,00700 |
| 26+ | 过渡高度、限速、垂直角、RF 圆心等航段几何 | | 14000 |

> 本项目端点选择基本只用 **字段 1-12 + section**；13 之后是推荐导航台/航段几何/高度。

## [表1] section(字段7,5.4) / subsection(字段8,5.5)
- `D` = VHF 导航台（VOR/DME/TACAN）—— 注意：`section==D` 只说明「它是个 VOR」，**未必是本场 VOR**
- `E` = enroute 航点；子段 `A`=enroute waypoint → **SID 出口 / 进场衔接点**（如 `ROVER,RJ,E,A`）
- `P` = 机场(terminal)；子段 `C`=终端航点（`TORAM,RJ,P,C`）、`G`=跑道（`RW16L,RJ,P,G`）、`N`=终端 NDB

→ **项目判据**：`section==E` ⇒ enroute 衔接点；本场 VOR 须**另判**（见下）。

### 本场 VOR 辨识（重要：section==D 不够）
本场 VOR 特指**设在机场内或跑道附近、用于引导飞机飞抵本场**的 VOR，而非「机场附近/航路上的任意 VOR」。
- 例：RJTT 的本场 VOR 是 **TTE**（东京 VOR/DME，场内），不是 **XAC**（XAC 是 XAC1K 这条 STAR 的入口 feeder VOR）。
- 辨识（XAC 与 TTE 都是 section D，不能只凭 section）：
  - **按位置**：取距机场基准点最近（场内/跑道旁，约 ≤ 几 NM）的 VOR/DME（`earth_nav.dat` 坐标判定）。
  - **佐证**：本场 VOR 常作为本场各进近(APPCH)的 recommended navaid（字段13-17，如 RJTT 进近多引用 TTE）。
- ⚠️ **项目影响**：router 选「本场 VOR」作 SID 枢纽 / enroute 尾时，须按上法取场内 VOR，
  勿把程序里出现的任意 `section-D`（如 XAC）当本场 VOR。

## [表2] 航点描述码（字段9,5.17，共 4 字符）
- **第1位 类型**：`E` 必要航点 / `V` VOR / `N` NDB / `G` 跑道 / `A` 机场 / `F` 航路外 / `Y` fly-over
- **第2位 终点**：`E` 航段或程序段终点 / `B` 整个 SID-STAR-进近终点 / `U` 未公布交叉点
- **第3位**：多为空；`S`=stepdown 下降定位点
- **第4位 进近(IAP)定位点分类**：`A`=IAF / `I`=IF(中间进近定位点) / `F`=FAF / `M`=MAP复飞点 / `B`=过渡末点(IAF↔final) / `H`=等待点
  - `A/I/F/M/B` 是进近专有分类，**仅出现在 APPCH 记录**；`H`(等待) STAR/APPCH 都可能见；SID 第4位通常为空。
  - （Navigraph DFD 另列 `C`=FACF、`D`=中间进近定位点、`E`=published FAF，各程序版本略有出入）

→ **项目判据**：在 **APPCH 记录**里取第4位 ∈ {A, I}（必要时含 B）的进近定位点 ⇒ 若其落在航路网上即可作 enroute 尾。

> ⚠️ **两个 "IF" 别混**：此处第4位的 **I=IF=Intermediate Fix**（进近中间定位点，**仅 IAP**）；
> [表3] path&terminator 的 **IF=Initial Fix**（程序首段航段类型，SID/STAR/APPCH 都用）。同写 "IF" 含义不同。

## [表3] path & terminator 航段类型（字段12,5.21）
- 到点：`IF`=Initial Fix(程序首段定位点) / `TF` 直飞到点 / `DF` 直接到点 / `CF` 航道到点 / `AF` 弧到点 / `RF` 定半径弧
- 到高度：`VA` 航向到高度 / `CA` 航道到高度 / `FA` 定位点到高度
- 截获或人工终止：`CI`、`VI` 截获 / `CR`、`VR` 到径向 / `FM`、`VM` 人工终止
- 距离或等待：`FC`、`FD`、`CD`、`VD` 到距离 / `HA`、`HF`、`HM` 等待 / `PI` 程序转弯

> ⚠️ 此 `IF`=Initial Fix（航段类型）≠ [表2] 第4位的 `I`=IF=Intermediate Fix（进近中间定位点，仅 IAP）。

## [表4] route-type（字段2,5.7）—— 4/5/6 是 1/2/3 的 RNAV 版
- **SID**：1/2/3 = 传统 跑道转换 / 公共 / enroute 转换；**4/5/6 = 对应 RNAV 版**
  （ROVE3A 为 RNAV-SID，故跑道段=4、enroute 转换段=6）
- **STAR**：1/2/3 = 传统 enroute 转换 / 公共 / 跑道转换；4/5/6 = RNAV 版
  （XAC1K=5 RNAV 公共；KSFO ALWYS3 含 4 enroute 转换 / 5 公共 / 6 跑道转换）
- **APPCH（字母）**：`I`=ILS / `D`=VOR-DME / `V`=VOR / `N`=NDB / `R`=RNAV-RNP / `X`=LDA / `P`=GPS / `J`=GLS / `A`=进近过渡（IAF→IF feeder）
