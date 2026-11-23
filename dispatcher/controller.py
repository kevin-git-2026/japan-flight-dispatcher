# ================= 控制层（编排：应用状态 / 后台任务 / 日志；零 GUI 依赖）=================
# 本模块【绝不 import 任何 GUI 框架】——不 import tkinter、不 import flet。
# 铁律：只抛异常、只返回值、只回调；【跨线程 marshal 由 UI 层负责】
#       （tkinter 的 root.after(0,…) / Flet 的 page.run_task(coro)）。
# 这些函数原本是 gui.py 里的 _init_worker / _plan_worker / _compute_proc / _volanta_worker 的主体。

import os
import time
from concurrent.futures import ThreadPoolExecutor

from .config import get_real_run_path
from .navdata import find_navdata_file, check_airac_currency, airac_cycle
from .data import load_japan_icao_set, load_airports_from_navigraph, load_aip_routes_from_csv
from .scenery import scan_installed_sceneries
from .airlines import init_airline_data
from .volanta import (
    load_volanta_flown_routes, volanta_auto_enabled, set_volanta_auto,
    try_fetch_volanta_json_via_session, _open_volanta_in_browser,
)
from .routing import calculate_distance_nm, find_aip_route, get_random_route, NoRouteError
from .flightaware import fetch_real_flights_with_filter
from .planner import build_flight_plan, parse_runway_ft, parse_dist, simbrief_url
from .router import (generate_route, route_geometry, route_length_nm, reload_transfer_points, transfer_entry,
                     splice_route_ends)
from .aircraft import perf_of
from . import procedures, weather, timed, ntrack, vatjpn, swim_atis, operations
from . import eta as eta_mod

_WIND_WAIT_SEC = 8.0     # 规划时最多等高空风多久（与 SWIM / METAR 并行取；平时 0.1–0.5 s，冷请求偶尔更久）


class NavDataMissing(Exception):
    """未找到自带的导航数据（UI 负责提示用户去 Navigraph 下载）。"""


class AppState:
    """一次会话的应用状态：初始化产出、规划时读。"""

    def __init__(self):
        self.dat_path = None
        self.scenery_map = None
        self.aip_data = []
        self.aip_index = set()
        self.flown_counts = {}
        self.volanta_auto = False
        self.volanta_meta = {}
        self.navdata_notice = None       # 本机导航数据比 VATJPN 最新移管表旧 → UI 弹窗的文案


class LogSink:
    """file-like：把 print() 汇入 UI 日志区。emit 由 UI 提供，并【由 UI 负责 marshal 回 UI 线程】。
    --windowed / --noconsole 下 sys.stdout 为 None，而复用的业务函数大量 print()，故必须在
    任何业务调用之前装好它。"""

    def __init__(self, emit):
        self._emit = emit

    def write(self, s):
        if s:
            try:
                self._emit(s)
            except Exception:
                pass
        return len(s) if s else 0

    def flush(self):
        pass


# ---------- 初始化 ----------

def init_app():
    """一次性初始化（后台线程调）：导航数据 → AIRAC → 地景扫描 → 航司 → AIP → 移管表 → Volanta。
    返回 AppState；未找到导航数据 → 抛 NavDataMissing。"""
    dat = find_navdata_file()
    if not dat:
        raise NavDataMissing()
    print(f"📁 已读取程序自带的导航数据：{os.path.relpath(dat, get_real_run_path())}")
    check_airac_currency(dat)

    valid = load_japan_icao_set(dat)
    print("🌍 正在检测已安装的机场地景（XP / MSFS，首次较慢、之后走缓存）...")
    scenery_map, cached = scan_installed_sceneries(valid_icaos=valid)
    if scenery_map is None:
        print("ℹ️ 未检测到 X-Plane / MSFS 地景目录，本次跳过地景标注。")
    else:
        xp = sum(1 for s in scenery_map.values() if "XP" in s)
        ms = sum(1 for s in scenery_map.values() if "MSFS" in s)
        print(f"🌍 已{'读取缓存' if cached else '扫描'}到 {len(scenery_map)} 个已装地景机场（XP:{xp} / MSFS:{ms}）。")

    init_airline_data()
    aip = load_aip_routes_from_csv()
    aip_index = {(row[0].strip().upper(), row[1].strip().upper())
                 for row in aip if len(row) >= 2} if aip else set()

    # VATJPN 移管表：按本机导航数据的 AIRAC 选期，期次或本机 AIRAC 变了才重建（STAR 配对按本机 CIFP 现推）。
    # 必须在任何航路生成之前——router 会缓存它，以及由它派生的进场端点学习。
    tp = vatjpn.refresh(dat, airac_cycle(dat))
    if tp["data"] is not None:
        reload_transfer_points(tp["data"])

    auto = volanta_auto_enabled()
    if auto:
        print("🔑 正在用 Volanta 登录会话自动同步已飞数据...")
        if try_fetch_volanta_json_via_session(skip_if_fresh=3600, diag=True):
            print("✅ Volanta 已飞数据已是最新。")
        else:
            print("ℹ️ 未能自动刷新，沿用已保存的数据。（如需更新，可点「同步 Volanta」）")
    flown, vmeta = load_volanta_flown_routes()

    st = AppState()
    st.dat_path = dat
    st.scenery_map = scenery_map
    st.aip_data = aip
    st.aip_index = aip_index
    st.flown_counts = flown or {}
    st.volanta_auto = bool(auto)
    st.volanta_meta = vmeta or {}
    st.navdata_notice = tp["notice"]
    return st


NAVDATA_HELP = ("未找到导航数据。\n\n请前往 https://navigraph.com/downloads 下载\n"
                "「X-Plane 12」导航数据，解压放入程序目录的 NavData 文件夹\n"
                "后重启本程序。")
NAVDATA_LOG = ("❌ 未找到导航数据。请前往 https://navigraph.com/downloads 下载「X-Plane 12」导航数据，"
               "放入程序目录的 NavData 文件夹后重启。")
NAVDATA_STALE_TITLE = "导航数据需要更新"       # 正文 = AppState.navdata_notice（vatjpn 按期次拼）


# ---------- 规划 ----------

