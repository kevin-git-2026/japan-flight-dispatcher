# STAR 选择（标准进场程序）

> 上游：[../SKILL.md](../SKILL.md)。字段与码表见 [cifp_format.md](cifp_format.md)。

## 概念
- STAR = 连接**进场点**到指定跑道**进近程序(IAP)**之间的航路：起点 = 进场点，终点 = 接入 IAP 的 IAF/IF。
- 分**基础 STAR** 与**带过渡(transition) STAR**（过渡 = 在基础 STAR 头部或尾部延长；日本较少见，世界其余地区常见）。
- 数据在 `NavData/CIFP/<ICAO>.dat`。route-type：5=RNAV 公共段、4=RNAV enroute 转换、6=RNAV 跑道转换（传统版相应用 2/1/3）。

## 基础 STAR 完整记录（RJTT RW34B 的 XAC1K，真实 CIFP 全字段）
```
STAR:010,5,XAC1K,RW34B,XAC,RJ,D, ,V  H, ,   ,IF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,14000, ,   ,    ,   , , , , , , , , ;
STAR:020,5,XAC1K,RW34B,ANZAC,RJ,P,C,E  H, ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,13000,     ,     , ,230,    ,   , , , , , , , , ;
STAR:030,5,XAC1K,RW34B,TT450,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
STAR:040,5,XAC1K,RW34B,TT451,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
STAR:050,5,XAC1K,RW34B,TT452,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
STAR:060,5,XAC1K,RW34B,TT453,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
STAR:070,5,XAC1K,RW34B,WANDA,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,13000,     ,     , ,230,    ,   , , , , , , , , ;
STAR:080,5,XAC1K,RW34B,WEDGE,RJ,P,C,E  H,L,   ,TF, , , , , ,      ,    ,    ,    ,    , ,08000,     ,     , ,   ,    ,   , , , , , , , , ;
STAR:090,5,XAC1K,RW34B,UMUKI,RJ,E,A,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,+,06000,     ,     , ,   ,    ,   , , , , , , , , ;
STAR:100,5,XAC1K,RW34B,KAIHO,RJ,E,A,EE H, ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
```
- 首点 **XAC**（`RJ,D` = section D 的 VOR）以 **IF=Initial Fix 首段**（航段类型，非进近的 IF）起；
  ⚠️ XAC 是**本条 STAR 的入口 feeder VOR、非本场 VOR**（RJTT 本场 VOR 是 TTE，见 cifp_format「本场 VOR 辨识」）。
- UMUKI/KAIHO（`RJ,E,A`）= section E 的航点，是 enroute 衔接侧（STAR 入口）。

## 带过渡 STAR 完整记录（KSFO ALWYS3 的 INYOE 过渡）
```
STAR:010,4,ALWYS3,INYOE,INYOE,K2,E,A,E  H, ,   ,IF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,18000, ,   ,    ,   , , , , , , , , ;
STAR:020,4,ALWYS3,INYOE,DYAMD,K2,P,C,EE  , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,+,FL270,     ,     , ,270,    ,   , , , , , , , , ;
```

## 选择要点
- 最重要三因素：**进场跑道、进场点、进场时间**。
- STAR ↔ 适用落地跑道**互相约束**：给出一个 STAR 时须给适用落地跑道；用户指定跑道时应选合适 STAR。
- 在 enroute 决策（[enroute.md](enroute.md) case 1/3）里，STAR 的产物是**进场点**：取 STAR 头、即 `section==E` 的那个航点（接 enroute 尾）。
- 关键判据：`section=='E'` 的航点 = STAR 入口（接 enroute 尾）；`section=='D'` = VHF 导航台（**未必是本场 VOR**）；末点描述码标接入 IAP 的 IAF/IF。
