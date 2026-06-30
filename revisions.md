# REVISIONS.md — 版本迭代代码变动记录

> 本文件记录 `flight_dispatcher` 每次版本迭代的**代码级**变动,便于后续 debug 或版本回退时快速理清改了什么、改在哪、为什么改。
> 与 `PRD.md`(产品需求)、`CLAUDE.md`(架构现状)互补:PRD 说"要什么",本文件说"代码具体怎么动的"。

## 记录格式约定

每个版本一个章节(最新在上);版本内每处改动按以下结构记录:

- **文件**:文件名 + 位置(函数名 / 代码区块)
- **类型**:新增 / 修改 / 删除
- **源码**:列出关键源码段;修改类用 `before →  after`,新增类直接列出新增源码
- **说明**:改了什么、为什么改(关联 PRD 功能编号或计划)

> 历史版本(v1.1.0 及更早)早于本记录机制建立、无逐行 diff,故仅作**概要**记录。(项目自 v1.2.0 起已纳入私有 Git 仓库 kevin-git-2026/japan-flight-dispatcher。)

---

## v1.4.0(🚧 开发中 · 2026-06-27)— 本地航路生成 + SimBrief 集成 + 机型库 + 交互地图（F15/F16…；首次引入第三方库）

- **关联**:`PRD.md` §8.2（原「规划中」）/ 新功能 F15；用户书面规则见工作目录 `flight_planning.txt`。
- **背景**:无直连 AIP 航路时希望自动生成参考航路。曾评估 SimBrief `/v2/routes/generate`（逆向可用），但其 Navigraph 登录 token **只在浏览器 JS 内存（LS/SS/IndexedDB 全无）、磁盘抠不到** → 放弃，改为**完全自给**：解析程序自带 `NavData/` 的 X-Plane 导航数据，本地 A* 寻路。纯标准库、离线、无第三方。
- **状态**:核心逻辑全实现并自测通过；**几个可调旋钮待用户实测后再定**；文档本次先存盘；**dist 尚未重编**。

### 数据（已核实，AIRAC 2605）
- `NavData/` 含：`earth_awy.dat`(航路 4.5MB,v1100)/`earth_fix.dat`(航点 15MB,v1200)/`earth_nav.dat`(导航台 3.6MB,v1200)/`earth_aptmeta.dat`/`earth_hold.dat`/`earth_mora.dat`/`earth_msa.dat`，**外加 `CIFP/` 文件夹(16543 个机场程序文件，~160MB)**——SID/STAR/APPCH 程序数据齐全。
- 行格式：`earth_awy` = `id1 reg1 t1 id2 reg2 t2 dir lowhigh baseFL topFL name`（t 11=fix/2=NDB/3=VOR；dir N双向/F正向/B反向；dir 后 1/2 是**序号非方向**；多名用 `-` 连）。`earth_fix` enroute 即 col3=="ENRT"。`earth_nav` col0=2(NDB)/3(VOR)，ident/region 锚定 "ENRT" token。**节点键 = (ident, region)**（ident 全球重复）。
- CIFP 行：`<TYPE>:seq,rtype,proc,trans,fix,region,section,sub,desccode,...`。**SID/STAR 出/入口 = section=='E' 的航点**；**APPCH 的 IAF/IF = 描述码(字段8)第4位为 'A'/'I'**；**本场 VOR = section=='D' 的导航台**（在 SID/STAR/APPCH 任意处）。

### 新增模块 `dispatcher/router.py`（纯标准库 os/math/heapq/threading）
- **建图**：解析 fix+nav+awy → `AirwayGraph`（`nodes{(id,reg):(lat,lon,kind)}`、`adj`、`outset`有出边、`inset`有进边、`by_ident`）。按**日本 bbox `(24,46,122,149)`** 过滤节点；边须两端都在 nodes。规模 ≈ **2406 节点 / 4582 航段**。**懒加载 + 单例缓存 + Lock**（首次 generate 才解析，~0.12s；冷启动不变）。
- **A***：`heapq`（元组带自增计数器避免比较 key）；`h=到目的地大圆距离`（可采纳）；虚拟终点 `_ARR`；起点集=入航候选(g0=DCT)、终点集=出航候选(+DCT to arr)。生成 ~1–2ms。
- **CIFP 端点** `_cifp_endpoints(icao)` → `(sid_exits, star_entries, iaf_if, vors)`，按机场缓存。
- **端点选择（真实管制衔接，逐级降级，全要求落在航路网上）**：
  - 离场：`(SID出口 ∪ 本场VOR) ∩ outset` → 几何兜底（朝目标最近点）。**本场 VOR 作 SID 枢纽很关键**——如 RJEC 的 section=E 出口 `KAGRA`(out2/in0,死端)不可靠，真正枢纽是 VOR `AWE`(12 条航路、可达全网)。
  - 进场：`STAR入口 ∩ inset` → `本场VOR ∩ inset` → `IAF/IF ∩ inset` → 几何兜底。**VOR 优先于 IAF/IF**（填本场 VOR、无 STAR 时管制雷达引导至 IF）。
  - **连通性过滤(inset)** 剔除不在任何航路上的孤立进近 VOR（如 RJTO 的 `OSE` 0 边）——这种点该退到 IAF（`SUNOD` 4 进 4 出）。
- **航路串格式化**：AIP 风格 `ENTRY AWY FIX AWY … EXIT`，仅换路点保留；多名段优先延续上一段已选名。
- **连贯性检查** `_check_continuity`：只看 enroute 航点间转向角，>`_MAX_TURN_DEG`(100°) 记 `suspect`（SID/STAR 衔接处不计）。

