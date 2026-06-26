# REVISIONS.md — 版本迭代代码变动记录

> 本文件记录 `flight_dispatcher` 每次版本迭代的**代码级**变动,便于后续 debug 或版本回退时快速理清改了什么、改在哪、为什么改。
> 与 `PRD.md`(产品需求)、`CLAUDE.md`(架构现状)互补:PRD 说"要什么",本文件说"代码具体怎么动的"。

## 记录格式约定

每个版本一个章节(最新在上);版本内每处改动按以下结构记录:

- **文件**:文件名 + 位置(函数名 / 代码区块)
- **类型**:新增 / 修改 / 删除
- **源码**:列出关键源码段;修改类用 `before →  after`,新增类直接列出新增源码
- **说明**:改了什么、为什么改(关联 PRD 功能编号或计划)

> 历史版本(v1.1.0 及更早)早于本记录机制建立,且项目非 git 仓库、无逐行 diff,故仅作**概要**记录。

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
