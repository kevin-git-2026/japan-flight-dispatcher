# 日本航班智能搜索与规划脚本 — 产品需求文档 (PRD)

| 项目 | 内容 |
| --- | --- |
| 产品名称 | 日本航班智能搜索与规划脚本 (Japan Flight Dispatcher) |
| 当前版本 | v1.3.1（已发布）；**v1.4.0 开发中**（本地航路生成 + SimBrief 集成 + 机型库 + 交互地图） |
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
| F13 | 图形界面(GUI) | v1.3.1 起**唯一前端**为 **tkinter 图形界面**(`dispatcher/gui.py`):表单输入 + 结果卡 + 日志区 + Volanta 同步控件;所有阻塞/联网走后台线程,经 `after()` 回主线程更新。GUI 为薄表现层,复用全部业务逻辑(`planner.build_flight_plan` 计算) |
| F14 | 仅地景机场随机规划 | 随机规划时可选「**仅在两端都已安装地景的机场之间生成航线**」,与「Volanta 优先未飞」加权叠加;未检测到地景目录时该选项灰显/跳过(`has_scenery` 软降级) |
| F15 | 本地航路生成（🚧 v1.4.0） | 无直连 AIP 航路时，用自带 `NavData`(`earth_awy/fix/nav` + `CIFP/`)本地 A* 寻路生成参考航路；含 CIFP SID/STAR 端点选择、双端 AIP 桥接、**沿 airway 加密**(距离精确 + 画图密)。详见 §8.2 |
| F16 | SimBrief 一键派遣（🚧 v1.4.0） | 结果卡生成 SimBrief custom-options 预填链接(orig/dest/type/airline/fltnum)，用用户**自己浏览器的 SimBrief 登录态**出专业 OFP；零凭据、可公开 |
| F17 | 机型库可搜索下拉（🚧 v1.4.0） | 机型从 `aircrafts.json`(SimBrief 212 机型，精简剥隐私)选；GUI 可搜索 Combobox，选中给 SimBrief `aircraft_id`，手输兜底 |
| F18 | 航路交互地图（🚧 v1.4.0） | 每条航路一个「🗺️ 地图」链接，弹独立窗口用 `tkintermapview` 把航路画在 OSM 真实地图上(可拖拽/缩放)，分时段多航路可分别开窗 |
| F19 | 航路距离 + 偏差（🚧 v1.4.0） | 每条 AIP / 生成航路显示沿 airway 累加的精确长度与「较大圆 +X%」偏差 |

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
| 点「同步 Volanta」 | 先用浏览器里已有的 Volanta 登录会话直接拉取(无需开窗);取不到有效令牌才打开 Edge(回退默认浏览器)跳转 **`fly.volanta.app/map`**(地图页,`/flights` 对未登录用户会卡加载)让用户**登录**,后台每 3 秒轮询直到拉到 `/api/v1/Flights` 数据(可随时点「取消同步」,最长 3 分钟) |
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

v1.2.0 起,原本约 1100 行的单文件 `flight_dispatcher.py` 已**按功能拆分为 `dispatcher/` 子包**(每个模块一个职责),便于维护;根目录的 `flight_dispatcher.py` 退化为**薄壳入口**。**v1.3.0 起**新增 `dispatcher/gui.py`(tkinter)与 `dispatcher/planner.py`(计算/渲染解耦);**v1.3.1 起 GUI 为唯一前端**,移除命令行版(`dispatcher/app.py` 与 `--cli`),入口直接 `run_gui()`。GUI 版打包命令为 `pyinstaller --onefile --windowed flight_dispatcher.py`。仍为纯标准库实现。

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
| `dispatcher/planner.py` (F13) | `build_flight_plan`→`FlightPlan`、`parse_runway_ft`/`parse_dist` | 一次规划的计算(FlightAware 抓取 + 模拟呼号),供 GUI 调用;渲染由 GUI 负责 |
| `dispatcher/gui.py` (F13) | `DispatcherGUI`、`run_gui` | **tkinter 图形界面**(唯一前端):后台线程初始化/规划/Volanta、stdout→日志重定向、渲染 `FlightPlan` |

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
- **浏览器同步(登录兜底)**:在「未开启自动、用户本次选 Y」且本机已有登录会话取不到有效令牌时触发;打开 **`fly.volanta.app/map`**(地图页;**不用 `/flights`——它对未登录用户会卡在加载**)让用户**登录**,随后**轮询 `try_fetch_volanta_json_via_session` 直到新令牌出现、API 拉取成功**(无需滚动,最长 3 分钟兜底)。Orbx 令牌有效约 14 天,期间直接用令牌调 API、不再开浏览器;过期后再次引导到 `/map` 登录拿新令牌。同步偏好持久化在 `volanta_data.json` 的 `preference`(`auto`)(详见 2.4)。
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