def plan(state, f, refresh_measured=None):
    """一次完整规划（后台线程调）：选机场 → 天气与实测运用 → AIP/生成航路（按运行规则定端点）→ 各航路几何
    → FlightAware+SimBrief → 进离场预筛。返回 (FlightPlan, proc)；任何硬错误抛异常，由 UI 显示。
    refresh_measured(icaos)：UI 注入的「规划前查一次 SWIM」（LiveService.ensure_fresh）；controller 不碰 UI。"""
    min_rwy = parse_runway_ft(f["runway"])
    dmin, dmax = parse_dist(f["dmin"], f["dmax"])
    dist_blank = not ((f.get("dmin") or "").strip() or (f.get("dmax") or "").strip())   # 两栏都留空 → 200–450 只是默认值
    all_airports = load_airports_from_navigraph(state.dat_path, state.scenery_map, min_rwy)
    if not all_airports:
        raise RuntimeError("未能找到任何符合条件的机场。请检查跑道长度或数据文件。")
    strict = f["strict"]
    if strict and not state.aip_data:
        print("⚠️ 航路数据下载失败，已为您转为自由规划模式。")
        strict = False
    active = {f["sim"]} if f.get("sim") in ("XP", "MSFS") else None    # 问题1：所用模拟器(单选)

    # v2.0.1：地域限定 + 严格基于现实航班（FlightAware 逐候选核验）
    dep_region = f.get("dep_region") or None
    arr_region = f.get("arr_region") or None
    real_flight = bool(f.get("real_flight"))
    airline = f.get("airline") or ""
    rf_actype = (f.get("aircraft") or "") if real_flight else ""   # 现实航班模式且填了机型 → 机型也须匹配真实排班
    rf_who = "、".join(x for x in (f"航司 {airline}" if airline else "",
                                   f"机型 {rf_actype}" if rf_actype else "") if x)  # 日志用的「航司/机型」描述
    # 现实航班模式的口径是「地域 + AIP 已公布航路 + FlightAware」，本不含航程限制。
    # 若用户没填航程，就【不套默认 200-450 窗】（否则关东→琉球这类长途地域对全被卡掉、池子清空）；
    # 用户显式填了航程才按其筛。
    if real_flight and not ((f.get("dmin") or "").strip() or (f.get("dmax") or "").strip()):
        dmin, dmax = 0.0, 1e9

    def _has_real_flight(d_obj, a_obj):
        """该航线现实中是否真有【符合条件】的航班：填了航司→该航司必须在飞；填了机型→须有该机型的真实排班
        （这样航司↔机型才现实自洽，如 JAL 不飞 A320）；都没填→任一直飞航班。机型匹配走 FlightAware 的
        filter_aircraft（is_aircraft_match 模糊到机型族），返回值 matched=同时满足航司+机型。"""
        try:
            matched, flights = fetch_real_flights_with_filter(d_obj.code, a_obj.code, airline, rf_actype)
            return bool(matched) if rf_actype else bool(flights)   # 填了机型→须 matched(航司+机型都中)；否则任一真实航班
        except Exception:
            return False                                # 网络/抓取失败按「未核实到」处理，继续换候选

    if f["dep"] and f["dest"]:
        dep_obj = next((a for a in all_airports if a.code == f["dep"]), None)
        arr_obj = next((a for a in all_airports if a.code == f["dest"]), None)
        if not dep_obj or not arr_obj:
            raise RuntimeError("找不到指定机场（检查 ICAO 是否正确、跑道是否够长）。")
        dist = calculate_distance_nm(dep_obj, arr_obj)
        route = find_aip_route(state.aip_data, f["dep"], f["dest"]) if state.aip_data else None
        if strict and not route:
            raise RuntimeError("未查到该航线的 AIP 航路。")
        flown_count = state.flown_counts.get((f["dep"], f["dest"]), 0)
        if real_flight and not _has_real_flight(dep_obj, arr_obj):   # 两端固定→不改端，仅提示
            who = (rf_who + " ") if rf_who else ""
            print(f"⚠️ 严格基于现实航班：未在 FlightAware 查到 {who}执飞 {dep_obj.code}→{arr_obj.code} 的直飞航班"
                  "（该航线可能无真实排班/机型不符，或 FlightAware 无覆盖）。已照常规划，请自行核对。")
    else:
        def _route_len(d_obj, a_obj):
            """候选航线的真实航路长(NM)：优先官方 AIP(取最短变体)，否则本地生成航路；都没有→None。"""
            rows = ([r for r in state.aip_data
                     if len(r) > 5 and r[0].strip().upper() == d_obj.code
                     and r[1].strip().upper() == a_obj.code] if state.aip_data else [])
            best_len = None
            for r in rows:
                rs = r[5].strip()
                if not rs:
                    continue
                try:
                    pts = route_geometry(d_obj, a_obj, rs, state.dat_path)
                    L = route_length_nm(pts) if pts else None
                except Exception:
                    L = None
                if L and (best_len is None or L < best_len):
                    best_len = L
            if best_len is not None:
                return best_len
            if strict:                                  # 严格模式只认 AIP，不走生成
                return None
            try:
                g = generate_route(d_obj, a_obj, dat_path=state.dat_path,
                                   aip_data=state.aip_data, airports=all_airports)
                return g["dist_nm"] if g else None
            except Exception:
                return None
        accept_fn = on_verify = on_giveup = None
        if real_flight:
            def on_verify(dc, ac, i, cap):
                who = (rf_who + " ") if rf_who else ""
                print(f"🔎 严格基于现实航班：核实 {dc}→{ac} 是否有 {who}真实航班…（第 {i}/{cap} 条）")
            def on_giveup(cap):
                who = (rf_who + " 的") if rf_who else ""
                print(f"⚠️ 已核验 {cap} 条候选，均未在 FlightAware 查到{who}真实直飞航班"
                      "（现实无此排班/机型不符，或部分小机场·时段 FlightAware 无覆盖）。已返回一条候选，请自行核对。")
            accept_fn = _has_real_flight
        # ⚠️ real_flight 模式的候选池收敛（用户提出的更优思路）：**现实中在飞的航线必然是 AIP 已公布航路**，
        #    所以直接【只在 AIP 表里两端都在所选地域的机场对中抽】，再逐对 FlightAware 核实——
        #    池子小、几乎全是真航线(命中率高)、还快。等价于强制 strict_aip 过滤（复用现成的 aip_index 机制）。
        #    实测 关东→琉球：AIP 限定后 11 对(全是 ANA/JAL 干线、含 RJTT→ROAH)，而枚举距离要 40 对(混入大島→那霸等没人飞的)。
        #    有 AIP 数据才强制；下载失败则退回距离枚举(仍 FlightAware 核实)，不至于把池子清空。
        pick_strict = strict or (real_flight and bool(state.aip_index or state.aip_data))
        # real_flight 模式【不再叠加真实航路长窗口校验】——只按大圆距离入窗(枚举阶段) + AIP 限定 + FlightAware 现实核验。
        #    否则「大圆入窗、真实航路长略超窗上沿」的干线会被【静默剔除在 FlightAware 核验之前】：
        #    RJTT→ROAH 大圆 840(进 200-850 窗)、真实航路长 876(超窗)→ 压根没被核验、程序误报「查不到」，而 ANA 天天飞。
        route_len_fn = None if real_flight else _route_len
        if real_flight and pick_strict and not strict:
            print("🎯 严格基于现实航班：仅在 AIP 已公布航路的机场对中抽取（现实在飞的航线必有官方航路）。")
        def _pick(lo, hi):
            return get_random_route(
                all_airports, lo, hi, state.aip_data, pick_strict, f["dep"], f["dest"],
                state.flown_counts, state.aip_index, require_both_scenery=f["scenery_only"],
                active_sims=active, route_len_fn=route_len_fn,
                dep_region=dep_region, arr_region=arr_region,
                accept_fn=accept_fn, on_verify=on_verify, on_giveup=on_giveup)
        try:
            dep_obj, arr_obj, dist, route, flown_count = _pick(dmin, dmax)
        except NoRouteError as e:
            # 航程两栏都留空 = 用户没限航程，200–450 只是默认值：被它清空（熊本 → 北海道 716–976 NM、琉球 ↔ 关东、
            # 同一地域内的短途）就不限航程再抽一次（用户定：先按默认窗口、抽不到再放开）。填了航程的照原样报具体原因。
            if not (dist_blank and e.reason == "distance"):
                raise
            print("ℹ️ 航程留空时默认抽 %.0f–%.0f NM，但%s%s都在 %s，已不限航程重新抽取。"
                  % (dmin, dmax, " " if e.subject[:1].isascii() else "", e.subject, e.range_text))
            dep_obj, arr_obj, dist, route, flown_count = _pick(0.0, 1e9)

    # 各 AIP 航路：长度（与 find_aip_route 同序）+ 地图航点（每条一份）+ 航点(供 F21 端点预筛复用)
    # + 进近移管（F32：这条航路对上 VATJPN 移管表的哪个到着组 → 移管点 / 高度 / 席位）
    # 放在取天气之前：F36 的高空风按这些航路的几何取样（与大圆一起一次取完）
    matched = aip_dists = aip_maps = aip_pts = aip_xfers = None
    gen_map = gen_xfer = None
    tp_arr = transfer_entry(arr_obj.code)
    if route:
        matched = [r for r in state.aip_data
                   if len(r) > 5 and r[0].strip().upper() == dep_obj.code and r[1].strip().upper() == arr_obj.code]
        aip_dists, aip_maps, aip_pts, aip_xfers = [], [], [], []
        for r in matched:
            rs = r[5].strip()
            try:
                pts = route_geometry(dep_obj, arr_obj, rs, state.dat_path) if rs else None
            except Exception:
                pts = None
            aip_pts.append(pts)
            aip_dists.append(route_length_nm(pts) if pts else None)
            aip_maps.append((pts, "%s→%s  %s" % (dep_obj.code, arr_obj.code, rs)) if pts else None)
            aip_xfers.append(vatjpn.match_arrival(tp_arr, rs, [p[0] for p in pts or ()]))

    # F36：高空风与 SWIM / METAR 并行取（ETA 推算用）；最多等 _WIND_WAIT_SEC，取不到就按无风算
    pool = ThreadPoolExecutor(max_workers=1)
    t_wind = time.time()
    wind_job = pool.submit(weather.fetch_winds_aloft, wind_points(dep_obj, arr_obj, aip_pts))
    pool.shutdown(wait=False)

    # 天气与实测运用状况放在航路生成之前：生成航路要按运行规则 / 实测定端点（面板也用这同一份）
    eobt_min = operations.default_eobt_min()
    if refresh_measured:
        try:
            refresh_measured([dep_obj.code, arr_obj.code])   # 规划也算一次用户发起的 SWIM 查询（用户定）
        except Exception as e:                          # noqa: BLE001
            print(f"⚠️ 查询 SWIM 失败（已忽略）: {e}")
    print("🌦️ 正在获取机场天气（METAR / 网格回退）…")
    dep_wx = weather.resolve_airport_wx(dep_obj.code, dep_obj.lat_dd, dep_obj.lon_dd)
    arr_wx = weather.resolve_airport_wx(arr_obj.code, arr_obj.lat_dd, arr_obj.lon_dd)
    nt = measured_configs(dep_obj, arr_obj)
    gc_nm = calculate_distance_nm(dep_obj, arr_obj)
    try:
        winds = wind_job.result(timeout=max(0.0, _WIND_WAIT_SEC - (time.time() - t_wind)))
    except Exception:                                   # noqa: BLE001  超时 / 网络失败 → 按无风
        winds = None
    em = eta_model(dep_obj, arr_obj, f.get("aircraft"), winds)
    print("⏱️ " + eta_log(em, eobt_min))
    ends_at = EndsAt(dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, state.dat_path)
    ends = ends_at(em.eta_jst(eobt_min))                 # 航路还没有：按大圆几何的 ETA 定端点（生成航路用）

    # F15：无 AIP 航路且非严格模式 → 用本地导航数据 A* 生成一条参考航路（两分支统一在此处理）
    generated = generated_warn = generated_dist = gr = None
    if route is None and not strict:
        try:
            gr = generate_by_rules(state, dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, all_airports, ends=ends,
                                   em=em, ends_at=ends_at)
            if gr:
                generated = gr["route_str"]
                generated_dist = gr["dist_nm"]
                # warn 有就显示 —— 两种：① 含大角度转弯（suspect）② 短程已降级为直飞（source='direct'）。
                # router 出的是【完整成句】的提示，UI 只负责画，不再自己拼后缀。
                generated_warn = gr.get("warn") or None
                print("🧭 无 AIP 航路，已用本地导航数据生成参考航路"
                      + ("（短程降级为直飞）。" if gr.get("source") == "direct" else "。"))
            else:
                print("ℹ️ 本地导航数据未能连通该航线，跳过航路生成。")
        except Exception as e:
            print(f"⚠️ 航路生成失败（已忽略）: {e}")   # 绝不让生成中断规划
    if not route and generated and gr and gr.get("coords"):
        gen_map = (gr["coords"], "%s→%s  %s" % (dep_obj.code, arr_obj.code, generated))
        gen_xfer = vatjpn.match_arrival(tp_arr, generated, [p[0] for p in gr["coords"]])

    # AIP 不适用于本时段 / 本运行时：按运行规则改端（每行各一条，保留各自的高度 / 机型条件）
    alts = None
    if matched:
        alts = aip_alternatives(state, dep_obj, arr_obj, matched, aip_dists, aip_pts, ends_at, eobt_min, em,
                                gc_nm, strict, nt)
        for v in (alts.get("rows") or ()):
            if v and v.get("route"):
                print("🌙 %s" % v["log"])

    print("🔎 正在拉取现实排班...")
    fp = build_flight_plan(dep_obj, arr_obj, dist, route,
                           f["airline"], f["aircraft"], f["time"], flown_count,
                           generated_route=generated, generated_route_warn=generated_warn,
                           timed_route=_alt_sb_route(alts, matched),
                           generated_route_dist=generated_dist, aip_route_dists=aip_dists,
                           aip_maps=aip_maps, gen_map=gen_map, aip_xfers=aip_xfers, gen_xfer=gen_xfer)
    fp.active_sims = active                             # 问题1：渲染按所用模拟器标注地景
    fp.generated_route_ops = (gr or {}).get("ops")
    fp.aip_alts = (alts or {}).get("rows")
    fp.aip_alt_note = (alts or {}).get("note")
    # F36：结果卡每条航路的预计飞行时间（EOBT → 落地，按航路几何 + 该行高度条件）
    fp.aip_etas = ([em.estimate(eobt_min, p, alt_cond=_alt_of(r)) if p else None
                    for r, p in zip(matched, aip_pts)] if matched else None)
    fp.gen_eta = em.estimate(eobt_min, gr.get("pts")) if (gr and gr.get("pts") and generated) else None
    proc = compute_proc(state, dep_obj, arr_obj, generated,   # F20/F21：逐 AIP 候选预筛跑道/SID/STAR
                        matched=matched, aip_dists=aip_dists, aip_pts=aip_pts,
                        strict_ops=f.get("strict_ops"), gr=gr,
                        dep_wx=dep_wx, arr_wx=arr_wx, nt=nt, eobt_min=eobt_min, alts=alts)
    proc["aip_strict"] = bool(strict)
    proc["aip_gc"] = gc_nm
    proc["eta"] = em
    return fp, proc


