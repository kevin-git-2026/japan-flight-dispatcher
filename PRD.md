# 日本航班智能搜索与规划脚本 — 产品需求文档 (PRD)

| 项目 | 内容 |
| --- | --- |
| 产品名称 | 日本航班智能搜索与规划脚本 (Japan Flight Dispatcher) |
| 当前版本 | **v2.0.0**（F25 UI 迁移 tkinter → Flet：Flutter 渲染，业务逻辑零改动）；v1.6.0（运行规则应用引擎 F24）；v1.5.0（天气网格回退 F22 + 运行规则编辑器 F23）；v1.4.x（本地航路生成 + SimBrief + 机型库 + 交互地图） |
| 交付形态 | PyInstaller 打包的 `flight_dispatcher.exe`（v1.3.1 起**仅 tkinter 图形界面**；命令行版已移除） |
| 运行平台 | Windows 为主(全盘扫描逻辑针对盘符),兼容类 Unix |
| 技术栈 | Python 标准库为主；**v1.4.0 起引入第三方库 `tkintermapview` + `Pillow`（交互地图）**——缺失时地图功能自动禁用、其它仍纯标准库可用 |
| 语言约定 | 全部用户界面文案为简体中文 + emoji 前缀 |
| 文档维护 | 与 `CLAUDE.md` 保持架构描述一致 |

---

## 1. 产品概述

### 1.1 一句话定位

为 X-Plane 11/12 模拟飞行玩家自动生成"日本地区"的飞行任务:智能抽取或指定起降机场 → 查询机场间的官方推荐 AIP 航路 → 交叉比对现实世界航班排班,最终输出一份可直接用于模拟飞行的航班规划单。

### 1.2 解决的核心痛点

模拟飞行玩家在规划一次飞行时,通常需要手动完成以下繁琐步骤:
- 在地图上挑选距离合适、跑道长度满足机型要求的起降机场;
- 查询两机场之间符合日本航空情报出版物 (AIP) 规定的推荐航路;
- 上 FlightAware 等网站查找该航线上真实存在的航班号、机型与时刻,以提升模拟的真实感;
- 确认机场是否已安装插件地景、是否为军用机场(可能缺少民航设施)。

本工具将以上流程一键自动化。

### 1.3 目标用户

- 主要用户:使用 X-Plane 11/12 的飞友(已安装 Navigraph 导航数据)。
- 次要用户:其他平台飞友,可自行从 Navigraph 下载 `earth_aptmeta.dat` 放入工作目录后使用。

---

## 2. 功能需求

### 2.1 功能清单