### 8.2 未来规划(摘自 README 更新日志)
- **本地 A* 航路生成(🚧 开发中,v1.4.0,F15):无 AIP 航路时自研寻路**——当 `jp-routes` 查不到直连 AIP 航路时,用程序自带 `NavData/`(`earth_awy/fix/nav` + **`CIFP/` 进离场程序**)在本地 A* 寻路生成参考航路。纯标准库、离线、无第三方。新增 `dispatcher/router.py`,`routing.py` 不耦合。
  - **背景**:曾评估 SimBrief `/v2/routes/generate`(逆向可用),但其 Navigraph 登录令牌**只在浏览器 JS 内存、磁盘抠不到** → 放弃,改完全自给。
  - **优先级链**:**Rule 0** 直连官方 AIP(app 层 `find_aip_route`,最高优先,不调 generate)→ **Rule 5** 借「dep→arr附近机场(≤100nm)」的官方 AIP + A* 补接(仅当 ① 干净·无锐角弯 ② ≤1.25×最优 ③ 不冲过头,三闸全过才借)→ **case 1–4** 在端点间 A*。
  - **端点选择(取自 CIFP，真实管制衔接)**:离场=SID 出口(section E)∪ **本场 VOR**(SID 枢纽);进场=STAR 入口 → 本场 VOR → IAF/IF(APPCH 描述码);全部**连通性过滤**(须在航路网上,剔除孤立进近 VOR);几何最近点作末位兜底。
  - **质量**:enroute 大锐角(接近掉头)转弯 → 标「请自行检查斟酌」;输出结果卡单列「🧭 生成航路(非官方 AIP、仅供参考)」。
  - **状态**:核心已实现自测过;**可调旋钮(K=1.25、overshoot=20nm、转弯阈100°、bbox 等)待用户实测后定**;dist 待重编。设计详见 `revisions.md` v1.4.0、规则见 `flight_planning.txt`。
  - **v1.4.0 续作(同期已实现)**:① Rule 5 升级**双端桥接**(dep 也可用附近机场替身，所借 AIP 头点 ≤50nm DCT 接)；② **航路加密**(Dijkstra 沿 airway 补中间 fix → 距离精确 + 画图密，RJFR→RJEC 11→21 点)；③ **SimBrief 一键派遣(F16)**(生成预填 URL，用用户自己 SimBrief 登录态出 OFP)；④ **机型库(F17)**(`aircrafts.json` 212 机型 + 可搜索下拉)；⑤ **航路交互地图(F18)**(`tkintermapview` 内嵌 OSM 地图，每条航路独立开窗)；⑥ **航路距离/偏差(F19)**。**首次引入第三方库** `tkintermapview`/`Pillow`(仅地图用，缺失自动禁用)。详见 `revisions.md` v1.4.0 续作。
- **⏸️ 分时段规划 + 向 SimBrief 提交航路(v1.4.0 已尝试 → 整段删除、延后)**:`routes.csv` 的 `Time Restriction`(`EOBT/ETA HHMM-HHMM`，**UTC**) = 分时段载体(全库 366 条)；SimBrief 不看时段会给固定航路。设计:加 GUI 开关「按真实运行时间与规则规划航路」，按起飞时间(**JST−9h=UTC**) + 可选高度/机型从多条 AIP 选适用航路 → 填进 SimBrief `route` 参数(补盲区) + 自己显示也用；夜间提示「因减噪可能更长/复杂」。`build_flight_plan(timed_route=)` / `_build_simbrief_url(route=)` 接口已就绪**并保留**。
  > **2026-06-27 延后**:计算层一度在 `routing.py` 实现完整(未接 GUI),但「按机型/高度精筛唯一航路」一层不可靠——`Aircraft` 列实为自由文本「适用条件」(混 `JET/DH8D/PROP` 与地理条件 `for AP west of 139E…`、机场条件 `only for RJCW` 等),`JET↔PROP` 靠硬编码白名单**必不完整**;`Altitude` 的 `FLxxx±` 单阈值正则会把 `FL180-FL230` 区间方向解析反——故**整段删除、整体延后**。完整删除代码 + 数据分析见 `revisions.md` v1.4.0「🗑️ 已删除代码留底」。**重做方向**:时间可解析、机型/高度留给用户自选(不程序自动选唯一)。
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