def measured_configs(dep_obj, arr_obj):
    """实测运用状况 {ICAO: cfg}：SWIM 真实 ATIS（23 场，未过期的报文）优先，其次国土交通省 ntrack（羽田）。
    cfg 形如 {time_jst, approaches, ldg, dep}。取到就是选规则 / 定端点 / 进离场预选的首选依据。"""
    nt = {}
    for obj in (dep_obj, arr_obj):
        if obj.code in nt:
            continue
        cfg = swim_atis.store.config(obj.code)
        if not cfg and ntrack.supports(obj.code):
            cfg = ntrack.fetch_latest(obj.code)
        if cfg:
            nt[obj.code] = cfg
    return nt


def _wind_sky(wx):
    wx = wx or {}
    return wx.get("wind"), weather.parse_sky(wx.get("metar_raw"))


def _alt_of(row):
    """AIP 原始行的高度条件（与 timed.annotate_routes 的 alt 同一个值：面板 / F21 按它算该行的巡航高度）。"""
    return ((row[3] if len(row) > 3 else "") or "").strip()


def wind_points(dep_obj, arr_obj, geoms=()):
    """高空风的取样点：大圆 + 各条 AIP 航路几何，每约 50 NM 一点（weather.fetch_winds_aloft 再吸附 / 去重）。"""
    pts = eta_mod.gc_points((dep_obj.lat_dd, dep_obj.lon_dd), (arr_obj.lat_dd, arr_obj.lon_dd), 50.0)
    for g in geoms or ():
        if g:
            pts += eta_mod.resample(g, 50.0)
    return pts


