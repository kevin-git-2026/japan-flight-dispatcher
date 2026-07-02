# SimBrief 派遣链接参数速查（dispatch custom-options prefill）

> 用途：SimBrief 一键派遣预填 URL（`https://dispatch.simbrief.com/options/custom?...`）的可自定义参数清单，供本项目 F16/F20（及延后的分时段规划）查验。
> 来源：Navigraph 开发者文档 + Dispatch Redirect Guide（权威接口）× 一条 SimBrief UI 序列化的真实 URL 交叉核对。
> 预填链接**公开、用用户自己浏览器 SimBrief 登录态出 OFP**，无需任何凭据。**必填**：`orig`、`dest`、`type`；其余省略即取默认（`route` 省略 = SimBrief 推荐航路）。

## ⚠️ 两套「方言」——接代码一律用官方文档名

SimBrief 自家 UI 导出的完整表单 URL，部分参数名与**官方 dispatch-redirect API 不同**。开发对接**用左列官方名**（稳定契约）；右列是内部 UI 态，可能随改版变。

| 用途 | ✅ 官方文档 API（用这个） | 🔶 UI 序列化里的名字 |
| --- | --- | --- |
| 离场时刻 | `deph` + `depm`（+ `date=DDMonYY`） | `date=30 Jun 2026 - 20:55` |
| 航段时间 | `steh` + `stem` | `stehour` / `stemin` |
| 重量单位 | `units=LBS`/`KGS` | `pounds=0`/`1` |
| 巡航模式 | `cruise` + `civalue` | `cruisemode` + `cruisesub` |

**跑道 `origrwy`/`destrwy` 两套完全一致**，最稳。图例：✅=官方文档 · 🔶=仅 UI 观察到（可用但非文档、优先用官方等价项）。

## 航班标识

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `orig` | ICAO | `KORD` | 起飞机场（**必填**） |
| ✅ `dest` | ICAO | `KSFO` | 落地机场（**必填**） |
| ✅ `type` | ICAO 机型 或 airframe 内部 id | `B738` / `123456_1582090020` | 机型（**必填**；后者=UI URL 的 `type=80_...`，即 `aircrafts.json` 的 `aircraft_id`） |
| ✅ `airline` | 字符串 | `ABC` | 航司代码 |
| ✅ `fltnum` | 数字串 | `1234` | 航班号 |
| ✅ `callsign` | 字符串 | `ABC1234` | ATC 呼号 |

## 航路 / 跑道（核心）

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `route` | 空格分隔航路串（首可含 SID 名、尾可含 STAR 名） | `PLL GAROT OAL MOD4` | enroute 航路；**省略 = SimBrief 推荐航路** |
| ✅ **`origrwy`** | 裸跑道号（**无 `RW` 前缀**，单跑道可省 L/R） | `06L` / `34` | **离场跑道** |
| ✅ **`destrwy`** | 同上 | `36R` | **落地跑道** |
| ✅ `find_sidstar` | `1`/`0` 或 `R`/`C` | `R` | 自动插 SID/STAR；`R`=偏好 RNAV、`C`=传统 |
| ✅ `omit_sids` / `omit_stars` | `1`/`0` | `0` | 是否略去 SID/STAR |
| 🔶 `rc_sid` / `rc_star` | 程序名 | `ESKOB3` | 喂给 SimBrief 自动选路器的 SID/STAR 提示 |
| 🔶 `rc_origrwy` / `rc_destrwy` | 裸跑道号 | `18L` | 自动选路器的跑道提示 |

## 时刻 / 时间

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `date` | `DDMonYY` | `11JUL13` | 飞行日期 |
| ✅ `deph` | 时 0–23 | `16` | 离场**小时**（SimBrief 该字段为 UTC） |
| ✅ `depm` | 分 0–59 | `30` | 离场**分钟** |
| ✅ `steh` / `stem` | 时 / 分 | `4` / `30` | 计划航段时间 |

