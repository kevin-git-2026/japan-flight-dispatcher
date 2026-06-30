# SID 选择（标准离场程序）

> 上游：[../SKILL.md](../SKILL.md)。字段与码表见 [cifp_format.md](cifp_format.md)。

## 概念
- SID = 连接**起飞跑道末尾**到**离场点**（接入飞行计划航路 enroute 的起点）的航路。
- 分**基础 SID** 与**带过渡(transition) SID**（过渡 = 在基础 SID 上延长到更远的 enroute 点）。
- 数据在 `NavData/CIFP/<ICAO>.dat`。route-type 4/5/6 = RNAV 跑道转换 / 公共 / enroute 转换（见 cifp_format [表4]）。

## 基础 SID 完整记录（RJTT RW34B 的 ROVE3A，真实 CIFP 全字段）
```
SID:010,4,ROVE3A,RW34B, , , , ,    , ,   ,VA, , , , , ,      ,    ,    ,3380,    ,+,00700,     ,14000, ,   ,    ,   , , , , , , , , ;
SID:020,4,ROVE3A,RW34B,TORAM,RJ,P,C,E   ,R,   ,DF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
SID:030,4,ROVE3A,RW34B,PLUTO,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
SID:040,4,ROVE3A,RW34B,KAIJI,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
SID:050,4,ROVE3A,RW34B,SPOON,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,-,FL150,     ,     , ,   ,    ,   , , , , , , , , ;
SID:080,4,ROVE3A,RW34B,ROVER,RJ,E,A,EE  , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,+,12000,     ,     , ,   ,    ,   , , , , , , , , ;
```
- 首段 010 是 **VA 航段**（path&terminator=VA：从跑道按航向爬升到高度、无 fix）。
- 其后经 TORAM…（`RJ,P,C` 终端航点）到 **ROVER（`RJ,E,A` = section E 的离场点）= enroute 头**。

## 带过渡 SID 完整记录（ROVE3A 的 INUBO 过渡）
```
SID:010,6,ROVE3A,INUBO,ROVER,RJ,E,A,E   , ,   ,IF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,14000, ,   ,    ,   , , , , , , , , ;
SID:020,6,ROVE3A,INUBO,BRUCE,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,+,FL150,     ,     , ,   ,    ,   , , , , , , , , ;
SID:025,6,ROVE3A,INUBO,LEWIS,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,FL170,     ,     , ,   ,    ,   , , , , , , , , ;
SID:027,6,ROVE3A,INUBO,SILVA,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,FL170,     ,     , ,   ,    ,   , , , , , , , , ;
SID:030,6,ROVE3A,INUBO,INUBO,RJ,E,A,EE  , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,+,FL250,     ,     , ,   ,    ,   , , , , , , , , ;
```
- 过渡段（route-type 6 = RNAV enroute 转换）从 ROVER 起，首段为 **IF=Initial Fix**（航段类型，**非进近的 Intermediate Fix**），延伸到 INUBO（`RJ,E,A`）= 新的 enroute 出口。

## 选择要点
- 最重要三因素：**离场跑道、离场点、离场时间**。
- SID ↔ 适用起飞跑道**互相约束**：给出一个 SID 时须给适用起飞跑道；用户指定跑道时应选合适 SID。
- 在 enroute 决策（[enroute.md](enroute.md) case 1/2）里，SID 的产物是**离场点**：取 SID 尾、即 `section==E` 的那个航点（如 ROVER）。
  - 注意 section=E 的出口有时不可靠（死端），真正可达全网的可能是本场 VOR 枢纽——选离场点须做**连通性过滤**（在 earth_awy 图上有出边），见 [enroute.md](enroute.md)。
