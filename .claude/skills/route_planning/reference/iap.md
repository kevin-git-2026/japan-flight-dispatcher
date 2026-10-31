# IAP 选择（标准仪表进近程序）

> 上游：[../SKILL.md](../SKILL.md)。字段与码表见 [cifp_format.md](cifp_format.md)。

## 说明
- IAP 规划**不是本项目目前实现的重点**；但其中的 **IAF/IF 在 enroute 规划（[enroute.md](enroute.md) case 2/4）有价值**——
  当落地机场无 STAR 时，若 IAP 的 IAF/IF 本身就在某 ATS 航路上（earth_awy 图里有边），可作 enroute 尾。
- **能否落地也看 IAP**：一条跑道只要有 IAP，没有 STAR 衔接也能选作落地跑道（雷达引导切进近，如 RJSS RW30 的 VOR/DME RWY30）。既无 IAP 也无 STAR 衔接的跑道不能直接落（如 RJSS RW12）。盘旋进近（`VORA` 这类 ident 不含跑道号）不限跑道。
- **目视盘旋时 STAR 跟进近走，不跟落地跑道**：伊丹南向运行先飞 32 方向的进场和进近（ILS 32L / RNP 32L / RNP 32R），再目视盘旋落同一条跑道的另一端（32L 落 14R，32R 落 14L）。所以选 RW14 时 STAR 仍是 `ROKKO1` / `IZUM1R` / `AGPU1R`，尽管 CIFP 里它们只服务 RW32。
- **SimBrief 不接受目视盘旋**：它配不上「落 14 + 32 的进近程序」，提交给 SimBrief 时落地跑道要写 32（飞进近的那条）。
- 数据在 `NavData/CIFP/<ICAO>.dat`。

## 完整记录（RJTT 的 LDA W 22 程序，真实 CIFP 全字段）
> route-type `A` = 进近过渡段（BACON transition）；`X` = LDA 最后进近段。
```
APPCH:010,A,X22-W,BACON,BACON,RJ,E,A,E  A, ,   ,IF, , , , , ,      ,    ,    ,    ,    ,+,07000,     ,14000, ,   ,    ,   , , , , , ,0,D,S;
APPCH:020,A,X22-W,BACON,BIBLO,RJ,P,C,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,+,06000,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:030,A,X22-W,BACON,BEAST,RJ,P,C,EE B, ,   ,TF, ,IKL,RJ,P,I,      ,    ,    ,    ,    ,+,05500,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:010,X,X22-W, ,BEAST,RJ,P,C,E  I, ,   ,IF, ,IKL,RJ,P,I,      ,0965,0159,    ,    ,+,05500,     ,14000, ,   ,    ,   , , , , , ,0,D,S;
APPCH:020,X,X22-W, ,BONDO,RJ,P,C,E  F, ,   ,CF, ,IKL,RJ,P,I,      ,0965,0127,2770,0031, ,05000,     ,     , ,   ,    ,   ,TTE,RJ,D, , ,0,D,S;
APPCH:030,X,X22-W, ,MX22,RJ,P,C,E  M, ,   ,CF, ,IKL,RJ,P,I,      ,0965,0011,2770,0116, ,01000,     ,     , ,   ,-325,   , , , , , ,0,D,S;
APPCH:040,X,X22-W, , , , , ,  M , ,   ,VI, , , , , ,      ,    ,    ,3450,    , ,     ,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:050,X,X22-W, ,KASGA,RJ,P,C,E   , ,   ,CF, ,TTE,RJ,D, ,      ,0155,0210,0150,0160,+,04000,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:060,X,X22-W, ,KASGA,RJ,P,C,EE H,R,   ,HM, , , , , ,      ,    ,    ,0160,0047,+,04000,     ,     ,-,210,    ,   , , , , , ,0,D,S;
```
- **描述码第4位**定位进近定位点：BACON=`A`=IAF；BEAST=`I`=IF；BONDO=`F`=FAF；MX22=`M`=MAP；末段 `HM`=等待。
- 第 13-17 字段 `IKL/RJ/P/I` = 推荐导航台（即 LDA 台）；BONDO/KASGA 段引用 `TTE`（本场 VOR）作推荐导航台。

## 用于 enroute 的判据
- 重点用**描述码第4位**定位 IAF(A) / IF(I) / FAF(F) / MAP(M)。
- 其中 **IAF/IF 若本身在某 ATS 航路上**（earth_awy 图里有边），可作 enroute 尾（见 [enroute.md](enroute.md) case 2/4）。
- ⚠️ 此处描述码第4位的 `I`=IF=Intermediate Fix（进近中间定位点）≠ path&terminator 的 `IF`=Initial Fix（首段航段类型）。
- APPCH 行 route-type（字段2 字母）：`A`=进近过渡段，其余字母 = 进近类型（`I`=ILS、`D`=VOR/DME、`V`=VOR、`N`=NDB、`R`=RNAV/RNP、`X`=LDA、`P`=GPS、`J`=GLS）。