| 编号 | 功能 | 说明 |
| --- | --- | --- |
| F1 | 起降机场抽取 | 支持完全随机、固定出发/固定目的、或两者均固定 |
| F2 | AIP 推荐航路查询 | 基于 `jp-routes.vercel.app` 数据,按起降 ICAO 精确匹配 |
| F3 | 现实航班排班查询 | 基于 FlightAware,支持按航司 / 机型 / 起飞时间筛选 |
| F4 | 模拟呼号生成 | 现实排班查询失败(零排班)时降级生成模拟呼号;**按航线两端所在地理大区挑选合理航司**(避免「北海道 AIR DO 飞福冈-冲绳」这类离谱组合),用户指定航司则优先用户的 |
| F5 | 多源地景检测 | 直接扫描 **XP `Custom Scenery` + MSFS `Community`** 地景文件夹,判断机场是否已装地景并**标注来源(XP/MSFS)**;`installed_scenery.json` 指纹缓存,目录未变则秒开 |
| F6 | 军用机场标记 | 根据机场类型标记军用机场并给出风险提示;**随机抽线时按「军用端数量」分层优先民用机场**(避免随到可能缺民航助航设施、不一定可飞的军用机场;用户固定军用端时仍可规划) |
| F7 | 跑道长度过滤 | 按机型所需最短跑道长度筛选可用机场 |
| F8 | 航程范围控制 | 按大圆距离(NM)上下限筛选航线 |
| F9 | 严格 AIP 模式 | 可强制要求所抽取航线必须存在官方 AIP 航路 |
| F10 | AIP 航路离线缓存 | 首次联网后写入本地缓存,后续可离线查询 AIP 航路 |
| F11 | Volanta 优先未飞航线 | 读取 Volanta 已飞记录,随机规划时**按已飞次数加权**软优先未飞的**有向**航线。**首次启动询问是否同步**;选 Y 后程序**用浏览器里的 Volanta 登录会话(Orbx token)自动调 `/api/v1/Flights` 拉取**并记住偏好(以后启动零操作自动同步,需近 ~14 天登录过 Volanta),解析后并入只增不减的单一数据文件 `volanta_data.json`;选 N/回车则下次再问,亦可放入导出文件 |
| F12 | 自带导航数据 + AIRAC 自检 | 导航数据改为程序根目录自带的 `NavData` 文件夹(摆脱 XP 目录依赖,只飞 MSFS 的用户也可用);启动读 `cycle_info.txt` 自检 AIRAC 周期,过期则提示通过 Navigraph 更新 |
| F13 | 图形界面(GUI) | **唯一前端**。v1.3.1–v1.6.0 为 tkinter;**v2.0.0(F25)起为 Flet**(Python 写、Flutter 渲染,`dispatcher/ui_flet/`):表单输入 + 结果卡 + 进离场面板 + 日志区 + Volanta 同步控件;所有阻塞/联网走后台线程,经 `Shell.post`(=`page.run_task`)回事件循环线程更新。GUI 始终是**薄表现层**,复用全部业务逻辑(`planner.build_flight_plan` 计算);v2.0.0 起更进一步——控制逻辑与数据处理全部下沉到 `controller`/`viewmodel`,渲染层**零业务逻辑** |
| F14 | 仅地景机场随机规划 | 随机规划时可选「**仅在两端都已安装地景的机场之间生成航线**」,与「Volanta 优先未飞」加权叠加;未检测到地景目录时该选项灰显/跳过(`has_scenery` 软降级) |
| F15 | 本地航路生成（🚧 v1.4.x） | 无直连 AIP 航路时，用自带 `NavData`(`earth_awy/fix/nav` + `CIFP/`)本地 A* 寻路生成参考航路；**沿 airway 加密**(距离精确 + 画图密)。端点取自**从 `routes.csv` 学到的各机场真实进/离场过渡点 ∪ VATJPN 移管表官方门 ∪ CIFP**(并集 + 方向过滤，A* 自选最优)。**续作2 航路质量**：优先 RNAV 航路、学习单向航路方向(修逆飞)、偏好高频干线走廊。**续作3(`1.4.1_alpha2`)**：进/离场端点学习 + **删除 AIP 桥接**(基于真实端点的直接 A* 即理论最优) + 修本场VOR 辨识。详见 §8.2 |
| F16 | SimBrief 一键派遣（🚧 v1.4.0） | 结果卡生成 SimBrief custom-options 预填链接(orig/dest/type/airline/fltnum)，用用户**自己浏览器的 SimBrief 登录态**出专业 OFP；零凭据、可公开 |
| F17 | 机型库可搜索下拉（🚧 v1.4.0） | 机型从 `aircrafts.json`(SimBrief 212 机型，精简剥隐私)选；GUI 可搜索 Combobox，选中给 SimBrief `aircraft_id`，手输兜底 |
| F18 | 航路交互地图（🚧 v1.4.0） | 每条航路一个「🗺️ 地图」链接，弹独立窗口用 `tkintermapview` 把航路画在 OSM 真实地图上(可拖拽/缩放)，分时段多航路可分别开窗 |
| F19 | 航路距离 + 偏差（🚧 v1.4.0） | 每条 AIP / 生成航路显示沿 airway 累加的精确长度与「较大圆 +X%」偏差 |
| F20 | 跑道 + SID/STAR 选择 + 天气（🚧 v1.4.1） | 规划后在结果区让用户选**出发/到达跑道 + SID/STAR**：按【航路首点=离场点、末点=进场点】**预筛**真正接得上本航路的程序(标准写法 `SID.TRANS`)；**机场无 SID/STAR 时仍列全部物理跑道供选**(进近走 IAP/雷达引导，不因无程序就弃选跑道)；抓 **NOAA METAR+TAF**，按跑道朝向算逆/顺风+侧风分量、按日本经验(顺风≤10/侧风≤30节)标适航、合规预选但用户可改。选定即显示并拼进 SimBrief `route`；**不改本地航路生成**。跑道长度显示用**米**、风用**节**。详见 §8.2 |
| F21 | 多 AIP 航路按时段选 + 全段预览（🚧 v1.4.1） | 一航线多条 AIP(按 EOBT/ETA 时段、机型、巡航高度分)时**弹窗**让用户选一条：不勾「严格遵循现实运行规则」＝罗列全部(时段/用途/机型/高度/距离/航路+选择框)手动选；勾＝填 **EOBT+机型+巡航高度**自动定唯一(时间可靠自动筛，机型/高度按用户给的参考值判属)。选定即重驱动 SID/STAR 预筛 + SimBrief `route`。另加「🗺️ 预览完整航路」把 **SID+enroute+STAR 全段**画到地图。详见 §8.2 |
| F22 | 网格天气回退（✅ v1.5.0） | 很多日本小机场(RJTO/RJTH/RJAF/RJER 等)的 METAR **只在日间更新或完全取不到**，夜间会返回白天的旧报文。当 METAR **缺测或过期**(观测 >2h)时，改用机场坐标处的 **Open-Meteo 网格模型天气**(纯标准库 JSON、无需 key、`models=jma_msm` 日本本地 5km)**合成一条标准格式 METAR**，走原有解析/显示流水线驱动选跑道。清楚标注「🌐 模型合成·非实测」；能见度不可信故保守(默认 9999、绝不臆造雾)、降水强度按速率、云分层。天气数据来自 **Open-Meteo (CC BY 4.0)**。详见 §8.2 |
| F23 | 机场运行规则编辑器（✅ v1.5.0） | 日本很多机场(如羽田)有按【时段+风】切换跑道/程序的成套运行规则。新增「⚙️ 编辑机场运行规则」可视化编辑器：为任意 RJ/RO 机场编写规则(**运行条件=时段(JST)+星期几+换向门槛[顺/逆/侧风≥N节]+好天门槛[云底高+能见度]**、**离场跑道+SID**、**进场跑道+STAR+IAP**)，各下拉用真实 CIFP 数据(含新增 IAP 枚举)填充，存运行目录 `operation.json`。支持规则的**增删改查**、多机场隔离、原子保存。换向门槛含 **侧风(crosswind)** 种类(都心「16 侧风超即改 22/23」可编辑，通用于任何机场)。规划时的应用见 **F24**。详见 §8.2 |
| F24 | 运行规则应用引擎（✅ v1.6.0） | 把 F23 编辑的 `operation.json` 规则**在规划时应用**：进离场面板新增 **EOBT(JST) 输入**(默认当前 JST) + **「按机场运行规则预选」开关**，按 当前时段(离场 EOBT / 到达 ETA)+星期+实测风+天气 匹配出该航班应用哪条规则，**自动预选**跑道 + SID/STAR(并展示 IAP)、标注「🎯 运行规则: <名> → 跑道/程序」。**决策支持**——用户仍可随意改选；开关关闭即回到原按风预选。匹配「均等」不按上下优先级(条件命中 + 迎风取舍)，恶天自动用恶天配置。该 EOBT 同时提交给 SimBrief 链接(撤轮挡时刻)。详见 §8.2 |
| F25 | UI 迁移 tkinter → Flet（✅ v2.0.0） | 界面换成 **Flutter 渲染**（[Flet](https://flet.dev) = Python 写、Flutter 画），**业务逻辑零改动**。收益：**彩色 emoji**(tk 只能单色)、**深色模式跟随系统**(tk 做不到)、Material 3 观感、地图 marker 可用任意控件绘制(**甩掉 `tkintermapview` + `Pillow` 两个第三方依赖**)。前提是先把 `gui.py` 的**控制逻辑与数据处理全部剥离**到 `controller.py`(编排) + `viewmodel.py`(各界面纯数据 Model) —— 两套 UI 共用同一 Model，行为天然一致，且这些逻辑**首次可脱离 GUI 单测**。**唯一功能倒退：Flet 无多窗口**，地图不能并排开多个窗口比对，改为**同一视图内标签页切换**。详见 §8.2 |

### 2.2 交互输入项(每轮规划)

| 提示项 | 输入示例 | 默认 / 回车行为 |
| --- | --- | --- |
| 出发机场 | `RJAA` (4 位 ICAO) | 回车 = 随机 |
| 目的机场 | `RJBB` (4 位 ICAO) | 回车 = 随机 |
| 执飞航司 | `ANA` (3 位 ICAO) | 回车 = 不限 |
| 机型代码 | `737` | 回车 = 跳过 |
| 起飞时间区间 | `08:00-15:30` | 回车 = 跳过 |
| 最短跑道长度 | `1800m` 或 `5900ft` | 回车 = 5900 ft |
| 最短航程 | `200` (NM) | 回车 = 200 |
| 最长航程 | `450` (NM) | 回车 = 450 |
| 严格 AIP 航路 | `Y` / `N` | — |
| 仅地景机场随机规划 (F14) | `Y` / `N` | 回车 = 否；未检测到地景目录时不询问 |

> 备注:若最短航程 > 最长航程,程序自动交换二者。

### 2.3 输出内容

一次成功的规划输出包含:
- 起飞机场代码(附地景来源标记 `[地景:XP]`/`[地景:MSFS]`/`[地景:XP+MSFS]`/`[⚠️无地景]` 与 `[🛡️军用机场]`);
- 降落机场代码(同上标记);
- 大圆距离 (NM);
- AIP 推荐航路列表(若存在,逐条编号);
- 地景 / 军用风险提醒;
- 现实排班结果(完美匹配 / 仅参考排班 / 降级模拟呼号);
- FlightAware 完整排班表链接;
- (启用 Volanta 时)若抽中的是已飞航线,附 `[⚠️Volanta:已飞过 N 次]` 标记。

### 2.4 Volanta 同步(F11 相关)

GUI 用「**同步 Volanta**」按钮 + 「**自动同步**」复选框管理 Volanta 同步,行为由**同步偏好**(`volanta_data.json` 的 `preference` 字段)决定:

- **已开启自动同步**(`preference == "auto"`,复选框勾选)→ 启动时由初始化后台线程**静默自动同步**(用浏览器里的登录会话拉取),**不开窗、无需点击**;登录过期则沿用已保存的已飞数据。
- **未开启**(`ask` / 缺省,复选框未勾选)→ 启动**不**扫浏览器;用户**手动点「同步 Volanta」** 才同步:

| 操作 | 行为 |
| --- | --- |
| 点「同步 Volanta」 | 先用浏览器里已有的登录会话直接拉取(无需开窗);取不到有效令牌才打开 Edge(回退默认浏览器)跳转 **`fly.volanta.app/map`** 让用户**登录**。令牌在 `/map` 登录后即生成,但 Chromium 把它写到磁盘有 ~30s~1min 延迟,故后台每 3s 轮询、**最长 5 分钟**,并用**弹窗**引导:①开始即弹「正在等待令牌写入,登录后请稍候,可滚动加速」;②约 1 分钟仍无则弹「请去航班(Flights)页刷新+滚动催落盘」;成功也弹提示(可随时点「取消同步」) |
| 勾选「自动同步」 | `set_volanta_auto(True)` 写 `auto`,以后启动自动同步;取消勾选写 `ask` |

> 行为约定:
> - **偏好持久化** —— 勾选「自动同步」即把 `preference` 记为 `auto`;取消勾选或删除 `volanta_data.json` 恢复手动(删除会一并清空已飞累积库)。
> - **手动数据仍读** —— 无论是否同步,启动都会读取现有本地数据(手动导出的 json / CSV / 累积库)。
> - **隐私** —— 仅在「已开启自动」或「本次点了同步」时,程序才会去读浏览器 Local Storage 找登录令牌;否则绝不碰浏览器。
> - 全程容错,同步失败/超时/未登录都只降级为"本次不启用优先",不影响主流程。

---

## 3. 系统架构

### 3.1 总体数据流

> GUI 把「一次性启动初始化」放后台线程(经 `after()` 回主线程更新状态/日志),把「规划」做成**「规划航线」按钮事件**(每点一次跑一轮 `_plan_worker`,非无限循环);结果渲染进窗体控件(`_render_plan`)。

```
启动 run_gui() → 后台 _init_worker（一次性初始化）
  │
  ├─ 定位导航数据 (find_navdata_file：读自带 NavData；缺失则弹窗提示去 Navigraph)
  │     └─ AIRAC 自检 (check_airac_currency：cycle_info.txt 过期则提示更新)
  │
  ├─ 多源地景扫描 (scan_installed_sceneries：XP Custom Scenery + MSFS Community
  │     → dict{ICAO: 来源}，installed_scenery.json 指纹缓存，目录未变秒开)
  ├─ 加载 AIP 航路数据 (load_aip_routes_from_csv,网络优先 → 本地缓存)
  ├─ (可选) Volanta 已飞航线获取(由 volanta_data.json 的偏好决定)
  │     ├─ 已开启自动(auto):静默用登录会话(localStorage 的 Orbx token)调
  │     │   /api/v1/Flights → 解析起降对并入 volanta_data.json(零操作)
  │     ├─ 未开启:不扫浏览器;由「同步 Volanta」按钮按需触发
  │     └─ load_volanta_flown_routes:读累积库(+可选 json/CSV 导入)→ 已飞有向航线
  │
  └─ 初始化完成 → 启用表单（_on_init_done，经 after() 回主线程）

每次点「规划航线」→ 后台 _plan_worker
  ├─ 快照表单输入(机场 / 航司 / 机型 / 时间 / 跑道 / 航程 / 严格 / 仅地景)
  ├─ 重新加载机场列表 (load_airports_from_navigraph,因跑道阈值每次可变)
  ├─ 选择航线
  │    ├─ 双固定:直接计算大圆距离 + 查 AIP 航路
  │    └─ 含随机:get_random_route 枚举候选 + 按已飞次数加权抽取
  │       (优先未飞航线;按军用端数量分层优先民用机场;可选仅两端有地景 F14)
  ├─ build_flight_plan 计算:抓取现实排班 / 无排班降级模拟呼号 → FlightPlan
  └─ _render_plan 渲染进结果控件（经 after() 回主线程）
```

### 3.2 代码组织

v1.2.0 起,原本约 1100 行的单文件 `flight_dispatcher.py` 已**按功能拆分为 `dispatcher/` 子包**(每个模块一个职责),便于维护;根目录的 `flight_dispatcher.py` 退化为**薄壳入口**。**v1.3.0 起**新增 `dispatcher/gui.py`(tkinter)与 `dispatcher/planner.py`(计算/渲染解耦);**v1.3.1 起 GUI 为唯一前端**,移除命令行版(`dispatcher/app.py` 与 `--cli`)。

**v2.0.0(F25)起分三层**:**逻辑层**(17 个模块,零 GUI 依赖) → **`controller.py` + `viewmodel.py`**(编排 + 各界面纯数据 Model,零 GUI 依赖、可 headless 单测) → **渲染层**(`ui_flet/`,唯一前端;tkinter 的 `gui.py` 已于 Phase 7 删除)。原则:**每个界面 = 一个纯数据 Model + 一层薄渲染**;渲染层只做「造控件 / 读控件 / 写控件 / 绑事件」,零业务逻辑。
打包命令随之改为 `flet pack flight_dispatcher.py -n flight_dispatcher -y --pyinstaller-build-args="--exclude-module=numpy" …`(仍是 PyInstaller 一族,**不需要 Visual Studio / Flutter SDK**)。第三方依赖:`flet` + `flet-map`(v2.0.0 起);`tkintermapview` + `Pillow` 已随 tkinter 一并删除。实测 **onefile 54.2 MB / 冷启动 1.8s**,冻结态 `get_real_run_path()` 正确返回 exe 同级目录(非 `_MEIPASS`)。

| 模块 | 关键函数 | 职责 |
| --- | --- | --- |
| `dispatcher/model.py` | `Airport` | 机场数据模型 |
| `dispatcher/config.py` | `get_real_run_path`、`list_drives`、`load_sim_config`/`save_sim_config` | 运行路径锚点、盘符枚举、`installed_scenery.json` 读写 |
| `dispatcher/navdata.py` | `find_navdata_file`、`check_airac_currency`、`locate_xp_root` | 定位导航数据(读自带 NavData)、AIRAC 自检、定位 XP 根(供地景扫描) |
| `dispatcher/scenery.py` (F5) | `find_msfs_packages_dir`、`scan_xp_sceneries`、`scan_msfs_sceneries`、`scan_installed_sceneries` | 扫 XP/MSFS 地景目录提 ICAO,合并标注来源,指纹缓存 |
| `dispatcher/data.py` | `load_airports_from_navigraph`、`load_japan_icao_set`、`load_aip_routes_from_csv` | 解析机场 / 真实 ICAO 白名单 / AIP 数据 |
| `dispatcher/volanta.py` | `try_fetch_volanta_json_via_session`、`load_volanta_flown_routes`、`_open_volanta_in_browser`、`volanta_auto_enabled`/`set_volanta_auto` | 用登录会话(Orbx token)拉 `/api/v1/Flights` 自动生成 json → 解析 Volanta 已飞有向航线 |
| `dispatcher/flightaware.py` | `is_aircraft_match`、`time_to_minutes`、`parse_user_time_range`、`fetch_real_flights_with_filter` | 机型模糊匹配、时间解析、抓取并过滤 FlightAware 排班 |
| `dispatcher/airlines.py` | `airport_region`、`pick_sim_airline`、`init_airline_data`(读 `airlines.json`) | 读取外置数据文件 `airlines.json`(航司运营网络 + 机场地理大区映射),无真实排班时按航线两端挑合理航司;首次运行自动生成,损坏/缺失回退内置默认 |
| `dispatcher/routing.py` | `calculate_distance_nm`、`find_aip_route`、`get_random_route`(含 `require_both_scenery` 过滤) | 大圆距离、AIP 匹配、航线抽取(软优先未飞 + 可选仅地景机场) |
| `dispatcher/planner.py` (F13) | `build_flight_plan`→`FlightPlan`、`parse_runway_ft`/`parse_dist`、`simbrief_url` | 一次规划的计算(FlightAware 抓取 + 模拟呼号 + SimBrief 链接),供 UI 调用;渲染由 UI 负责 |
| **`dispatcher/controller.py`** (F25,✅ v2.0.0) | `AppState`、`init_app`、`plan`、`compute_proc`、`volanta_sync`、`LogSink`、`NavDataMissing` | **编排层(零 GUI 依赖)**:应用状态 + 后台任务 + 日志。**绝不 import 任何 GUI 框架;只抛异常、只返回值、只回调**,跨线程 marshal 交给 UI |
| **`dispatcher/viewmodel.py`** (F25,✅ v2.0.0) | `result_spans`、`map_model`、`ProcPanelModel`、`OpsEditorModel`、`AipTableModel`、`AircraftModel`、`MultiSelectModel`、`form_to_rule`/`rule_to_form`、`enabled_controls` | **各界面的纯数据 Model(零 GUI 依赖)**:**每个界面 = 一个可 headless 单测的 Model + 一层薄渲染**。两套 UI 共用同一 Model → 行为天然一致;这也是这些逻辑**第一次**能脱离 GUI 单测 |
| **`dispatcher/ui_flet/`** (F25,✅ v2.0.0) | `run_flet`;`shell` / `theme` / `app` / `form_view` / `result_view` / `log_view` / `proc_view` / `aip_dialog` / `ops_view` / `map_view` | **Flet 前端(Flutter 渲染)**——v2.0.0 起的正式前端。**`shell.py` 是唯一允许碰 `page` 的模块**(Flet 的 `page.update()` 非线程安全,marshal 强制经 `page.run_task`) |

> **拆包关键点**:`config.py` 的 `get_real_run_path()` 是所有同级文件(`NavData/`、`installed_scenery.json`、`routes_cache.csv`、`volanta_data.json`)的锚点。源码模式下它返回**包目录的上一级**(`dirname(dirname(__file__))` = 项目根),而非模块自身目录,从而拆包后定位到与单文件时**完全相同**的根目录;冻结(exe)模式仍取 `sys.executable` 所在目录。

### 3.3 核心数据模型

```python
class Airport:
    code             # ICAO 机场代码
    lat_dd / lon_dd  # 十进制经纬度
    scenery_sources  # None=未检测 / set()=无地景 / {'XP','MSFS'}=有地景；has_scenery 由它派生
    is_military      # 是否为军用机场
```

---

## 4. 关键模块详细说明

### 4.1 导航数据定位 (`find_navdata_file`)

**导航数据(`earth_aptmeta.dat`)定位**(F12,以**自带 NavData** 为唯一方案):

- **程序自带 `NavData` 文件夹**:读 `<程序目录>\NavData\earth_aptmeta.dat`(在 NavData 下浅层兜底递归),**彻底解耦 XP**,只飞 MSFS、没装 XP 的用户也可用;缺失时 GUI 弹窗提示前往 Navigraph 下载。
- (历史)命令行版还有「exe 同级 `.dat` → 自动扫 X-Plane 安装(`locate_xp_root`)→ 手动粘贴路径」三级兜底(`find_xp_data_files`),已随 CLI 在 v1.3.1 移除;`locate_xp_root` 保留,仅供地景扫描定位 XP 根。

> 地景检测已独立为 F5 多源扫描(见 4.6),故 `scenery_packs.ini` 不再是必需,`.ini` 一律「可选」。
> **AIRAC 自检**(`check_airac_currency`):启动读 `NavData\cycle_info.txt`(`AIRAC cycle` + `Valid (from/to)` 失效日期),过期则提示**前往 Navigraph 下载页 `https://navigraph.com/downloads` 重新下载「X-Plane 12」导航数据并替换 NavData 文件夹**。导航数据完全缺失时,`load_airports_from_navigraph` 的错误提示同样给出该下载链接与放置说明。

> `get_real_run_path()` 决定所有同级文件(`installed_scenery.json`、`routes_cache.csv`、`volanta_data.json`)的锚点:冻结(exe)模式取 exe 所在目录,源码模式取项目根目录。

### 4.2 机场加载 (`load_airports_from_navigraph`)

解析空格分隔的 `.dat` 文件,过滤规则:
- 仅保留 ICAO 区域为 `RJ` / `RO`(日本)的机场;
- 跑道长度 ≥ `min_runway_ft`;
- `type_flag == 'M'` 标记为军用机场;
- 地景来源由 F5 多源扫描得到的 `scenery_map`(`dict{ICAO: {'XP','MSFS'}}`)注入:`scenery_sources = scenery_map.get(code)`,`has_scenery` 由其派生。

> 注意:主循环 **每轮都重新加载机场列表**,因为跑道长度阈值 `min_runway_ft` 是每轮可变的输入。

### 4.3 AIP 航路数据 (`load_aip_routes_from_csv`)

**网络优先 + 本地缓存** 策略:
1. 优先联网下载 `https://jp-routes.vercel.app/public/routes.csv`(5 秒超时),成功后同步刷新 `routes_cache.csv`。
2. 网络失败时退入本地缓存;若缓存超过 28 天(2,419,200 秒)给出过期提示但仍继续使用。
3. CSV 列结构:`DEP, DEST, Time Restriction, Altitude, Aircraft, Route, Remarks`。
4. `find_aip_route` 按起降 ICAO 精确匹配返回所有符合的航路行。

### 4.4 现实航班抓取 (`fetch_real_flights_with_filter`)

- HTTP GET FlightAware `findflight` 页面,用正则提取页面内嵌的 `FA.findflight.resultsContent` JSON 数组。
- 过滤维度:航司 ICAO 前缀、机型(经 `is_aircraft_match` 模糊匹配)、起飞时间区间。
- 返回逻辑:最多返回 5 条匹配项;若无匹配项则返回最多 5 条该航线上的其他参考排班;若抓取整体失败,调用方降级为随机模拟呼号。

> **脆弱耦合点**:整套抓取依赖 FlightAware 的页面结构,正则 `FA.findflight.resultsContent = [...]` 是最易因对方改版而失效的环节。

### 4.5 航线抽取 (`get_random_route`)

- **枚举 + 加权抽取**:枚举所有满足约束(大圆距离落在 `[min_dist, max_dist]` + 可选 AIP)的候选航线,按权重 `random.choices` 抽一条(已取代早期"拒绝采样、最多 150k 次循环"的做法)。权重规则见下两条。
- 大圆距离由 `calculate_distance_nm` 用 Haversine 公式计算(单位 NM,地球半径取 3440.065 NM)。
- `strict_aip=True` 时还要求该航线必须存在 AIP 航路。
- **仅地景机场(F14,`require_both_scenery=True`)**:在距离过滤之后追加「两端都须 `has_scenery`」的过滤,与 AIP 过滤、军用分层、Volanta 加权自然叠加。`scenery_sources is None`(未检测到地景目录)时 `has_scenery` 恒 True、过滤失效;零候选时抛 `RuntimeError` 被调用方接住。仅作用于随机规划(固定双端不走本函数)。
- 支持 `fixed_dep` / `fixed_dest` 固定一端或两端。
- **软优先未飞(F11,按已飞次数加权)**:传入 Volanta 已飞次数 `dict{(dep,arr): count}` 时,改为**枚举所有满足约束(距离 + 可选 AIP)的候选航线 → 按 `w = 1/(count+1)²` 加权随机抽取**(`random.choices`)。未飞(count=0,权重 1.0)基本总被优先;无未飞航线时自动优先飞得少的;`flown` 为空则权重相等、**退化为均匀随机**(与原行为一致)。严格 AIP 模式预建 `set((dep,dest))` 索引,避免枚举时逐对线性查表。抽中航线的 `count` 随结果返回,供显示。
- **优先民用机场(避免随到不可飞的军用机场)**:候选按「军用端数量」分 0/1/2 三层(两端民用 / 一端军用 / 两端军用),**只从军用端最少的非空层里加权抽线**。这样普通情况只抽两端民用的航线;**重度用户**(民用航线几乎都飞过、未飞的只剩军用)也不会因「未飞权重高」而被推去军用机场(宁可重飞最少飞的民用航线)。用户**显式固定**军用端时退到「一端军用」层(另一端仍优先民用),并照常显示 `[🛡️军用机场]` 提醒;实在没有民用候选才退到含军用层。军用机场常缺民航助航设施/进近,故软优先民用以保证规划可飞。

### 4.6 Volanta 已飞航线读取 (`load_volanta_flown_routes` 等)

让随机规划**优先安排没飞过的有向航线**,数据取自用户的 Volanta 飞行记录。

- **数据来源(主)**:Volanta 的 **`/api/v1/Flights` 接口**(一次返回全部航班的干净 JSON)。程序用**用户自己浏览器里 Volanta 的登录会话令牌**(localStorage 里的 Orbx JWT)自动调用它并保存为 `volanta_flights.json`——零操作,逆向定位与安全边界详见下方「最佳数据源」条。
- **解析方式**:`_load_volanta_json` 逐条解析 `volanta_flights.json`(每条为 `{flight:{...}, summarisedPositions:[...]}`,取 `flight.originIcao`/`destinationIcao`),**统计每条有向航线的已飞次数**得到 `dict{(dep, arr): count}`(供加权抽取用)。纯标准库,契合「仅标准库 + PyInstaller」硬约束。
- **统计口径**:`sum(count)` = 总飞行次数(与 Volanta「已完成航班数」对齐,实测两者一致),`len(dict)` = 去重后的不同有向航线数;**自环航班**(`dep==arr`,本场/复飞)对 A→B 规划无意义,跳过。启动提示同时显示二者(如「230 次飞行、覆盖 177 条不同有向航线」)。
- **浏览器同步(登录兜底)**:在「未开启自动、用户本次选 Y」且本机已有登录会话取不到有效令牌时触发;打开 **`fly.volanta.app/map`** 让用户**登录**,随后**轮询 `try_fetch_volanta_json_via_session` 直到令牌落盘、API 拉取成功**。**令牌在 `/map` 登录后即生成**(不需 `/flights`),但 **Chromium 把 localStorage 从内存写到磁盘有 ~30s~1min 延迟(空闲更久、偶尔 >180s)**,程序读磁盘 leveldb 故需等落盘——这是旧 180s 偶尔超时的根因(2026-06-28 修)。现轮询放宽到 **300s** 并用两段弹窗引导(开始「等待写入」/ 约 1 分钟后「去航班页刷新+滚动催落盘」),成功也弹提示;滚动/导航会产生 localStorage 写入触发提前落盘(这就是用户手动滚 `/flights` 能成功的原因)。Orbx 令牌有效约 14 天,期间直接用令牌调 API、不再开浏览器;过期后再次引导到 `/map` 登录。同步偏好持久化在 `volanta_data.json` 的 `preference`(详见 2.4)。
- **方向语义**:有向(`RJTT→RJBB` ≠ `RJBB→RJTT`)。
- **CSV 兜底(可选)**:工作目录放入 Volanta 官方导出的 `volanta_flights.csv` 时,用标准库 `csv` 解析其起降列,每条飞行对已飞次数 `+1`,累加进同一 `dict`。仅作离线兜底——官方导出需排队约 15 天,日常完整数据已由上面的会话自动拉取覆盖。
- **最佳数据源 `/api/v1/Flights`(程序用登录会话自动拉取)**:浏览器懒加载导致缓存常不完整;最干净完整的来源是 **`/api/v1/Flights` 接口**(一次返回全部航班)。程序用**用户自己浏览器里自己的 Volanta 登录令牌**直接调它(类比 yt-dlp 的 `--cookies-from-browser`):
  - 逆向发现:API 拒收 Firebase idToken(issuer 不符),它要的是 **Orbx 签发的 JWT**(`iss="Orbx"`,有效期约 **14 天**;Volanta 是 Orbx 旗下),存在浏览器 **localStorage**。`_extract_volanta_api_token` 扫 localStorage 但**只取 `iss=="Orbx"` 且就近有 `fly.volanta.app` 来源标记**的令牌,绝不碰其它网站令牌;`_fetch_volanta_flights_json` 带 `Authorization: Bearer` 调 API(请求 gzip 避开 br)。
  - **安全边界**:token 仅在内存用于这一次请求,**绝不落盘、绝不发往 api.volanta.app 以外任何地方**;读的是用户自己浏览器里自己的会话、访问自己的数据,无第三方、无外传。
  - `try_fetch_volanta_json_via_session` 解析响应、把权威次数原子并入 `volanta_data.json`(**不再在磁盘保留 ~900KB 原始响应**;`skip_if_fresh` 控制:上次拉取够新则不重复联网,启动用 3600s)。**用户开启自动同步后,主流程启动即静默拉取**(~0.5s);只要近 ~14 天用过 Volanta,用户**零操作**(无需开发者工具/滚动)即得完整数据。浏览器同步的轮询里也走这条快路径(用户登录后即抓取,无需滚动)。
  - 用户手动导出的 `volanta_flights.json`(DevTools Copy 的响应)放入工作目录也会被读取:`load_volanta_flown_routes` 用 `_load_volanta_json` 解析(每条 `{flight:{...}, summarisedPositions:[...]}`,取 `flight.originIcao`/`destinationIcao`)并入 `volanta_data.json` 后**删除**该导入文件(已吸收,保持单文件)。
- **单一数据文件 `volanta_data.json`(浓缩偏好 + 累积库 + 拉取时间)**:所有 Volanta 持久化数据合并进一个文件——`{"preference": "auto"|"ask", "fetched_at": <unix>, "flown": {"DEP|ARR": 次数}}`,**取代早期分散的 `volanta_config.txt` / `volanta_flights.json` / `volanta_flown.json`**(首次运行自动迁移合并并删除旧三文件)。`flown` 为只增不减的累积库:`load_volanta_flown_routes` 把它与可选导入(`volanta_flights.json` 权威 / `volanta_flights.csv` 兜底)合并——**航线键取并集**(飞过的永不丢);**次数优先用权威源**,否则回退 CSV/累积库最大值;有变化则原子落盘。对加权只「飞过 vs 没飞过」要紧,次数量级影响小。`meta['latest']` 为 `fetched_at`(最后一次成功拉 API 的时间)。
  > **已移除**:早期"扫 IndexedDB leveldb 用正则提航线"(`find_volanta_leveldb_dirs`/`extract_flown_routes`/mtime 轮询)整套——V8 去重 + 懒加载导致它脆弱且不完整,已被 token→API→JSON 取代。`_read_leveldb_text` 保留,现仅用于读 localStorage 取登录令牌。

> **脆弱耦合点**:依赖 `/api/v1/Flights` 的响应字段(`flight.originIcao`/`destinationIcao` 等)与 localStorage 里 Orbx 令牌的格式(同 FlightAware 爬虫级别)。Volanta 改版、令牌过期、未登录、文件锁等任一失败时,**静默降级为"不启用优先"**,不影响主流程。

### 4.7 多源地景扫描 (F5,`scan_installed_sceneries` 等)

直接扫描两个模拟器的地景安装文件夹,得到「已装地景机场 → 来源」,供 `has_scenery` 与标注用。

- **通用定位**(不硬编码路径):XP 经 `locate_xp_root`(`installed_scenery.json` 记忆 + 全盘扫描);MSFS 经 `find_msfs_packages_dir`(多候选 `UserCfg.opt` 的 `InstalledPackagesPath`,覆盖 2020/2024 × Steam/MS Store,失败再全盘扫描)。
- **XP 提取**:遍历 `Custom Scenery` 各包,读 `Earth nav data\apt.dat` 机场行(行码 1/16/17,第 5 字段为 ICAO),仅留 `RJ/RO`。
- **MSFS 提取(四步级联,逐级降级、命中即止)**:① 包文件夹名;② `ContentInfo\…\ContentHistory.json`(`items[]` 中 `type=="Airport"` 的 `content`,**权威**);③ `scenery\*.bgl` 文件名;④ 都无则不计入(机模/库)。另用 `manifest.json` 的 `content_type=="SCENERY"` 预筛。
- **假阳性过滤(白名单)**:文件夹名/bgl 名是用 `R[JO]xx` 子串正则提 ICAO 的,凑巧形如 ICAO 的普通单词会被误抓(如 `ngt-road-mesh→ROAD`、`aerocaches→ROCA`)。故提取结果再用**导航数据里的真实 RJ/RO 机场白名单**(`load_japan_icao_set` 读 `earth_aptmeta.dat` 全部 RJ/RO 机场)二次校验——只有真实机场集合能区分 `ROAD`(非机场)与 `ROAH`(那霸)。正则本身也加了单词边界做第一道防线;白名单为权威判据(导航数据缺失时跳过过滤,避免误删)。
- **合并**:`dict{ICAO: set('XP'|'MSFS')}`;输出标注 `[地景:XP]` / `[地景:MSFS]` / `[地景:XP+MSFS]` / `[⚠️无地景]`。
- **缓存**:`installed_scenery.json` 统一存 `xp_root`/`msfs_packages`/`sceneries`/`fingerprint`;指纹 = `{sim目录: {包名: mtime}}`,只 `listdir+getmtime`;未变直接读缓存(实测 0.66s→0.032s);旧缓存里残留的假阳性会在下次缓存命中读取时自愈回写。

> **MSFS 召回局限**:`manifest.json` 无 ICAO 字段,命名极不规范且无 `ContentHistory.json` 的包会漏(要 100% 准需解析 bgl 二进制,本期不做)。非日本(RJ/RO 外)地景能扫到但不参与日本规划。

---

## 5. 外部依赖与约束

| 依赖 | 类型 | 风险 |
| --- | --- | --- |
| `jp-routes.vercel.app` | 第三方网站(爬取,非官方 API) | 可能随时变更或下线;有本地缓存兜底 |
| `flightaware.com` | 第三方网站(爬取,非官方 API) | 页面改版即失效;**无结果缓存** |
| `earth_aptmeta.dat`(程序自带 `NavData/`) | Navigraph 导航数据 | 需用户放入 NavData 文件夹;AIRAC 过期会提示更新 |
| XP `Custom Scenery` / MSFS `Community` 地景目录 | 模拟器本地文件夹(读 apt.dat / 包结构,非 API) | 缺失则该来源软降级;MSFS 命名极不规范的极少数包会漏;`installed_scenery.json` 缓存 |
| Volanta(`api.volanta.app` 官方接口 + 浏览器 localStorage 登录令牌) | 第三方(用用户自己浏览器会话调官方 API) | 前端改版 / 令牌过期(~14 天)/ 未登录即失效;失败降级为不启用优先 |

**单位约定**:
- `min_runway_ft` 接受用户单位(`1800m` 或 `5900ft`)并统一归一化为英尺,默认 5900 ft。
- 航程单位为海里 (NM)。

---

## 6. 非功能性需求

| 维度 | 要求 |
| --- | --- |
| 部署 | 单文件 exe,免安装;`pyinstaller --onefile --windowed flight_dispatcher.py` 构建(`--windowed` 去控制台,GUI 把 stdout 重定向到日志区故安全) |
| 依赖 | 仅 Python 标准库,无第三方包 |
| 离线能力 | AIP 航路支持离线(缓存);现实排班查询必须联网 |
| 容错 | 网络超时快速降级(AIP 5s / FlightAware 6s);数据文件缺失给出中文提示而非崩溃 |
| 国际化 | 全部文案简体中文 + emoji,新增输出须保持同一风格 |
| 健壮性 | 规划后台线程捕获异常,经 `after()` 转为结果区中文错误行(`❌ 发生错误`),单次失败不影响后续规划 |

---

## 7. 运行逻辑(GUI 时序)

> GUI 把启动初始化放在后台线程(经 `after()` 回主线程更新状态/日志),把规划做成**事件驱动**——每点一次「规划航线」按钮执行一轮 `_plan_worker`(也在后台线程);计算统一走 `planner.build_flight_plan`。

```
[启动初始化 — 仅一次,后台 _init_worker]
  find_navdata_file()             # 定位导航数据(读自带 NavData;缺失则弹窗提示去 Navigraph)
  check_airac_currency()          # AIRAC 周期自检，过期则提示更新
  scan_installed_sceneries()      # 多源地景扫描(XP/MSFS)→ scenery_map，指纹缓存秒开
  init_airline_data()             # 加载/生成 airlines.json(航司执飞规则)
  load_aip_routes_from_csv()      # 加载 AIP(网络优先)→ 预建 aip_index
  # Volanta:按 volanta_data.json 偏好——已开启(auto)则静默用登录会话拉 API;
  #          未开启则不扫浏览器(由「同步 Volanta」按钮按需触发)
  load_volanta_flown_routes()     # 解析 json(+CSV)并入累积库 → 已飞有向航线集合(失败则空集,优雅降级)
  → _on_init_done(经 after() 回主线程):存结果、启用表单

[每次点「规划航线」— 后台 _plan_worker]
  快照表单输入 → 解析跑道长度 / 航程范围
  load_airports_from_navigraph()         # 按本次跑道阈值重载机场
  if 机场列表为空: 抛错(结果区中文提示)
  if 严格 AIP 但 AIP 数据为空: 转自由规划模式

  if 出发与目的均固定:
      直接计算距离 + 查 AIP 航路;严格模式且无 AIP → 抛错
  else:
      get_random_route(...)              # 枚举候选 + 加权抽取(1/(count+1)²);按军用端数量分层优先民用;可选仅两端有地景
  build_flight_plan(...)                 # 触发现实排班抓取(无排班→按航线挑航司生成模拟呼号)→ FlightPlan
  → _render_plan(经 after() 回主线程):渲染结果卡;已飞航线标注「已飞过 N 次」
```

---

## 8. 已知限制与未来规划

### 8.1 当前限制
- 仅支持日本地区(`RJ` / `RO` 区域)。
- 现实排班结果无缓存,每次均需联网抓取。
- 强依赖两个第三方网站的页面结构,无官方 API 保障。
- 无批处理/无人值守模式:仅 GUI(需窗口交互);v1.3.1 起已移除命令行版(CLI),没有 stdin 管道 / 命令行参数式一次性批量规划的接口。
- Volanta 优先未飞:**首次启动询问是否同步**,选 Y 后程序用浏览器里的 Volanta 登录会话(Orbx token,~14 天有效)自动调 `/api/v1/Flights` 拉取并记住偏好——需用户近期在浏览器登录过 Volanta;否则可手动登录同步或放入导出的 `volanta_flights.json`。
- **Volanta 令牌读取仅支持 Chromium 系浏览器(Edge / Chrome / Brave)**:程序从其 **LevelDB 格式**的 Local Storage 抠出 Orbx 登录令牌;**Firefox / Safari 等非 Chromium 浏览器使用不同的本地存储格式,读不到**——即便用户在其中登录 Volanta 也无法自动获取(此时只能手动放入导出的 `volanta_flights.json` / `volanta_flights.csv`)。且浏览器/路径定位当前以 **Windows** 为主(`%LOCALAPPDATA%` 布局、自动打开优先用 Edge)。**非 Chromium 浏览器与 macOS/Linux 支持列为下一轮「跨平台兼容」更新重点**(见 8.2)。
- 导航数据需用户自行放入 `NavData` 文件夹并定期更新(AIRAC 28 天周期,过期会提示)。
- 地景检测仍需访问 sim 安装目录;MSFS 命名极不规范且无 `ContentHistory.json` 的包可能漏标地景。
- **地图不能并排开多个窗口(v2.0.0 起,永久)**:Flet **无多窗口**,tk 版「一条航路一个窗口、可并排比对分时段航路」做不到;改为**同一地图视图内的标签页切换**(N 条航路一键切换,信息不丢、只是不能同屏并排)。这是 UI 迁移(F25)唯一无法复刻的功能。
- **地图瓦片不再用 OSM 官方源(v2.0.0 起)**:`tile.openstreetmap.org` **403 封禁 flutter_map 的 User-Agent**,而 Flet 的 `TileLayer` 无 headers 字段、改不了 UA → 改用 **CartoDB**(浅色 `voyager` / 深色 `dark_all`,跟随系统深色模式)。署名 `© OpenStreetMap contributors · © CARTO`。

### 8.2 未来规划(摘自 README 更新日志)
- **本地 A* 航路生成(🚧 开发中,v1.4.0,F15):无 AIP 航路时自研寻路**——当 `jp-routes` 查不到直连 AIP 航路时,用程序自带 `NavData/`(`earth_awy/fix/nav` + **`CIFP/` 进离场程序**)在本地 A* 寻路生成参考航路。纯标准库、离线、无第三方。新增 `dispatcher/router.py`,`routing.py` 不耦合。
  - **背景**:曾评估 SimBrief `/v2/routes/generate`(逆向可用),但其 Navigraph 登录令牌**只在浏览器 JS 内存、磁盘抠不到** → 放弃,改完全自给。
  - **优先级链**:**Rule 0** 直连官方 AIP(app 层 `find_aip_route`,最高优先,不调 generate)→ 否则 **case 1–4** 在端点间 A*。（~~Rule 5 借邻近机场官方 AIP 桥接~~ 已于 `1.4.1_alpha2` 删除——端点学习落地后直接 A* 即理论最优，桥接借邻场离场点反致倒飞，详见续作3。）
  - **端点选择(学到端点 ∪ 移管门 ∪ CIFP，并集 + 方向过滤，A* 自选最优)**:**从 `routes.csv` 学到的各机场真实进/离场过渡点**(`dep_heads`/`arr_tails`) ∪ VATJPN 移管表官方门 ∪ CIFP(离场=SID 出口 ∪ **本场 VOR**；进场=STAR 入口/本场 VOR/IAF·IF);**本场 VOR 按坐标取距机场最近的一个**(`_onfield_vor`，≤15nm，排除 section-D 里的中途点/feeder VOR);全部**连通性过滤**(须在航路网上);几何最近点(方向感知)作末位兜底。
  - **质量**:enroute 大锐角(接近掉头)转弯 → 标「请自行检查斟酌」;输出结果卡单列「🧭 生成航路(非官方 AIP、仅供参考)」。
  - **状态**:核心已实现自测过;**可调旋钮(K=1.25、overshoot=20nm、转弯阈100°、bbox 等)待用户实测后定**;dist 待重编。设计详见 `revisions.md` v1.4.0、规则见 `flight_planning.txt`。
  - **v1.4.0 续作(同期已实现)**:① Rule 5 升级**双端桥接**(dep 也可用附近机场替身，所借 AIP 头点 ≤50nm DCT 接)；② **航路加密**(Dijkstra 沿 airway 补中间 fix → 距离精确 + 画图密，RJFR→RJEC 11→21 点)；③ **SimBrief 一键派遣(F16)**(生成预填 URL，用用户自己 SimBrief 登录态出 OFP)；④ **机型库(F17)**(`aircrafts.json` 212 机型 + 可搜索下拉)；⑤ **航路交互地图(F18)**(`tkintermapview` 内嵌 OSM 地图，每条航路独立开窗)；⑥ **航路距离/偏差(F19)**。**首次引入第三方库** `tkintermapview`/`Pillow`(仅地图用，缺失自动禁用)。详见 `revisions.md` v1.4.0 续作。
  - **v1.4.0 续作2(航路质量，借 `route_planning` skill 系统迭代)**：让生成航路贴近真实运行，**全部从 `routes.csv` 学习**(官方发布航路永不逆向用航路)——① **优先 RNAV 航路**(A* 罚纯传统边 + 标名优先 Q/T/Y/Z·L/M/N/P；RNAV 占比 74→92%)；② **航路方向学习修单向逆飞**(earth_awy 把「单向 RNAV 与双向航路共挂段」标 N 丢方向 → 逐段学 `legal_seg`，单向 RNAV 只在被实飞证实方向标名，修 Y284/Y43/Y312 逆飞；纯标名层、不改图)；③ **高频干线走廊加权**(`seg_pop` 走廊热度 → A* 软偏好真实常用走廊；干线占比 73→80%、+0.4% 距离)；④ **VATJPN 移管表官方进/离场端点**(`transfer_points.json` 56 机场进场门/离场头，优先于 CIFP 猜 STAR/本场 VOR/IAF，含方向过滤 + 离场门「大锐角/超大圆即弃门用本场台离场」质量门控)。综合实例 RJSA→RJSS 由 279nm(+79%·逆飞·收东门) → 163nm(+5%·收北门 SDE·本场台离场)。新增数据文件 `transfer_points.json`(随 exe 同级)。设计详见 `revisions.md` v1.4.0 续作2；领域知识在 `route_planning` skill。
  - **v1.4.1_alpha2 续作3(AIP 桥接 × 走廊融合)**：① **进/离场端点学习**——`_learn_routes` 从 `routes.csv` 多学各机场真实进/离场过渡点(如 RJGG 北向 KCC、RJFM 东向 MADOG)，作端点候选首选(只取航路串首/末 token 是航点的，避免占位航路把远端 fix 当离场点)；② **端点选择改并集**(学到端点 ∪ 移管门 ∪ CIFP + 方向过滤 → A* 自选最优，对直接航路严格不劣)；③ **删除 Rule 5 AIP 桥接**(桥接借邻场离场点致倒飞/绕远·弊大于利；删后总距反而更短、退化 0)；④ **本场 VOR 算法修复**(`_onfield_vor` 按坐标取最近 ≤15nm，修 RJSY→RJCH 误用 37nm 外的 SID 中途点 YTE 离场)。修 RJFM→RJBE(MADOG)、RJGG→RJCN(KCC)、RJSY→RJCH(YAYOI Y312·+0.5%)。设计详见 `revisions.md` 续作3。
  - **v1.4.1 续作4（生成航路进场尾段 + SimBrief 对齐）**：① **VATJPN 到着尾段补全**——生成航路进场原来只到「进场门」就停，现按 `transfer_points.json` 新增的 `arr_dct` 把门后的 **DCT 直飞点**补进 enroute（如 RJFM 北向 `KUE ESKAP KROMA ENBEN MZE`、RJOO `AGPUK MIRAI ABENO IKOMA`，方便管制引导），带「背离本场即截断」裁剪；25 机场受益，尾点全从图坐标解析、`route_geometry` 自动跟随。② **SimBrief 链接对齐**——结果卡「一键签派」原来发空 `route` 让 SimBrief 自算（与本工具航路完全不符），现默认带**本工具展示的航路**（生成航路含尾段优先，否则首条 AIP），与 F20/F21 面板链接口径统一。详见 `revisions.md`。
  - **v1.4.1 修复（FlightAware 中转误判为直飞）**：现实排班检索原来把中转联程的各**航段**误当直飞列出（如 SYO→ITM 把经 HND 的 `ANA398`/`ANA21` 当直飞）；因 `findflight` 每条结果只是一个航段(带 `origin`/`destination`)。现只保留 `origin`==出发且 `destination`==到达的**直飞段**，两端 IATA 从结果数据自推（无需对照表）。实网验证：SYO→ITM→0 条(降级模拟呼号)、HND→ITM→5 条真直飞。详见 `revisions.md`。
  - **v1.4.1-alpha4 进场走廊奖励**：生成航路的进场落点原来只按 A* **最短**选，会漏掉「略长但真实常飞」的走廊（如 RJKN→RJFF 南向本该走 `Y25 ISKUP`，却落近场 DGC）。现加**走廊奖励**：把落点换成**方向合规、最常飞的学到进场门**（`routes.csv` 学到尾点带频次=真实落地热度），前提改落它 ≤1.15×最短、且频次 >1.5×当前落点（频次守卫挡「两门频次相近误换方向」，如 RJTT→RJOO 保持官方 IKOMA 不换 IZUMI）。同时保留 VATJPN 合法到着（如 RJFF 的 FUGEN 经路）作可达备选。详见 `revisions.md`。
  - **v1.4.1-alpha4 修复（无 STAR 机场仍可选跑道）**：F20 面板原来在机场**无 SID/STAR** 时把跑道也一并置「（无可选程序）」不可选（如 RJTT→RJER，RJER 无 STAR）。但很多机场没有 STAR、跑道却有**仪表进近程序(IAP)/雷达引导**，不该因无程序就弃选进场跑道。现改：`matching_choices` 若无任何程序则**回退列出全部物理跑道**（label 空=无 SID/STAR、进近走 IAP），出/到对称生效；GUI 提示改三态（无跑道数据 / 无可用 SID·STAR·可选跑道 / 端点未直接匹配）。详见 `revisions.md`。
- **F20 跑道 + SID/STAR 选择 + 天气(🚧 v1.4.1,未提交)**:规划后在结果区让用户为该航班细化进离场。**新增 `dispatcher/procedures.py`**(解析 CIFP `RWY:`/`SID`/`STAR`：跑道长度由**两端跑道头坐标**算·朝向=跑道号×10·CIFP 该两字段不可靠;`enumerate_procedures` 出每程序的服务跑道(`RW34B`→双跑道)/enroute 过渡/连接航点(section **E∪D**,含 VOR);`matching_choices` 按**航路首/末点预筛**接得上的 `SID.TRANS`/`STAR`,**按过渡端点匹配**(离场 SID 接过渡末点=TRANS、进场 STAR 接过渡首点;详见 F21 端点修复),无命中回退全部;服务全跑道(ALL/common)的程序挂到**物理跑道**;**机场完全无 SID/STAR 时回退列全部物理跑道·label 空**(alpha4:进近走 IAP,别因无程序弃选跑道))。**新增 `dispatcher/weather.py`**(METAR+TAF 都从 **NOAA tgftp** 取·非 metar-taf.com;`parse_wind`/`runway_wind` 带符号逆风+侧风/`runway_ok` 顺风≤10·侧风≤30节)。GUI 结果区下方面板:每机场 METAR+TAF 块紧贴其行;跑道下拉显示**米长度 + 逆/顺风X节 侧风Y节 + 适航✓/超限**(合规→逆风→号排序·合规预选但可改);跑道→程序级联;选定即拼进 SimBrief `route`(=SID+enroute+STAR,经 `planner.simbrief_url`+`FlightPlan.sb_base`)。**不改本地航路生成**;无 CIFP/断网优雅降级。设计详见计划文件 `~/.claude/plans/giggly-weaving-zephyr.md` 与 `revisions.md` F20。
- **F21 多 AIP 航路按 EOBT/机型/高度选 + 全段航路预览(🚧 v1.4.1,未提交)**:一航线多条官方 AIP(按运行时段 EOBT/ETA、机型、巡航高度分)时,规划后**自动弹窗**让用户选一条(单条不弹)。**两重严格度**:不勾「严格遵循现实运行规则」＝弹窗**罗列全部**(仿真实 AIP 表:时段/用途(日间·夜间)/机型/高度/距离/航路 + 行首选择框)手动选;勾＝弹窗收 **EOBT(撤轮挡,JST)+机型(JET/PROP)+巡航高度**,时间可靠自动筛、机型/高度按**用户给的参考值**判属(True/False/无法判)→ 唯一匹配即**自动定唯一**(破解 v1.4.0「凭脏列自动选」死结:现由用户显式开关+亲自给参考值;`FL180-FL230` 区间也能正确判属)。选定即重驱动 F20 的 SID/STAR 预筛 + 重建 SimBrief `route`,可随时切换。**新增 `dispatcher/timed.py`**(时间层复活 + `alt_matches`/`aircraft_matches`/`filter_candidates`/`resolve_unique`/`describe_restriction`)。**全段预览**:选定后「🗺️ 预览完整航路」把 **SID+enroute+STAR 全段**画到地图(复用 F18);`procedures.py` 加坐标索引(`earth_fix` 含 terminal + `earth_nav`)+ `procedure_coords`/`full_route_coords`。**顺带修 F20 端点匹配 bug**:过渡按【端点】(离场末点/进场首点)而非全集匹配,修 RJCC `TOBBY` 被误标 `X.BUTOS`(应给裸 SID)。设计详见计划文件 `~/.claude/plans/giggly-weaving-zephyr.md` 与 `revisions.md` F21。**这是下条「分时段规划」延后项的重做兑现**(时间可解析、机型/高度留用户自选)。
- **F22 网格天气回退(✅ v1.5.0)**:很多日本小机场(RJTO/RJTH/RJAF/RJER 等)的 METAR **只在日间更新或完全取不到**,夜间返回白天的旧报文;现状 `weather.py` 取到 `obs_time` 却从不使用、无过期判断。当 METAR **缺测或过期**(观测 >`_METAR_STALE_SEC`=2h)时,改用机场坐标处的 **Open-Meteo 网格模型天气**近似。**采纳用户反馈**:不另造展示字段,而是把网格数据**编码成一条标准格式 METAR 串**(合成 METAR),直接喂现有 `parse_wind` 与显示流水线——`_wind_desc`/`_fill_rwy`/`parse_wind` **完全不改**。`weather.py` 新增 `metar_age_sec`/`fetch_grid_weather`(纯标准库 urllib+json、无需 key、`models=jma_msm` 日本本地 5km、其 `visibility`/`gust` 恒 null、域外才回退 `best_match`)/`grid_to_metar`(编码,**已用真实 RJFK 报文校准**)/`resolve_airport_wx`(METAR 新鲜→用实测;缺测/过期→合成网格 METAR)。**编码取舍(校准结论)**:Open-Meteo `visibility` 不可靠(RJFK 模型 920m vs 实报 9999)→**保守**(默认 9999、仅 WMO 雾码/强降水才信低能见度、绝不臆造 FG/BR);降水强度按 `precipitation` 速率、毛毛雨归并 RA;云用 `cloud_cover_low/mid/high` **分层**(勿塌成单一 OVC)、云底未知记 `///`;温压极准。GUI `_compute_proc` 改用 `resolve_airport_wx`、`_wx_text` 两分支同一渲染仅标题标注不同(网格标「🌐 Open-Meteo·模型合成 METAR·非实测」)。**决策支持而非替用户拍板**;`jma_msm` 域外/断网优雅降级。天气数据来自 **Open-Meteo (CC BY 4.0)**(一次性署名 + README 致谢)。设计与 RJFK 校准详见 `~/.claude/plans/giggly-weaving-zephyr.md` 与 `revisions.md` v1.5.0。
- **F23 机场运行规则可视化编辑器(✅ v1.5.0)**:日本很多机场有成套运行规则(羽田典型:南風運用時 16L/16R 起飞、22/23 落地;北風運用反之;夜間又不同——按**时段+风**切换跑道/SID/STAR/IAP),这些规则可指导航路/跑道规划。本版交付**可视化编辑器**:「⚙️ 编辑机场运行规则」→ 弹窗为任意 RJ/RO 机场编写规则,存运行目录 `operation.json`(按机场 ICAO 键)。**用户已定**:①本轮**仅编辑器+存储**(规划时应用留待下一版,`operation.json` 结构已为其预留 `cond`/`dep`/`arr` 分段);②运行条件的「气象」**用顺/逆风换向门槛**——真实运行惯例是「相对某参照跑道的**风分量**到 N 节就换向」,故 `cond={time_jst, ref_runway, wind_kind, wind_min_kt}`(`wind_kind`=顺风 tailwind/逆风 headwind,分量按报告风 vs 参照跑道算,复用 `weather.runway_wind`;留空=默认构型):**顺风超**→触发换向(羽田相对 34 顺风≥10 即切南風運用)、**逆风超**→可作例外(都心運用:相对 22 逆风≥20 则保持南風A,靠规则优先级把该例外排在都心規則之上);②b **好天/坏天按云底高+能见度界定**(业内标准,如 LDA:云底≥1500ft·SCT 起算[少云 few 不计]·能见度≥6000m),故 cond 加 `ceiling_min_ft`/`ceiling_cover`(FEW/SCT/BKN/OVC 起算算云底)/`visibility_min_m`——好天规则填门槛、坏天规则不填并排其后作兜底;②c **深夜运用按星期几不同**(如羽田深夜 23:00–6:00 跑道构型逐日不同),故 cond 加 `days`(1=周一…7=周日 ISO,空=每天);③规则**分离场/进场**(SID 属离场、STAR/IAP 属进场)。**新增 `dispatcher/operations.py`**(运行目录读写:`load_operations`缺失/损坏→`{}`、`save_operations`原子写+剔除空机场、`airport_rules`/`airports`;仿 airlines.py;**v1.6.0 起纳入仓库跟踪并随发布包分发**,内含羽田 RJTT 整套规则作样例/默认,同 transfer_points.json)。**`procedures.py` +`enumerate_approaches`**(净新增:扫 CIFP `APPCH:` 按编码名 `p[2]` 归组→类型(首字母映射 I=ILS/D=VOR-DME/R=RNAV/X=LDA…)+跑道+后缀,合成显示名如「ILS RWY16L」「LDA W RWY22」;CIFP 无自然语言名故合成;盘旋进近如 VORA 用原始编码名)。**`gui.py` 编辑器窗口**(仿 F21 弹窗脚手架):顶部机场 ICAO 下拉(列已有规则机场+可键入新码);左侧规则 Treeview + 「＋新增」「📋复制」「－删除」按钮;右侧详情表单(名称、时段(JST,逗号多段)、**星期[7 勾选,全不勾=每天]**、**换向门槛[参照跑道 + 顺/逆风 + ≥N节]**、**好天门槛[云底≥Nft + 云量口径 Combobox + 能见度≥Mm]**、离场跑道+SID 多选 Listbox、进场跑道+STAR+IAP 多选 Listbox(**SID/STAR/IAP 带模糊搜索 + 点击即切换**),四类候选用真实 CIFP 数据填);底部保存/关闭。**增删改查 + 复制 + 拖拽排序**全在内存工作副本(`_ops_all`/`_ops_rules`),唯「💾 保存」原子落盘;切机场/保存前提交工作副本回全量 dict**隔离多机场**;IAP 存编码 ident(稳定)、显示合成名;未保存关闭有守卫。**易用性**:「📋复制」把所选规则整条深拷贝(相同跑道/程序不必每条重输);**Treeview 内拖拽调序**——但**规则匹配为「均等 + 恶天顺位下移」模型**(下一版应用引擎,用户 2026-07-03 定):引擎按条件命中选规则(时段+星期+风门槛)、**不按上下优先级**;风门槛达标即选该方向(南風顺风≥10/都心逆风≥15)、无门槛者为默认(北風);好/恶天靠位置——好天规则带云底/能见度门槛、其恶天规则紧跟下一行,天气不达好天门槛即「从好天规则往下数一条」;故拖拽调序只用来保证「好天→恶天」成对相邻(好天在上)、非全局优先级(用户手册写法留后期);**SID/STAR/IAP 列表带模糊搜索 + 点击即切换**(`selectmode=multiple`、值集合模型,过滤时隐藏的已选项不丢),免滚动查找 ctrl 点选。**纯编辑器、与规划解耦**(不改 `_plan_worker`/proc 面板)。设计与 CRUD 细节详见 `~/.claude/plans/giggly-weaving-zephyr.md` 与 `revisions.md` v1.5.0 F23。（**v1.6.0 更新**：都心例外的编码从「逆风≥20@22」改为更准的 **`crosswind@RW16L≥15`**——`wind_kind` 加了 **侧风(crosswind)** 种类；规划时的应用见 F24。）
- **F24 运行规则应用引擎(✅ v1.6.0)**:把 F23 编辑存下的 `operation.json` 规则**在规划时应用**，为该航班预选跑道/SID/STAR/IAP。进离场面板加 **EOBT(JST) 输入**(默认当前 JST、可改)+ **「按机场运行规则预选」开关**(默认勾)+ 两行「🎯 运行规则」标注。**新增 `dispatcher/operations.py` 的 `evaluate_gates(cond,ctx)` + `select_rule(rules,side,ctx,rows)`**：四闸(time/days/wind/weather，存在即须成立、缺省即过、天气未知按过=好天) + **均等取舍**(词典序 `路线不相容→−时段/星期具体度→−有满足风门槛→所选跑道侧风→顺风→−有天气门槛→列表序`)——路线相容按 端点标签∩规则程序 解 05 组↔34R 组、具体度解 深夜压全天、满足风门槛解 南風↔北風、所选跑道侧风解 都心(22 压 16)、有天气门槛解 好天压恶天孪生;**恶天自动用恶天配置**(恶天孪生无天气闸恒过、好天规则天气闸不满足被滤，等价「从好天往下数一条」、不依赖相邻)。风门槛 `wind_kind` 支持 **顺风/逆风/侧风**(侧风分量取 `runway_wind` 的 crosswind)。**新增 `weather.parse_sky`**(解 METAR 云组/能见度→云底 + 能见度，网格合成 METAR 云底 `///`=未知→好天门槛按过) + **`weather.now_jst`**(当前 JST 分钟+ISO 星期)。**`gui.py`**：`_apply_ops_rules`/`_apply_ops_side` 在 `_fill_rwy` 之后按 side 组 ctx(离场 EOBT-JST、到达 ETA-JST=EOBT+航程) → `select_rule` → 覆盖按风预选的跑道 + 级联 SID/STAR + 标注(含 IAP);EOBT 改动/开关切换即重跑;该 **EOBT 同时回填 SimBrief `deph`/`depm`**(撤轮挡时刻)。**决策支持、可改**：开关关或无规则 → 完全等同原按风预选;无 METAR/无 CIFP/规则跑道不在端点候选等全程优雅降级。设计详见 `~/.claude/plans/giggly-weaving-zephyr.md` 与 `revisions.md` v1.6.0。
- **F25 UI 迁移 tkinter → Flet(✅ v2.0.0)**:用户希望界面用 Flutter。**Flutter 本体(Dart)不能融合本项目**——Dart 进程跑不了 Python,硬上只能重写 3,837 行逻辑或搭 Python sidecar + IPC,均不划算。**[Flet](https://flet.dev) 是答案**:Python 写、Flutter 渲染,业务逻辑原地保留。本项目罕见地为此做好了准备——全仓 201 处 tkinter 调用**全在 `gui.py`**,其余 17 个模块引用数 = **0**。
  - **硬要求(用户已定)**:① **17 个逻辑模块零编辑**(若发现必须改 → 停下来报告);② **`gui.py` 的控制逻辑与数据处理「全部」剥离**,剥完后 UI 层只剩「造/读/写控件 + 绑事件」;③ **全量切换到 Flet,不保留 tkinter**(用户已备份 v1.6.0,出问题 `git` 回退);④ 界面**整洁美观**但**保留现有基本结构**(不做颠覆性改版)。
  - **架构**:新增 **`controller.py`**(编排:应用状态/后台任务/日志) + **`viewmodel.py`**(各界面的纯数据 Model) + **`ui_flet/`**(6 个渲染模块)。原则:**每个界面 = 一个可 headless 单测的纯数据 Model + 一层薄渲染** —— 两套 UI 共用同一 Model,行为天然一致;`viewmodel.py` 变大是**好事**,它是 tk→Flet 之间唯一的行为契约。剥离后 `gui.py` 1,925→1,186 行,且**只 import tkinter + controller + viewmodel**(不再直接碰任何逻辑模块)。
  - **收益**:**emoji 变彩色**(tk 只能单色,gui.py 那条妥协注释作废);**深色模式跟随系统**(tk 做不到);Material 3 观感 + 浮动标签表单(省掉一整列独立 Label);地图 marker 可用任意控件绘制 → **甩掉 `tkintermapview` + `Pillow` 两个第三方依赖**;结果卡链接改闭包绑定 → 干掉 tk 的动态 tag 簿记;拖拽排序用原生 `ReorderableListView`(不用手写命中测试)。
  - **代价**:exe 由约 15MB → **54MB**(内含 Flutter 预制客户端),冷启动 2.3s;**地图不能并排多开**(见 8.1)。
  - **线程模型(关键)**:Flet 的 `page.update()` **非线程安全** → 跨线程 marshal 是**强制**的,且收敛到 `shell.py` 一个模块:`run_bg` = `page.run_thread`、**`post` = `page.run_task(coro)`**(即 `root.after(0,…)` 的对应物)。stdout 桥一批一个 patch + 重入守卫。
  - **验证策略**:①**黄金基准(oracle)**——先用**未改的 tk 版**跑出 21 组行为快照(结果卡全文/跑道下拉/运行规则标注/SimBrief URL…),剥离后**逐字 diff 必须零差异**,这是唯一能把「剥离错了」和「新 UI 写错了」区分开的手段;②**零 GUI 冒烟**直测各 Model;③**Flet 控件树冒烟**(控件是纯 dataclass,可脱离窗口构造后遍历断言,**比 tkinter 冒烟更强**);④**真客户端自测钩子**(`DISPATCHER_SELFTEST`)抓「控件构造得出、但 Flutter 端渲染抛错」。
  - **进度**:8 个阶段全部完成 ✅ —— Phase 0 预研(Go) · 1 剥离(与 tk 基准零差异) · 2 骨架(结果卡逐字一致) · 3 地图 · 4 进离场面板 · 5 F21 弹窗 · 6 F23 编辑器 · 7 删 tkinter · 8 打包发布。每阶段均有 headless 冒烟 + 真客户端复验;设计与留底详见 `revisions.md` v2.0.0。
- **⏸️ 分时段规划(v1.4.0 尝试→删除→已由 F21 重做兑现)**:`routes.csv` 的 `Time Restriction`(`EOBT/ETA HHMM-HHMM`，**UTC**) = 分时段载体(全库 366 条)；SimBrief 不看时段会给固定航路。v1.4.0 计算层一度在 `routing.py` 实现完整(未接 GUI),但「按机型/高度精筛唯一航路」一层不可靠——`Aircraft`/`Altitude` 是自由文本「适用条件」、`JET↔PROP` 靠硬编码白名单必不完整、`FLxxx±` 单阈值正则把 `FL180-FL230` 区间方向解析反——故**整段删除**。完整删除代码见 `revisions.md` v1.4.0「🗑️ 已删除代码留底」。**F21 已按重做方向兑现**:时间自动筛、机型/高度按用户参考值判属并留用户自选(不凭脏列硬选)。
- **跨平台兼容(下一大轮次重点)**:提升脚本可用性、扩展到全平台(macOS / Linux 的路径定位与打包),并扩展 Volanta 令牌读取到**非 Chromium 浏览器(Firefox / Safari 等)**的本地存储格式(当前仅支持 Edge/Chrome/Brave 的 LevelDB,见 8.1);
- 支持非日本地区的航班规划;
- 基于 FR24 API 的航班查询(Volanta 优先未飞航线已部分满足"避免重复航线"的诉求)。

### 8.3 版本历史
- **v1.0.0 → v1.1.0**:
  - 新增自动读取 `.ini` 与 `.dat` 文件功能,也支持手动指定路径;
  - 新增 AIP 航路缓存机制,可离线读取 AIP 航路缓存 CSV。
- **v1.1.0 → v1.2.0**:
  - 新增 F11 Volanta 优先未飞航线:读取浏览器 IndexedDB 中的 Volanta 已飞记录,随机规划时软优先未飞的有向航线;
  - 启动时每日询问一次是否同步(打开浏览器 + 轮询缓存刷新),`volanta_config.txt` 记录同步日期;
  - 支持 `volanta_flights.csv` 官方导出兜底。
- **v1.2.0(续)**:版本号不变,持续迭代(代码级细节见 `revisions.md`):
  - **解耦 X-Plane**:F12 导航数据改为程序自带 `NavData` 文件夹(只飞 MSFS、没装 XP 的用户也可用)+ AIRAC 周期过期自检(过期提示 Navigraph 链接);F5 升级为多源地景检测:直接扫 XP `Custom Scenery`(apt.dat)+ MSFS `Community`(四步级联,含 `ContentHistory.json` 权威源),合并标注来源(XP/MSFS),`installed_scenery.json` 指纹缓存秒开,统一存模拟器目录记忆。
  - **地景假阳性修复**:用导航数据真实 ICAO 白名单 + 正则单词边界,剔除 `ROAD/ROCA` 等把普通单词误当 ICAO 的假阳性。
  - **代码拆包**:约 1100 行单文件拆为 `dispatcher/` 子包(一模块一职责),`flight_dispatcher.py` 退化为薄壳入口;运行/打包命令不变。
  - **模拟呼号(F4)按航线挑航司**:无真实排班时按地理大区选合理航司;航司/区域数据外置为可编辑的 `airlines.json`(首次运行自动生成、损坏回退内置默认)。
  - **随机抽线优先民用机场(F6)**:按军用端数量分层,优先两端民用,避免飞得勤的用户被推去可能不可飞的军用机场。
  - **Volanta(F11)演进**:修复同步「假成功」(读到真数据且稳定才算成功)→ 引入只增不减的累积库 → 发现 `/api/v1/Flights` 一次返回全部航班 → **程序用浏览器里的 Volanta 登录会话(localStorage 的 Orbx token)自动调 API 拉完整数据,零操作**;移除早期脆弱的「IndexedDB 正则扫描」整套;改为**「询问 + 偏好持久化」**(选 Y 才记住自动、未开启前不扫浏览器);最终把同步偏好/已飞累积库/拉取时间**浓缩进单一 `volanta_data.json`**(取代 `volanta_config.txt`/`volanta_flights.json`/`volanta_flown.json`,首次运行自动迁移并删除旧三文件)。
  - **打包健壮性**:`main()` 开头强制 UTF-8 输出,修复冻结/重定向时 emoji 触发的 GBK 编码崩溃。
- **v1.2.0 → v1.3.0**:
  - **GUI 化(F13)**:新增 `dispatcher/gui.py`(tkinter)作默认前端——表单输入 + 结果卡 + 日志区 + Volanta 同步控件;阻塞/联网全走后台线程,经 `after()` 回主线程;`sys.stdout/stderr` 重定向到日志区(解决 `--windowed` 下 stdout=None 让复用 `print()` 崩溃的问题)。CLI 保留,`--cli` 启动;入口按参数分发。打包改 `--onefile --windowed`。
  - **计算/渲染解耦**:新增 `dispatcher/planner.py`(`build_flight_plan`→`FlightPlan`),把 `print_flight_info` 的计算(FlightAware 抓取 + 模拟呼号)抽出,CLI/GUI 共享同一计算、各自渲染(CLI 输出逐字不变)。
  - **仅地景机场随机规划(F14)**:`get_random_route` 加 `require_both_scenery`,随机规划时可选「仅在两端都已装地景的机场间抽线」,与 Volanta 优先未飞叠加;未检测到地景目录时该选项失效/灰显。
- **v1.3.0 → v1.3.1**:
  - **移除命令行版(CLI/终端),GUI 成为唯一前端**:v1.3.0 GUI 实测稳定后,删除 `dispatcher/app.py`(CLI 主循环 + `print_flight_info`)与入口的 `--cli` 分支(入口直接 `run_gui()`),并清理仅服务于 CLI 交互流的辅助函数——`navdata.py` 的 `find_xp_data_files`(带阻塞 `input()` 的 XP 路径兜底,GUI 一直只用 `find_navdata_file`)、`volanta.py` 的 `prompt_sync_volanta` / `sync_volanta_via_browser`(Y/N 询问与 print 轮询,GUI 用按钮 + 自身可取消轮询替代)。计算/数据层(`planner`/`routing`/`volanta` 数据层/地景扫描)与 GUI 行为零变化;打包命令不变。
- **v1.4.x → v1.6.0**(要点;逐条见 §2.1 功能表与 §8.2):本地 A* 航路生成(F15,含 RNAV 偏好/单向航路方向学习/干线走廊加权/进离场端点学习)、SimBrief 一键派遣(F16)、机型库(F17)、航路交互地图(F18)、航路距离偏差(F19)、跑道+SID/STAR 选择+天气(F20)、多 AIP 按时段选(F21)、网格天气回退(F22)、机场运行规则编辑器(F23)、运行规则应用引擎(F24)。
- **v1.6.0 → v2.0.0(✅ 已发布)**:
  - **UI 迁移 tkinter → Flet(F25)**:界面换成 **Flutter 渲染**,业务逻辑零改动。**主版本号跳到 2.0.0**——不是因为功能变了,而是因为**前端整个换了**(tkinter 将被删除)、**exe 体积由约 15MB 涨到 54MB**(内含 Flutter 预制客户端)、并有一处**永久功能倒退**(地图不能并排多开)。
  - **架构重构(迁移的前置,也是本次最大的工程价值)**:`gui.py` 的**控制逻辑与数据处理全部剥离**到 `controller.py`(编排) + `viewmodel.py`(各界面纯数据 Model)。剥离后 `gui.py` 1,925→1,186 行、只剩渲染;**17 个逻辑模块零编辑**;这些逻辑**第一次**能脱离 GUI 单测。用**黄金基准(oracle)逐字 diff** 保证剥离不改行为。
  - **收益**:彩色 emoji、深色模式跟随系统、Material 3 观感;**甩掉 `tkintermapview` + `Pillow` 两个第三方依赖**(Flet 的地图 marker 可用任意控件绘制)。