def eta_model(dep_obj, arr_obj, aircraft_text, winds):
    """F36：一次规划的 ETA 模型（机型剖面 aircrafts.json · perf + 高空风）。机型留空 / 查不到 → 按默认机型估。"""
    perf, aid, fallback = perf_of(aircraft_text)
    return eta_mod.EtaModel(dep_obj, arr_obj, perf, eta_mod.Winds(winds) if winds else None, aid, fallback)


def eta_log(em, eobt_min):
    """规划日志里的一行 ETA 依据（按大圆）。"""
    r = em.estimate(eobt_min)
    who = ("%s（未填 / 查不到机型，按 %s 估）" % (em.ac_id, em.ac_id)) if em.fallback else (em.ac_id or "机型库缺失")
    if r["source"] == "legacy":
        return "ETA 按旧算法估（机型库缺失：EOBT + 15 分 + 航程 ÷ 450 kt）"
    wind = ("高空风 Open-Meteo %s" % em.winds.model) if em.winds else "未取到高空风，按无风估"
    return ("ETA 估算：%s · 按大圆 FL%d · EOBT %s 起约 %d 分钟落地 · %s"
            % (who, r["fl"], _hhmm(eobt_min), round(r["block"]), wind))


class EndsAt:
    """按 ETA 取两端限定（离场只随 EOBT 变、到达随 ETA 变）；同一个 ETA 只算一次。
    F36：每条航路按自己的 ETA 选到达端的规则，AIP 各行 / 生成航路 / 改端版都从这里取。"""

    def __init__(self, dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, dat_path, apply_ops=True, ops_data=None,
                 measured_default=True):
        self.args = (dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, dat_path, apply_ops, ops_data)
        self.measured_default = measured_default
        self._memo = {}

    def __call__(self, eta_jst):
        k = int(eta_jst)
        if k not in self._memo:
            self._memo[k] = rule_endpoints(*self.args, eta_jst=k, measured_default=self.measured_default)
        return self._memo[k]


