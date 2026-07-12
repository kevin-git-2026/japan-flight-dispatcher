# ================= 视图模型（纯数据派生 + 各界面 Model；零 GUI 依赖）=================
# 本模块【绝不 import 任何 GUI 框架】——不 import tkinter、不 import flet。
# 判定标准：一个函数若删掉所有控件调用后仍有实质逻辑，它就该在这里（或 controller.py）。
#
# 每个界面 = 一个纯数据 Model（可 headless 单测） + 一层薄渲染：
#   ResultModel      结果卡 → span 列表（tk 画成 tag / flet 画成 TextSpan）
#   MapModel         航路坐标 → 折线 + 三档 marker + bounds
#   AircraftModel    机型库 → 过滤 / 显示串↔SimBrief id
#   ProcPanelModel   F20/F21/F23：跑道·SID/STAR·天气·EOBT·运行规则 的完整状态机
#   AipTableModel    F21 多 AIP 航路选择表
#   OpsEditorModel   F23 运行规则编辑器（含 MultiSelectModel）
# 两套 UI 共用同一个 Model → 行为天然一致。

import copy
import math

from . import ntrack, operations, procedures, router, timed, weather
from .aircraft import aircraft_choices
from .planner import simbrief_url as _planner_simbrief_url


# ================= 结果卡（ResultModel）=================

class Span:
    """结果卡的一段文本：style=语义样式名；action=点击动作（('url',u) / ('simbrief',u) / ('map',coords,title)）。"""
    __slots__ = ("text", "style", "action")

    def __init__(self, text, style="", action=None):
        self.text = text
        self.style = style
        self.action = action

    def __repr__(self):
        return "Span(%r, %r%s)" % (self.text, self.style, ", action" if self.action else "")


def result_spans(plan, has_map=True):
    """FlightPlan → span 列表（结果卡的全部内容）。UI 只负责把 span 画成 Tk tag 或 ft.TextSpan。
    这是结果卡在两套 UI 下逐字一致的关键——文案与配色语义全在这里定死。"""
    dep, arr = plan.dep, plan.arr
    active = getattr(plan, "active_sims", None)          # 问题1：按所用模拟器标注地景
    out = []

    def ins(text, style="", action=None):
        out.append(Span(text, style, action))

    def _map_link(mp):
        """mp=(coords,title)：为该条航路插入一个独立的「在地图查看」链接。"""
        if not (has_map and mp and mp[0]):
            return
        coords, title = mp
        ins("       🗺️ 在地图查看本航路\n", "maplink", ("map", coords, title))

    def ins_airport(role, ap):
        ins(f"  {role} : ", "label")
        ins(ap.code, "code")
        lbl = ap.scenery_label(active)                   # " [地景:XP]" / " [⚠️无XP地景]" / ""
        if lbl:
            ins(lbl, "scn_yes" if ap.has_scenery_for(active) else "scn_no")
        if ap.is_military:
            ins(" [🛡️军用机场]", "mil")
        ins("\n")

    ins("  🛫  航 线 规 划 成 功\n", "h1")
    ins("  " + "─" * 44 + "\n", "sep")
    ins_airport("起飞机场", dep)
    ins_airport("降落机场", arr)
    ins("  大圆距离 : ", "label"); ins(f"{plan.dist_nm:.1f} NM\n", "dist")
    if plan.flown_count and plan.flown_count > 0:
        ins(f"  🔁 Volanta : 这条有向航线你已飞过 {plan.flown_count} 次（可考虑换一条）\n", "flown")

    def _dev(route_len):
        """航路长度 + 相对大圆偏差的展示串；缺数据返回 None。"""
        if not route_len or not plan.dist_nm:
            return None
        pct = (route_len - plan.dist_nm) / plan.dist_nm * 100.0
        return "航路长 %.0f NM（较大圆 %+.1f%%）" % (route_len, pct)

    if plan.aip_routes:
        ins("\n  📜 AIP 航路\n", "section")
        _dists = plan.aip_route_dists or []
        _maps = plan.aip_maps or []
        for i, rr in enumerate(plan.aip_routes, 1):
            cols = [x.strip() for x in rr.split(",")]     # rr=逗号拼接行 → [DEP,DEST,时段,高度,机型,航路,备注]
            route_s = cols[5] if len(cols) > 5 else rr
            restr = cols[2] if len(cols) > 2 else ""
            alt = cols[3] if len(cols) > 3 else ""
            ac = cols[4] if len(cols) > 4 else ""
            ins(f"  [{i}] ", "muted"); ins(f"{route_s}\n", "aip")
            cond = " · ".join(x for x in [("时段 " + restr) if restr else "",
                                          ("高度 " + alt) if alt else "",
                                          ("机型 " + ac) if ac else ""] if x)
            if cond:
                ins("       条件：" + cond + "\n", "muted")
            _dd = _dev(_dists[i - 1] if i - 1 < len(_dists) else None)
            if _dd:
                ins("       └ " + _dd + "\n", "muted")
            _map_link(_maps[i - 1] if i - 1 < len(_maps) else None)

    if plan.generated_route:
        ins("\n  🧭 生成航路（本地导航数据，非官方 AIP）\n", "section")
        ins("  " + plan.generated_route + "\n", "aip")
        _gd = _dev(plan.generated_route_dist)
        if _gd:
            ins("  " + _gd + "\n", "muted")
        _map_link(plan.gen_map)
        ins("  ⚠️ 起讫点取自 SID/STAR 衔接点、中间为 A* 连出的 enroute 航路；仅供参考，未含具体 SID/STAR 程序段。\n", "warn")
        if plan.generated_route_warn:
            # router 出的是完整成句的提示（大角度转弯 / 短程降级为直飞），这里只负责画，不再拼后缀
            ins("  ⚠️ " + plan.generated_route_warn + "\n", "warn")

    if (not dep.has_scenery_for(active)) or (not arr.has_scenery_for(active)):
        ins("  ⚠️ 地景提醒: [⚠️无…地景] = 未在所选模拟器的地景文件夹中检测到该机场插件地景\n", "warn")
    if dep.is_military or arr.is_military:
        ins("  🛡️ 军用提醒: 军用机场可能无民航设施与 SID/STAR，请酌情考虑！\n", "warn")

    ins("\n")
    if plan.is_exact and plan.real_flights:
        ins("  ✅ 完美匹配！为您检索到以下现实排班 :\n", "success")
        for fl in plan.real_flights:
            ins("     ✈ ", "muted"); ins(f"{fl}\n", "flight")
    elif plan.real_flights:
        ins("  ℹ️ 仅找到该航线上的其他参考排班 :\n", "partial")
        for fl in plan.real_flights:
            ins("     ✈ ", "muted"); ins(f"{fl}\n", "flight")
    else:
        ins("  ❌ 未找到排班，已降级生成模拟呼号", "nomatch")
        ins("（⚠️未必符合现实运行）: ", "muted"); ins(f"{plan.sim_callsign}\n", "callsign")

    ins("\n  🔗 查看 FlightAware 完整排班表:\n", "label")
    ins("  ", "label"); ins(plan.url + "\n", "link", ("url", plan.url))

    if plan.simbrief_url:
        ins("\n  🛩️ SimBrief 一键签派 : ", "label")
        ins("点击生成并查看simbrief计划（需登录）\n", "sblink", ("simbrief", plan.simbrief_url))
    return out