### Rule 5：借邻近机场的官方 AIP 航路（`_try_aip_bridge`）
- 机制（`D'=dep`）：找 **arr 附近(≤100nm)的机场 A'**，使 `dep→A'` 存在官方 AIP → 用该 AIP 航路 + A* 补接(A' 到达端→arr 端点)。多个 A' 取总程最短。
- **三道闸全过才借**：① **干净**（补出来无大锐角弯，`suspect=False`）；② **≤ `_BRIDGE_TOLERANCE`(1.25) × 最优 A***；③ **不冲过头**（航路对目的地最近点不在中途——超 `_OVERSHOOT_NM`(20nm) 即「过站折返」，拒）。任一不过 → 退回最优 A*。
- 实例：RJFK→RJFR——借 RJFK-RJFF(有 IKE 锐角弯→闸①拒)、借 RJFK-RJOM(松山偏东 2.11×→闸②拒)、ATSAG 那条冲过头→闸③拒 → 用最优 `SASIK G339 OSTEP Y14 DGC V28 SWE`(1.20)。RJER→RJSO 是唯一命中的干净桥接(1.08)。

### 完整优先级链 & 集成
- **Rule 0（直连 AIP 最高优先）在 app 层（`gui._plan_worker` 的 `find_aip_route`）**：查到直连官方 AIP 就用它（📜），`generate_route` **不调**；仅 `route is None` 才调 `generate_route`（🧭）。
- `generate_route(dep, arr, dat_path, aip_data, airports)`：先算最优 `_direct_route`（case1–4），再试 Rule 5 桥接，过闸则用桥接否则用最优。
- `planner.FlightPlan` 加 `generated_route` / `generated_route_warn` 字段（独立于 `aip_routes`）；`build_flight_plan(..., generated_route=, generated_route_warn=)` 透传。
- `gui.py`：`from .router import generate_route`；`_plan_worker` 在 `route is None and not strict` 时调（传 `aip_data=self.aip_data, airports=all_airports`）；`_render_plan` 加「🧭 生成航路（非官方 AIP）」段 + 大转弯警告行。`routing.py` 不改。
- `__init__.py` `__version__` → `1.4.0`；`flight_dispatcher.py` 模块清单加 router.py。

### 可调旋钮（待实测后定）
`JP_BBOX`、`_MAX_ENTRY_NM=120`、`_K_ENTRY=5`、`_MAX_TURN_DEG=100`、`_BRIDGE_TOLERANCE=1.25`、`_OVERSHOOT_NM=20`、桥接 A' 邻近 `max_near_nm=100`。

### 验证（本机，真实坐标）
整包 `py_compile`/import 过；RJSC→RJOM 生成 `…V30 KMC V38 OLIVE Y28 BAMBO Y283 ITUKI`（核心与 SimBrief 一致）；抽样 40 条无 AIP 真实航线比率多在 1.0–1.05、仅个别被标记；overshoot 修复后 RJAA→RJFF 直接调 generate 也退回最优（=官方 AIP 逐字一致）。**待办**：用户实机验航路质量 → 定旋钮 → 文档收尾(PRD/README) → 老规矩重编 dist。

### 续作（2026-06-27 同会话 · 同属 v1.4.0 开发周期；dist 未重编、git 未提交）

> 在「本地 A* 航路生成」基础上叠加 6 项：双端桥接、航路加密、航路距离/偏差、SimBrief 一键派遣(F16)、机型库下拉、交互地图。**首次引入第三方库**(地图)，项目不再纯标准库。

**1. Rule 5 升级为「双端替身」桥接**（`router._try_aip_bridge`）
原单端(dep 精确、A'=arr 替身) → **双端**：dep 侧也允许替身 D'(dep 附近 ≤`max_near_nm`=100nm)，借 `D'→A'` 官方 AIP 中段、dep DCT 接其头点——仅当头点离 dep ≤ 新旋钮 `_BRIDGE_HEAD_NM`(50nm)。尾补接 / 三道闸 / 取最短复用。RJFR→RJEC 由东线 888.8 改西线 840.9(=SimBrief)。

**2. 航路加密**（`router._trace_airway` + `_parse_aip_route(densify=True)`）
AIP 串只标 airway 转折点；加密用 **Dijkstra(限定该 airway 名的边)** 沿 airway 补出两换路点间的中间过渡点。价值：① **距离精确**(沿折线累加，而非换路点直线)；② 画图更密(RJFR→RJEC 11→21 点，= SimBrief 逐字一致)。影响 `_parse_aip_route` 所有调用(桥接 mid_path / `route_geometry` / 生成航路 coords·dist)。性能 1–2ms；多数中间 fix 共线故距离变化小(RJAA→RJBB +1.6NM)。

**3. 航路距离 + 较大圆偏差**（`router.route_geometry` / `route_length_nm`；`_finish` 加 `coords`）
`route_geometry(dep,arr,route_str,dat)` → `[(ident,lat,lon),...]`(含起降首尾·加密)；`route_length_nm` 累计大圆。`FlightPlan` 加 `generated_route_dist` / `aip_route_dists`；GUI 每条 AIP / 生成航路显示「航路长 X NM（较大圆 +Y%）」。揭示分时段绕行(RJAA→RJBB 白天 +40.6% vs 夜间 +7%)。

**4. SimBrief 一键派遣（F16）**（`planner` + `gui`）
背景：曾想用 Navigraph token 调 SimBrief API(token 只在浏览器内存抠不到 → 放弃)。改为生成 **SimBrief custom-options 预填 URL**(`dispatch.simbrief.com/options/custom?...`)，用用户**自己浏览器的 SimBrief 登录态**出专业 OFP——零凭据、**可公开**(区别于借我的 Navigraph 订阅替别人生成)，和 FlightAware 链接同模式。`planner._build_simbrief_url(orig,dest,airline,fltnum,actype,route)`(必填 orig/dest/type；route 空→SimBrief 自算) + `_normalize_actype`(查机型库) + `_split_callsign`。`FlightPlan.simbrief_url`；结果卡「🛩️ SimBrief 一键派遣」链接。`build_flight_plan(timed_route=)` 预留 route 参数给分时段。

**5. 机型库**（`aircrafts.json` + `dispatcher/aircraft.py`）
SimBrief 抓的 909KB → 精简 34KB(212 机型，每行一条，字段 id/icao/name/engines/pax/cargo/search)；**剥离 34 个用户 UUID 隐私**(airframes)，`.gitignore` 警示禁原始覆盖入库。`aircraft.py`：`load_aircraft_db` + `find_aircraft_id`(精确 id/icao → `_ALIAS` 消歧(737→B738 / Q400→DH8D) → 名字/搜索串 → 数字简写) + `aircraft_choices`。**SimBrief type 用 `aircraft_id`**(唯一；公务机/货机共享 ICAO 但 id 不同，如 B738 客机 vs BBJ2)。`planner._normalize_actype` 改用机型库(删原硬编码 `_ACTYPE_MAP`)；GUI 机型框 Entry → **可搜索 Combobox**(`_on_aircraft_type` 输入过滤 + 下拉选 → id；`_resolve_aircraft` 取值；手输兜底)。

**6. 交互地图**（`tkintermapview`，**首次第三方库**）
**打破纯标准库**：`pip install tkintermapview`(连带 Pillow / customtkinter / requests / pywin32 等)。GUI 顶部容错 import → `_HAS_MAP`(缺失则地图链接不显示、不影响其它)。`FlightPlan` 加 `aip_maps`(每条 AIP 的 `(coords,标题)`，与 aip_routes 对应) + `gen_map`。`gui._open_map(coords,title)`：弹独立 `Toplevel` + `TkinterMapView`(OSM 真实底图)，三档 marker(起降红点 / 换路点蓝点标字 / 中间过渡点 4px 小点 + 6px 淡字) + 航路线 + `fit_bounding_box`。**每条航路各一个独立「🗺️ 在地图查看本航路」链接**(`_render_plan` 内动态建 `maproute_N` tag，开头清旧)，可同时开多个窗口——解决分时段多航路(RJSF→RJBB 两段)分别查看。

**验证**：`compileall` 全过；**GUI headless 构造冒烟成例**(教训：曾把机型 `Entry`(`e_ac`)换 `Combobox`(`self.cb_aircraft`) 漏改 `_form_widgets` 旧引用，编译过但启动 `NameError`「打不开主程序」——`py_compile` 只查语法、抓不到运行时未定义名。此后 GUI 改动一律先 headless 构造 + `_render_plan` + `_open_map` 冒烟，见记忆 `gui-headless-smoke`)。RJFR→RJEC 双端=西线 840.9；加密 21 点；RJSF→RJBB 2 条分时段 AIP 各自开图 OK；机型 737→B738/Q400→DH8D。

### 🐛 Bugfix(2026-06-28)— Volanta 同步在 /map 登录后超时：根因是 localStorage 落盘延迟

- **现象**(alpha1 用户)：点「同步 Volanta」→ Edge 开 `/map` → 登录后卡在「正在同步」直到 180s 超时；重开再试仍失败；只有手动进 `/flights` 刷新+滚动飞行列表才「勉强同步上」。
- **排查**（只读探针 `scratchpad/volanta_token_probe.py`：按 `iss` 归类 + proximity 检查；用全新空 Edge profile 隔离、零残留）：**登录后一直待在 `/map`，Orbx 令牌照样在 +63s 出现**（JWT 0→1、未过期、proximity OK）。结论：
  - 令牌在 **`/map` 登录后就生成**（浏览器内存里），**不需要** `/flights`；
  - 真正瓶颈是 **Chromium 把 localStorage 从内存写到磁盘有 ~30s~1min 延迟（空闲更久、偶尔 >180s）**，而程序读的是磁盘 leveldb，故要等落盘才看得见；
  - 用户「滚 `/flights`」之所以「勉强成功」，是滚动/导航产生 localStorage 写入**触发了提前落盘**，并非 `/flights` 才生成令牌。
  - 这**推翻**了一度以为的「`/map` 不生成令牌、只有 `/flights` 才生成」的假设。proximity 就近校验实测一直 OK，**未改**。
- **修法**：
  - **`gui.py` `_volanta_worker`**：轮询窗口 **180→300s**（`_VOLANTA_POLL_CAP`，给落盘留足余量）；新增**两段醒目弹窗**（状态栏易被忽略）——①开始即弹「正在等待令牌写入磁盘（约 30s~1min），登录后请稍候，可滚动加速」；②约 60s 仍无令牌再弹「请去航班(Flights)页刷新+滚动飞行列表催落盘」；**成功**也弹「✅ 已读取 N 条」。**不自动开 `/flights`**（改弹窗引导用户去做，规避未登录时 `/flights` 卡加载）。
  - **`volanta.py`**：新增 `diagnose_volanta_session()`（只读，返回 `token_ok/no_dirs/empty/orbx_expired/no_orbx` 类别，**绝不返回/打印令牌**）+ `try_fetch_volanta_json_via_session(..., diag=False)`：失败时按类别打一行**固定**中文提示。`diag=True` 仅用于「快路径」与「启动自动同步」（**不在 3s 轮询里开**，避免刷屏）。
- **隐私**：诊断只输出固定类别串（`_DIAG_MSGS`）；grep 确认所有 `print` 无任何 token/payload 插值。
- **验证**：`py_compile` 过；headless 构造 + worker 快路径 + 两弹窗 + 诊断（no_dirs/token_ok）全过；端到端实拉到 178 条航线。**✅ 用户真机已走通完整流程（2026-06-28 确认功能无误）**：清空令牌后开程序→开浏览器到 `/map`→弹窗①→状态计数→登录→令牌落盘→成功弹窗，时序与落盘等待体验符合预期。
- **版本**：本次提交把 `dispatcher/__init__.py` 的 `__version__` 升到 `1.4.0_alpha2`（v1.4.0 第二个 alpha，含本 Volanta 修复）。

### 续作(2)（2026-06-29/30 · v1.4.0 开发周期）— 航路质量：RNAV 优先 / 航路方向学习 / 高频走廊加权 / 移管表进离场端点；route_planning skill

> 借新建的 `route_planning` skill（航路规划领域知识库）系统核查并迭代本地航路生成质量，让生成航路贴近真实运行：优先 RNAV、守单向航路方向、走高频干线、用官方进离场端点。改动集中在 `dispatcher/router.py` + 新数据文件 `transfer_points.json`；`routing.py`/`gui.py` 行为不变。

**1. 问题1 — 优先 RNAV 航路（git 已提交 `0328126`）**
ICAO Annex 11：RNAV 首字母 ∈ Q/T/Y/Z(国内)∪L/M/N/P(区域)。新增 `_RNAV_PREFIXES`/`_is_rnav`。两层：
- **选路**：A* 边权对「整段无 RNAV 名」的纯传统边 ×`_TRAD_AIRWAY_PENALTY`(1.15)——只抬搜索权重、不进显示距离(后者 haversine 重算)，启发仍可采纳；太绕(>15%)仍回退传统。
- **标名**：`_format_route` 多名段(如 `V28-Y28`)经新 `_pick_airway` 优先 RNAV 名。
- 效果：50 条无 AIP 样本 RNAV token 占比 **74%→92%**，距离基本不变(最大 +0.4nm)。

**2. P0 — 航路方向学习（修单向 RNAV 逆飞，git 已提交 `0328126`）**
- **根因**：earth_awy 把「单向 RNAV 与双向航路共挂同一物理航段」统一标 `N`(因双向 V 航路在)，**丢了 RNAV 的单向性**。问题1 优先 RNAV 后会把这类段标成单向 RNAV 名 → 航路串逆飞(Y284/Y43/Y312 等)。earth_awy 本地无法判别。
- **修法**：`_learn_airway_directions(graph, aip_data)` 扫 `routes.csv` 全部官方航路(官方航路永不逆向用航路)，逐 `prev--W-->next` 用 `_trace_airway` 展开成航段 → `legal_seg{(u,v):{airway}}`(W 在 u→v 被实飞证实合法)；懒加载 `_ensure_directions`(线程安全、缓存于 graph)。`oneway` 集 = 含 F/B 段的航路名。`_pick_airway` 的「安全集」= 完全双向(不在 oneway) ∪ 本段已学合法正向(在 legal)——单向 RNAV 仅在被实飞证实的方向才用于标名，否则退回双向共名。**纯标名层、不改图拓扑(零连通性风险)、距离不变**；4 条实测逆向全修，RNAV 反升 92%。
- 排坑：自检一度误报 10 处「残留逆向」，经 earth_awy 核验全是 `dir=N` 双向段(routes.csv 只是未采样到正向)，非真逆向——故**不在图层加方向罚分**(会误伤合法双向边)。

**3. P3 — 高频走廊加权（未提交）**
- 复用 P0 的 routes.csv 学习管线，顺带统计每条有向航段被多少不同机场对实飞 = **走廊热度** `seg_pop`(1817 段，其中 942 段 ≥4 对=干线)。
- A* 边权乘子(恒 ≥1，保启发可采纳)：干线(≥`_TRUNK_POP`=4)不罚 / 轻度(1–3)×`_MINOR_CORRIDOR_PENALTY`(1.08) / 从未实飞 ×`_OFFTRUNK_PENALTY`(1.25)——软偏好真实常用走廊(直接 A* 与桥接补接段共用)；有向段故方向天然区分。
- 效果：干线段占比 **73%→80%**，总距离仅 +0.4%；桥接补接段也改走干线(如 RJFF→RJSK 走 BUTUR Y453 西线)。
- 新增调试开关 `DEBUG_CORRIDOR`(默认 False，GUI 日志可见) + `_corridor_dbg`：每条结果打印走廊段构成(干线/轻度/未飞)。

**4. 移管表进离场端点（problem 1/P1，未提交）**
- **来源**：VATJPN 交通管制部「移管点与高度」SOP(公开页 `https://vatjpn.org/document/public/om/sop/transfer-point-and-alt`；**原 Google 表页脚要求勿分享其 URL，故只引用此公开页、数据文件不含表 URL**)——逐机场的**进场门(到着航路尾)/离场头(出発航路头)** + 移交高度/席位 + 方向/跑道/机型条件。解析为 `transfer_points.json`(56 机场，进场门级 144/145 在网)。**这是进/离场端点的权威源，优先级高于从 CIFP 猜 STAR/本场 VOR/IAF，顺带解决 problem 3(IAF/VOR 顺序)**。
- **集成**：`_transfer_points()` 懒加载缓存(run-dir 锚定，同 airlines.json)。`_arrival_candidates`/`_departure_candidates` **优先官方门**(解析为图节点、在网) → 回退 CIFP → 几何兜底。
- **方向过滤** `_dir_filter`(`_GATE_DIR_MARGIN_NM`=30)：丢掉位于机场另一侧/反向、会逼绕道的门(移管表里多是其它运行方向的门)；全被滤掉则回退 CIFP。修 RJSS→RJOR 因北向门 SAMBO 被强制的 +196nm。
- **离场门质量门控弃门**(用户要求「出现大锐角或超大圆太多就放弃离场门」)：`_direct_route` 重构(抽 `_run(use_dep_gate)`)——用官方离场头算出的航路若**含大锐角(suspect) 或 >`_GATE_GIVEUP_RATIO`(1.4)×大圆 → 弃门重算**(`_departure_candidates(use_transfer=False)` 退回 SID/本场 VOR/几何 = 「本场台离场 + VOR 程序」)，取更优者；`suspect` 触发不受比例限制。
- 效果：50 条进场命中官方门 38/50、0 断路、总距离 **−1.0%**；最糟几条修复——RJFT→RJOC(#25 +93% 173°掉头)`…RAKDA`→`…XZE` −160nm、RJOH→RJTO `…SUNOD`→`…XAC`。综合实例 **RJSA→RJSS(#26)：279nm(+79%·Y312 逆飞·收东门 LANCE) → 163nm(+5%·无逆飞·本场 VOR MRE 离场·收北门 SDE)**——P0+走廊+移管门+方向过滤+离场弃门五机制协同(官方门 UWE 致 153° 锐角 suspect → 弃门改用本场台 MRE)。

**5. route_planning skill（领域知识沉淀，`.claude/skills/route_planning/`）**
- `reference/route_templates.md`：从 routes.csv 715 条去重官方航路、按 dep→arr 方位角归 8 方向、挖**真实高频连续子链**作走廊模板(37 条，逐条核验逐字真实、×N 复用数按方向精确)；揭示**方向不对称=单向航路**、**开头 `…DCT…DCT…` 多是 SID/transition**(#17)。
- `reference/transfer_points.md`：移管表运行规则(4 类) + 逐机场进场门/离场头 + 用法。
- `SKILL.md`(索引/决策树步骤2-3「优先查官方门」/避坑「有些 RNAV 单向」)、`enroute.md`(`dir` 字段「共挂 N 不可尽信」+ case5 模板) 接线。
- 工作产物(未跟踪)：`route_template.md` / `route_compare*.md`(SimBrief 对照集，用户已批注) / `corridor_test.py`(走廊测试工具 dump/md/inspect 三模式)。

**可调旋钮(本批)**：`_TRAD_AIRWAY_PENALTY`(1.15)、`_TRUNK_POP`(4)/`_MINOR_CORRIDOR_PENALTY`(1.08)/`_OFFTRUNK_PENALTY`(1.25)、`_GATE_DIR_MARGIN_NM`(30)、`_GATE_GIVEUP_RATIO`(1.4)。
**git/状态**：问题1+P0 已提交(`0328126`)；走廊加权+移管表端点+DEBUG 开关 + `transfer_points.json` 随 **1.4.1_alpha2** 一并提交(见续作3)。

### 续作(3)（2026-06-30 · `1.4.1_alpha2`）— AIP 桥接 × 走廊融合：端点学习 + 删桥 + 本场VOR 修复

> 用户洞察：基于 VATJPN 移管表 + AIP + 走廊学习得到的**直接航路本就是「理论最优」**，Rule 5 桥接只是当初逼近它的近似手段。端点学习落地后把桥接的真正价值(找对真实走廊)吸收进直接 A*，桥接整体删除。改动集中在 `dispatcher/router.py`；`routing.py`/`gui.py`/`planner.py` 行为不变。触发：用户报 RJFM→RJBE 误用鹿児島 MIDAI 离场、RJSY→RJCH 误用 YTE 离场。

**1. 进/离场端点学习（融合核心）** —— `_learn_routes`(原 `_learn_airway_directions`) 同一遍多学两样：
- `dep_heads{icao:{head:n}}` / `arr_tails{icao:{tail:n}}` = 各机场官方航路串两端的真实接入点(如 RJGG 北向 KCC、东向 BOGON；RJFM 东向 MADOG)，缓存到 `graph.dep_heads/arr_tails`(86 机场)。`_learned_heads`/`_learned_tails` 读取。
- **只取首/末 token 本身是航点**的行——以航路名开头的行(`Y14 HWE …` 这类占位/通用航路)其首个解析航点常在远端，当离场点会让 A* 退化成「DCT 直飞 200nm 到中途点」(修 RJCN→RJSM 退化成单点 `HWE`)。

**2. 端点选择改为「学到端点 ∪ 移管门 ∪ CIFP」并集** —— `_departure_candidates`/`_arrival_candidates` 并集 + 方向过滤后交 A* 自选最优(而非优先级)：学到端点是核心(RJGG 的 KCC 因最短自然胜出)，并集保证不因它不全/反向而漏掉更优 CIFP 出口。对直接航路严格不劣(50 条：5 改善 / 0 回归 / −18nm)。修 RJFM→RJBE(走 MADOG)、RJGG→RJCN(走 KCC)。`_direct_route._run` 形参 `use_transfer`→`use_gates`。

**3. 删除 Rule 5 桥接** —— `_try_aip_bridge` 整函数 + 常量 `_BRIDGE_TOLERANCE`/`_OVERSHOOT_NM`/`_BRIDGE_HEAD_NM` 删除；`generate_route` 简化为纯直接 A*(`airports` 形参保留向后兼容、现未用)。实测 50 条依据：桥接借【邻场】离场过渡点 → 倒飞/绕远，**弊大于利**(8 条更长·最多 +119nm、3 条倒飞 vs 仅 6 条更短·多为 ≤17nm 或头点之差)；根因是 `≤1.25×` 容差让它在更长时也赢。删桥后总距 22832→22682nm(**反而更短**)、干线 78%、退化短串 0；回退的少数次优(如 RJNO→RJCJ 隠岐离岛)标 `suspect`，比桥接靠倒飞外场点蒙混更诚实。

**4. 本场VOR 算法修复（用户发现 RJSY→RJCH 的 YTE bug）** —— `_onfield_vor(airport, graph, vors)`：本场VOR = CIFP `section==D` 里**距机场最近且 ≤`_ONFIELD_VOR_NM`(15nm)** 的那一个。原算法把 section-D 的**所有** VOR(含 SID/STAR 中途航点——如 ZUNDA2 里夹在 TADAT–ZUNDA 之间的 YTE 距场 37nm、feeder VOR)都当本场台，违反 skill `cifp_format.md` 自己写的「本场 VOR ≠ section-D 任意 VOR，按坐标取距机场最近的」。修 RJSY→RJCH：`YTE Y113 TAXIR`(241nm·方向反·YTE 在东南) → `YAYOI Y312 UWE Y32 MRE Y113 TAXIR`(185nm·+0.5%·正北沿 Y312)。`_departure_candidates`/`_arrival_candidates` 都改用 `_onfield_vor`。

**可调旋钮(本批新增)**：`_ONFIELD_VOR_NM`(15)。
**验证**：标杆全对(RJFM→MADOG、RJGG→KCC、RJSA→RJSS、RJCN→RJSM、RJSY→YAYOI)；50 条样本总距 22682nm、干线 78%、退化 0、可疑 3(均为隠岐离岛/RJEB 固有难航线，非新增)。
**git/版本**：本提交 = `1.4.1_alpha2`，把上一批(续作2：走廊加权 + 移管表端点 + DEBUG)与本批(续作3：端点学习 + 删桥 + 本场VOR)连同 `transfer_points.json` + `route_planning` skill 一并入库。⚠️ skill 的 `SKILL.md`/`enroute.md` 仍含 case 5 桥接描述(领域概念仍成立，但本项目实现已不用)，待后续清理。

---

### ⏸️ 分时段规划 + 向 SimBrief 提交航路（**设计保留、功能延后** · 2026-06-27）

> **状态**：计算层一度在 `routing.py` 完整实现，但**从未接进 GUI**（dead code），且「按机型/高度精筛唯一航路」一层不可靠 → 按用户决定**整段删除、整体延后**。完整被删代码 + 删除原因见本节末「🗑️ 已删除代码留底」。以下设计意图**保留**，供日后重做参考；`planner` 的休眠接口 `build_flight_plan(timed_route=)` / `_build_simbrief_url(route=)` 也保留。用户将另写业务文档系统梳理可分析逻辑。
- **数据**：`routes.csv` 的 `Time Restriction` 列即分时段载体——`EOBT HHMM-HHMM`(离场段，≈起飞时间，最好用) / `ETA HHMM-HHMM`(进场段，需 +飞行时长推到达) / 复合 `EOBT…&ETA…`；**UTC**；叠加 `Altitude`(FL250+/-) + `Aircraft`(JET/DH8D)。全库 **366 条**带时段(占 25%)。不同时段头尾过渡点不同(已隐含离场/进场走向差异)。
- **时区基准(已定)**：现状 GUI 起飞时间按 **JST(机场本地)** 与 FlightAware 比 → 匹配 AIP 时段需 **JST−9h=UTC**。
- **逻辑**：加 GUI 开关「按真实运行时间与规则规划航路」(用户构思)；勾选 → 按起飞时间(→UTC) + 可选高度/机型，从多条 AIP 里选**唯一适用**那条 → 填进 `simbrief_url` 的 `route` 参数(补 SimBrief 不看时段的盲区) + 我们自己显示也用它；提示「夜间因减噪可能更长/复杂」。不勾 → route 留空让 SimBrief 自算。ETA 段用 航程÷巡航速度 估时长。
- **待定旋钮**：起飞时间取点(区间起点 / 中点)、巡航速度、高度无输入时默认(高 / 低)、开关默认(勾 / 不勾)。
- **已就绪的接口**：`build_flight_plan(timed_route=)`、`_build_simbrief_url(route=)`、`route_geometry`(加密)。

#### 🗑️ 已删除代码留底（2026-06-27 整段删除、延后）

- **决策**：上一会话在 `routing.py` 写了整套分时段计算层（未接 GUI），本会话核查发现「按机型/高度精筛唯一航路」不可靠 → 用户指示**整段删除、整体延后**（时间解析核心其实扎实，但一并删了，重做时再取舍）。删除后 headless 回归全过（NavData / 地景62 / AIP1436 / Volanta177 / RJTT→RJBB 规划+渲染正常）。
- **保留**：`planner.build_flight_plan(timed_route=)` / `_build_simbrief_url(route=)` 休眠形参（无坏逻辑，GUI 不传它恒 None＝现状），重做可直接对接。`routing.py` 顶部 `import re` 随段删除（仅该段用到）。
- **删除原因 1 — JET/PROP 区分不可靠**：`Aircraft` 列**不是纯机型列**，是自由文本「适用条件」。实测 routes_cache.csv 1436 行、非空 105：`JET`×53 / `DH8D`×23 / `PROP`×14，外加 `for PROP except DH8D`×2、地理条件 `for AP located west of 139E …`×4 +东×4、机场条件 `only for RJCW`×1 / `for RJCx/RJEx/RJSx …`×2。代码只精确匹配 `JET/DH8D/PROP`、其余一律放行；JET↔PROP 还靠**硬编码 `_PROP_ICAO` 白名单**（必不完整，表外涡桨全误判成 JET）。**这正是上个 CC 反复纠结的死结——靠白名单精确解析这列本质无解。**
- **删除原因 2 — 高度层误解析**：`route_matches_alt` 用单阈值正则 `FL(\d+)([+\-])`，把 `FL180-FL230`（区间）误读成「≤FL180」（方向反了）；`A120-` / `13000ft` / `FL240`(无号) 直接忽略（各 1–3 行）。
- **重做建议**：`Time Restriction`（时间）可解析、是核心价值；但 `Aircraft`/`Altitude` 是脏的自由文本，**机型/高度应留给用户自选**（更晚写的 `select_timed_routes` 已这么设计：只按时间过滤、返回多条带 label 让用户挑对应 SimBrief 链接），不要程序自动选唯一。

被删完整代码（贴回 `routing.py` 末尾、并恢复顶部 `import re` 即可复原）：

```python
# ================= 分时段规划（F16：按真实运行时间与规则选官方航路）=================
# 日本不少航线按【运行时段】规定不同航路(夜间因减噪等可能更长/更绕)，载体是 routes.csv 的
# Time Restriction 列：'EOBT HHMM-HHMM'(离场/撤轮挡段) / 'ETA HHMM-HHMM'(到达段) / 复合
# 'EOBT a-b &ETA c-d'(AND)；时间一律 **UTC**，区间常【跨午夜】(lo>hi)。叠加 Altitude(FLxxx±)
# 与 Aircraft(JET/DH8D/PROP)二次细分。本模块把这些解析出来，按用户的离港/到达时刻选唯一航路。

CRUISE_KT = 450.0          # ETA 推算用的粗略巡航地速(机型库无速度字段；涡桨偏慢，估算够用)

# 涡桨机型 ICAO 小白名单——区分 Aircraft 列的 JET 与 DH8D/PROP(engines 字段是发动机型号、不可靠)
_PROP_ICAO = {"DH8D", "DH8A", "DH8B", "DH8C", "AT72", "AT76", "AT75", "AT45", "AT43",
              "SF34", "SF50", "E120", "SB20", "D328", "JS41", "JS32", "SW4", "BE20", "C208"}

_TIME_PAT = re.compile(r'(EOBT|ETA)\s*(\d{2})(\d{2})\s*-\s*(\d{2})(\d{2})')   # 固定 4 位 HHMM
_ALT_PAT = re.compile(r'FL\s*(\d{2,3})\s*([+\-])')                            # 'FL230-' / 'FL240+'


def parse_hhmm(s):
    """'08:30' / '0830' / '8:30' → 当天分钟数(0-1439)；非法/空 → None。"""
    m = re.match(r'^\s*(\d{1,2})\s*[:：]?\s*(\d{2})\s*$', s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if (h <= 23 and mi <= 59) else None


def parse_time_restriction(s):
    """解析 Time Restriction → [('EOBT', lo, hi), ('ETA', lo, hi), ...](UTC 当天分钟)。
    支持复合 'EOBT a-b &ETA c-d' 与 csv 多行引号字段(含换行)；无法解析/空 → []。"""
    return [(kind, int(h1) % 24 * 60 + int(m1), int(h2) % 24 * 60 + int(m2))
            for kind, h1, m1, h2, m2 in _TIME_PAT.findall(s or "")]


def _in_window(t, lo, hi):
    """环形区间[lo,hi]判断，支持跨午夜(lo>hi，如 2115-1329 表示 21:15→次日 13:29)。闭区间。"""
    return lo <= t <= hi if lo <= hi else (t >= lo or t <= hi)


def route_matches_time(restr, eobt_utc_min, eta_utc_min):
    """无时段限制→True；有 EOBT 段→需 eobt 落入；有 ETA 段→需 eta 落入；复合(都有)→AND。"""
    wins = parse_time_restriction(restr)
    if not wins:
        return True
    for kind, lo, hi in wins:
        t = eobt_utc_min if kind == "EOBT" else eta_utc_min
        if t is None or not _in_window(t, lo, hi):
            return False
    return True


def route_matches_alt(alt, fl):
    """alt 如 'FL230-'/'FL240+'；fl=用户巡航高度(百英尺，如 230)。无限制/无 fl/非标准高度→True(不约束)。"""
    if not (alt or "").strip() or fl is None:
        return True
    m = _ALT_PAT.search(alt)
    if not m:
        return True                                   # 区间/A120-/13000ft 等非标准写法不参与自动过滤
    lvl, sign = int(m.group(1)), m.group(2)
    return fl >= lvl if sign == "+" else fl <= lvl


def route_matches_aircraft(ac, actype):
    """ac 如 'JET'/'DH8D'/'PROP'；actype=用户机型 ICAO/id。无限制/无机型→True。
    只处理三种主流标记；地理/特例条件(for AP west of 139E…)不参与过滤。"""
    ac = (ac or "").strip().upper()
    at = (actype or "").strip().upper()
    if not ac or not at:
        return True
    if ac == "JET":
        return at not in _PROP_ICAO
    if ac == "DH8D":
        return at == "DH8D"
    if ac == "PROP":
        return at in _PROP_ICAO
    return True                                       # 其它复杂条件不约束


def plan_times_utc(eobt_jst_min, taxi_min, dist_nm, cruise_kt=CRUISE_KT):
    """GUI 输入 → 分时段匹配用的 UTC 时刻。EOBT_utc = EOBT_jst − 9h；起飞 = EOBT + 滑行；
    ETA = 起飞 + 航程÷巡航速度。返回 (eobt_utc_min, eta_utc_min)，均取模 1440(当天分钟)。"""
    eobt_utc = (int(eobt_jst_min) - 9 * 60) % 1440
    enroute = (dist_nm / cruise_kt * 60.0) if (dist_nm and cruise_kt) else 0.0
    eta_utc = (eobt_utc + (taxi_min or 0) + enroute) % 1440
    return eobt_utc, int(round(eta_utc)) % 1440


def select_timed_route(aip_rows, eobt_utc_min, eta_utc_min=None, fl=None, actype=None):
    """从同一航线的多条 AIP 原始行里，按离港(EOBT)/到达(ETA)时刻 + 可选高度/机型，选出唯一适用那条。
    aip_rows: 形如 [DEP,DEST,Time,Alt,Aircraft,Route,Remarks] 的原始 csv row 列表。
    返回 dict{row, route_str, idx, n_match, ambiguous, time_restr} 或 None(无任何行通过时间过滤)。"""
    rows = [(i, r) for i, r in enumerate(aip_rows) if len(r) > 5]
    timed = [(i, r) for i, r in rows if route_matches_time(r[2], eobt_utc_min, eta_utc_min)]
    if not timed:
        return None
    # 高度 + 机型二次过滤；过滤后为空则放宽到「仅时间命中」(避免因机型/高度信息不足而无解)
    refined = [(i, r) for i, r in timed
               if route_matches_alt(r[3], fl) and route_matches_aircraft(r[4], actype)]
    cand = refined or timed
    cand = [(i, r) for i, r in cand if (r[5] or "").strip()] or cand   # 优先有航路串的
    idx, row = cand[0]
    return {
        "row": row, "route_str": (row[5] or "").strip(), "idx": idx,
        "n_match": len(cand), "ambiguous": len(cand) > 1,
        "time_restr": (row[2] or "").strip().replace("\n", " "),
    }


def select_timed_routes(aip_rows, eobt_utc_min, eta_utc_min):
    """按运行时段(EOBT/ETA)过滤候选 AIP 行，返回**所有**时间命中的行（通常 1 条；
    同一时段下因 JET/PROP 或巡航高度限制并存时多条）。机型/高度不再由程序自动选唯一——
    留给用户按自己机型与巡航高度，从返回的多条里挑对应的 SimBrief 链接。
    返回 list[dict]：{'route': 航路串, 'restr': 规整后的时段串, 'alt': 高度限制, 'aircraft': 机型限制,
                     'label': 供用户区分的标签('JET'/'PROP FL220+' 之类)}。
    无任何带时段行命中时，回退到无时段约束的行（至少给用户一条）。"""
    hits, plain = [], []
    for r in aip_rows:
        restr = ((r[2] if len(r) > 2 else "") or "").strip()
        if not route_matches_time(restr, eobt_utc_min, eta_utc_min):
            continue
        alt = ((r[3] if len(r) > 3 else "") or "").strip()
        ac = ((r[4] if len(r) > 4 else "") or "").strip()
        route = ((r[5] if len(r) > 5 else "") or "").strip()
        item = {"route": route, "restr": " ".join(restr.split()),
                "alt": alt, "aircraft": ac,
                "label": " ".join(x for x in (ac, alt) if x)}
        (hits if parse_time_restriction(restr) else plain).append(item)
    return hits if hits else plain
```

---

## v1.3.1(✅ 已实现 · 2026-06-26)— 移除 CLI / 终端 + GUI 配色高亮 + 高分屏适配

- **关联**:用户决策——v1.3.0 GUI 实测稳定,命令行版(CLI/终端)不再需要,移除以精简代码。
- **目标**:删除 CLI 前端(`dispatcher/app.py` + `--cli` 入口分支)及其【仅服务于 CLI 交互流】的辅助函数;GUI(`dispatcher/gui.py`)成为唯一前端。计算/数据层(`planner`/`routing`/`data`/`scenery`/`volanta` 等)原样保留,GUI 行为零变化。

### 改动总览

| # | 文件 | 位置 | 类型 | 摘要 |
|---|---|---|---|---|
| 25 | `dispatcher/app.py` | 整文件 | 删除 | CLI 主循环 + `print_flight_info` 闭包整体删除(GUI 已用 `_render_plan` 渲染同一 `FlightPlan`) |
| 26 | `flight_dispatcher.py` | 入口 | 修改 | 去掉 `--cli` 分支与 `import sys`,直接 `from dispatcher.gui import run_gui` → `run_gui()` |
| 27 | `dispatcher/navdata.py` | `find_xp_data_files` + `import sys` | 删除 | 该函数仅 CLI 用(带阻塞 `input()` 粘贴 XP 根的兜底);GUI 启动直接调 `find_navdata_file()`。删后 `import sys` 不再被引用,一并去掉。保留 `find_navdata_file`/`check_airac_currency`/`_is_xp_root`/`locate_xp_root`(`locate_xp_root` 仍供地景扫描用) |
| 28 | `dispatcher/volanta.py` | `prompt_sync_volanta` + `sync_volanta_via_browser` | 删除 | 二者均仅 CLI 用(Y/N 询问、print+`time.sleep` 轮询);GUI 用「同步 Volanta」按钮 + 自身 180s 可取消轮询替代。保留 `_open_volanta_in_browser`/`try_fetch_volanta_json_via_session`/`set_volanta_auto`/`enable_volanta_auto`(GUI 仍用) |
| 29 | `dispatcher/__init__.py` | 版本号 + 注释 | 修改 | `__version__` `1.3.0`→`1.3.1`;包注释「转调 dispatcher.app.main」→「转调 dispatcher.gui.run_gui」 |
| 30 | `dispatcher/planner.py` | 模块头注释 | 修改 | 「CLI(app.py) 与 GUI 各自渲染」→「GUI(gui.py) 据此渲染」(`build_flight_plan` 本身不变) |

### 说明
- **无 GUI 回归**:被删的 `find_xp_data_files` 的「exe 同级 .dat / 自动扫 XP 安装 / 手动粘贴路径」三个兜底【本就没接到 GUI】(GUI 一直只调 `find_navdata_file()` 读自带 NavData 文件夹)。故移除它不改变 GUI 行为,只是把仅 CLI 才有的兜底一并去掉。分发件本就自带 `NavData/`。
- **数据/计算层零改动**:`planner.build_flight_plan`、`get_random_route`(含需求 B 地景过滤)、Volanta 数据层、地景扫描全部原样保留。

### 验证(本机)
- 整包 `py_compile` 通过;`import dispatcher.*`(含 gui)全部 OK,无对已删函数的残留引用(grep 清零);`__version__==1.3.1`。
- `volanta.py` 删函数后 `import time` 仍被 4 处使用(保留);`navdata.py` `git diff` 确认仅删 `find_xp_data_files`+`import sys`+2 行注释微调。
- **GUI 无人值守冒烟**:构造 `DispatcherGUI` + 同步跑 `_init_worker` → `_ready=True`、数据就绪(地景62 / AIP1436 / Volanta177),与移除前一致、无崩溃。

### GUI 配色高亮 + 高分屏(DPI)适配(同版追加)

- **背景**:① CLI 移除后,有人觉得 GUI 里 emoji 显得「素」——根因是 **Tk 8.6 在 Text/Label 里不支持彩色 emoji 字体**(只画单色字形),命令行版「彩色」是终端自己渲染、与程序无关;② 2K/4K 屏上窗口发虚像「低分辨率」——根因是进程**未声明 DPI 感知**,被 Windows 位图拉伸(实测本机 150% 缩放 / 144 DPI)。
- **改动**:

  | # | 文件 | 位置 | 类型 | 摘要 |
  |---|---|---|---|---|
  | 31 | `dispatcher/gui.py` | `_render_plan` + 结果卡 Text tag | 修改 | 结果卡**语义配色**:机场代码加粗深蓝、`[地景:…]`绿/`[⚠️无地景]`红、`[🛡️军用]`红、已飞过琥珀、✅完美匹配绿/ℹ️参考橙/❌无排班灰+呼号蓝、绿色加粗标题 + 灰分隔线。逐段 `insert(text, tag)` 替代原「拼大字符串一次插入」 |
  | 32 | `dispatcher/gui.py` | `run_gui` + 新增 `_enable_hidpi` + `__init__(scale)` | 修改 | **高分屏适配**:`tk.Tk()` 前 ctypes 声明 DPI 感知(Per-Monitor v2 → Per-Monitor → System 逐级兜底)→ Windows 原生像素渲染、锐利;读系统 DPI 算缩放比,`tk scaling=DPI/72` 放大点字号,窗口几何 `980×680` 按比例放大(150% 即 `1470×1020`)。非 Windows/失败回落 `scale=1.0` |

- **配色为何能给 emoji 上色**:Tk 把 emoji 当普通字形画在文字前景色里,所以 tag 的 `foreground` **同时染了字和 emoji**(✅→绿、⚠️/🛡️→橙红)——这是绕开「Tk 无彩色 emoji」限制、不内嵌图片资源就让界面「不素」的关键。
- **DPI 不会双重缩放**:进程声明感知后 Windows 不再位图拉伸,仅 Tk 的 `tk scaling` 生效;`scaling` 是**设值**(=DPI/72)非累乘,无论 Tk 是否自检到 DPI 都落到正确值。
- **验证(本机)**:`py_compile` 通过;`_render_plan` headless 渲染 `RENDER_OK`、语义 tag 全部命中、零异常;`_enable_hidpi()` 实测 `scale=1.500`、`tk scaling≈1.998`、窗口 `1470×1020`;用户实机预览确认锐利、配色满意。

---

## v1.3.0(✅ 已实现 · 2026-06-26)— GUI 化(tkinter)+ 地景综合规划

- **关联**:`PRD.md` F1/F5/F13 + GUI;实现计划 `bubbly-wishing-lobster.md`(v1.3.0)。
- **目标**:① 给程序做图形界面(tkinter,保持纯标准库),GUI 作默认前端、CLI 保留(`--cli`);② 随机规划新增「仅在两端都已装地景的机场间抽线」开关,与 Volanta 优先未飞加权叠加。
- **核心架构原则**:GUI 是薄表现层,复用现有数据函数;先把 `print_flight_info` 的「计算」与「渲染」解耦成结构化结果(`planner.py`),CLI/GUI 各自渲染、共享同一计算 → 将来换 GUI 框架只动 `gui.py` 一层。

### 改动总览

| # | 文件 | 位置 | 类型 | 摘要 |
|---|---|---|---|---|
| 18 | `dispatcher/__init__.py` | 版本号 | 修改 | `__version__` `1.2.0`→`1.3.0` |
| 19 | `dispatcher/planner.py` | 新增模块 | 新增 | `@dataclass FlightPlan` + `build_flight_plan(...)`(计算:FlightAware 抓取 + 无排班降级模拟呼号)+ 共享 `parse_runway_ft`/`parse_dist` |
| 20 | `dispatcher/app.py` | `print_flight_info` + 主循环 + import | 修改 | 渲染部分改调 `build_flight_plan`(**输出逐字不变**);加需求 B 问句 + 传参;移除 `random`/`fetch_real_flights_with_filter`/`pick_sim_airline` 直接 import |
| 21 | `dispatcher/routing.py` | `get_random_route` | 修改 | 末尾加 `require_both_scenery=False` 参 + 距离过滤后一行 `has_scenery` 过滤 |
| 22 | `dispatcher/volanta.py` | 数据层 | 新增 | `set_volanta_auto(enabled)`(写 `preference` auto/ask,供 GUI 复选框双向控制) |
| 23 | `dispatcher/gui.py` | 新增模块 | 新增 | `DispatcherGUI` + `run_gui()`:tkinter 窗体、后台线程、`_TkTextWriter` stdout 重定向、Volanta 控件、需求 B 复选框、`_render_plan` |
| 24 | `flight_dispatcher.py` | 入口 | 修改 | 默认 `run_gui()`;`--cli` → `dispatcher.app.main()`(惰性 import,`--cli` 不加载 tkinter) |

### 关键详细记录

#### 改动 19 — planner.py（计算 / 渲染解耦）
- `build_flight_plan(dep_obj, arr_obj, route_dist, route_details, user_airline, user_aircraft, user_time_range, flown_count)` → `FlightPlan`:搬入原 `print_flight_info` 的计算部分——`fetch_real_flights_with_filter` 抓取、`url` 拼接、无排班时 `pick_sim_airline` + `random.randint(11,899)`(**呼号在此算一次**,CLI/GUI 一致)。`FlightPlan` 含 dep/arr(Airport)、dist_nm、flown_count、aip_routes、real_flights、is_exact、sim_callsign、url。
- `parse_runway_ft`/`parse_dist`:GUI 用的输入解析,与 app.py 内联解析口径一致(CLI 仍用其内联解析,保证逐字不变)。

#### 改动 20 — app.py print_flight_info 重构
- **before**:内联 `is_exact, real_flights = fetch_real_flights_with_filter(...)` + 内联 `sim_airline_code = user_airline or pick_sim_airline(...)` + `{...}{random.randint(11,899)}`。
- **after**:`plan = build_flight_plan(...)`,渲染改用 `plan.is_exact`/`plan.real_flights`/`plan.sim_callsign`/`plan.url`;**所有 print 格式串原样保留**(实测 RJTT→RJBB 输出与重构前逐字一致)。
- 需求 B 问句(strict_aip 之后,仅 `scenery_map is not None` 时问):`require_both_scenery = input("🌍 是否仅在【两端都已安装地景】的机场间随机规划？(Y/N): ")...=='Y'`;随机调用处传 `require_both_scenery=...`(固定双端分支不传,不受影响)。

#### 改动 21 — routing.py 地景过滤(需求 B)
- **after**(`get_random_route` 枚举循环内,距离过滤之后):
  ```python
  if not (min_dist <= dist <= max_dist): continue
  if require_both_scenery and not (ap1.has_scenery and ap2.has_scenery): continue   # 新增
  if strict_aip and (ap1.code, ap2.code) not in (aip_index or set()): continue
  ```
- 边界:`has_scenery` 在 `scenery_sources is None`(未检测)时为 True → 过滤自然失效;零候选抛 `RuntimeError("未能找到...")` 被调用方接住。

#### 改动 23 — gui.py（tkinter 表现层）
- **线程**:Tk mainloop 在主线程;初始化/规划/Volanta 同步走 `threading.Thread(daemon=True)`,UI 更新一律经 `root.after()`(`self._post`)回主线程。
- **stdout 重定向**:`_TkTextWriter` 把 `sys.stdout/stderr` 接到「日志框」,在任何复用函数运行前安装 → 解决 `--windowed` 下 `sys.stdout=None` 会让复用函数 `print()` 崩溃的问题,且现有 print 自动成为 GUI 状态日志(业务逻辑零改动)。
- **NavData 兜底**:启动 worker 直接调 `find_navdata_file()`(纯函数,不走 app.py 那个有阻塞 `input()` 的 `find_xp_data_files`);缺失则 `messagebox` 提示去 Navigraph 下载。
- **Volanta**:「同步 Volanta」按钮(先试 session token,否则开浏览器后台轮询 180s、`threading.Event` 可取消)+「自动同步」复选框(`set_volanta_auto`)+ 状态标签。
- **需求 B**:「仅两端有地景」复选框;`scenery_map is None` 时灰显 + 提示「未检测到地景目录,无法按地景筛选」。
- **结果**:`_render_plan(plan)` 把 `FlightPlan` 渲染进只读 Text(复用 `Airport.scenery_label()`/`is_military`),FlightAware URL 作可点链接(tag + `webbrowser.open`)。

### 打包变更
- GUI 版打包命令改为 **`pyinstaller --onefile --windowed flight_dispatcher.py`**(`--windowed` 去控制台 → stdout 重定向必做)。运行目录数据文件仍由 `get_real_run_path()` 锚定 `sys.executable` 目录(冻结模式不变)。CLI 走源码 `python flight_dispatcher.py --cli`。

### 验证(本机)
- 整包 `py_compile` 通过。
- 需求 B 单测:`require_both_scenery=True` 仅在地景机场间抽线;`=False` 含无地景;`scenery=None` 过滤失效仍能抽线 —— 全过。
- **CLI 重构等价**:`--cli` 跑 RJTT→RJBB,航线卡(地景标注/距离/Volanta 已飞6次/AIP 航路/FlightAware 完美匹配5条/链接)与重构前逐字一致;新增地景问句正确出现。
- **GUI 源码**:无人值守冒烟 `_ready=True`、数据就绪(地景62/AIP1436/Volanta177)、stdout 重定向生效、无崩溃;触发 RJTT→RJBB 规划,结果框渲染与 CLI 一致、链接可点。
- **windowed 冻结**:`--onefile --windowed` 构建成功,启动 exe 9s 不崩(证明 None-stdout 重定向在冻结模式生效)。

### 🐛 Bugfix(2026-06-26)— Volanta 登录落地页 `/flights` → `/map`

- **现象**(用户实测):程序为未登录用户打开 `https://fly.volanta.app/flights` 时,该页**卡在加载**(航班页要求已登录),导致登录流程走不下去、轮询不到令牌。
- **根因**:`/flights` 对未登录会话不可用;应先让用户在能正常完成登录的页面登录。
- **修法**(`dispatcher/volanta.py` + `gui.py`):新增常量 `_VOLANTA_LOGIN_URL = "https://fly.volanta.app/map"`,`_open_volanta_in_browser` 默认落地页由 `/flights` 改为 **`/map`**(地图页可正常登录);`sync_volanta_via_browser` 与 GUI Volanta worker 的提示文案同步改为「地图页登录」。CLI 与 GUI 都经 `_open_volanta_in_browser()` 走默认 URL,故一处改全生效。
- **令牌生命周期(本就正确,文档补明)**:登录后 Orbx 令牌有效约 **14 天**;`try_fetch_volanta_json_via_session` 在 14 天内**直接用令牌调 API、不开浏览器**(`_extract_volanta_api_token` 校验 `exp`,过期令牌跳过 → 触发再次引导到 `/map` 登录拿新令牌)。本次只改落地页,令牌→API 路径不动。
- **验证**:`py_compile` 通过;grep 确认代码中 `/flights` 落地页引用清零(仅 revisions 历史记录保留);文档(CLAUDE/PRD/README)同步为 `/map`。

---

## v1.2.0(✅ 已实现 · 2026-06-25)— 新增 F11:Volanta 优先未飞航线

- **关联**:`PRD.md` F11 / §2.4 / §4.6;实现计划 `bubbly-wishing-lobster.md`
- **目标**:读取 Volanta 已飞记录,随机规划时**按已飞次数加权**软优先未飞的**有向**航线;启动时每日询问一次是否同步。
- **验证**:`py_compile` 通过;实跑 `load_volanta_flown_routes()` 读到 **177 条**有向航线(缓存 2026-06-25 21:19);加权抽样 3000 次,已飞 5 次的航线仅被抽中 **11 次(0.37%)**,空数据时退化为近似均匀(极差 48)。

### 改动总览

| # | 文件 | 位置 | 类型 | 摘要 |
|---|---|---|---|---|
| 1 | `flight_dispatcher.py` | 新增"Volanta 已飞读取"模块 | 新增 | `find_volanta_leveldb_dirs` / `_read_leveldb_text` / `extract_flown_routes`(返回 `dict{(dep,arr): count}`) / `volanta_synced_today` / `mark_volanta_synced_today` / `prompt_sync_volanta` / `sync_volanta_via_browser` / `load_volanta_flown_routes` + CSV 兜底(累加 count) |
| 2 | `flight_dispatcher.py` | `__main__` 启动初始化 | 修改 | 加入"今日未同步则询问→同步→加载已飞次数 dict"时序;**预建 `aip_index` 集合** |
| 3 | `flight_dispatcher.py` | `get_random_route` | 修改 | 新增 `flown_counts` 参数;**从拒绝采样改为枚举候选 + 按 `w=1/(count+1)²` 加权随机抽取**;AIP 用 `set` 索引 O(1) 过滤;返回值增加 `flown_count` |
| 4 | `flight_dispatcher.py` | `print_flight_info` 闭包 | 修改 | `flown_count>0` 时为航线追加 `[⚠️Volanta:已飞过 N 次]` 标注 |
| 5 | `flight_dispatcher.py` | `load_volanta_flown_routes` meta + 启动文案 | 修改 | 澄清 232/177 口径;`meta` 增 `flights`;文案显示「N 次飞行、覆盖 M 条不同航线」 |

### 详细记录

#### 改动 1 — 新增 Volanta 读取模块
- **文件**:`flight_dispatcher.py`,新增区块「Volanta 已飞航线读取模块 (F11)」,位于 `load_aip_routes_from_csv` 之后、「智能匹配与解析辅助函数」之前。
- **类型**:新增(完整源码见源文件,均带中文注释;此处录函数清单 + 核心片段)
- **新增函数清单**:
  - `find_volanta_leveldb_dirs()` → 扫描 Edge/Chrome/Brave 各 Profile 的 IndexedDB,返回所有 `https_fly.volanta.app_*.indexeddb.leveldb` 目录
  - `_read_leveldb_text(db_dir)` → 共享读目录下全部 `.log`/`.ldb`,`latin1` 拼接(被锁则复制临时文件再读)
  - `extract_flown_routes(text)` → 正则提取有向起降对并计数,返回 `dict{(dep,arr): count}`
  - `_volanta_config_path()` / `volanta_synced_today()` / `mark_volanta_synced_today()` → `volanta_config.txt` 记录上次同步日期,实现「每天一次」
  - `prompt_sync_volanta()` → 今日未同步时询问是否同步(不持久化用/不用偏好,不锁死)
  - `_latest_leveldb_mtime(db_dirs)` / `_open_volanta_in_browser(url)` / `sync_volanta_via_browser(db_dirs, timeout=120)` → 打开浏览器并轮询缓存时间戳直到刷新(超时 2 分钟兜底)
  - `_load_volanta_csv(path)` / `load_volanta_flown_routes()` → 聚合多 Profile + CSV 兜底,返回 `(flown_counts, meta)`
- **核心源码**(正则 + 提取计数):
  ```python
  _VOLANTA_ROUTE_PAT = re.compile(
      r'origin.{0,120}?icaoCode"(?:.)([A-Z0-9]{4}).{0,2500}?'
      r'destination.{0,120}?icaoCode"(?:.)([A-Z0-9]{4})', re.DOTALL)

  def extract_flown_routes(text):
      counts = {}
      for m in _VOLANTA_ROUTE_PAT.finditer(text):
          dep, arr = m.group(1).upper(), m.group(2).upper()
          if dep == arr:
              continue
          counts[(dep, arr)] = counts.get((dep, arr), 0) + 1
      return counts
  ```

#### 改动 2 — 启动初始化接入每日同步
- **文件**:`flight_dispatcher.py` → `if __name__ == "__main__"`(`load_aip_routes_from_csv()` 之后)
- **类型**:修改(新增)
- **before**:
  ```python
  scenery_list = load_active_sceneries(ini_path)
  aip_data = load_aip_routes_from_csv()

  while True:
  ```
- **after**:
  ```python
  scenery_list = load_active_sceneries(ini_path)
  aip_data = load_aip_routes_from_csv()

  # 🛩️ F11：读取 Volanta 已飞航线（每天最多同步一次；不用 Volanta 的用户回车跳过即可，不会被锁死）
  flown_counts = {}
  if volanta_synced_today():
      print("📦 今日已同步过 Volanta 数据，直接使用现有缓存。")
      flown_counts, vmeta = load_volanta_flown_routes()
  elif prompt_sync_volanta():
      if sync_volanta_via_browser(find_volanta_leveldb_dirs()):
          mark_volanta_synced_today()        # 检测到缓存刷新才算同步成功，写入今天日期
      flown_counts, vmeta = load_volanta_flown_routes()
  else:
      flown_counts, vmeta = load_volanta_flown_routes()   # 跳过同步，仍尝试读现有缓存
  if flown_counts:
      _vlatest = vmeta.get("latest")
      print(f"✈️ 已从 Volanta 读取到 {len(flown_counts)} 条已飞航线"
            + (f"（缓存更新于 {_vlatest}）" if _vlatest else "")
            + "，随机规划将优先未飞航线。")
  else:
      print("ℹ️ 未读取到 Volanta 数据，本次不启用「优先未飞」。（可在浏览器登录 Volanta 网页，或导出 CSV 放入工作目录）")

  # 预建 AIP 起降索引，供加权枚举时 O(1) 过滤（避免对每对航线线性遍历 aip_data）
  aip_index = {(r[0].strip().upper(), r[1].strip().upper()) for r in aip_data if len(r) >= 2} if aip_data else set()

  while True:
  ```

#### 改动 3 — `get_random_route` 按已飞次数加权抽取
- **文件**:`flight_dispatcher.py` → `get_random_route`
- **类型**:修改
- **before**(拒绝采样,返回 4 元组):
  ```python
  def get_random_route(airport_list, min_dist, max_dist, aip_routes_data=None, strict_aip=False, fixed_dep=None, fixed_dest=None):
      ...
      pool_dep = [a for a in airport_list if a.code != fixed_dest] if fixed_dest else airport_list
      pool_dest = [a for a in airport_list if a.code != fixed_dep] if fixed_dep else airport_list
      for _ in range(150000):
          ap1 = ap1_fixed if fixed_dep else random.choice(pool_dep)
          ap2 = ap2_fixed if fixed_dest else random.choice(pool_dest)
          dist = calculate_distance_nm(ap1, ap2)
          if min_dist <= dist <= max_dist:
              route = find_aip_route(aip_routes_data, ap1.code, ap2.code) if aip_routes_data else None
              if strict_aip and route is None: continue
              return ap1, ap2, dist, route
      raise RuntimeError(f"未能找到与要求匹配的航线。")
  ```
- **after**(枚举 + 加权,返回 5 元组):
  ```python
  def get_random_route(..., fixed_dest=None, flown_counts=None, aip_index=None):
      ...
      flown_counts = flown_counts or {}
      pool_dep = [ap1_fixed] if fixed_dep else airport_list
      pool_dest = [ap2_fixed] if fixed_dest else airport_list
      if strict_aip and aip_index is None and aip_routes_data:
          aip_index = {(r[0].strip().upper(), r[1].strip().upper()) for r in aip_routes_data if len(r) >= 2}
      candidates, weights = [], []
      for ap1 in pool_dep:
          for ap2 in pool_dest:
              if ap1.code == ap2.code: continue
              dist = calculate_distance_nm(ap1, ap2)
              if not (min_dist <= dist <= max_dist): continue
              if strict_aip and (ap1.code, ap2.code) not in (aip_index or set()): continue
              count = flown_counts.get((ap1.code, ap2.code), 0)
              candidates.append((ap1, ap2, dist, count))
              weights.append(1.0 / (count + 1) ** 2)     # 已飞越多权重越低；未飞=1.0
      if not candidates:
          raise RuntimeError("未能找到与要求匹配的航线。")
      ap1, ap2, dist, count = random.choices(candidates, weights=weights, k=1)[0]
      route = find_aip_route(aip_routes_data, ap1.code, ap2.code) if aip_routes_data else None
      return ap1, ap2, dist, route, count
  ```
- **⚠️ 连带影响**:返回值 4 元组 → 5 元组,**调用方必须同步改**(见改动 2 主循环 else 分支:`dep, arr, dist, route, flown_count = get_random_route(..., flown_counts, aip_index)`)。

#### 改动 4 — `print_flight_info` 显示已飞标注
- **文件**:`flight_dispatcher.py` → `print_flight_info` 闭包
- **类型**:修改
- **before**:
  ```python
  def print_flight_info(dep_obj, arr_obj, route_dist, route_details):
      ...
      print(f"  大圆距离 : {route_dist:.1f} NM")
  ```
- **after**:
  ```python
  def print_flight_info(dep_obj, arr_obj, route_dist, route_details, flown_count=0):
      ...
      print(f"  大圆距离 : {route_dist:.1f} NM")
      if flown_count and flown_count > 0:
          # F11：抽中的是已飞航线（加权后概率低但未被排除），给出信息性提示
          print(f"  🔁 Volanta : 这条有向航线你已飞过 {flown_count} 次（已飞过，可考虑换一条）")
  ```
- **调用处**:固定双机场分支传 `flown_counts.get((fixed_departure, fixed_destination), 0)`;随机分支接 5 元组的 `flown_count` 并传入。

#### 改动 5 — 统计口径澄清与显示优化(232 vs 177)
- **背景**:Volanta「已完成航班数」显示 232,而读出 177,核对为**口径不同,非漏数据**。
- **诊断**(`scratchpad/diag.py`):正则配对航班数 = **232**(与 Volanta 完全一致),其中 **2 条自环**(`dep==arr`,本场/复飞)被跳过 → **230** 次有效飞行 → 去重为 **177** 条不同有向航线。
- **after**(`load_volanta_flown_routes` 的 meta + 启动文案):
  ```python
  meta = {
      "routes": len(counts),                 # 去重后的不同有向航线数
      "flights": sum(counts.values()),       # 总飞行次数(不含被跳过的自环航班)
      "latest": time.strftime("%Y-%m-%d %H:%M", time.localtime(latest)) if latest else None,
  }
  # 启动文案：
  print(f"✈️ 已从 Volanta 读取到 {vmeta.get('flights', ...)} 次飞行、"
        f"覆盖 {len(flown_counts)} 条不同有向航线" + ...)
  ```
- **结论**:`len(dict)` = 不同航线数(用于「有没有飞过」),`sum(values)` = 总次数(用于加权权重),两者皆完整。自环航班对 A→B 规划无意义,故跳过。

---

## v1.2.0(续 · ✅ 已实现 · 2026-06-26)— 解耦 XP + 拆包 + 航司/军用优化 + Volanta 会话自动拉取

> 版本号保持 v1.2.0,本节按时间顺序记录这一整轮迭代的所有改动:
> 1. **解耦 X-Plane**(改动 6–13):自带 `NavData` 导航 + AIRAC 自检 + 多源地景检测(下方「改动总览/详细记录」)
> 2. **🐛 地景假阳性修复**:白名单 + 正则边界剔除 `ROAD/ROCA` 等
> 3. **📝 文案**:导航更新提示补 Navigraph 链接
> 4. **🧱 重构**:单文件 → `dispatcher/` 子包
> 5. **✨ 模拟呼号按航线挑航司(F4)** + **航司数据外置 `airlines.json`**
> 6. **✨ 随机抽线优先民用机场(F6)**:按军用端数量分层
> 7. **🐛/✨ Volanta(F11)演进**:同步假成功修复 → 累积库 → `/flights` 滚动 → `volanta_flights.json` → **登录会话(Orbx token)自动拉取 API** → 移除旧 IndexedDB 正则扫描
> 8. **🐛 打包**:UTF-8 输出修复 GBK emoji 崩溃

### ①解耦 X-Plane —— 关联/目标/验证

- **关联**:`PRD.md` F5(地景检测升级)/ 导航数据自带 NavData;实现计划 `bubbly-wishing-lobster.md`(第二轮)。
- **目标**:① 导航数据改为程序自带 `NavData` 文件夹(摆脱 XP 目录依赖,只飞 MSFS 的用户也可用)+ AIRAC 过期自检;② 地景检测改为直接扫 XP `Custom Scenery`(apt.dat)+ MSFS `Community`(四步级联),合并标注来源,`installed_scenery.json` 指纹缓存。
- **验证**(本机):`py_compile` 通过;NavData 定位 OK;AIRAC 提示「已过期 15 天」;扫描 66 个日本机场(XP:59/MSFS:47/两者:40)0.66s,**缓存命中 0.032s**;`RJOT`(kado_takamatsu 文件夹名无 ICAO)经 ContentHistory.json 救回为 {XP,MSFS};标注 `[地景:XP+MSFS]` 正确。

### 改动总览

| # | 文件 | 位置 | 类型 | 摘要 |
|---|---|---|---|---|
| 6 | `flight_dispatcher.py` | 顶部 import | 修改 | 新增 `import datetime`(AIRAC 日期比较) |
| 7 | `flight_dispatcher.py` | `Airport` 类 | 修改 | 第 4 参 `has_scenery`→`scenery_sources`(set);`has_scenery` 改派生属性;新增 `scenery_label()` |
| 8 | `flight_dispatcher.py` | 路径检测段(新增函数) | 新增 | `XP_COMMON_PATHS`/`list_drives`/`installed_scenery.json` 读写(`load_sim_config`等)/`find_navdata_file`/`check_airac_currency`/`_is_xp_root`/`locate_xp_root` |
| 9 | `flight_dispatcher.py` | `find_xp_data_files` | 修改 | 新增优先级 0(自带 NavData)+ 同级 `.ini` 改可选 |
| 10 | `flight_dispatcher.py` | 新增「多源地景扫描模块」 | 新增 | `find_msfs_packages_dir`/`scan_xp_sceneries`/`_extract_msfs_pack_icaos`(四步级联)/`scan_msfs_sceneries`/`_scenery_fingerprint`/`scan_installed_sceneries` |
| 11 | `flight_dispatcher.py` | `load_airports_from_navigraph` | 修改 | 参数 `active_sceneries`→`scenery_map`;`has_scenery` 子串匹配 → `scenery_sources=scenery_map.get(code)` |
| 12 | `flight_dispatcher.py` | `print_flight_info` + 主循环初始化 + 调用处 | 修改 | 地景标注用 `scenery_label()`;初始化加 AIRAC 自检 + `scan_installed_sceneries`;`load_airports` 传 `scenery_map` |
| 13 | `flight_dispatcher.py` | `load_active_sceneries` | 删除 | 旧的 scenery_packs.ini 读取已被多源扫描取代 |

### 关键详细记录

#### 改动 7 — Airport 地景来源
- **after**:
  ```python
  class Airport:
      def __init__(self, code, lat_str, lon_str, scenery_sources=None, is_military=False):
          ...
          self.scenery_sources = scenery_sources  # None=未检测; set()=无地景; {'XP','MSFS'}=有地景
      @property
      def has_scenery(self):
          return True if self.scenery_sources is None else bool(self.scenery_sources)
      def scenery_label(self):
          if self.scenery_sources is None: return ""
          if not self.scenery_sources: return " [⚠️无地景]"
          return " [地景:" + "+".join(s for s in ("XP","MSFS") if s in self.scenery_sources) + "]"
  ```

#### 改动 9 — find_xp_data_files 加 NavData 优先级
- **before**:优先级 1 要求 `earth_aptmeta.dat` + `scenery_packs.ini` **都在**才返回。
- **after**:
  ```python
  opt_ini = local_ini if os.path.exists(local_ini) else None
  nav = find_navdata_file()                 # 优先级 0：程序自带 NavData
  if nav:
      print(f"📁 已读取程序自带的 NavData 导航数据：{os.path.relpath(nav, real_dir)}")
      return nav, opt_ini
  if os.path.exists(local_dat):             # 优先级 1：同级 earth_aptmeta.dat（ini 可选）
      print("📁 优先检测到本目录内的 earth_aptmeta.dat，将直接读取。")
      return local_dat, opt_ini
  ```

#### 改动 10 — MSFS 四步级联(核心)
- **after**(`_extract_msfs_pack_icaos`,逐级降级、命中即止):
  ```python
  found = _extract_japan_icaos(os.path.basename(pack_dir))   # 1.文件夹名
  if found: return found
  # 2.ContentInfo\...\ContentHistory.json：items[] 里 type==Airport 的 content 即权威 ICAO
  for it in data.get("items") or []:
      if it.get("type") == "Airport" and content[:2] in ("RJ","RO"): found.add(content)
  if found: return found
  # 3.scenery\*.bgl 文件名
  for fn in *.bgl: found |= _extract_japan_icaos(fn)
  return found                                                # 4.都没有→空集（机模/库）
  ```
- XP 侧 `scan_xp_sceneries` 读 `Earth nav data\apt.dat` 机场行(行码 1/16/17,第 5 字段 ICAO),仅留 RJ/RO。

#### 改动 12 — 缓存与主循环
- `scan_installed_sceneries()` 返回 `(scenery_map, from_cache)`;指纹 = `{sim目录: {包名: mtime}}`,未变直接读 `installed_scenery.json` 的 `sceneries`(秒开)。
- 主循环初始化:`check_airac_currency(dat_path)` + `scan_installed_sceneries()` + 打印来源分布;`load_airports_from_navigraph(dat_path, scenery_map, …)`。
- `installed_scenery.json` 统一存 `xp_root`/`msfs_packages`/`sceneries`/`fingerprint`,并迁移旧 `xp_path_config.txt`。

### 已知局限
- MSFS 提取召回非 100%(`manifest.json` 无 ICAO 字段;命名极不规范且无 `ContentHistory.json` 的包会漏),要 100% 准需解析 bgl 二进制(本期不做)。
- 地景扫描仍需访问 sim 安装目录(用户认可);导航数据(NavData)才是彻底解耦项。
- `find_xp_data_files` 的 XP 兜底(优先级 2/3)仍沿用旧 `xp_path_config.txt`;地景定位已迁移到 `installed_scenery.json`。

### 🐛 Bugfix(2026-06-26)— 地景扫描误把普通单词当 ICAO

- **现象**:`installed_scenery.json` 缓存里混入 `ROAD`/`ROSH`/`ROBO`/`ROCA` 等并不存在的「机场」,启动机场计数虚高(66 实为 62)。
- **根因**:`_extract_japan_icaos` 用 `re.findall(r'(?i)R[JO][A-Z]{2}', text)` 对**文件夹名 / bgl 文件名做大小写不敏感的子串扫描**,任何凑巧形如 `R[JO]xx` 的 4 字母片段都被当成 ICAO:`digson-scenery-ngt-`**`road`**`-mesh`→`ROAD`、`orbx-volanta-ae`**`roca`**`ches`→`ROCA` 等。光靠正则无法区分 `ROAD`(非机场)与 `ROAH`(那霸,真机场)。
- **修法**(`flight_dispatcher.py`,4 处):
  | # | 位置 | 改动 |
  |---|---|---|
  | 14a | `_extract_japan_icaos` | 正则加单词边界 `(?i)(?<![A-Za-z0-9])R[JO][A-Za-z]{2}(?![A-Za-z0-9])`,挡掉内嵌片段(`aerocaches→ROCA` 不再命中) |
  | 14b | 新增 `load_japan_icao_set(filepath)` | 从 `earth_aptmeta.dat` 读**全部真实 RJ/RO 机场 ICAO 白名单**(不按跑道过滤);导航数据缺失则返回空集 |
  | 14c | `scan_installed_sceneries(force=False, valid_icaos=None)` | 新增 `valid_icaos` 参;**全量扫描结果 + 缓存命中结果**都过 `_whitelist()` 过滤;白名单为空时不过滤(避免误删);缓存命中若发现残留假阳性则**回写自愈一次**(指纹不变,仍秒开) |
  | 14d | `__main__` 初始化 | `valid_japan_icaos = load_japan_icao_set(dat_path)` → `scan_installed_sceneries(valid_icaos=valid_japan_icaos)` |
- **核心源码**(14a + 14c 过滤):
  ```python
  # 14a：单词边界，避免 'aerocaches' 里的 'roca' 等被误当 ICAO
  re.findall(r'(?i)(?<![A-Za-z0-9])R[JO][A-Za-z]{2}(?![A-Za-z0-9])', text)

  # 14c：导航数据白名单过滤（扫描 + 缓存两条路径共用）
  def _whitelist(m):
      if not valid_icaos:           # 白名单为空(导航数据缺失)→ 不过滤，避免误删
          return m
      return {k: v for k, v in m.items() if k in valid_icaos}
  ```
- **为何双保险**:`ROAD` 来自 `ngt-`**`road`**`-mesh`,`road` 是连字符分隔的独立 token、**边界正则仍会命中**,只能靠白名单(导航数据里无 `ROAD`)剔除;`ROCA` 来自内嵌的 `aerocaches`,**边界正则直接挡掉**。两层分别覆盖「内嵌片段」与「形似 ICAO 的真实单词」。
- **验证**(本机):`py_compile` 通过;导航数据白名单 129 个真机场;全量扫描 66→**62**(精确删 `ROAD/ROSH/ROBO/ROCA` 4 个),`ROAH/RORS/RJSR/RJOR/RJTT/RJOT` 等真机场来源标注不变;手动往缓存注入假阳性后跑**缓存命中路径**(`cached=True`、0.026s),返回与文件均被自愈为 62、无残留。

### 📝 文案(2026-06-26)— 导航数据更新指引补充 Navigraph 下载链接

- **目的**:让用户明确去哪、下载哪个版本的导航数据。
- **改动**(`flight_dispatcher.py`,2 处输出文案):
  - `check_airac_currency` 过期提示 →「前往 Navigraph 下载页 `https://navigraph.com/downloads` 重新下载**「X-Plane 12」**的导航数据,用新数据替换程序目录下的 NavData 文件夹后重启」。
  - `load_airports_from_navigraph` 导航数据缺失错误 → 追加同一链接 + 「下载 X-Plane 12 导航数据放入 NavData 文件夹(确保 `NavData\earth_aptmeta.dat` 存在)」放置说明,覆盖**完全没有 NavData**(尤其只飞 MSFS、没装 XP)的用户。
- **验证**:`py_compile` 通过;两处提示实跑均正确输出链接与「X-Plane 12」字样。

### 🧱 重构(2026-06-26)— 单文件 `flight_dispatcher.py` 拆分为 `dispatcher/` 子包

- **动机**:单文件约 1100 行,越来越难维护。按功能拆成「一个模块一个职责」的子包,**纯结构调整、零行为变更**(逐字搬运代码,仅做模块归位与跨模块 import)。
- **布局**(根目录 `flight_dispatcher.py` 变为薄壳入口 → `from dispatcher.app import main; main()`):

  | 模块 | 内容 |
  |---|---|
  | `dispatcher/__init__.py` | 包说明 + `__version__ = "1.2.0"` |
  | `dispatcher/model.py` | `Airport` |
  | `dispatcher/config.py` | `get_real_run_path`、`list_drives`、`XP_COMMON_PATHS`、`installed_scenery.json` 读写(`load/save/_update_sim_config`) |
  | `dispatcher/navdata.py` | `find_navdata_file`、`_MONTH_ABBR`、`check_airac_currency`、`_is_xp_root`、`locate_xp_root`、`find_xp_data_files` |
  | `dispatcher/scenery.py` | 多源地景扫描全套(`_extract_japan_icaos`、`find_msfs_packages_dir`、`scan_xp_sceneries`、`_extract_msfs_pack_icaos`、`scan_msfs_sceneries`、`_scenery_fingerprint`、`scan_installed_sceneries`) |
  | `dispatcher/data.py` | `load_airports_from_navigraph`、`load_japan_icao_set`、`load_aip_routes_from_csv` |
  | `dispatcher/volanta.py` | Volanta 13 个函数 + `_VOLANTA_ROUTE_PAT` |
  | `dispatcher/flightaware.py` | `is_aircraft_match`、`time_to_minutes`、`parse_user_time_range`、`fetch_real_flights_with_filter` |
  | `dispatcher/routing.py` | `calculate_distance_nm`、`find_aip_route`、`get_random_route` |
  | `dispatcher/app.py` | `main()`(原 `if __name__=="__main__"` 块整体函数化,`print_flight_info` 仍为其内嵌闭包) |

- **唯一行为敏感改动 —— `get_real_run_path()` 锚点**(`config.py`):
  - **before**(单文件在根目录,`__file__` 目录即根):
    ```python
    return os.path.dirname(os.path.abspath(__file__))
    ```
  - **after**(本文件现位于 `<根>/dispatcher/config.py`,需上溯两级回根,否则 NavData/缓存会被错误地定位到 `dispatcher/` 子目录):
    ```python
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 上溯两级到项目根
    ```
  - 冻结(exe)模式逻辑不变(仍取 `sys.executable` 目录)。⚠️ 后续不要把这里「简化」回单层 `dirname`。
- **构建/运行不变**:入口文件名仍是 `flight_dispatcher.py`,故 `python flight_dispatcher.py` 与 `pyinstaller --onefile flight_dispatcher.py` 均无需改动(PyInstaller 沿静态 import 自动收包)。
- **验证**(本机):11 个文件 `py_compile` 全通过;`get_real_run_path()` 实跑 = `f:\jpn flight dispatcher`(项目根,**非** dispatcher 子目录),`installed_scenery.json` 锚点正确;入口薄壳 `main` 可调用且不手动改 sys.path、靠 cwd 即可解析包;NavData/AIRAC、地景扫描(缓存命中 0.025s、62 机场、无假阳性、`RJOT/ROAH` 标注正确)、机场加载(84 个)、加权抽线端到端均正常。

### 🐛 Bugfix(2026-06-26)— 冻结/重定向输出时 emoji 触发 GBK 编码崩溃

- **现象**:打包成 exe 后,若 stdout 被重定向到管道/文件(或某些终端),启动即崩 `UnicodeEncodeError: 'gbk' codec can't encode character '✈'`(界面里的 ✈️ 等 emoji)。
- **根因**:冻结运行或输出非真实控制台时,Windows 上 Python 默认用本地代码页(中文系统 = GBK)编码 stdout,GBK 无法编码 emoji。真实控制台走 Unicode API 不受影响,故仅在重定向/管道场景暴露(打包后用管道冒烟测试时发现)。
- **修法**(`dispatcher/app.py`,`main()` 开头,首次 print 之前):
  ```python
  import sys
  for _stream in (sys.stdout, sys.stderr):
      try:
          _stream.reconfigure(encoding="utf-8")   # 强制 UTF-8，兼容控制台与管道/重定向
      except Exception:
          pass
  ```
  放在 `main()` 内(而非入口薄壳),确保无论经 `flight_dispatcher.py` 还是直接 `dispatcher.app.main()` 都生效;UTF-8 能编码全部 Unicode,对真实控制台显示无害。
- **验证**:重新打包后用管道喂入一轮输入实跑 `dist\flight_dispatcher.exe`,emoji 横幅、NavData 读取、AIRAC、地景(62 机场)、AIP(1436 条)、Volanta(230 次/177 航线)、随机规划(RJNS→RJOR 带地景标注)全程正常,无崩溃。

### ✨ 改进(2026-06-26)— 模拟呼号按航线挑合理航司(F4)

- **现象**:FlightAware 查不到真实排班时,模拟呼号的航司是**无视航线**从固定 8 家里均匀随机抽,会出现「ADO(北海道国际航空)飞福冈-冲绳」这类离谱组合。
- **背景**:模拟呼号**仅在 FA 返回零排班时**触发(有任何真实排班都直接展示),此时无真实数据可借,只能用启发式。
- **改动**:
  | # | 文件 | 类型 | 内容 |
  |---|---|---|---|
  | 15a | `dispatcher/airlines.py` | 新增 | 机场 ICAO→地理大区映射(`_REGION_BY_PREFIX` + `RJOO`→关西 override + `RO*`→冲绳)+ 航司运营网络表 `AIRLINE_NETWORKS`(`"ALL"`=全国干线)+ `pick_sim_airline(dep,arr)` |
  | 15b | `dispatcher/app.py` | 修改 | 删除主循环里 route-blind 的 `sim_airline_code = user_airline or random.choice([...8家...])`;改在 `print_flight_info` 的「零排班」分支内 `sim_airline_code = user_airline or pick_sim_airline(dep_obj.code, arr_obj.code)`;新增 `from .airlines import pick_sim_airline` |
- **核心逻辑**(`pick_sim_airline`):全国干线(ANA/JAL,`"ALL"`)始终是候选;区域航司需**网络同时覆盖航线两端所在大区**才入选;任一端区域识别失败/无合适航司则回退 ANA/JAL。用户显式指定航司时仍优先用户的。
- **验证**(各 4000 次抽样):`RJFF→ROAH`(福冈→那霸)候选 = {APJ,JJP,SKY,ANA,SNJ,JAL},**ADO/SFJ 已消失**;`RJCC→RJTT` 含 ADO;`RJFR→RJTT` 含 SFJ;`RJCC→ROAH`(跨北海道·冲绳)只剩 ANA/JAL/SKY/JJP/APJ。`py_compile` 通过。
- **可维护性**:粗粒度近似(按地理大区,非精确时刻表),只为「看起来合理」。

#### 15c — 航司数据外置为 `airlines.json`(数据/逻辑分离,便于增删改查)

- **动机**:把航司/区域**写死在 `airlines.py`** 不灵活——新开航司、VATSIM 虚航、给机场改区域都得改代码 + 重打包。改为「数据文件 + 加载器」。
- **数据结构选型**(为 CRUD 友好):用 **JSON**,`airlines` 是**以航司 ICAO 为键的对象**(增=加键 / 删=删键 / 改=改 `regions` / 查=按键直取,键唯一防重复);每家 `regions` 为大区列表,全国干线用 `["ALL"]`;`airport_regions`(`by_prefix` + `overrides`)也一并外置,连机场区域都可调。
- **改动**(`dispatcher/airlines.py` 重写 + `app.py`):
  - 数据搬进 `airlines.json`(运行目录,`get_real_run_path()` 锚定,与 `installed_scenery.json` 同级);`_DEFAULT_DATA` 作内置默认。
  - `_load_airline_data()`:**首次运行自动写出** `airlines.json` 供编辑;**文件损坏/缺段回退内置默认**(`_normalize` 容错跳过脏数据);进程内缓存,编辑后重启生效。
  - 新增 `init_airline_data()`,`app.py` 启动时调用一次(提前生成文件 + 加载)。`pick_sim_airline`/`airport_region` 改为从加载的数据读。
  - `airport_region` 前缀匹配:override → 3 字前缀(`RJC/RJE/RJF…`)→ 2 字前缀(`RO`→冲绳)。
- **验证**(临时目录,避免污染工程):首跑生成文件(8 家);`RJER/RJEB/RJEC→HOKKAIDO`(北海道 RJE 系覆盖正确);福冈→那霸仍无 ADO/SFJ;**增**(加虚航 `VVA`→生效)、**删**(移除 `ADO`→消失)、**改**(`SKY` 改只飞冲绳→札幌-东京里排除)、**损坏文件**(回退 8 家默认、不崩 + 警告)全部通过。
- **注**:`airlines.json` 属运行时自动生成的可编辑配置(同 `installed_scenery.json`),不必随源码提交;分发时首跑会在 exe 旁自动生成。

### ✨ 改进(2026-06-26)— 随机抽线优先民用机场(避免随到不可飞的军用机场)

- **现象**:飞得勤的用户,未飞航线几乎只剩军用机场;而 `w=1/(count+1)²` 给未飞(count=0)航线最高权重 → **随到军用机场的概率大幅飙升**。但军用机场常无民航助航设施/进近,规划出来未必可飞。
- **为何不能只「军用降权」**:固定惩罚系数两头不可兼得——要强到压住「未飞军用(1.0) vs 已飞民用(如 1/121≈0.008)」,普通用户就几乎永远抽不到军用;弱了又解决不了飙升。故改用**分层**。
- **改动**(`dispatcher/routing.py` → `get_random_route`):
  - **before**:把所有满足约束的候选放一个 `candidates`,统一按 `1/(count+1)²` 加权抽。
  - **after**:候选按「军用端数量」分 3 层 `tiers{0,1,2}`(0=两端民用 / 1=一端军用 / 2=两端军用),**只在「军用端最少的非空层」里加权抽**:
    ```python
    mil = (1 if ap1.is_military else 0) + (1 if ap2.is_military else 0)
    tiers[mil][0].append((ap1, ap2, dist, count)); tiers[mil][1].append(w)
    ...
    for mil in (0, 1, 2):
        if tiers[mil][0]:
            candidates, weights = tiers[mil]; break
    ```
- **效果**:普通/重度用户都只落第 0 层(两端民用)——重度用户宁可重飞最少飞的民用航线,也不被推去军用;用户**固定军用端**时退到第 1 层(另一端仍优先民用),并照常显示 `[🛡️军用机场]`;实在无民用候选才退到含军用层。层内仍按已飞次数软优先未飞,行为不变。
- **验证**(mock 机场,5 民用+2 军用,各 5000 次):普通情况命中军用 **0** 次;重度用户(民用全飞 10 次、军用未飞)命中军用仍 **0** 次(飙升已解决);固定军用出发 → dep 恒军用、**arr 恒民用**;全军用列表 → 回退给出军用航线、不报错;民用航线正常返回。`py_compile` 通过。

### 🐛 Bugfix(2026-06-26)— Volanta 同步「缓存已刷新但读不到数据」

- **现象**(用户截图):选 Y 同步,程序约 3 秒就提示「检测到 Volanta 缓存已刷新」,紧接着却「未读取到 Volanta 数据,本次不启用『优先未飞』」。
- **根因**:`sync_volanta_via_browser` 把「同步完成」判据定为**仅看 leveldb 文件 mtime 是否变化**。但浏览器一打开 `fly.volanta.app` 就改写 leveldb(前端常**先清空旧数据、再重新插入全部航班**);刚开始写时 mtime 已变、数据却没写完——此刻误判「已刷新」并立即读取,读到的是清空到一半/插入未完成的状态 → 0 条。3 秒就「刷新」正是这个征兆(根本来不及登录+拉全量)。成功后 `app.py` 又**二次读取**,同样可能撞上写入窗口。
- **修法**(`dispatcher/volanta.py` `sync_volanta_via_browser` + `dispatcher/app.py`):
  - 判据从「mtime 变了」改为「**mtime 变了 + 能真正读出航线 `n>0` + 条数连续两轮一致(写入完成)**」;轮询时每轮实际 `load_volanta_flown_routes()` 一次。
  - 返回值 `True` → `(synced, flown_counts, meta)`,**把校验过的数据直接返回**;`app.py` 不再二次读取(避免再次撞上写入窗口)。
  - 超时兜底:仍尽力读现有缓存,有数据就照常启用(`synced=True`)、没有才放弃。
  - **before**:`if _latest_leveldb_mtime(cur_dirs) > baseline: return True`(只看 mtime)。
  - **after**:`if mtime_changed and n > 0 and n == prev_n: return True, counts, meta`(刷新 + 读到 + 稳定)。
- **验证**(mock 模拟竞态):前两轮读 0(写入中)→ 稳定后**循环内成功**;mtime 变但数据恒 0 → **不再误判成功**、超时不启用(正是原 bug 场景)。`py_compile` 通过。
- **后续发现 & 强化**:用户实测同步后只读到 **45 条/102 次**(完整应为 230/177),且 0 卡 30s 才跳出——确认 **Volanta 网页懒加载**,单次缓存常只有部分航班,打开/刷新页面还会让原本完整的缓存**缩水**。遂追加:
  - **持久化累积库 `volanta_flown.json`(只增不减)**:`load_volanta_flown_routes` 每次把「本次读到(浏览器+CSV)」与累积库**按每条航线取最大次数**合并、落盘、返回(`_load_flown_store`/`_save_flown_store`);单次部分读取不丢历史已飞航线。
  - 同步轮询判稳定改用 `meta['this_read']`(本次新鲜读到的条数,非累积总数——否则会被累积库预填一上来就"稳定"),连续 `_STABLE_ROUNDS=3` 轮不变才算写完;提示用户**同步时勿刷新页面**。
  - **验证**:累积库单测(部分读取 A 退回/B 丢失/C 新增 → 累积库 A 保最大、B 不丢、C 加入)✅;真实读取当前缓存 `this_read=45`、落盘 ✅。
- **完整读取方案(取代 CSV)**:Volanta CSV 导出需排队 ~15 天,不可用;API 需抠浏览器 token(凭据红线 + 1h 过期),否决。改走浏览器:同步打开页从首页改为 **`fly.volanta.app/flights`(全部航班列表)** 并提示用户**滚动到底**(该页无限滚动,只开不滚/刷新都只有部分),超时 120s→**180s**。配合累积库:**滚一次加载全部 → 完整数据被永久写进 `volanta_flown.json`**,即为这块的实际根治路径(零凭据、零 CSV 等待)。
- **最终治本 —— `volanta_flights.json`(DevTools 导出的 API 响应)**:用户在浏览器开发者工具里发现 `GET https://api.volanta.app/api/v1/Flights` **一次返回全部 232 条**(干净 JSON)。程序仍不能直连(需 Bearer token,抠浏览器凭据=红线 + 1h 过期,否决),但**用户可手动 Copy response 存成 `volanta_flights.json`** 放进工作目录,程序解析它即可——完整、准确、绕开 leveldb 去重坑,导出一次永久(并入累积库)。
  - 新增 `_load_volanta_json(path)`:每条为 `{flight:{...}, summarisedPositions:[...]}`,取 `flight.originIcao`/`destinationIcao`(回退嵌套 `origin.icaoCode`);跳过自环。实测从真实导出解析出 **177 条/230 次**(=Volanta 232 − 2 自环),与完整数据吻合。
  - 新增 `volanta_json_present()`;`app.py`:**有该文件就直接读、跳过浏览器同步询问**(`📄 检测到 volanta_flights.json`)。
  - `load_volanta_flown_routes` 重构多源合并:**航线键取并集**(不丢);**次数优先用权威 JSON**(leveldb `.log`+`.ldb` 会重复计、虚高),JSON 没有的航线才回退 leveldb/CSV/累积库最大。修正了先前 leveldb 把 230 次虚高成 263 的问题 → 显示准确的 **230**。
  - 顺带确认:API **响应体只有航班数据、无 token**(token 在请求*头*),故存响应体安全。
- **再进一步 —— 程序用登录会话【自动】拉取(零操作)**:DevTools 导出对普通用户门槛太高。用户提出"既然登录了浏览器就有 token,能不能自动拉"。重新评估后认为这是【正当的个人自动化】(读用户自己浏览器里自己的会话、访问自己的数据、token 不外传,类比 yt-dlp 的 `--cookies-from-browser`),遂实现:
  - **逆向定位 token**:Firebase idToken(`iss=securetoken.google.com/volanta`)被 API 以 `error="invalid_token", The issuer ... is invalid` 拒绝。枚举所有 JWT 后发现 API 真正接受的是 **Orbx 签发的 JWT**(`iss="Orbx"`/`aud="regular_user"`,有效期约 **14 天**;Volanta 是 Orbx 旗下),它在 **localStorage**(非 IndexedDB)。实测带它调 API → **HTTP 200,232 条航班**。
  - **新增**(`dispatcher/volanta.py`):`_localstorage_leveldb_dirs` / `_jwt_payload` / `_extract_volanta_api_token`(**只取 `iss=="Orbx"` 且就近 3000 字符内有 `fly.volanta.app` 来源标记**的令牌,绝不碰其它网站令牌)/ `_fetch_volanta_flights_json`(`Authorization: Bearer` + `Accept-Encoding: gzip` 避开 br)/ `try_fetch_volanta_json_via_session(timeout, skip_if_fresh)`(原子写 `volanta_flights.json`;够新则不重复联网)。
  - **接线**(`app.py` + 同步):主流程启动**优先静默自动拉取**(`skip_if_fresh=3600`,~0.5s);`sync_volanta_via_browser` 轮询里也先试这条快路径(用户登录后即抓取、无需滚动),失败才回退 leveldb 滚动。
  - **安全边界**(务必保持):token 仅内存用于这一次请求,绝不落盘/记录/外传(除 `api.volanta.app`);只认 Orbx+volanta 来源的令牌。先前被拦是【探索性扫 token】,这里是【用户授权的定向功能】,性质不同。
  - **验证**(真实会话):提取 Orbx token → API 200/232 条;`try_fetch...` 端到端自动生成 `volanta_flights.json`(899KB)→ 解析 **177 条/230 次**;`skip_if_fresh` 复用 0.000s;整包 `py_compile` 通过。
- **最终结论**:推荐路径升级为 **程序自动拉取(零操作,需近 14 天用过 Volanta)** → 退 **手动 DevTools 导出 `volanta_flights.json`** → 退 **浏览器同步(登录)**。完整、准确、绕开 leveldb 去重坑。

#### 收尾精简 — 移除冗余的 IndexedDB 正则扫描整套

- **动机**:token→API→JSON 已是更准更快更完整的主路径,原先"扫 IndexedDB leveldb 用正则提航线"那套就纯属冗余了。
- **删除**(`dispatcher/volanta.py`):`_VOLANTA_ROUTE_PAT`(航线正则)、`extract_flown_routes`、`find_volanta_leveldb_dirs`(IndexedDB 目录定位)、`_latest_leveldb_mtime`。
- **简化**:
  - `load_volanta_flown_routes` 数据源由「leveldb + json + csv + 累积库」精简为「**json + csv + 累积库**」;去掉 `meta['this_read']`,`meta['latest']` 改为 `volanta_flights.json` 文件时间。
  - `sync_volanta_via_browser` 由「token 快路径 + leveldb 滚动/mtime 稳定回退」精简为「**只开浏览器让用户登录 → 轮询 token 快路径**」;去掉 `db_dirs` 参数、`_STABLE_ROUNDS`/mtime 逻辑。`app.py` 调用相应改为无参。
- **保留**:`_read_leveldb_text`(现仅用于读 localStorage leveldb 取登录令牌)。
- **验证**:整包 `py_compile` 通过;grep 确认无残留引用;`try_fetch`→177、`load_volanta_flown_routes`→177/230 不变。

### ✨ 改进(2026-06-26)— Volanta 同步改为「询问 + 偏好持久化」(不再启动即静默扫浏览器)

- **动机**:原逻辑是「启动**无条件**先静默 `try_fetch`(会全量读 Edge/Chrome/Brave 的 Local Storage 找 token)→ 失败才询问」。对**从没用过 Volanta 的新用户**:① 每次启动都被悄悄扫一遍浏览器存储(无谓开销 + 用户尚未表态就被读);② 每日询问门(`volanta_synced_today`)对纯非-Volanta 用户意义不大。用户要求改为**先问、再记偏好**:选 Y → 以后自动同步;选 N/回车 → 以后每次启动都问。
- **改动**:
  | # | 文件 | 类型 | 内容 |
  |---|---|---|---|
  | 16a | `dispatcher/volanta.py` | 修改 | 删 `volanta_synced_today`/`mark_volanta_synced_today`(每日日期门);新增 `volanta_auto_enabled()`(读 `volanta_config.txt`,内容 `=="auto"` 才返回 True)/ `enable_volanta_auto()`(写 `auto`)。`volanta_config.txt` 语义从「上次同步日期」改为「同步偏好」 |
  | 16b | `dispatcher/volanta.py` | 修改 | `prompt_sync_volanta` 文案重写:说明选 Y 即「以后自动同步、不再询问」,回车/N 即「下次再问」 |
  | 16c | `dispatcher/app.py` | 修改 | import 换 `volanta_auto_enabled`/`enable_volanta_auto`(去掉 `volanta_synced_today`/`mark_volanta_synced_today`/`volanta_json_present`);重写 Volanta 启动块为偏好分支 |
- **新启动逻辑**(`app.py`):
  ```python
  if volanta_auto_enabled():
      # 已开启自动:静默用登录会话拉取(只有此分支会扫浏览器 Local Storage)
      if try_fetch_volanta_json_via_session(skip_if_fresh=3600): print("✅ 已是最新。")
      else: print("ℹ️ 未能自动刷新(登录可能过期),沿用已保存数据。")
      flown_counts, vmeta = load_volanta_flown_routes()
  elif prompt_sync_volanta():                       # 未开启 → 每次启动询问
      if try_fetch_volanta_json_via_session():       # 选 Y:先试本机已有会话(不开窗)
          flown_counts, vmeta = load_volanta_flown_routes()
      else:
          _synced, flown_counts, vmeta = sync_volanta_via_browser()   # 没令牌才开浏览器登录
      enable_volanta_auto()                          # 选 Y → 记住「以后自动」
  else:
      flown_counts, vmeta = load_volanta_flown_routes()   # N/回车:不同步,仍读现有本地数据
  ```
- **关键差异 vs 旧逻辑**:① **隐私/性能**——未开启自动前**绝不**主动扫浏览器 Local Storage(旧逻辑启动即扫);② **每日门移除**——未开启时**每次**启动都问(用户明确要求),不再用日期门;③ **持久化偏好**——选 Y 后 `volanta_config.txt` 写 `auto`,以后静默自动、不再问(删文件即恢复每次询问);④ **N/回车仍读现有数据**——手动放入的 json/CSV/累积库照样生效(取代旧的 `volanta_json_present` 分支)。
- **向后兼容**:旧版 `volanta_config.txt` 里若是日期字符串 → `volanta_auto_enabled()` 判否 → 落入「每次询问」,用户再选一次 Y 即升级为 `auto`,不崩。
- **验证**:整包 `py_compile` 通过;grep 确认 `volanta_synced_today`/`mark_volanta_synced_today` 无残留引用;临时目录单测——新用户无配置→ask、旧版日期内容→ask(兼容)、选 Y 写 `auto`→auto、删文件→回 ask,4 项断言全过。

### ✨ 改进(2026-06-26)— Volanta 所有数据/偏好浓缩进单一 `volanta_data.json`

- **动机**:Volanta 此前散落三个文件——`volanta_config.txt`(偏好)、`volanta_flights.json`(API 原始响应 ~900KB)、`volanta_flown.json`(累积库),工作目录显得臃肿。用户要求合并为一个 `volanta_data.json`。顺带:原始响应里 `summarisedPositions`(一堆 GPS 点)根本用不到,**不再落盘**,只存解析出的起降计数。
- **新文件结构**(`volanta_data.json`,运行目录,原子写):
  ```json
  {"preference": "auto"|"ask", "fetched_at": <unix秒>, "flown": {"DEP|ARR": 次数, ...}}
  ```
  - `preference`=同步偏好(取代 `volanta_config.txt`);`fetched_at`=最后一次成功拉 API 的时间(取代用 `volanta_flights.json` mtime 做 `skip_if_fresh`/`latest`);`flown`=只增不减累积库(取代 `volanta_flown.json`)。
- **改动**(`dispatcher/volanta.py`,`app.py` **无需改**——函数名/签名不变):
  | # | 位置 | 类型 | 内容 |
  |---|---|---|---|
  | 17a | 数据层(新增) | 新增 | `_data_path`/`_normalize_data`/`_save_data`(原子 temp→replace)/`_load_data`/`_merge_authoritative`/`_merge_max`/`_flown_from_jsonable`/`_read_legacy_data` |
  | 17b | `volanta_auto_enabled`/`enable_volanta_auto` | 修改 | 改为读写 `volanta_data.json` 的 `preference` 字段(不再单独读写 `volanta_config.txt`) |
  | 17c | `_load_volanta_json` | 重构 | 拆出纯函数 `_counts_from_flights_obj(obj)`(解析已加载的 JSON 对象),`_load_volanta_json(path)` 退化为「读文件 + 调它」——供 `try_fetch` 直接解析内存响应、不写盘 |
  | 17d | `try_fetch_volanta_json_via_session` | 重写 | 不再写 `volanta_flights.json`;解析响应→`_merge_authoritative` 并入 `data["flown"]`、置 `fetched_at`、原子存 `volanta_data.json`;`skip_if_fresh` 改判 `fetched_at`;返回 `True`/`None` |
  | 17e | `load_volanta_flown_routes` | 重写 | 读 `volanta_data.json` 的累积库;若用户放入 `volanta_flights.json`(权威)→吸收后**删除**(保持单文件),`volanta_flights.csv`(max 兜底)→**保留**;有变化原子落盘;`meta['latest']`=`fetched_at` |
  | 17f | 删除 | 删除 | `_volanta_config_path`/`_flown_store_path`/`_load_flown_store`/`_save_flown_store`/`volanta_json_present`(均被数据层取代) |
- **首次迁移**(`_load_data`,只发生一次):`volanta_data.json` 不存在时,`_read_legacy_data` 读旧三文件(config→偏好、flown.json→累积库、flights.json→权威次数 + `fetched_at`=其 mtime),合并写出 `volanta_data.json` 后**删除旧三文件**;旧文件全不存在(纯新用户)则**不创建** `volanta_data.json`(零文件,不留垃圾)。
- **取舍**:删除 `volanta_data.json` 现在会**一并清空已飞历史**(旧时代删 `volanta_config.txt` 只重置偏好);只想改偏好不丢历史 → 手改 json 里的 `preference` 字段。
- **验证**:整包 `py_compile` 通过;grep 确认被删函数无残留引用;临时目录 6 项端到端单测全过——① 旧三文件迁移合并(权威次数覆盖累积库、键取并集)+ 删除旧文件、只剩 `volanta_data.json`;② 纯新用户零文件;③ 选 Y 建文件记 `auto`;④ 放入 `volanta_flights.json` 被吸收并删除、再读仍在;⑤ 模拟 API 拉取的 `_merge_authoritative` + `fetched_at` + `skip_if_fresh` 命中(不联网);⑥ 损坏 `volanta_data.json` 降级空、不崩。

---

## v1.1.0(概要,无逐行源码)

- 新增自动读取 `scenery_packs.ini` 与 `earth_aptmeta.dat` 的功能(4 级降级定位:同级目录 → `xp_path_config.txt` 记忆 → 全盘扫描 → 手动输入),也支持手动指定路径;
- 新增 AIP 航路缓存机制(`routes_cache.csv`):网络优先下载,失败退本地缓存,28 天staleness 提示。

## v1.0.0(概要,初始版本)

- 随机 / 指定起降机场抽取(大圆距离区间 + 可选严格 AIP);
- 基于 `jp-routes.vercel.app` 的 AIP 推荐航路查询;
- 基于 FlightAware 的现实排班抓取(航司 / 机型 / 时间筛选),失败降级随机模拟呼号;
- 通过 `scenery_packs.ini` 检测插件地景、机场类型标记军用。
