# enroute（飞行计划航路）规划

> 本项目核心。上游：[../SKILL.md](../SKILL.md)。端点取自 [sid.md](sid.md)/[star.md](star.md)/[iap.md](iap.md)，
> 字段格式见 [cifp_format.md](cifp_format.md)。

## 一、相关规则与数据基础（A* 建图）

### 航路命名（ICAO Annex 11；日本国内航路遵循此标准，并非特例）
- **国内航路**：传统 = H/J/V/W，RNAV = Q/T/Y/Z（故日本 V=传统、Y/Z=RNAV）
- **区域航路**：传统 = A/B/G/R，RNAV = L/M/N/P（如 A204 即区域传统航路）
- **前缀**：K=直升机低空 / U=高空 / S=超音速
- 一般**优先使用 RNAV 航路（Y/Z 等）**；若太绕或无法到达目的地，再回退传统航路（V、A 等）。

### 数据来源
- **AIP 航路**（case 0 的载体）：来自 `jp-routes.vercel.app` 的 `routes.csv`，列为
  `DEP,DEST,Time Restriction,Altitude,Aircraft,Route,Remarks`；按起降 ICAO 精确匹配。
- **航路图**（建 A* 图）：`earth_awy.dat`，每行一条航段，字段为
  `id1 reg1 t1  id2 reg2 t2  dir  lowhigh  baseFL topFL  name`
  - `t` = 航点类型（11=fix / 2=NDB / 3=VOR）
  - `dir` = N 双向 / F 正向 / B 反向（**A\* 不得逆向走 F、B 单向航路**）
    - ⚠️ **共挂段的 `N` 不可尽信**：有些单向 RNAV 航路与双向航路共挂同一物理航段时，earth_awy 把整段标 `N`（丢了单向性）。判方向应再参照 `routes.csv` 实飞向 / [route_templates.md](route_templates.md)（router 标名层即据此修正，避免标出逆向单向航路）。
  - `baseFL/topFL` = 该航段可用高度带；同段多名用 `-` 连接
- **节点键 = (ident, region)**（ident 全球重复，须配 region 去歧义）。

### 连通性（选端点的硬前提）
- 离场点 / 进场点 / 虚拟离场点都**必须落在航路网上**（在 earth_awy 图中有进/出边）。
- 剔除「孤立」航点（如 0 条边的进近 VOR）——这种应退到真正在航路上的 IAF/IF。
- 判定「某 IAF/IF 是否直接接在航路上」= 该点在 earth_awy 图中作为某 airway 的航点存在。

## 二、一般逻辑（决策树）

> 输入 dep、arr（RJ/RO ICAO）。先走 case 0；无直连 AIP 时按「dep 有无 SID」「arr 有无 STAR」分 case 1-4，并可用 case 5 桥接。

### case 0（最优先）
优先检查两机场间**有没有直连 AIP 航路**。有就直接使用 AIP 航路；没有，进入下面的流程。

### case 1：两端均有 SID 与 STAR（大机场）
1. 规划大圆航线，明确大圆航线的方向（自西向东 / 自北向南 等）；
2. 结合大圆方向，明确**离场点**（SID 尾 = enroute 头）的选择；
3. 通过和大圆比对，明确**进场点**（STAR 头 = enroute 尾）的选择；
4. 基于导航数据内航路与航点的连接规则（注意航路可能分运行方向和高度限制），用 **A\*** 寻找离场点与进场点之间**尽可能接近**（贴大圆）的航线。

### case 2：有 SID，无 STAR
1. 规划大圆、定方向；
2. 明确**离场点**（SID 尾 = enroute 头）；
3. 没有 STAR 时，按日本 AIP 规则有 2 种替代：
   - (a) 查落地机场跑道的 IAP。若 IAP 的 **IAF/IF 直接接在现有 enroute 后面**，则将该 IAF/IF 作 enroute 尾；
   - (b) 否则将机场**本场 VOR**（场内 VOR，如 RJTT 的 TTE，辨识见 [cifp_format.md](cifp_format.md)）作 enroute 尾；