def rule_endpoints(dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, dat_path, apply_ops=True, ops_data=None,
                   eta_jst=None, measured_default=True):
    """生成航路两端的限定 (离场, 到达)：operations.route_endpoints 的结果或 None。
    离场按 EOBT、到达按 ETA（eta_jst；F36 由 EtaModel 按该航路的几何算，不给 → 旧算法按大圆）；
    实测优先、对不上以实测为主、没有实测按规则（用户定）。实测是否算数：operations.measured_at（离场按 EOBT、到达按 ETA）。
    apply_ops=False（面板关了「按机场运行规则预选」）→ 都不限定。ops_data：面板手上那份规则（不传 → 现读文件）。"""
    if not apply_ops:
        return None, None
    try:
        ops = operations.load_operations() if ops_data is None else ops_data
        dw, ds = _wind_sky(dep_wx)
        aw, asky = _wind_sky(arr_wx)
        dctx, actx = operations.rule_contexts(eobt_min, calculate_distance_nm(dep_obj, arr_obj), dw, ds, aw, asky,
                                              eta_jst=eta_jst)
        nd = operations.measured_at((nt or {}).get(dep_obj.code), dctx, measured_default)
        na = operations.measured_at((nt or {}).get(arr_obj.code), actx, measured_default)
        ed = operations.route_endpoints(ops, dep_obj.code, "dep", dctx, nd, dat_path)
        ea = operations.route_endpoints(ops, arr_obj.code, "arr", actx, na, dat_path)
        return ed, ea
    except Exception as e:                              # noqa: BLE001
        print(f"⚠️ 运行规则定端点失败（已按最短规划）: {e}")
        return None, None


def _fixes(e):
    return frozenset(e["fixes"]) if e else None


def _ops_basis(dep_obj, arr_obj, route_str, ed, ea, eobt_min, nt, dat_path, unreached=(), note=None):
    """结果卡写依据用的 ops：两端按哪条规则 / 哪份实测，实际接上的是哪几条程序（航路起点 / 终点能衔接的规则程序）。
    生成航路与 AIP 改端共用，写法由 viewmodel.rule_basis_lines 定。"""
    exit_, entry = procedures.sid_star_endpoints(route_str)
    used = {}
    for side, e, icao, kind, pt in (("dep", ed, dep_obj.code, "SID", exit_), ("arr", ea, arr_obj.code, "STAR", entry)):
        used[side] = [n for n in (e or {}).get("procs", [])
                      if pt and pt in procedures.connect_idents(icao, kind, [n], dat_path=dat_path)]
    return {"dep": ed, "arr": ea, "dep_icao": dep_obj.code, "arr_icao": arr_obj.code,
            "eobt_min": eobt_min, "nt": {k: v.get("time_jst", "") for k, v in (nt or {}).items()},
            "dep_point": exit_, "arr_point": entry, "dep_used": used["dep"], "arr_used": used["arr"],
            "unreached": list(unreached or []), "note": note}


def generate_by_rules(state, dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, airports=None, apply_ops=True,
                      ends=None, em=None, ends_at=None, measured_default=True):
    """按运行规则 / 实测定端点后生成航路；附上 ops（依据，给结果卡）、ends_key（面板据此判断改 EOBT 后要不要重算）
    与 pts（航路几何，面板按它算 ETA）。ends：plan() 已算好的 (离场, 到达) 端点；不传就按大圆 ETA 现算。
    F36：航路生成出来后按它自己的几何再算一次 ETA；到达端该受的限定变了（例如 ETA 晚到跨进深夜时段）→ 按新 ETA
    再生成【一次】，采用第二次的结果（用户定：最多重算一次）。"""
    if ends_at is None:
        ends_at = EndsAt(dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, state.dat_path, apply_ops,
                         measured_default=measured_default)
    if ends is None:
        ends = ends_at(em.eta_jst(eobt_min)) if em else rule_endpoints(
            dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, state.dat_path, apply_ops,
            measured_default=measured_default)
    ed, ea = ends

    def gen(ed, ea):
        return generate_route(dep_obj, arr_obj, dat_path=state.dat_path, aip_data=state.aip_data, airports=airports,
                              dep_fixes=ed["fixes"] if ed else None, arr_fixes=ea["fixes"] if ea else None)
    gr = gen(ed, ea)
    if not gr:
        return gr
    pts = route_geometry(dep_obj, arr_obj, gr["route_str"], state.dat_path)
    if em and apply_ops:
        eta2 = em.eta_jst(eobt_min, pts)
        ed2, ea2 = ends_at(eta2)
        if (_fixes(ed2), _fixes(ea2)) != (_fixes(ed), _fixes(ea)):
            gr2 = gen(ed2, ea2)
            if gr2:
                print("⏱️ 按生成航路自身的 ETA %s 重新选了到达端的运行规则，已重新生成一次：%s → %s"
                      % (_hhmm(eta2), gr["route_str"], gr2["route_str"]))
                gr, ed, ea = gr2, ed2, ea2
                pts = route_geometry(dep_obj, arr_obj, gr["route_str"], state.dat_path)
    gr["pts"] = pts
    gr["ends_key"] = (_fixes(ed), _fixes(ea))
    # 依据里的实测时刻只在这一端真用上实测时才显示（e["measured"]），不在报文覆盖时段内的那端自然不写
    gr["ops"] = _ops_basis(dep_obj, arr_obj, gr["route_str"], ed, ea, eobt_min, nt, state.dat_path,
                           gr.get("ops_unreached") or [], gr.get("ops_note"))
    return gr


# ---------- AIP 改端：AIP 不适用于本时段 / 本运行时 ----------