def error_spans(msg):
    return [Span(f"❌ 发生错误：{msg}\n", "warn")]


# ================= 地图（MapModel）=================

def plan_maps(plan):
    """本次规划里所有【可看地图】的航路：[(coords, title)] —— 各 AIP 行 + 生成航路，跳过没几何的。
    Flet 无多窗口，故不再「一条航路开一个窗口」，而是把它们做成同一个地图视图里的标签页。"""
    out = []
    for mp in (getattr(plan, "aip_maps", None) or []):
        if mp and mp[0]:
            out.append(mp)
    gm = getattr(plan, "gen_map", None)
    if gm and gm[0]:
        out.append(gm)
    return out


def _fit_zoom(bounds, vw=1040, vh=580):
    """把 bounds 装进 vw×vh 逻辑像素视口所需的 Web-Mercator 缩放级别（256px 瓦片）。
    视口取值比实际地图区（约 1160×680）保守一点 → 四周自然留出余量，端点不会贴边。
    自己算而不用 flutter_map 的 CameraFit —— 后者要等控件量到尺寸才准，构造期给不出可靠值。"""
    dlat = max(bounds["north"] - bounds["south"], 1e-6)
    dlon = max(bounds["east"] - bounds["west"], 1e-6)
    clat = (bounds["north"] + bounds["south"]) / 2.0
    z_lon = math.log2(360.0 * vw / (256.0 * dlon))
    # 墨卡托把纬度按 1/cos(lat) 拉伸 → 同样度数的纬度占更多像素
    z_lat = math.log2(360.0 * vh * max(math.cos(math.radians(clat)), 0.1) / (256.0 * dlat))
    return max(2.0, min(11.0, min(z_lon, z_lat)))


_DCT = "DCT"