## 高度 / 性能

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `fl` | 英尺或 `FLxxx` | `34000` / `FL340` | 巡航高度（空=自动） |
| ✅ `cruise` | `LRC`/`CI`/… | `CI` | 巡航模式 |
| ✅ `civalue` | 数字或 `AUTO` | `25` | 成本指数值 |
| ✅ `climb` / `descent` | `spd/spd/mach` | `250/300/78` | 爬升 / 下降剖面 |

## 备降

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `altn` | ICAO | `KLAX` | 主备降场 |
| ✅ `altn_count` | 整数 | `4` | 备降场数 |
| ✅ `altn_#_id` / `_rwy` / `_route` | ICAO / 跑道 / 航路串 | `RJGG` / `18` / `BEKL5C …` | 逐个备降细定（#=1–4） |
| ✅ `altn_avoid` | 空格分隔 ICAO | `KBDL KALB` | 备降顾问排除场 |
| ✅ `toaltn` / `eualtn` | ICAO | `KBOS` | 起飞备降 / 航路备降 |

## 载荷 / 燃油

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `pax` | 整数 | `100` | 旅客数 |
| ✅ `cargo` | 千磅小数 | `5.0` | 货重 |
| ✅ `manualzfw` | 千磅小数 | `40.1` | 手工 ZFW |
| ✅ `addedfuel` + `addedfuel_units` | 数 + `wgt`/`min` | `20` + `min` | 附加油（重量或分钟） |
| ✅ `contpct` | 小数 或 `pct/min` | `0.05` / `0.05/15` | 裕度油 |
| ✅ `resvrule` | 分钟 | `45` | 备份油分钟 |
| ✅ `taxiout` / `taxiin` | 分钟 | `10` / `4` | 滑出 / 滑入 |
| ✅ `fuelfactor` | `Pnn`/`Mnn` | `P00` | 燃油修正因子 |

## 输出格式 / 选项

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `planformat` | 版式码 | `LIDO` | OFP 版式 |
| ✅ `units` | `LBS`/`KGS` | `KGS` | 重量单位 |
| ✅ `maps` | `detail`/`simple`/`none` | `detail` | 航图详略 |
| ✅ `navlog` / `etops` / `stepclimbs` / `tlr` / `notams` / `firnot` | `1`/`0` | `1` | 各输出板块开关（`tlr`=起降跑道分析） |

## 机组 / 杂项

| 参数 | 格式 | 示例 | 用途 |
| --- | --- | --- | --- |
| ✅ `reg` / `fin` / `selcal` | 字符串 | `N123XX` / `123` / `XXXX` | 注册号 / 尾号 / SELCAL |
| ✅ `cpt` / `pid` | 字符串 / 数字 | `JOHN DOE` / `12345` | 机长名 / SimBrief 用户号 |
| ✅ `manualrmk` | 字符串（`\n` 换行） | `TEST REMARK` | 签派备注 |
| ✅ `static_id` | 字符串 | `ABC_123` | 静态 id（覆盖同一份 OFP） |

## 对本项目的落点

- **F16（现有一键派遣）**：`orig`/`dest`/`type`/`airline`/`fltnum` + `route`，已实现（`planner._build_simbrief_url`）。
- **F20（跑道 + SID/STAR）**：新增 **`origrwy` / `destrwy`**（用户在面板选的跑道，去 `RW` 前缀）；SID/STAR 名继续写进 `route` 串首尾；`find_sidstar`/`omit_sids` 可作可选精调。
- **⏸️ 分时段规划（延后）**：对应 **`date` + `deph` + `depm`**（UTC；GUI 输入 JST → −9h）。

## 来源

- Navigraph Developer Portal — Using the SimBrief API: <https://developers.navigraph.com/docs/simbrief/using-the-api>
- Navigraph Forum — Dispatch Redirect Guide: <https://forum.navigraph.com/t/dispatch-redirect-guide/5299>
- 交叉核对样本：一条 SimBrief UI 导出的真实 custom-options URL（ROAH→RJTT，含 `origrwy=18L`/`destrwy=16L`）。