def _hhmm(m):
    return "%02d:%02d" % (m // 60 % 24, m % 60)


def _alt_why(dep_obj, arr_obj, row, fit, sides, ed, ea, eobt_min, eta_min):
    """这一行为什么不能照用 → 一句话（结果卡与日志用）。"""
    parts = []
    if not fit["time_ok"]:
        parts.append("本次 EOBT %s · ETA %s 不在该 AIP 时段内（%s）"
                     % (_hhmm(eobt_min), _hhmm(eta_min), timed.describe_restriction(row[2] if len(row) > 2 else "")))
    lead, trail = procedures.lead_trail_fixes(row[5] if len(row) > 5 else "")
    for side, e, icao, pts, word in (("dep", ed, dep_obj.code, lead, "起点"), ("arr", ea, arr_obj.code, trail, "末端")):
        if side in sides and e:
            rules = "、".join("「%s」" % r for r in e.get("rules") or []) or "（实测）"
            parts.append("%s 此刻生效的运行规则%s的程序接不上 AIP %s %s"
                         % (icao, rules, word, "/".join((pts or [])[-1:] if side == "dep" else (pts or [])[:1])))
    return "；".join(parts)


def aip_row_etas(em, eobt_min, rows, geoms):
    """每条 AIP 行自己的 ETA（JST 当日分钟）：按该行原 AIP 的几何 + 该行的高度条件；几何缺失 → 大圆。"""
    return [em.eta_jst(eobt_min, g or None, alt_cond=_alt_of(r)) for r, g in zip(rows, geoms)]


def aip_alternatives(state, dep_obj, arr_obj, rows, aip_dists, aip_pts, ends_at, eobt_min, em, gc_nm,
                     strict=False, nt=None):
    """AIP 改端（用户提出：深夜到羽田时「大体和 AIP 一致，但是最后一段有不同」）。
    → {key, rows, note, default}：
      key   = operations.aip_key（面板改 EOBT / 规则开关后比它，变了就后台重算）；
      rows  = None（有适用的 AIP 行 → 照旧，结果与以前逐字一致）或与 AIP 行同序的列表，每项为
              None / 改端版 {route, dist, pts, map, xfer, ops, why, log} / 改不了 {note}；
      note  = 整体说明（严格 AIP 模式不改端时）；default = 默认选中哪一行（第一条改端行）或 None。
    触发：没有一条 AIP 行既在时段内、又能接上此刻生效规则 / 实测的程序（operations.aip_splice_ends）；
    只改冲突的那一端（router.splice_route_ends）。
    F36：每行按自己的 ETA（原 AIP 几何）判时段、选到达端的规则；改端版按自己的几何再算一次 ETA，
    到达端该受的限定变了 → 按新的限定再改端【一次】（用户定：最多重算一次）。"""
    geoms = list(aip_pts or [None] * len(rows))
    etas = aip_row_etas(em, eobt_min, rows, geoms)
    ed = ends_at(etas[0] if etas else em.eta_jst(eobt_min))[0]
    eas = [ends_at(e)[1] for e in etas]
    fits = operations.aip_fit(rows, eobt_min, etas, ed, eas)
    out = {"key": operations.aip_key(fits, ed, eas), "rows": None, "note": None, "default": None}
    plan_sides = operations.aip_splice_ends(fits, ed, eas, gc_nm)
    if plan_sides is None:
        return out
    if strict:
        out["note"] = ("本次 EOBT %s · ETA %s 没有适用的 AIP 航路（时段或运行规则不符）；"
                       "已勾选「严格要求 AIP 规定航路」，不改端，仍按 AIP 显示"
                       % (_hhmm(eobt_min), " / ".join(sorted({_hhmm(e) for e in etas}))))
        return out
    tp_arr = transfer_entry(arr_obj.code)
    res_rows = []
    for i, (row, fit, sides, ea, eta_min) in enumerate(zip(rows, fits, plan_sides, eas, etas)):
        why = _alt_why(dep_obj, arr_obj, row, fit, sides, ed, ea, eobt_min, eta_min)
        rs = row[5].strip() if len(row) > 5 else ""
        if not sides:
            res_rows.append({"note": why + "；两端没有与之冲突的运行规则，无从改端 → 仍按 AIP 显示，请自行核对"})
            continue

        def splice(ea_):
            try:
                return splice_route_ends(dep_obj, arr_obj, rs, state.dat_path, state.aip_data,
                                         dep_fixes=ed["fixes"] if "dep" in sides else None,
                                         arr_fixes=ea_["fixes"] if ("arr" in sides and ea_) else None)
            except Exception as e:                      # noqa: BLE001
                return None, "改端出错（%s）" % type(e).__name__
        res, fail = splice(ea)
        if res and "arr" in sides:
            ea2 = ends_at(em.eta_jst(eobt_min, res["coords"], alt_cond=_alt_of(row)))[1]
            if ea2 and _fixes(ea2) != _fixes(ea):        # 改端版自己的 ETA 落进了另一组到达规则 → 按它再改一次
                res2, _f2 = splice(ea2)
                if res2:
                    print("⏱️ AIP [%d] 改端版的 ETA 落进另一组到达规则，已按它重新改端一次：%s → %s"
                          % (i + 1, res["route_str"], res2["route_str"]))
                    res, ea = res2, ea2
        if not res:
            res_rows.append({"note": why + "；改端失败：%s → 仍按 AIP 显示，请自行核对" % fail})
            continue
        route_s = res["route_str"]
        pts = res["coords"]
        ops = _ops_basis(dep_obj, arr_obj, route_s, ed if "dep" in sides else None,
                         ea if "arr" in sides else None, eobt_min, nt, state.dat_path)
        aip_len = aip_dists[i] if aip_dists and i < len(aip_dists) else None
        where = "、".join(x for x in (("离场端在 %s 汇入 AIP" % res["join"]) if res.get("join") else "",
                                       ("进场端在 %s 离开 AIP" % res["branch"]) if res.get("branch") else "") if x)
        res_rows.append({"route": route_s, "dist": res["dist_nm"], "pts": pts, "ops": ops, "why": why, "where": where,
                         "aip_route": rs, "aip_dist": aip_len,
                         "eta": em.estimate(eobt_min, pts, alt_cond=_alt_of(row)),
                         "map": (pts, "%s→%s  %s" % (dep_obj.code, arr_obj.code, route_s)),
                         "xfer": vatjpn.match_arrival(tp_arr, route_s, [p[0] for p in pts]),
                         "log": "%s→%s AIP [%d] 改端（%s）：%s → %s" % (dep_obj.code, arr_obj.code, i + 1, where,
                                                                      rs, route_s)})
        if out["default"] is None:
            out["default"] = i
    out["rows"] = res_rows
    return out


def _alt_sb_route(alts, rows):
    """改端时 SimBrief 默认航路 = 默认选中那行的改端串；没改端 → None（照旧用首条 AIP）。"""
    if not alts or not alts.get("rows") or alts.get("default") is None:
        return None
    return alts["rows"][alts["default"]]["route"]


def _aip_candidates(state, dep_obj, arr_obj, orig, alts):
    """面板候选：有改端版的行换成改端版（航路 / 长度 / 几何 / 端点预筛都换），时段 / 高度 / 机型保留，
    另记 variant=True 与原 AIP 串；其余行原样。"""
    rows = (alts or {}).get("rows") or []
    out = []
    for i, c in enumerate(orig):
        v = rows[i] if i < len(rows) else None
        if v and v.get("route"):
            dr, dm, ar, am, gate = _prefilter(state, dep_obj, arr_obj, v["route"], v["pts"])
            out.append({**c, "route": v["route"], "dist": v["dist"], "pts": v["pts"],
                        "dep_rows": dr, "dep_matched": dm, "arr_rows": ar, "arr_matched": am, "arr_gate": gate,
                        "variant": True, "aip_route": c.get("route"), "variant_why": v.get("why")})
        else:
            out.append(c)
    return out


def regenerate_aip(state, dep_obj, arr_obj, rows, orig, eobt_text, dep_wx, arr_wx, nt, apply_ops, strict,
                   gc_nm, ops_data=None, em=None, measured_default=True):
    """面板改了 EOBT / 运行规则开关、AIP 的适用性跟着变了 → 重算改端（后台线程调）。
    → {kind, key, alts, candidates, selected, sb_route}。rows / orig = 规划时的 AIP 原始行与原候选。
    em = 规划时的 EtaModel（面板的同一个）；nt = 全部实测，measured_default = EOBT 是否还是规划时的默认值
    （改过 → 实测只在报文覆盖的时段内算数，离场按 EOBT、到达按 ETA）。
    sb_route = 结果卡 SimBrief 链接的航路：改端 → 默认行的改端串；不改端 → 首条 AIP（与规划时 build_flight_plan 相同）。"""
    j = timed.parse_hhmm(eobt_text) if eobt_text else None
    eobt_min = j if j is not None else operations.default_eobt_min()
    if em is None:
        em = eta_model(dep_obj, arr_obj, "", None)
    ends_at = EndsAt(dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, state.dat_path, apply_ops, ops_data,
                     measured_default)
    alts = aip_alternatives(state, dep_obj, arr_obj, rows, [c.get("dist") for c in orig], [c.get("pts") for c in orig],
                            ends_at, eobt_min, em, gc_nm, strict, nt)
    return {"kind": "aip", "key": alts["key"], "alts": alts,
            "candidates": _aip_candidates(state, dep_obj, arr_obj, orig, alts),
            "selected": alts["default"],
            "aip_etas": [em.estimate(eobt_min, c.get("pts"), alt_cond=_alt_of(r)) if c.get("pts") else None
                         for r, c in zip(rows, orig)],
            "sb_route": _alt_sb_route(alts, rows) or ((rows[0][5].strip() or None) if rows and len(rows[0]) > 5 else None)}


def _prefilter(state, dep_obj, arr_obj, base_route, pts):
    """一条航路串 → (dep_rows, dep_matched, arr_rows, arr_matched, arr_gate)。pts 有则复用其航点、否则现算。"""
    route_fixes = []
    try:
        if pts is None and base_route:
            pts = route_geometry(dep_obj, arr_obj, base_route, state.dat_path)
        if pts:
            route_fixes = [p[0] for p in pts[1:-1]]      # 去首尾机场，留 enroute
    except Exception:
        route_fixes = []
    # 端点从航路串的【航路名结构】定：SID 出口=第一个航路名前的点、STAR 入口=最后一个航路名后的点。
    # 不能拿 route_fixes[0]/[-1]——串开头的 SID 机体被逐点展开(TIGER SUMAR AYAME SETOH)会让
    # 预筛只匹配到裸 SID(无过渡)；真正的 SID 出口是它们之后、第一个航路名前的那个点(SOUJA)。
    dep_exit, arr_entry = procedures.sid_star_endpoints(base_route)

    def _order(fixes, end):                              # 把端点排到 [0]（在则切片去掉端点前的终端区点，不在则前插）
        if end and end in fixes:
            return fixes[fixes.index(end):]
        return ([end] if end else []) + fixes
    try:
        dr, dm = procedures.matching_choices(dep_obj.code, state.dat_path, _order(route_fixes, dep_exit), "dep")
    except Exception:
        dr, dm = [], False
    try:
        ar, am = procedures.matching_choices(arr_obj.code, state.dat_path,
                                             _order(list(reversed(route_fixes)), arr_entry), "arr")
    except Exception:
        ar, am = [], False
    # STAR 入口 = 本次实际用的【进场门】。带上它，面板才能查 VATJPN 的「门↔STAR 配对」去预选 STAR
    gate = arr_entry or (route_fixes[-1] if route_fixes else None)
    return dr, dm, ar, am, gate


def generated_candidate(state, dep_obj, arr_obj, gr):
    """生成航路 → 面板的单个候选（无时段/机型/高度）。带 generated / ends_key，面板据此在改 EOBT 时判断要不要重算。"""
    generated = gr["route_str"]
    gpts = gr.get("pts")                                 # generate_by_rules 已按航路串算好（面板按它算 ETA，须同一份）
    if gpts is None:
        try:
            gpts = route_geometry(dep_obj, arr_obj, generated, state.dat_path)
        except Exception:
            gpts = None
    dr, dm, ar, am, gate = _prefilter(state, dep_obj, arr_obj, generated, gpts)
    return {"route": generated, "restr": "", "alt": "", "aircraft": "", "dist": None, "pts": gpts,
            "dep_rows": dr, "dep_matched": dm, "arr_rows": ar, "arr_matched": am, "arr_gate": gate,
            "generated": True, "ends_key": gr.get("ends_key", (None, None))}


def compute_proc(state, dep_obj, arr_obj, generated, matched=None,
                 aip_dists=None, aip_pts=None, strict_ops=False, gr=None,
                 dep_wx=None, arr_wx=None, nt=None, eobt_min=None, alts=None):
    """F20/F21（后台线程）：为每条 AIP 航路（或生成航路）预算端点预筛的跑道/SID·STAR。
    matched=该航线全部 AIP 原始行（>1 条→用户在弹窗按 EOBT/机型/高度选或定唯一）；aip_pts/aip_dists 与之同序
    （复用 plan() 已算几何、免重算）。天气 / 实测运用 / EOBT 由 plan() 先取好传进来（没传就现取）。
    alts = aip_alternatives 的结果：有改端版的行在面板里用改端版，默认选中第一条改端行。
    任一步失败都不影响主规划（返回空/None，UI 优雅降级）。"""
    candidates = []
    orig = []
    if matched:                                          # AIP 分支：逐条候选（含时段/高度/机型 + 端点预筛）
        for i, anno in enumerate(timed.annotate_routes(matched)):
            pts = aip_pts[i] if (aip_pts and i < len(aip_pts)) else None
            dist = aip_dists[i] if (aip_dists and i < len(aip_dists)) else None
            dr, dm, ar, am, gate = _prefilter(state, dep_obj, arr_obj, anno["route"], pts)
            orig.append({**anno, "dist": dist, "pts": pts,   # pts 供全段航路预览复用 enroute 几何
                         "dep_rows": dr, "dep_matched": dm, "arr_rows": ar, "arr_matched": am,
                         "arr_gate": gate})
        candidates = _aip_candidates(state, dep_obj, arr_obj, orig, alts)
    elif generated:                                      # 生成航路：单候选
        candidates.append(generated_candidate(state, dep_obj, arr_obj, gr or {"route_str": generated}))

    if dep_wx is None or arr_wx is None:
        print("🌦️ 正在获取机场天气（METAR / 网格回退）…")
        dep_wx = weather.resolve_airport_wx(dep_obj.code, dep_obj.lat_dd, dep_obj.lon_dd)
        arr_wx = weather.resolve_airport_wx(arr_obj.code, arr_obj.lat_dd, arr_obj.lon_dd)
    if nt is None:
        nt = measured_configs(dep_obj, arr_obj)
    default = (alts or {}).get("default")
    return {
        "aip_candidates": candidates, "selected": default if default is not None else 0,
        "strict_ops": bool(strict_ops),
        "dep_wx": dep_wx, "arr_wx": arr_wx, "ntrack": nt,
        "eobt_min": eobt_min,
        # AIP 改端的后台重算要用：原始行、原候选、上次的判据（改 EOBT / 规则开关后比它）
        "aip_rows": list(matched) if matched else None, "aip_orig": orig or None,
        "aip_key": (alts or {}).get("key"),
    }


def regenerate_generated(state, dep_obj, arr_obj, eobt_text, dep_wx, arr_wx, nt, apply_ops, em=None,
                         measured_default=True):
    """面板改了 EOBT / 运行规则开关、生效的规则跟着变了 → 按新的 EOBT 重新生成航路（后台线程调）。
    em / nt / measured_default 同 regenerate_aip。→ {gr, candidate, gen_map, gen_xfer, eta}；生成失败 → None。"""
    j = timed.parse_hhmm(eobt_text) if eobt_text else None
    eobt_min = j if j is not None else operations.default_eobt_min()
    if em is None:
        em = eta_model(dep_obj, arr_obj, "", None)
    gr = generate_by_rules(state, dep_obj, arr_obj, eobt_min, dep_wx, arr_wx, nt, apply_ops=apply_ops, em=em,
                           measured_default=measured_default)
    if not gr:
        return None
    generated = gr["route_str"]
    gen_map = (gr["coords"], "%s→%s  %s" % (dep_obj.code, arr_obj.code, generated)) if gr.get("coords") else None
    gen_xfer = (vatjpn.match_arrival(transfer_entry(arr_obj.code), generated, [p[0] for p in gr["coords"]])
                if gr.get("coords") else None)
    return {"gr": gr, "candidate": generated_candidate(state, dep_obj, arr_obj, gr),
            "gen_map": gen_map, "gen_xfer": gen_xfer,
            "eta": em.estimate(eobt_min, gr.get("pts")) if gr.get("pts") else None}


# ---------- Volanta ----------

# 同步轮询窗口：令牌在 /map 登录后即生成，但 Chromium 把它从内存写到磁盘（我们读的 leveldb）
# 有 ~30 秒~1 分钟延迟、空闲时甚至更久——这正是旧的 180s 偶尔超时的根因。放宽到 300s，并用弹窗引导。
VOLANTA_POLL_CAP = 300

POPUP_WAIT = ("已打开 Volanta 登录页。请点「确定」后在浏览器中登录。\n\n"
              "登录后，令牌需要约 30 秒~1 分钟才会写入磁盘，程序会自动获取，请耐心等待。\n"
              "想更快：在 Volanta 页面上滚动或点几下即可。")
POPUP_SLOW = ("还没获取到登录令牌。请在浏览器打开 Volanta 的「航班 / Flights」页，\n"
              "刷新该页并向下滚动飞行记录列表——这会促使令牌尽快写入磁盘。\n\n"
              "程序仍在后台自动获取，关闭本提示不影响。")


def volanta_sync(cancel_evt, on_status, on_popup):
    """Volanta 同步（后台线程调）。on_status(text) / on_popup(kind∈{'wait','slow'}) 由 UI 提供并负责 marshal。
    返回 'synced' | 'cancelled' | 'timeout' | 'error:<msg>'。"""
    try:
        # 1) 快路径：本机已有有效令牌（14 天内同步过）→ 无需浏览器
        if try_fetch_volanta_json_via_session(diag=True):
            return "synced"
        # 2) 打开 /map 让用户登录。令牌登录后即生成，但要等它从内存写到磁盘才读得到。
        on_status("Volanta：已打开浏览器，请在地图页登录…")
        _open_volanta_in_browser()
        on_popup("wait")                                  # 醒目弹窗①：正在等待令牌写入
        waited = 0
        popup2_done = False
        while waited < VOLANTA_POLL_CAP and not cancel_evt.is_set():
            cancel_evt.wait(3)
            waited += 3
            if cancel_evt.is_set():
                break
            if try_fetch_volanta_json_via_session():      # 轮询不开 diag，避免每 3s 刷屏（状态栏+弹窗已反馈）
                return "synced"
            on_status(f"Volanta：登录后请稍候，正在等待令牌写入磁盘…（{waited}/{VOLANTA_POLL_CAP}s）")
            if (not popup2_done) and waited >= 60:        # 约 1 分钟仍无 → 弹窗②升级引导
                popup2_done = True
                on_popup("slow")
        if cancel_evt.is_set():
            return "cancelled"
        # 超时前最后再试一次并打开 diag：仍取不到就在日志里留一行原因（例如在别的浏览器登录了）
        if try_fetch_volanta_json_via_session(diag=True):
            return "synced"
        return "timeout"
    except Exception as e:                                # noqa: BLE001
        return "error:%s" % e


def reload_volanta(state):
    """同步成功后重读已飞航线，写回 AppState。返回 (flown, vmeta)。"""
    flown, vmeta = load_volanta_flown_routes()
    state.flown_counts = flown or {}
    state.volanta_meta = vmeta or {}
    return state.flown_counts, state.volanta_meta


def set_auto_sync(on):
    """写 Volanta 自动同步偏好。返回给用户看的日志串。"""
    set_volanta_auto(bool(on))
    return ("🔖 Volanta 自动同步偏好已设为："
            + ("auto（以后启动自动同步）" if on else "ask（每次手动）"))
