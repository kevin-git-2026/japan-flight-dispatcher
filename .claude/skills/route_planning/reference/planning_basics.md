# 航路规划概述（planning_basics）

> 上游：[../SKILL.md](../SKILL.md)。

## 目的
在符合 ICAO 等相关组织规定的空域运行规则下，规划前往目的地机场的最佳线路。

## 航路规划在做什么（7 环节）
1. 起飞机场选择 → [dep_apt.md](dep_apt.md)
2. 落地机场选择 → [arr_apt.md](arr_apt.md)
3. 标准离场程序（SID, Standard Instrument Departure）选择 → [sid.md](sid.md)
4. 航线（enroute）规划 → [enroute.md](enroute.md)
5. 标准进场程序（STAR, Standard Terminal Arrival Route）选择 → [star.md](star.md)
6. 进场跑道（ARR RWY）选择
7. 标准仪表进近程序（IAP, Instrument Approach Procedure）选择 → [iap.md](iap.md)

## 本项目侧重点
**第 4 步 enroute（航线）规划**。SID/STAR/IAP 在本项目里主要作为 enroute 的**端点来源**
（离场点取自 SID 尾、进场点取自 STAR 头或 IAP 的 IAF/IF / 本场 VOR）。

## 说明
- 第 6 步「进场跑道选择」本框架未单列章节展开；它隐含在 SID/STAR 的「程序 ↔ 适用跑道互相约束」里。
- 所有 SID/STAR/IAP 数据均来自程序自带 `NavData/CIFP/<ICAO>.dat`，格式见 [cifp_format.md](cifp_format.md)。
