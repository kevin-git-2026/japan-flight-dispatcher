---
name: route_planning
description: >-
  日本(RJ/RO)真实世界航路规划的领域知识与技巧——起降机场选择、SID/STAR/IAP 端点选择、
  无直连 AIP 时的本地 A* enroute 寻路、邻近机场 AIP 桥接(Rule 5)、CIFP/ARINC 424.20
  数据格式(字段与码表)、本场 VOR 辨识、航路真实长度/质量判据、分时段(Time Restriction)规则、8 方向常见 AIP 航路模板与单向航路方向。
  当开发或调试本项目的航路生成(dispatcher/router.py、planner)、判断「两个日本机场之间该如何规划
  enroute 航路」、或解析 NavData/CIFP 的 SID/STAR/APPCH 记录时，使用本 skill。
---

# 日本航路规划 (route_planning)

> 本项目（日本航班智能搜索与规划）的航路规划领域知识底座。本文件给**总流程 + enroute 决策树 + 索引 + 避坑**；
> 数据格式与各环节细则按需读 `reference/` 下对应文件（渐进式披露）。
> 约定：正文中文、航空术语保留英文缩写（SID/STAR/IAF/IF/VOR/enroute…）。
> 数据已用真实 RJTT/KSFO 的 CIFP 记录 + Navigraph DFD + X-Plane CIFP 规范交叉验证。

## 一、总流程（7 环节）

一次完整航路规划分 7 步：
**1. 起飞机场选择 → 2. 落地机场选择 → 3. SID → 4. enroute（本项目核心）→ 5. STAR → 6. 进场跑道 → 7. IAP**

本项目侧重第 4 步 **enroute**；SID/STAR/IAP 在 enroute 决策里主要作为**端点来源**被引用（离场点/进场点）。

| 环节 | 细则文件 |
| --- | --- |
| 概述 + 7 环节 + 项目侧重 | [reference/planning_basics.md](reference/planning_basics.md) |
| 起飞机场选择（跑道长度等） | [reference/dep_apt.md](reference/dep_apt.md) |
| 落地机场选择 | [reference/arr_apt.md](reference/arr_apt.md) |
| **CIFP/ARINC 424 数据格式**（字段表 + 码表 + 本场 VOR 辨识） | [reference/cifp_format.md](reference/cifp_format.md) |
| SID 选择 | [reference/sid.md](reference/sid.md) |
| **enroute 规划**（数据/决策树/桥接/质量/分时段） | [reference/enroute.md](reference/enroute.md) |
| STAR 选择 | [reference/star.md](reference/star.md) |
| IAP（其 IAF/IF 供 enroute 用） | [reference/iap.md](reference/iap.md) |
| **方向航路模板**（8 方向常见 AIP 走廊；桥接/选路/方向参考） | [reference/route_templates.md](reference/route_templates.md) |
| **进场/离场移交点**（各机场进场门/离场头，VATJPN SOP；进场端点权威源） | [reference/transfer_points.md](reference/transfer_points.md) |

## 二、enroute 决策树（核心，详见 enroute.md）

输入 dep、arr（RJ/RO ICAO），输出贴近大圆、符合航路连接规则的 enroute 航路串。

- **case 0（最优先）**：两机场间有**直连 AIP 航路** → 直接用，结束。
- 无直连 AIP → 按「dep 有无 SID」「arr 有无 STAR」分四类，套路统一：
  1. 规划大圆航线、明确方向；
  2. **离场点**：**优先查 [transfer_points.md](reference/transfer_points.md) 的官方离场头**；否则 有 SID → SID 尾(=enroute 头)，无 SID → 挑「最快切入航路的航点」作虚拟离场点；
  3. **进场点**：**优先查 [transfer_points.md](reference/transfer_points.md) 的官方进场门**（按到达方向择门，权威源）；否则 有 STAR → STAR 头(=enroute 尾)，无 STAR → IAP 的 IAF/IF 若直接接在航路上则用它、再否则用**本场 VOR**；
  4. **A\*** 在离场点 ↔ 进场点间找尽量贴大圆的航线（守航路方向 N/F/B 与高度带）。
  - case 1=SID+STAR · case 2=SID 无 STAR · case 3=无 SID 有 STAR · case 4=两端都无。
- **case 5（桥接 Rule 5）**：dep/arr 各自附近机场间存在官方 AIP → 借中段 + 补接两端，**过三道闸**（①干净 ②≤1.25×最优 A\* ③不冲过头）才用，否则回退最优 A\*。
- **方向航路模板**（[route_templates.md](reference/route_templates.md)）：8 方向常见 AIP 走廊（真实高频连续子链，逐条核验）。case 5 桥接优先套用**同航向**模板；A\* 选路时也用它校验「该航向应走哪条干线」。
- **质量判据（通用）**：距离按**真实航路长**（沿 airway 累加，非大圆直线）；enroute 大锐角转弯(>~100°)标记可疑。

## 三、必读避坑（细节在各 reference）

- **优先 RNAV 航路**（Y/Z 等），太绕/不可达再回退传统（V/A）。命名按 ICAO Annex 11，日本国内航路并非特例。
- **有些 RNAV 航路是单向**：互为反向的两方向有时用不同航路（如 Y28 偏西向、Y57 偏东北向、Y12 偏西南向）。⚠️ `earth_awy.dat` 在「单向 RNAV 与双向航路共挂」的航段会统一标 `N`（丢失单向信息）——**用某条单向航路前先确认其在该航向合法**，否则会标出逆向航路串。可靠真值来源：`routes.csv` 的实飞用向 / [route_templates.md](reference/route_templates.md) 的方向表（router 的标名层即据此学习）。
- **连通性硬前提**：所选端点必须落在航路网上（earth_awy 图中有进/出边）；孤立点（0 边的进近 VOR）要退到真正在航路上的 IAF/IF。
- **本场 VOR ≠ section==D 的任意 VOR**：本场 VOR 是场内/跑道旁、引导回场的 VOR（RJTT 是 TTE，不是 feeder VOR XAC）；按坐标取距机场最近的 VOR/DME 来定（见 cifp_format.md「本场 VOR 辨识」）。
- **两个 "IF" 别混**：path&terminator 的 IF=Initial Fix（航段类型，SID/STAR/APPCH 首段都用）；描述码第4位的 I=IF=Intermediate Fix（进近中间定位点，**仅 IAP**）。
- **分时段的 Aircraft/Altitude 列是脏自由文本**，机型/高度只能按时间过滤后留给用户自选，勿程序自动选唯一（见 enroute.md）。