4. A\* 连接。

### case 3：无 SID，有 STAR
1. 规划大圆、定方向；
2. 明确离场点：可挑一个**能最快切入航路的航点作为虚拟离场点**；
3. 通过和大圆比对，明确进场点（STAR 头 = enroute 尾）；
4. A\* 连接。

### case 4：起降机场均无任何进离场程序
1. 规划大圆、定方向；
2. 挑一个能**最快切入 ATS 航路的航点作为虚拟离场点**；
3. 没有 STAR 时同 case 2 的两种替代：
   - (a) IAP 的 IAF/IF 若直接接在 enroute 后面，则作 enroute 尾
     - 例1：RJTO 的 LOC Z 03 与 RNP 21 程序的 IAF（**SUNDO**）直接接在 **Y588** 航路上
     - 例2：RJER 的 RNP Z 07 与 LOC 25 程序的 IAF（**LEDAX**）直接接在 **A204** 航路上
   - (b) 否则用机场**本场 VOR**（场内 VOR）作 enroute 尾；
4. A\* 连接。

### case 5：借邻近机场的 AIP 航路（桥接，Rule 5）
- 当 dep/arr 之间无直连 AIP，但「dep 附近(≤50nm)某机场 D'」与「arr 附近(≤100nm)某机场 A'」之间存在官方 AIP 时，
  借该 `D'→A'` 的 AIP 航路中段，dep、arr 两端各 DCT 或 A\* 补接到其头/尾点。
- **仅当三道闸全过才用桥接**，否则回退到 case 1-4 的最优 A\*：
  - ① **干净**：补接段无大锐角弯；
  - ② **不过长**：≤ 1.25 × 最优 A\*；
  - ③ **不冲过头**：航路对目的地的最近点不在中途（超 20nm 即「过站折返」）。
- 例：RJFK-RJFR 无直连 AIP，但借 RJFK-RJFF 的官方 AIP + 补 `RJFF→RJFR` 本场段。
- **方向模板**：常用 AIP 走廊已按 8 个航向汇总在 [route_templates.md](route_templates.md)（真实高频连续子链 + 各方向高频航路/航点 + 单向航路方向）——选「借哪条 AIP」「补接走哪条干线」时，优先查与本航向一致的模板。

## 三、航路质量与距离判据（case 1-5 通用）
- **距离**：按**沿 airway 累加的真实航路长**（Dijkstra 沿 airway 补中间 fix、densify），而非大圆直线；
  随机规划的航程区间 (min/max) 应作用于该真实航路长，并显示「较大圆 +X%」偏差。
- **转弯**：enroute 段若出现接近掉头的大锐角（> ~100°），标记为可疑、提示人工复核。

## 四、特殊情况：分时段运行
- 日本很多机场为减噪，深夜会使用和白天**不一样的 AIP 航路**。需明确 AIP 航路中的 EOBT 与 ETA 等时间限制。
  - 例1：RJTT 深夜离场走 OPPAR4 SID + JYOGA/UTIBO 离场点
  - 例2：RJTT-RJBB 深夜使用和白天不一样的 AIP 航路（RJTT 深夜运行规则见项目根目录 `Haneda_night.pdf`）
- **可解析载体**：`routes.csv` 的 `Time Restriction` 列，格式 `EOBT/ETA HHMM-HHMM`（**UTC**、可跨午夜；
  复合写法 `EOBT a-b &ETA c-d`）。GUI 起飞时间为 **JST**，匹配时段需 **JST − 9h = UTC**。
- ⚠️ **脏列警告**：同一行的 `Altitude` / `Aircraft` 是自由文本「适用条件」（JET/DH8D/PROP 混地理、机场等条件），
  无法机器可靠精筛——**机型/高度应在按时间过滤后返回多条候选、交由用户自选，切勿程序自动选唯一**
  （v1.4.0 曾因强行精筛此两列而把整段功能删除、延后）。
