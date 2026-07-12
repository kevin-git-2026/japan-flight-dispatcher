# ================= 控制层（编排：应用状态 / 后台任务 / 日志；零 GUI 依赖）=================
# 本模块【绝不 import 任何 GUI 框架】——不 import tkinter、不 import flet。
# 铁律：只抛异常、只返回值、只回调；【跨线程 marshal 由 UI 层负责】
#       （tkinter 的 root.after(0,…) / Flet 的 page.run_task(coro)）。
# 这些函数原本是 gui.py 里的 _init_worker / _plan_worker / _compute_proc / _volanta_worker 的主体。

import os

from .config import get_real_run_path
from .navdata import find_navdata_file, check_airac_currency
from .data import load_japan_icao_set, load_airports_from_navigraph, load_aip_routes_from_csv
from .scenery import scan_installed_sceneries
from .airlines import init_airline_data
from .volanta import (
    load_volanta_flown_routes, volanta_auto_enabled, set_volanta_auto,
    try_fetch_volanta_json_via_session, _open_volanta_in_browser,
)
from .routing import calculate_distance_nm, find_aip_route, get_random_route
from .planner import build_flight_plan, parse_runway_ft, parse_dist
from .router import generate_route, route_geometry, route_length_nm
from . import procedures, weather, timed, ntrack


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
    """一次性初始化（后台线程调）：导航数据 → AIRAC → 地景扫描 → 航司 → AIP → Volanta。
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
    return st


NAVDATA_HELP = ("未找到导航数据。\n\n请前往 https://navigraph.com/downloads 下载\n"
                "「X-Plane 12」导航数据，解压放入程序目录的 NavData 文件夹\n"
                "后重启本程序。")
NAVDATA_LOG = ("❌ 未找到导航数据。请前往 https://navigraph.com/downloads 下载「X-Plane 12」导航数据，"
               "放入程序目录的 NavData 文件夹后重启。")


# ---------- 规划 ----------

def plan(state, f):
    """一次完整规划（后台线程调）：选机场 → AIP/生成航路 → 各航路几何 → FlightAware+SimBrief → 进离场预筛+天气。
    返回 (FlightPlan, proc)；任何硬错误抛异常，由 UI 显示。"""
    min_rwy = parse_runway_ft(f["runway"])
    dmin, dmax = parse_dist(f["dmin"], f["dmax"])
    all_airports = load_airports_from_navigraph(state.dat_path, state.scenery_map, min_rwy)
    if not all_airports:
        raise RuntimeError("未能找到任何符合条件的机场。请检查跑道长度或数据文件。")
    strict = f["strict"]
    if strict and not state.aip_data:
        print("⚠️ 航路数据下载失败，已为您转为自由规划模式。")
        strict = False
    active = {f["sim"]} if f.get("sim") in ("XP", "MSFS") else None    # 问题1：所用模拟器(单选)

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
        dep_obj, arr_obj, dist, route, flown_count = get_random_route(
            all_airports, dmin, dmax, state.aip_data, strict, f["dep"], f["dest"],
            state.flown_counts, state.aip_index, require_both_scenery=f["scenery_only"],
            active_sims=active, route_len_fn=_route_len)

    # F15：无 AIP 航路且非严格模式 → 用本地导航数据 A* 生成一条参考航路（两分支统一在此处理）
    generated = generated_warn = generated_dist = gr = None
    if route is None and not strict:
        try:
            gr = generate_route(dep_obj, arr_obj, dat_path=state.dat_path,
                                aip_data=state.aip_data, airports=all_airports)
            if gr:
                generated = gr["route_str"]
                generated_dist = gr["dist_nm"]
                generated_warn = gr["warn"] if gr.get("suspect") else None
                print("🧭 无 AIP 航路，已用本地导航数据生成参考航路。")
            else:
                print("ℹ️ 本地导航数据未能连通该航线，跳过航路生成。")
        except Exception as e:
            print(f"⚠️ 航路生成失败（已忽略）: {e}")   # 绝不让生成中断规划

    # 各 AIP 航路：长度（与 find_aip_route 同序）+ 地图航点（每条一份）+ 航点(供 F21 端点预筛复用)
    matched = aip_dists = aip_maps = aip_pts = None
    gen_map = None
    if route:
        matched = [r for r in state.aip_data
                   if len(r) > 5 and r[0].strip().upper() == dep_obj.code and r[1].strip().upper() == arr_obj.code]
        aip_dists, aip_maps, aip_pts = [], [], []
        for r in matched:
            rs = r[5].strip()
            try:
                pts = route_geometry(dep_obj, arr_obj, rs, state.dat_path) if rs else None
            except Exception:
                pts = None
            aip_pts.append(pts)
            aip_dists.append(route_length_nm(pts) if pts else None)
            aip_maps.append((pts, "%s→%s  %s" % (dep_obj.code, arr_obj.code, rs)) if pts else None)
    elif generated and gr and gr.get("coords"):
        gen_map = (gr["coords"], "%s→%s  %s" % (dep_obj.code, arr_obj.code, generated))

    print("🔎 正在拉取现实排班...")
    fp = build_flight_plan(dep_obj, arr_obj, dist, route,
                           f["airline"], f["aircraft"], f["time"], flown_count,
                           generated_route=generated, generated_route_warn=generated_warn,
                           generated_route_dist=generated_dist, aip_route_dists=aip_dists,
                           aip_maps=aip_maps, gen_map=gen_map)
    fp.active_sims = active                             # 问题1：渲染按所用模拟器标注地景
    proc = compute_proc(state, dep_obj, arr_obj, generated,   # F20/F21：逐 AIP 候选预筛跑道/SID/STAR + 抓天气
                        matched=matched, aip_dists=aip_dists, aip_pts=aip_pts,
                        strict_ops=f.get("strict_ops"))
    return fp, proc


def compute_proc(state, dep_obj, arr_obj, generated, matched=None,
                 aip_dists=None, aip_pts=None, strict_ops=False):
    """F20/F21（后台线程）：为每条 AIP 航路（或生成航路）预算端点预筛的跑道/SID·STAR，并抓 dep/arr 的 METAR+TAF。
    matched=该航线全部 AIP 原始行（>1 条→用户在弹窗按 EOBT/机型/高度选或定唯一）；aip_pts/aip_dists 与之同序
    （复用 plan() 已算几何、免重算）。任一步失败都不影响主规划（返回空/None，UI 优雅降级）。"""
    def _prefilter(base_route, pts):
        """一条航路串 → (dep_rows, dep_matched, arr_rows, arr_matched)。pts 有则复用其航点、否则现算。"""
        route_fixes = []
        try:
            if pts is None and base_route:
                pts = route_geometry(dep_obj, arr_obj, base_route, state.dat_path)
            if pts:
                route_fixes = [p[0] for p in pts[1:-1]]  # 去首尾机场，留 enroute（首=离场点、末=进场点）
        except Exception:
            route_fixes = []
        try:
            dr, dm = procedures.matching_choices(dep_obj.code, state.dat_path, route_fixes, "dep")
        except Exception:
            dr, dm = [], False
        try:
            ar, am = procedures.matching_choices(arr_obj.code, state.dat_path, list(reversed(route_fixes)), "arr")
        except Exception:
            ar, am = [], False
        return dr, dm, ar, am

    candidates = []
    if matched:                                          # AIP 分支：逐条候选（含时段/高度/机型 + 端点预筛）
        for i, anno in enumerate(timed.annotate_routes(matched)):
            pts = aip_pts[i] if (aip_pts and i < len(aip_pts)) else None
            dist = aip_dists[i] if (aip_dists and i < len(aip_dists)) else None
            dr, dm, ar, am = _prefilter(anno["route"], pts)
            candidates.append({**anno, "dist": dist, "pts": pts,   # pts 供全段航路预览复用 enroute 几何
                               "dep_rows": dr, "dep_matched": dm, "arr_rows": ar, "arr_matched": am})
    elif generated:                                      # 生成航路：单候选（无时段/机型/高度）
        try:
            gpts = route_geometry(dep_obj, arr_obj, generated, state.dat_path)
        except Exception:
            gpts = None
        dr, dm, ar, am = _prefilter(generated, gpts)
        candidates.append({"route": generated, "restr": "", "alt": "", "aircraft": "", "dist": None, "pts": gpts,
                           "dep_rows": dr, "dep_matched": dm, "arr_rows": ar, "arr_matched": am})

    print("🌦️ 正在获取机场天气（METAR / 网格回退）…")
    # 实测运用状况（国土交通省 ntrack，目前仅羽田）：取到就是进离场预选的首选依据，取不到就走规则引擎
    nt = {}
    for obj in (dep_obj, arr_obj):
        if ntrack.supports(obj.code) and obj.code not in nt:
            cfg = ntrack.fetch_latest(obj.code)
            if cfg:
                nt[obj.code] = cfg
    return {
        "aip_candidates": candidates, "selected": 0, "strict_ops": bool(strict_ops),
        "dep_wx": weather.resolve_airport_wx(dep_obj.code, dep_obj.lat_dd, dep_obj.lon_dd),
        "arr_wx": weather.resolve_airport_wx(arr_obj.code, arr_obj.lat_dd, arr_obj.lon_dd),
        "ntrack": nt,
    }


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
        return "cancelled" if cancel_evt.is_set() else "timeout"
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