def _route_from_title(title):
    """标题形如 `RJTT→RJOO  LAXAS Y56 TOHME …` → 取出后半的航路串。"""
    parts = (title or "").split("  ", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def route_legs(coords, route_str):
    """每条【腿】一个标注：`[{lat, lon, label}]`，label = 航路名(Y56…) / SID·STAR 程序名 / `DCT`。

    航路串里**能在坐标序列里找到同名航点**的 token = 换路点，其余 token = 名称（airway 或程序名）。
    按顺序把换路点对到坐标下标；两个换路点之间的所有坐标（`_parse_aip_route` 沿 airway 加密出来的
    中间点）同属一条腿 → 共用「该换路点之前那个名称」；没有名称就是直飞 `DCT`。

    这套规则对 SID/STAR 也自动成立：程序的航点不在航路串里（串里只有程序名），
    于是它们整段落进「程序名」那一腿——离场是首腿、进场是末腿。

    （`router._parse_aip_route` 其实算过每段的 airway 名，但 `route_geometry` 只返回坐标、把名字丢了。
      这里就地反推，不动逻辑层。）
    """
    n = len(coords or [])
    if n < 2:
        return []
    idents = [c[0] for c in coords]
    ident_set = set(idents)

    majors, pending = [], None                    # [(换路点 ident, 它之前的名称)]
    for tok in (route_str or "").split():
        if tok in ident_set:
            majors.append((tok, pending))
            pending = None
        else:
            pending = tok
    trailing = pending                            # 串尾的名称（如 STAR 名，其航点不在串里）

    idx, p = [], 0                                # 换路点 → 坐标下标（按顺序匹配，容忍重名）
    for ident, name in majors:
        while p < n and idents[p] != ident:
            p += 1
        if p >= n:
            break
        idx.append((p, name))
        p += 1

    spans, prev = [], 0                           # [(起下标, 止下标, 名称)]
    for pos, name in idx:
        if pos > prev:
            spans.append((prev, pos, name))
        prev = pos
    if prev < n - 1:                              # 末换路点 → 终点（STAR 段，或直飞进场）
        spans.append((prev, n - 1, trailing))

    legs = []
    for a, b, name in spans:
        m = (a + b) // 2                          # 标注放在这条腿折线的中点
        legs.append({"lat": (coords[m][1] + coords[m + 1][1]) / 2.0,
                     "lon": (coords[m][2] + coords[m + 1][2]) / 2.0,
                     "label": name or _DCT})
    return legs


def map_model(coords, title=""):
    """航路坐标 → {polyline, markers, legs, bounds, center, zoom}。
    三档 marker：0=起降机场 / 1=换路点(ident 出现在航路串里) / 2=加密中间点。
    `legs` = 每条腿的航路名标注（见 route_legs）。
    纯数据，UI 只负责把它翻译成具体地图控件的图元。coords=[(ident, lat, lon), …]。"""
    if not coords:
        return None
    majors = set((title or "").split())
    markers = []
    for i, (ident, la, lo) in enumerate(coords):
        tier = 0 if (i == 0 or i == len(coords) - 1) else (1 if ident in majors else 2)
        markers.append({"lat": la, "lon": lo, "ident": ident, "tier": tier})
    pts = [(la, lo) for _id, la, lo in coords]
    bounds = center = None
    zoom = 7.0
    if len(pts) >= 2:
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        bounds = {"north": max(lats) + 0.6, "south": min(lats) - 0.6,
                  "west": min(lons) - 0.6, "east": max(lons) + 0.6}
        center = ((bounds["north"] + bounds["south"]) / 2.0,
                  (bounds["west"] + bounds["east"]) / 2.0)
        zoom = _fit_zoom(bounds)
    elif pts:
        center = pts[0]
    return {"polyline": pts if len(pts) >= 2 else [], "markers": markers,
            "legs": route_legs(coords, _route_from_title(title)),
            "bounds": bounds, "center": center, "zoom": zoom, "title": title}


def map_tab_label(i, title):
    """地图标签页的短标签：[序号] 首个航点 —— 光看 `RJTT→RJCC` 四条全一样，分不清哪条。"""
    parts = (title or "").split("  ", 1)
    route = parts[1].strip() if len(parts) > 1 else ""
    first = route.split()[0] if route else ""
    return "[%d] %s" % (i + 1, first) if first else "[%d]" % (i + 1)


# ================= 机型（AircraftModel）=================

class AircraftModel:
    """机型可搜索下拉：过滤候选 + 显示串 → SimBrief aircraft_id。"""

    def __init__(self):
        self.rows = aircraft_choices()                   # [(显示串, id, 搜索blob), …]
        self.labels = [lbl for lbl, _id, _blob in self.rows]
        self._to_id = {lbl: _id for lbl, _id, _blob in self.rows}

    def filter(self, typed):
        """按输入过滤候选（匹配 icao / 名字 / 厂商别名）；无输入或无命中 → 全量。"""
        t = (typed or "").strip().lower()
        if not t:
            return list(self.labels)
        return [lbl for lbl, _id, blob in self.rows if t in blob] or list(self.labels)

    def resolve(self, typed):
        """下拉选中的显示串 → 其 SimBrief id；手输则原样返回（供 FlightAware 匹配 + planner 再规范化）。"""
        v = (typed or "").strip()
        return self._to_id.get(v, v) if v else ""


# ================= 进离场面板的纯派生函数 =================

def wind_desc(wind, rwy_id):
    """(逆/顺风文本+侧风+适航, ok, headwind)；wind=(dir,spd,gust) 或 None / 静风 / VRB → ('', True, 0)。"""
    if not wind or not isinstance(wind[0], (int, float)) or not wind[1]:
        return "", True, 0.0
    hw, cw = weather.runway_wind(procedures.runway_heading_deg(rwy_id), wind[0], wind[1])
    ok = weather.runway_ok(hw, cw)
    wd = ("逆风%.0f节" % hw) if hw >= 0 else ("顺风%.0f节" % abs(hw))   # 顺/逆已表方向，分量取绝对值 + 单位「节」
    return "%s 侧风%.0f节 %s" % (wd, cw, "✓" if ok else "⚠️超限"), ok, hw


def wx_text(prefix, icao, wx):
    """该机场的天气块（F22）：实测 METAR 与网格合成 METAR 两分支【同一套渲染】（风摘要 + 报文原文），
    仅标题标注不同——网格分支明确标「Open-Meteo·<model> 模型合成·非实测」。wx=resolve_airport_wx 结果。"""
    raw = wx.get("metar_raw") if wx else None
    if not raw:
        head = "%s %s  ·  天气获取失败（断网或暂无观测）" % (prefix, icao)
        taf = wx.get("taf_raw") if wx else None
        return head + ("\n    %s" % taf if taf else "")
    wd, ws, gust = weather.parse_wind(raw)
    if not ws:
        wind_s = "静风"
    elif isinstance(wd, int):
        wind_s = "风 %03d°/%d节%s" % (wd, ws, ("阵%d" % gust if gust else ""))
    else:
        wind_s = "风 不定/%d节" % ws
    age = wx.get("metar_age_sec")
    stale = age is not None and age > weather._METAR_STALE_SEC
    if wx.get("source") == "grid":
        model = (wx.get("model") or "model").upper().replace("_", "-")
        toks = raw.split()
        zt = toks[1] if len(toks) > 1 and toks[1].endswith("Z") else ""
        head = "%s %s  🌐 %s  《Open-Meteo·%s 模型合成 METAR·非实测%s》\n    %s" % (
            prefix, icao, wind_s, model, (" · " + zt if zt else ""), raw)
        if stale:
            head += "\n    ⚠️ 实测 METAR 已 %.0f 小时前，改用网格合成" % (age / 3600.0)
    else:
        head = "%s %s  %s%s\n    %s" % (
            prefix, icao, wind_s, ("  ⚠️可能过期(%.0fh)" % (age / 3600.0) if stale else ""), raw)
    taf = wx.get("taf_raw")
    if taf:
        head += "\n    %s" % taf
    return head


def runway_items(rows, wind, in_use=None):
    """跑道行 → 显示项（长度[米] + 风分量 + 适航），并排序。
    rows=[(rwy_id, length_ft, labels)]；返回 [{disp, rwy, labels, ok, hw, in_use}]。

    `in_use` = 实测正在使用的跑道集合（ntrack，仅羽田）。命中的标「✅在用」并**排在最前**——
    于是「按风默认」自然落在实测在用的跑道里。其余跑道仍在下拉里可选（用户永远能改选）。"""
    used = set(in_use or ())
    items = []
    for rwy_id, length_ft, labels in rows:
        short = rwy_id.replace("RW", "")
        parts = [short] + (["%.0fm" % (length_ft * 0.3048)] if length_ft else [])   # 东亚习惯：显示层用米（数据底层为英尺）
        wdesc, ok, hw = wind_desc(wind, rwy_id)
        if wdesc:
            parts.append(wdesc)
        hit = rwy_id in used
        if hit:
            parts.append("✅在用")
        items.append({"disp": " · ".join(parts), "rwy": rwy_id, "labels": list(labels),
                      "ok": ok, "hw": hw, "in_use": hit})
    items.sort(key=lambda it: (not it["in_use"], not it["ok"], -it["hw"],
                               procedures._rw_sort_key(it["rwy"])))
    return items


def filter_labels(all_labels, typed):
    """SID/STAR 可搜索下拉：输入即过滤（无命中则回退全量）。"""
    t = (typed or "").strip().lower()
    if not t:
        return list(all_labels)
    return [v for v in all_labels if t in v.lower()] or list(all_labels)


def eobt_jst_min(text):
    """EOBT 输入(JST HHMM) → 当日分钟；空/非法 → None。"""
    return timed.parse_hhmm(text) if text else None


def eobt_utc_min(text):
    """EOBT(JST) → 当日 UTC 分钟（供 SimBrief deph/depm）；无 → None。"""
    j = eobt_jst_min(text)
    return None if j is None else (j - 540) % 1440


def eobt_zulu_text(text):
    """EOBT 输入旁的灰字提示：SimBrief 用的是 UTC(Zulu)，JST−9h。"""
    u = eobt_utc_min(text)
    return ("→ SimBrief %02d%02dZ" % (u // 60, u % 60)) if u is not None else ""


def aip_label(i, c):
    """AIP 航路下拉紧凑标签：[序号] 时段 · 距离 · 航路首…尾。"""
    restr = c.get("restr") or "全时段"
    dist = ("%.0fNM · " % c["dist"]) if c.get("dist") else ""
    toks = (c.get("route") or "").split()
    short = " ".join(toks) if len(toks) <= 4 else "%s…%s" % (" ".join(toks[:2]), toks[-1])
    return "[%d] %s · %s%s" % (i + 1, restr, dist, short)


def proc_notes(c):
    """该候选的降级提示（无跑道数据 / 无程序 / 端点未直接匹配）。"""
    notes = []
    dep_rows, arr_rows = c.get("dep_rows"), c.get("arr_rows")
    has_proc = lambda rows: any(r[2] for r in (rows or []))   # 行内 label 非空 = 该跑道挂有 SID/STAR
    if not dep_rows:
        notes.append("出发无跑道数据")
    elif not has_proc(dep_rows):
        notes.append("出发无可用 SID（可选跑道，雷达引导离场）")
    elif not c.get("dep_matched"):
        notes.append("出发端点未直接匹配 SID（已列全部）")
    if not arr_rows:
        notes.append("到达无跑道数据")
    elif not has_proc(arr_rows):
        notes.append("到达无 STAR（可选跑道，仪表进近 IAP）")
    elif not c.get("arr_matched"):
        notes.append("到达端点未直接匹配 STAR（已列全部）")
    return notes


# ================= 进离场面板（ProcPanelModel）=================

class ProcPanelModel:
    """F20/F21/F23 面板的完整状态机（纯数据）：AIP 候选 / 跑道 / SID·STAR / 天气 / EOBT / 运行规则预选。
    UI 只做三件事：把它的 items/labels 画成下拉、把用户点选喂回 select_*、把 ops_label/hint/summary 画成文字。"""

    def __init__(self, plan, proc, dat_path, ops_data=None):
        self.dat_path = dat_path
        self.dep_icao, self.arr_icao = plan.dep.code, plan.arr.code
        self.sb_base = getattr(plan, "sb_base", None)
        self.plan_simbrief_url = getattr(plan, "simbrief_url", None)
        self.dist_nm = getattr(plan, "dist_nm", None)
        self.candidates = list(proc.get("aip_candidates") or [])
        self.strict_ops = bool(proc.get("strict_ops"))
        # ops_data=None → 现读 operation.json；测试可注入固定规则集
        self.ops_data = operations.load_operations() if ops_data is None else (ops_data or {})

        dep_wx, arr_wx = proc.get("dep_wx"), proc.get("arr_wx")
        self.dep_wx, self.arr_wx = dep_wx, arr_wx
        self.wind = {"dep": dep_wx.get("wind") if dep_wx else None,
                     "arr": arr_wx.get("wind") if arr_wx else None}
        self.sky = {"dep": weather.parse_sky(dep_wx.get("metar_raw") if dep_wx else None),   # (layers, vis_m)
                    "arr": weather.parse_sky(arr_wx.get("metar_raw") if arr_wx else None)}

        # 实测运用状况（ntrack，目前仅羽田）：{ICAO: {time_jst, approaches, ldg, dep, raw}}
        self.ntrack = dict(proc.get("ntrack") or {})
        self.nt_iaps = {}                                # {ICAO: [CIFP 进近 ident]}
        for icao, cfg in self.ntrack.items():
            try:
                self.nt_iaps[icao] = ntrack.match_iaps(
                    cfg.get("approaches"), procedures.enumerate_approaches(icao, dat_path))
            except Exception:                            # noqa: BLE001
                self.nt_iaps[icao] = []

        self.apply_ops = True
        nm, _wd = weather.now_jst()                      # EOBT 默认＝当前 JST（可改）
        self.eobt = "%02d%02d" % (nm // 60, nm % 60)

        self.sel_idx = 0
        self.items = {"dep": [], "arr": []}              # [{disp,rwy,labels,ok,hw}]
        self.sel_rwy = {"dep": None, "arr": None}        # rwy_id ("RW34L") | None
        self.proc_labels = {"dep": [], "arr": []}
        self.sel_proc = {"dep": "", "arr": ""}
        self.ops_label = {"dep": "", "arr": ""}
        self.nt_label = {"dep": self._nt_text("dep"), "arr": self._nt_text("arr")}
        self.select_candidate(proc.get("selected", 0))

    # ---- 只读派生 ----
    @property
    def visible(self):
        return bool(self.candidates)

    @property
    def show_aip_row(self):
        return len(self.candidates) > 1

    @property
    def base_route(self):
        return (self.candidates[self.sel_idx].get("route") or "") if self.candidates else ""

    def wx_text(self, side):
        return wx_text("🛫 出发" if side == "dep" else "🛬 到达",
                       self.dep_icao if side == "dep" else self.arr_icao,
                       self.dep_wx if side == "dep" else self.arr_wx)

    def aip_labels(self):
        return [aip_label(i, c) for i, c in enumerate(self.candidates)]

    def hint_text(self):
        notes = proc_notes(self.candidates[self.sel_idx]) if self.candidates else []
        return ("ℹ️ " + "；".join(notes)) if notes else ""

    def summary_text(self):
        dep_rwy = (self.sel_rwy["dep"] or "").replace("RW", "") or "—"
        arr_rwy = (self.sel_rwy["arr"] or "").replace("RW", "") or "—"
        return ("已选：%s 跑道 %s / %s    →    %s 跑道 %s / %s"
                % (self.dep_icao, dep_rwy, self.sel_proc["dep"].strip() or "—",
                   self.arr_icao, arr_rwy, self.sel_proc["arr"].strip() or "—"))

    def route_str(self):
        """SimBrief 的 route：SID + enroute + STAR（过渡点已隐含在 enroute 首/末 token）。"""
        sid = self.sel_proc["dep"].strip()
        star = self.sel_proc["arr"].strip()
        return " ".join(x for x in [sid.split(".")[0], self.base_route, star.split(".")[0]] if x)

    def eobt_zulu_text(self):
        return eobt_zulu_text(self.eobt)

    def simbrief_url(self):
        if not self.sb_base:
            return None
        return _planner_simbrief_url(self.sb_base, self.route_str(), eobt_utc_min(self.eobt))

    def preview_coords(self):
        """当前选定的 SID + enroute + STAR 全段坐标（供地图预览）；失败/不足 → (None, 提示串)。"""
        if not self.candidates:
            return None, "无航路候选"
        c = self.candidates[self.sel_idx]
        try:
            coords = procedures.full_route_coords(
                self.dep_icao, self.sel_proc["dep"].strip(), self.sel_rwy["dep"],
                self.arr_icao, self.sel_proc["arr"].strip(), self.sel_rwy["arr"],
                c.get("pts"), self.dat_path)
        except Exception as e:                            # noqa: BLE001
            return None, "⚠️ 生成全段航路失败: %s" % e
        if len(coords) < 2:
            return None, "ℹ️ 全段航路坐标不足，无法预览（SID/STAR 航点未能解析）。"
        # 标题带上 SID/STAR 名（不能只给 enroute 串）：地图的每腿航路名标注要靠它，
        # 否则进离场那两腿找不到名字、会被标成 DCT。
        return coords, "%s→%s  %s" % (self.dep_icao, self.arr_icao, self.route_str())

    def proc_choices(self, side, typed=None):
        return filter_labels(self.proc_labels[side], typed)

    # ---- 用户操作 ----
    def select_candidate(self, idx):
        """选定第 idx 条 AIP/生成候选 → 换 base_route、按其预筛重填跑道/SID·STAR（按风默认）、再应用运行规则。"""
        if not self.candidates:
            return
        self.sel_idx = max(0, min(int(idx), len(self.candidates) - 1))
        c = self.candidates[self.sel_idx]
        self._fill_rwy("dep", c.get("dep_rows", []))
        self._fill_rwy("arr", c.get("arr_rows", []))
        self._apply_ops()                                 # 命中则覆盖按风预选

    def select_runway(self, side, rwy_id):
        it = next((i for i in self.items[side] if i["rwy"] == rwy_id), None)
        self.sel_rwy[side] = rwy_id if it else None
        self._fill_proc(side, it)

    def select_proc(self, side, label):
        self.sel_proc[side] = label or ""

    def set_eobt(self, text):
        """EOBT 改动：开关开则按新时段重选（含重置按风默认），否则仅影响 SimBrief 派生值。"""
        self.eobt = text or ""
        if self.apply_ops:
            self.select_candidate(self.sel_idx)

    def set_apply_ops(self, on):
        """勾/取消「按运行规则预选」：重跑当前候选（开→应用规则、关→回按风预选）。"""
        self.apply_ops = bool(on)
        self.select_candidate(self.sel_idx)

    # ---- 内部：实测运用状况（ntrack）----
    def _icao(self, side):
        return self.dep_icao if side == "dep" else self.arr_icao

    def _nt_cfg(self, side):
        return self.ntrack.get(self._icao(side))

    def _nt_rwys(self, side):
        """该侧【实测在用】的跑道集合；无数据 → None。离场看 DEP、到达看 LDG。"""
        cfg = self._nt_cfg(side)
        if not cfg:
            return None
        return set(cfg.get("dep" if side == "dep" else "ldg") or ()) or None

    def _nt_text(self, side):
        cfg = self._nt_cfg(side)
        if not cfg:
            return ""
        icao, t = self._icao(side), cfg.get("time_jst", "")
        if side == "dep":
            return "✈️ %s 实测运用（%s）：离场 %s" % (
                icao, t, "/".join(r.replace("RW", "") for r in cfg.get("dep") or []))
        iaps = self.nt_iaps.get(icao) or []
        return "✈️ %s 实测运用（%s）：落地 %s · 进近 %s%s" % (
            icao, t, "/".join(r.replace("RW", "") for r in cfg.get("ldg") or []),
            " / ".join(cfg.get("approaches") or []) or "—",
            ("（IAP %s）" % " ".join(iaps)) if iaps else "")

    # ---- 内部 ----
    def _fill_rwy(self, side, rows):
        # 实测在用的跑道标 ✅ 并排最前 → 按风默认自然落在其中；其余跑道仍可选（用户能改选）
        items = runway_items(rows, self.wind[side], in_use=self._nt_rwys(side))
        self.items[side] = items
        if items:
            self.sel_rwy[side] = items[0]["rwy"]
            self._fill_proc(side, items[0])
        else:
            self.sel_rwy[side] = None
            self._fill_proc(side, None)

    def _fill_proc(self, side, item):
        labels = (item or {}).get("labels") or []
        self.proc_labels[side] = list(labels)
        self.sel_proc[side] = self._default_proc(side, labels)

    def _default_proc(self, side, labels):
        """默认选哪条程序。

        到达侧优先用 **VATJPN 到着栏给的「门↔STAR 配对」**：官方把门后的 STAR 机体逐点展开写了出来
        （`…AGPUK MIRAI ABENO IKOMA` = STAR「IKOMAE」；`…TATSU NAKAH` = STAR「TATSU」），
        拿它反查即可**直接读出**该门配哪条 STAR——RJOO 的 AGPUK 上同时挂着 STAR「AGPUK」与「IKOMAE」，
        端点匹配两条都给，只有官方配对分得清。
        VATJPN 没给配对（没展开门后径路 / 该门无 STAR）→ 退回端点预筛的首个候选
        （其排序已由 procedures._label_order 按来向排过，不是字母序）。"""
        if not labels:
            return ""
        if side == "arr" and self.candidates:
            gate = (self.candidates[self.sel_idx] or {}).get("arr_gate")
            want = router.gate_stars(self._icao("arr"), gate) if gate else []
            hit = [l for l in labels if l.split(".")[0] in want]
            if hit:
                return hit[0]
        return labels[0]

    def _apply_ops(self):
        """v1.6.0：按 operation.json 运行规则预选跑道/SID·STAR（覆盖按风默认）。
        离场按 EOBT、到达按 ETA(=EOBT+航程) 匹配时段；关开关或无规则 → 保持按风预选。"""
        self.ops_label = {"dep": "", "arr": ""}
        if not self.apply_ops or not self.ops_data or not self.candidates:
            return
        c = self.candidates[self.sel_idx]
        j = eobt_jst_min(self.eobt)
        now_min, now_wd = weather.now_jst()
        dep_jst = j if j is not None else now_min
        try:
            dist = c.get("dist") or self.dist_nm or 0
            _eu, eta_utc = timed.plan_times_utc(dep_jst, dist)
            eta_jst = (eta_utc + 540) % 1440
        except Exception:
            eta_jst = dep_jst
        arr_wd = now_wd if eta_jst >= dep_jst else (now_wd % 7 + 1)   # 到达跨午夜 → 星期 +1
        self._apply_ops_side("dep", self.dep_icao, c.get("dep_rows", []),
                             {"jst_min": dep_jst, "weekday": now_wd, "wind": self.wind["dep"],
                              "sky_layers": self.sky["dep"][0], "vis_m": self.sky["dep"][1]}, "SID")
        self._apply_ops_side("arr", self.arr_icao, c.get("arr_rows", []),
                             {"jst_min": eta_jst, "weekday": arr_wd, "wind": self.wind["arr"],
                              "sky_layers": self.sky["arr"][0], "vis_m": self.sky["arr"][1]}, "STAR")

    def _apply_ops_side(self, side, icao, rows, ctx, proc_name):
        """单侧应用：命中规则 → 设跑道 + 级联程序 + 设 SID/STAR + 标注；无规则不动、无命中给提示。

        有实测运用状况（ntrack）时，把它作为 `config` 传给规则引擎：**ntrack 定「用哪些跑道 + 什么进近」
        （硬约束），规则定「在其中怎么选 + 配什么 SID/STAR」**（SID/STAR 正是 ntrack 给不了的）。
        风闸/天气闸由实测取代、时段/星期闸门保留——详见 operations.select_rule 的说明。"""
        nt = self._nt_cfg(side)
        cfg = None
        if nt:
            cfg = {"runways": set(nt.get("dep" if side == "dep" else "ldg") or ()),
                   "iaps": set(self.nt_iaps.get(icao) or ())}
        rules = operations.airport_rules(self.ops_data, icao)
        if not rules:
            return
        try:
            sel = operations.select_rule(rules, side, ctx, rows, config=cfg)
        except Exception as e:                            # noqa: BLE001
            print("⚠️ 运行规则匹配失败:", e)
            return
        if not sel:
            self.ops_label[side] = (
                ("🎯 %s 运行规则：实测在用的跑道未匹配到规则，已按风在【实测跑道】中预选" % icao)
                if nt else
                ("🎯 %s 运行规则：当前风/时段/天气无【可用】匹配规则"
                 "（超限跑道已排除，保持按风合规跑道）" % icao))
            return
        rule, rwy, proc_label = sel
        item = next((i for i in self.items[side] if i["rwy"] == rwy), None)
        if item:
            self.sel_rwy[side] = rwy
            self._fill_proc(side, item)                   # 级联该跑道路线相符的 SID/STAR（预选首个）
        if proc_label:
            self.sel_proc[side] = proc_label              # 规则程序命中航路端点→用它；否则保留路线相符首个
        iap = ""
        if side == "arr":
            # IAP 也以实测为准：ntrack 明确给出当前在用的进近，比规则里录的更准
            iaps = self.nt_iaps.get(icao) or (rule.get("arr") or {}).get("iaps") or []
            if iaps:
                iap = "  IAP %s" % " / ".join(iaps[:2])
        self.ops_label[side] = ("🎯 %s 运行规则：%s → RW%s / %s %s%s%s"
                                % (icao, rule.get("name", ""), (rwy or "").replace("RW", ""),
                                   proc_name, self.sel_proc[side] or "—", iap,
                                   "" if item else "（规则跑道不在本航路端点候选，仅标注）"))


# ================= F21 多 AIP 航路选择表（AipTableModel）=================

class AipTableModel:
    """F21 弹窗的表模型：仿真实 AIP 航路表（航路/时段/高度/机型/用途/距离）＋（严格模式）判定列。
    严格模式下由用户给的 EOBT/机型/巡航高度实时判定，唯一可用即可自动定。"""

    def __init__(self, candidates, sel_idx=0, strict=False):
        self.candidates = list(candidates)
        self.sel_idx = sel_idx
        self.strict = bool(strict)
        self.eobt = ""
        self.cat = "JET"
        self.fl = ""
        self.verdicts = None
        self.status = ""
        self.recompute()

    def rows(self):
        """[(idx, [选择, 航路, 时段, 高度, 机型, 用途, 距离, (判定)], verdict|None)]"""
        out = []
        for i, c in enumerate(self.candidates):
            v = self.verdicts[i] if self.verdicts else None
            dist = ("%.0f NM" % c["dist"]) if c.get("dist") else "-"
            row = ["●" if i == self.sel_idx else "○", c.get("route", ""), c.get("restr") or "-",
                   c.get("alt") or "-", c.get("aircraft") or "-",
                   timed.describe_restriction(c.get("restr", "")), dist]
            if self.strict:
                row.append({"match": "✓可用", "no": "✗不符", "unknown": "？待定"}.get(v, "-"))
            out.append((i, row, v))
        return out

    def set_inputs(self, eobt=None, cat=None, fl=None):
        if eobt is not None:
            self.eobt = eobt
        if cat is not None:
            self.cat = cat
        if fl is not None:
            self.fl = fl
        return self.recompute()

    def select(self, idx):
        self.sel_idx = max(0, min(int(idx), len(self.candidates) - 1))
        return self.recompute()

    def recompute(self):
        """→ auto_idx（严格模式下唯一可用的候选下标，UI 应自动选定它）｜None。"""
        if not self.strict:
            self.verdicts = None
            self.status = ""
            return None
        eobt = timed.parse_hhmm(self.eobt)
        fl = timed.parse_fl(self.fl)
        eobt_utc = eta_utc = None
        if eobt is not None:
            eobt_utc, eta_utc = timed.plan_times_utc(eobt, self.candidates[self.sel_idx].get("dist"))
        self.verdicts = timed.filter_candidates(self.candidates, eobt_utc, eta_utc, self.cat, fl)
        uniq = timed.resolve_unique(self.verdicts)
        if uniq is not None and eobt is not None and fl is not None:
            self.sel_idx = uniq
            self.status = "✓ 唯一匹配：第 %d 条已自动选定，可「确认并关闭」" % (uniq + 1)
            return uniq
        if eobt is not None or fl is not None:
            self.status = "当前无法唯一确定，请补齐 EOBT/机型/高度或手动勾选"
        else:
            self.status = "填入 EOBT/机型/高度自动定唯一航路，或直接手动勾选"
        return None


# ================= F23 运行规则编辑器 =================

class ValidationError(Exception):
    """表单校验失败（UI 负责把 message 弹给用户）。"""


class MultiSelectModel:
    """多选 + 模糊搜索的值集合模型（与显示解耦）——【过滤时隐藏的已选项不会丢】。
    _all=[(显示, 存值)…]；_sel=已选存值集合；_shown=当前过滤后可见的存值（顺序同显示）。"""

    def __init__(self):
        self._all = []
        self._sel = set()
        self._shown = []
        self.filter_text = ""

    def set_items(self, pairs):
        """重置候选与选择、清搜索。"""
        self._all = list(pairs)
        self._sel = set()
        self.filter_text = ""
        self._render()

    def set_selection(self, values):
        """按存值集合设选择、清搜索（换规则时避免旧过滤藏住新项）。"""
        self._sel = set(values or [])
        self.filter_text = ""
        self._render()

    def set_filter(self, text):
        self.filter_text = text or ""
        self._render()

    def _render(self):
        f = self.filter_text.strip().lower()
        self._shown = [val for disp, val in self._all
                       if not f or f in disp.lower() or f in val.lower()]

    def shown(self):
        """当前可见项 [(显示, 存值, 是否已选)]。"""
        f = self.filter_text.strip().lower()
        return [(disp, val, val in self._sel) for disp, val in self._all
                if not f or f in disp.lower() or f in val.lower()]

    def shown_selected_indices(self):
        return [i for i, val in enumerate(self._shown) if val in self._sel]

    def apply_shown_selection(self, indices):
        """用户在【可见项】里的选择变化 → 同步 _sel：只改当前显示项，隐藏的已选项保留。"""
        now = {self._shown[i] for i in indices if 0 <= i < len(self._shown)}
        self._sel = (self._sel - set(self._shown)) | now

    def toggle(self, value):
        """点击切换单个值（Flet 的 Checkbox 用）。"""
        if value in self._sel:
            self._sel.discard(value)
        else:
            self._sel.add(value)

    def read(self):
        """已选存值列表（保持候选原顺序）。"""
        return [val for _d, val in self._all if val in self._sel]


class FormData:
    """F23 详情表单的一份纯数据快照（无任何 tk.StringVar / flet 控件）。"""
    __slots__ = ("name", "time_text", "days", "ref_rwy", "wind_kind", "wind_min",
                 "ceiling", "ceiling_cover", "visibility",
                 "dep_runways", "dep_sids", "arr_runways", "arr_stars", "arr_iaps")

    def __init__(self, name="", time_text="", days=None, ref_rwy="", wind_kind="顺风", wind_min="",
                 ceiling="", ceiling_cover="SCT", visibility="",
                 dep_runways=None, dep_sids=None, arr_runways=None, arr_stars=None, arr_iaps=None):
        self.name = name
        self.time_text = time_text
        self.days = list(days) if days else [False] * 7
        self.ref_rwy = ref_rwy
        self.wind_kind = wind_kind                       # 顺风 / 逆风 / 侧风
        self.wind_min = wind_min
        self.ceiling = ceiling
        self.ceiling_cover = ceiling_cover
        self.visibility = visibility
        self.dep_runways = list(dep_runways or [])
        self.dep_sids = list(dep_sids or [])
        self.arr_runways = list(arr_runways or [])
        self.arr_stars = list(arr_stars or [])
        self.arr_iaps = list(arr_iaps or [])


_WKIND_TO_JSON = {"逆风": "headwind", "侧风": "crosswind"}          # 其余（顺风）→ tailwind
_WKIND_TO_LABEL = {"headwind": "逆风", "crosswind": "侧风"}         # 其余（tailwind）→ 顺风


def _parse_int(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def form_to_rule(fd):
    """表单 → 规则 dict；时段/风门槛不合法 → 抛 ValidationError。"""
    name = (fd.name or "").strip() or "新规则"
    times = []
    for seg in (fd.time_text or "").replace("，", ",").split(","):
        seg = seg.strip().replace("：", ":")
        if not seg:
            continue
        p = seg.split("-")
        if len(p) != 2 or timed.parse_hhmm(p[0]) is None or timed.parse_hhmm(p[1]) is None:
            raise ValidationError("时段应为 HHMM-HHMM（如 1500-1900），多段用逗号。\n无法解析：%s" % seg)
        times.append("%s-%s" % (p[0].strip(), p[1].strip()))
    days = [i + 1 for i, v in enumerate(fd.days) if v]           # 1=周一…7=周日(ISO)
    if len(days) == 7:
        days = []                                                # 全选 = 每天，规整为空
    ref = (fd.ref_rwy or "").strip().upper()
    ref_rwy = ("RW" + ref) if ref else None
    wkind = _WKIND_TO_JSON.get((fd.wind_kind or "").strip(), "tailwind")
    wmin = _parse_int(fd.wind_min)
    if wmin is not None and ref_rwy is None:
        raise ValidationError("填了风分量节数就得选一条参照跑道（顺/逆/侧风分量相对它算）。")
    ceil = _parse_int(fd.ceiling)
    vis = _parse_int(fd.visibility)
    ceilcov = ((fd.ceiling_cover or "").strip().upper() or "SCT") if ceil is not None else None
    return {"name": name,
            "cond": {"time_jst": times, "days": days, "ref_runway": ref_rwy, "wind_kind": wkind,
                     "wind_min_kt": wmin, "ceiling_min_ft": ceil, "ceiling_cover": ceilcov,
                     "visibility_min_m": vis},
            "dep": {"runways": list(fd.dep_runways), "sids": list(fd.dep_sids)},
            "arr": {"runways": list(fd.arr_runways), "stars": list(fd.arr_stars),
                    "iaps": list(fd.arr_iaps)}}


def rule_to_form(rule):
    """规则 dict → 表单快照。"""
    cond = rule.get("cond", {}) or {}
    dep, arr = rule.get("dep", {}) or {}, rule.get("arr", {}) or {}
    dset = set(cond.get("days") or [])
    return FormData(
        name=rule.get("name") or "",
        time_text=",".join(cond.get("time_jst") or []),
        days=[(i + 1) in dset for i in range(7)],
        ref_rwy=(cond.get("ref_runway") or "").replace("RW", ""),
        wind_kind=_WKIND_TO_LABEL.get(cond.get("wind_kind"), "顺风"),
        wind_min="" if cond.get("wind_min_kt") is None else str(cond.get("wind_min_kt")),
        ceiling="" if cond.get("ceiling_min_ft") is None else str(cond.get("ceiling_min_ft")),
        ceiling_cover=cond.get("ceiling_cover") or "SCT",
        visibility="" if cond.get("visibility_min_m") is None else str(cond.get("visibility_min_m")),
        dep_runways=dep.get("runways") or [], dep_sids=dep.get("sids") or [],
        arr_runways=arr.get("runways") or [], arr_stars=arr.get("stars") or [],
        arr_iaps=arr.get("iaps") or [])


def blank_rule():
    return {"name": "新规则",
            "cond": {"time_jst": [], "days": [], "ref_runway": None, "wind_kind": "tailwind",
                     "wind_min_kt": None, "ceiling_min_ft": None, "ceiling_cover": None,
                     "visibility_min_m": None},
            "dep": {"runways": [], "sids": []},
            "arr": {"runways": [], "stars": [], "iaps": []}}


def ops_row_values(rule):
    """规则 → 表格一行（名称/时段/星期/风/天气/离场/进场）。"""
    cond = rule.get("cond", {}) or {}
    tm = ",".join(cond.get("time_jst") or []) or "全天"
    days = cond.get("days") or []
    dtxt = "".join("一二三四五六日"[d - 1] for d in sorted(days) if 1 <= d <= 7) if days else "每天"
    ref = (cond.get("ref_runway") or "").replace("RW", "")
    wmin = cond.get("wind_min_kt")
    klabel = _WKIND_TO_LABEL.get(cond.get("wind_kind"), "顺风")
    wind = ("%s≥%s@%s" % (klabel, wmin, ref)) if (wmin is not None and ref) else "默认"
    wxp = []
    if cond.get("ceiling_min_ft") is not None:
        wxp.append("云≥%s" % cond["ceiling_min_ft"])
    if cond.get("visibility_min_m") is not None:
        wxp.append("能≥%s" % cond["visibility_min_m"])
    wx = "·".join(wxp) or "-"
    dep = ",".join(r.replace("RW", "") for r in (rule.get("dep", {}).get("runways") or [])) or "-"
    arr = ",".join(r.replace("RW", "") for r in (rule.get("arr", {}).get("runways") or [])) or "-"
    return [rule.get("name") or "(未命名)", tm, dtxt, wind, wx, dep, arr]


def move_rule(rules, from_idx, to_idx):
    """列表内移动一条规则（拖拽排序）→ 新的选中下标；越界/原地 → None（调用方不必刷新）。"""
    n = len(rules)
    if not (0 <= from_idx < n):
        return None
    to_idx = max(0, min(int(to_idx), n - 1))
    if to_idx == from_idx:
        return None
    rules.insert(to_idx, rules.pop(from_idx))
    return to_idx


class OpsEditorModel:
    """F23 编辑器的整套状态机（纯数据，零控件）：载入/切机场/增删改/复制/重排/脏标记/提交/保存。
    UI 只订阅它的状态重画。多机场隔离靠 commit_current()：切机场或保存前把工作副本写回全量 dict。"""

    def __init__(self, dat_path):
        self.dat_path = dat_path
        self.all = operations.load_operations()
        self.icao = None
        self.rules = []
        self.sel = None
        self.dirty = False                               # 规则列表有未保存改动
        self.form_dirty = False                          # 表单相对所选规则有未应用改动
        self.ms = {k: MultiSelectModel() for k in
                   ("dep_rwy", "dep_sid", "arr_rwy", "arr_star", "arr_iap")}
        self.runway_choices = []                         # 换向门槛的参照跑道（短名）

    # ---- 查 ----
    def existing_airports(self):
        return operations.airports(self.all)

    def existing_text(self):
        aps = self.existing_airports()
        return ("已有规则：" + " ".join(aps)) if aps else "（暂无机场规则；键入 ICAO + 载入即可新建）"

    def load_airport(self, icao):
        """查·机场级：提交当前机场 → 读该机场 CIFP 候选填四类多选 + 载入其规则。返回 True/False（是否有 CIFP 数据）。"""
        icao = (icao or "").strip().upper()
        if not icao:
            return True
        self.commit_current()
        self.icao = icao
        try:
            rwys = sorted(procedures._parse_runways(icao, self.dat_path), key=procedures._rw_sort_key)
        except Exception:
            rwys = []
        try:
            procs = procedures.enumerate_procedures(icao, self.dat_path)
            sids = sorted(procs.get("SID", {}).keys())
            stars = sorted(procs.get("STAR", {}).keys())
        except Exception:
            sids = stars = []
        try:
            iaps = procedures.enumerate_approaches(icao, self.dat_path)
        except Exception:
            iaps = []
        self.ms["dep_rwy"].set_items([(r.replace("RW", ""), r) for r in rwys])
        self.ms["dep_sid"].set_items([(s, s) for s in sids])
        self.ms["arr_rwy"].set_items([(r.replace("RW", ""), r) for r in rwys])
        self.ms["arr_star"].set_items([(s, s) for s in stars])
        self.ms["arr_iap"].set_items([(a["name"], a["ident"]) for a in iaps])   # 显示合成名、存回 ident
        self.runway_choices = [""] + [r.replace("RW", "") for r in rwys]
        self.rules = operations.airport_rules(self.all, icao)
        self.sel = None
        self.form_dirty = False
        has_cifp = bool(rwys or sids or stars or iaps)
        if not has_cifp:
            print(f"ℹ️ {icao} 无 CIFP 程序数据，候选为空（仍可建规则、但选不出跑道/程序）。")
        return has_cifp

    def rows(self):
        return [ops_row_values(r) for r in self.rules]

    def select_rule(self, idx):
        """选中并回填第 idx 条 → FormData（UI 拿它填表单）；越界 → None。"""
        if not (0 <= idx < len(self.rules)):
            return None
        self.sel = idx
        fd = rule_to_form(self.rules[idx])
        self._load_form_selection(fd)
        self.form_dirty = False
        return fd

    def _load_form_selection(self, fd):
        self.ms["dep_rwy"].set_selection(fd.dep_runways)
        self.ms["dep_sid"].set_selection(fd.dep_sids)
        self.ms["arr_rwy"].set_selection(fd.arr_runways)
        self.ms["arr_star"].set_selection(fd.arr_stars)
        self.ms["arr_iap"].set_selection(fd.arr_iaps)

    def clear_form(self):
        fd = FormData()
        self._load_form_selection(fd)
        self.form_dirty = False
        return fd

    def read_selection(self, fd):
        """把五个多选的当前选择灌进 FormData（UI 在收表单时调）。"""
        fd.dep_runways = self.ms["dep_rwy"].read()
        fd.dep_sids = self.ms["dep_sid"].read()
        fd.arr_runways = self.ms["arr_rwy"].read()
        fd.arr_stars = self.ms["arr_star"].read()
        fd.arr_iaps = self.ms["arr_iap"].read()
        return fd

    # ---- 增 / 改 / 删 / 复制 / 排序 ----
    def add_rule(self):
        """增：追加空白规则、选中 → FormData。未载入机场 → 抛 ValidationError。"""
        if not self.icao:
            raise ValidationError("请先在上方键入机场 ICAO 并「载入」。")
        blank = blank_rule()
        self.rules.append(blank)
        self.sel = len(self.rules) - 1
        self.dirty = True
        fd = rule_to_form(blank)
        self._load_form_selection(fd)
        self.form_dirty = False
        return fd

    def apply_rule(self, fd):
        """改：把表单收进所选规则。未选中 / 校验失败 → 抛 ValidationError。"""
        if self.sel is None:
            raise ValidationError("请先在左侧选中一条规则（或「＋ 新增规则」）。")
        rule = form_to_rule(self.read_selection(fd))
        self.rules[self.sel] = rule
        self.dirty = True
        self.form_dirty = False
        return rule

    def commit_form(self, fd):
        """切换规则前的【静默】提交（校验失败就放弃，不打扰用户）。返回是否提交了。"""
        if self.sel is None or not self.form_dirty:
            return False
        if not (0 <= self.sel < len(self.rules)):
            return False
        try:
            self.rules[self.sel] = form_to_rule(self.read_selection(fd))
        except ValidationError:
            return False
        self.dirty = True
        self.form_dirty = False
        return True

    def dup_rule(self, fd):
        """复用：深拷贝所选规则（含全部条件/程序），改名"…副本"、插其后 → FormData。
        免得相同的跑道/SID/STAR/IAP 每条都重输。"""
        if self.sel is None:
            raise ValidationError("请先选中一条规则再复制。")
        if self.form_dirty:                              # 先提交当前表单编辑，复制所见
            self.rules[self.sel] = form_to_rule(self.read_selection(fd))
        dup = copy.deepcopy(self.rules[self.sel])
        dup["name"] = (dup.get("name") or "规则") + " 副本"
        self.rules.insert(self.sel + 1, dup)
        self.sel += 1
        self.dirty = True
        ndf = rule_to_form(dup)
        self._load_form_selection(ndf)
        self.form_dirty = False
        return ndf

    def delete_rule(self):
        """删（UI 负责先确认）→ 清空后的 FormData。"""
        if self.sel is None:
            return None
        del self.rules[self.sel]
        self.sel = None
        self.dirty = True
        return self.clear_form()

    def move_rule(self, from_idx, to_idx):
        """拖拽排序（顺序只用于保持「好天→恶天」成对相邻，非全局优先级）。"""
        new = move_rule(self.rules, from_idx, to_idx)
        if new is None:
            return None
        self.sel = new
        self.dirty = True
        return new

    # ---- 提交 / 保存 ----
    def commit_current(self):
        """把当前机场工作副本写回全量 dict（切机场/保存前调，隔离多机场）。"""
        if self.icao:
            self.all[self.icao] = {"rules": list(self.rules)}

    def save(self, fd=None):
        """持久化：提交当前表单 + 当前机场 → 整体原子写 operation.json（剔除空机场）。返回机场数｜None（失败）。"""
        if fd is not None and self.sel is not None and self.form_dirty:
            self.rules[self.sel] = form_to_rule(self.read_selection(fd))   # 校验失败 → 抛给 UI
            self.form_dirty = False
        self.commit_current()
        if not operations.save_operations(self.all):
            return None
        self.all = operations._prune(self.all)           # 内存同步剔除空机场
        self.dirty = False
        n = len(operations.airports(self.all))
        print(f"💾 运行规则已保存到 operation.json（{n} 个机场）。")
        return n

    @property
    def has_unsaved(self):
        return bool(self.dirty or self.form_dirty)


# ================= 控件启停 / Volanta 状态文本 =================

def enabled_controls(ready, busy, vsyncing, has_dat, has_scenery):
    """哪些控件当前可用（规则是数据，映射到具体控件由 UI 做）。"""
    locked = busy or vsyncing
    form_on = bool(ready and not locked)
    return {
        "form": form_on,                                 # 输入框 / 勾选 / 机型下拉
        "plan": form_on,
        "ops_editor": bool(ready and has_dat),           # F23：就绪即可，不受规划中影响
        "scenery_only": bool(form_on and has_scenery),   # 还需检测到地景目录
        "volanta": True if vsyncing else bool(ready and not busy),
        "volanta_cancel": bool(vsyncing),                # 同步中 → 按钮变「取消同步」
    }


def volanta_status_text(flown, vmeta):
    flown = flown or {}
    if not flown:
        return "Volanta：未读取到数据（可点「同步 Volanta」）"
    vmeta = vmeta or {}
    n_flights = vmeta.get("flights", sum(flown.values()))
    latest = vmeta.get("latest")
    txt = f"Volanta：已读取 {n_flights} 次飞行 / {len(flown)} 条航线"
    if latest:
        txt += f"（更新于 {latest}）"
    return txt


def init_status_text(scenery_map, aip):
    scen_txt = "未检测到地景" if scenery_map is None else f"地景 {len(scenery_map)}"
    return f"✅ 初始化完成 · 导航数据已读取 · {scen_txt} · AIP {len(aip)}"
