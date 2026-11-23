# ================= 视图模型（纯数据派生 + 各界面 Model；零 GUI 依赖）=================
# 本模块【绝不 import 任何 GUI 框架】——不 import tkinter、不 import flet。
# 判定标准：一个函数若删掉所有控件调用后仍有实质逻辑，它就该在这里（或 controller.py）。
#
# 每个界面 = 一个纯数据 Model（可 headless 单测） + 一层薄渲染：
#   ResultModel      结果卡 → span 列表（tk 画成 tag / flet 画成 TextSpan）
#   MapModel         航路坐标 → 分段折线 + 符号 / 标签（含按缩放避让）+ bounds
#   AircraftModel    机型库 → 过滤 / 显示串↔SimBrief id
#   ProcPanelModel   F20/F21/F23：跑道·SID/STAR·天气·EOBT·运行规则 的完整状态机
#   AipTableModel    F21 多 AIP 航路选择表
#   OpsEditorModel   F23 运行规则编辑器（含 MultiSelectModel）
# 两套 UI 共用同一个 Model → 行为天然一致。

import copy
import math
import re
import time

from . import ntrack, operations, procedures, router, timed, weather
from . import eta as eta_mod
from .aircraft import aircraft_choices, perf_of
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


def xfer_alt(s):
    """移管表的高度原文 → 显示串：`at F220`/`F220`→`FL220`，`at A110`/`A110`→`11000ft`，
    `at BLW`→`≤`、`at ABV`→`≥`，`-`→`—`；认不出的写法原样（如 `COOR with E DEP`）。"""
    s = " ".join((s or "").split())
    if s in ("", "-"):
        return "—"
    s = re.sub(r"\bat\s+BLW\s*", "≤", s)
    s = re.sub(r"\bat\s+ABV\s*", "≥", s)
    s = re.sub(r"\bat\s+", "", s)
    s = re.sub(r"\bF(\d{3})\b", r"FL\1", s)
    return re.sub(r"\bA(\d{3})\b", lambda m: "%dft" % (int(m.group(1)) * 100), s)


def xfer_lines(rows):
    """移管行 → 每行一个显示串「点  高度  移管元 → 移管先」。同一点、同一组席位、只差条件（机型 / 跑道）的相邻行
    并成一行：`MUGIE  FL190 (JET) / FL170 (PROP)  …`。移管点 / 移管先写「→」的照原样显示（表里就是这么写的）。"""
    out = []
    for r in rows or ():
        alt = xfer_alt(r.get("alt")) + (" (%s)" % r["cond"] if r.get("cond") else "")
        key = (r.get("fix"), r.get("from"), r.get("to"))
        if r.get("cond") and out and out[-1][0] == key and out[-1][2]:
            out[-1][1].append(alt)
            continue
        out.append([key, [alt], bool(r.get("cond"))])
    lines = []
    for (fix, frm, to), alts, _c in out:
        hand = " → ".join(x for x in (frm, to) if x) if to != "→" else (frm + " →")
        lines.append("  ".join(x for x in (fix or "—", " / ".join(alts), hand) if x))
    return lines


def rule_basis_lines(ops):
    """生成航路的端点依据 → [(文字, span 类别)]：按哪条运行规则 / 哪份实测接的哪几条程序；没限定的一侧不写。"""
    if not ops:
        return []
    out = []
    eobt = ops.get("eobt_min")
    for side, name, word in (("dep", "离场", "SID"), ("arr", "进场", "STAR")):
        e = ops.get(side)
        if not e:
            continue
        icao = ops.get(side + "_icao", "")
        used = ops.get(side + "_used") or []
        pt = ops.get(side + "_point")
        if side in (ops.get("unreached") or []):
            continue                                     # 接不上的一侧由 note 说明
        tail = ("→ 接 %s（%s）" % (" / ".join(used), pt)) if used and pt else ("→ 在 %s 衔接" % pt if pt else "")
        rwys = "/".join(r.replace("RW", "") for r in e.get("runways") or [])
        seen = ops.get("nt", {}).get(icao)
        measured = ("实测（%s）" % seen) if seen else "实测"
        when = (" EOBT %02d:%02d" % (eobt // 60, eobt % 60)) if (side == "dep" and eobt is not None) else ""
        rules = "、".join("「%s」" % r for r in e.get("rules") or [])
        if e["source"] == "measured":
            text = "🎯 %s：%s %s 在用 %s，未匹配到运行规则 → 按实测运行%s" % (name, icao, measured, rwys, tail)
        elif e.get("measured"):
            text = "🎯 %s：按 %s %s 匹配运行规则%s %s" % (name, icao, measured, rules, tail)
        else:
            text = "🎯 %s：按 %s 运行规则%s（%s%s）%s" % (name, icao, rules, "风/天气" if not when else "风/天气 ·", when, tail)
        if e["source"] == "rule+measured":
            text += "；实测 %s 未匹配到规则，也按实测运行" % "/".join(r.replace("RW", "") for r in e.get("unmatched") or [])
        out.append((text, "muted"))
    if ops.get("note"):
        out.append(("ℹ️ " + ops["note"], "warn"))
    return out


def result_spans(plan, has_map=True):
    """FlightPlan → span 列表（结果卡的全部内容）。UI 只负责把 span 画成 Tk tag 或 ft.TextSpan。
    这是结果卡在两套 UI 下逐字一致的关键——文案与配色语义全在这里定死。"""
    dep, arr = plan.dep, plan.arr
    active = getattr(plan, "active_sims", None)          # 问题1：按所用模拟器标注地景
    out = []

    def ins(text, style="", action=None):
        out.append(Span(text, style, action))

    def _map_link(mp, text="       🗺️ 在地图查看本航路\n"):
        """mp=(coords,title)：为该条航路插入一个独立的「在地图查看」链接。"""
        if not (has_map and mp and mp[0]):
            return
        coords, title = mp
        ins(text, "maplink", ("map", coords, title))

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

    def _xfer(x, pad):
        """F32 进近移管：x = vatjpn.match_arrival 的结果。None（目的场不在移管表里）→ 不显示。"""
        if x is None:
            return
        if not x:
            ins(pad + "└ 进近移管：VATJPN 移管表里没有与这条航路对应的进场\n", "muted")
            return
        ins(pad + "└ 进近移管（VATJPN %s）%s：\n" % (x["route"], "" if x.get("full") else "（按进场门近似匹配）"), "muted")
        for ln in xfer_lines(x.get("rows")):
            ins(pad + "    " + ln + "\n", "muted")

    def _alt(v):
        """AIP 改端（该行此刻不适用）：原因 + 改端航路 + 依据 + 进近移管 + 地图；改不了 → 一行原因。"""
        if not v:
            return
        if not v.get("route"):
            ins("       ℹ️ " + v.get("note", "") + "\n", "warn")
            return
        ins("       🌙 " + v.get("why", "") + "\n", "warn")
        ins("       ↳ 按运行规则改端（非官方 AIP）：\n", "muted")
        ins("         " + v["route"] + "\n", "aip")
        extra = [x for x in (_dev(v.get("dist")),
                             ("较原 AIP %+.0f NM" % (v["dist"] - v["aip_dist"])) if v.get("aip_dist") else "",
                             v.get("where") or "") if x]
        if extra:
            ins("         └ " + " · ".join(extra) + "\n", "muted")
        if v.get("eta"):
            ins("         └ " + flight_time_text(v["eta"]) + "\n", "muted")
        for text, kind in rule_basis_lines(v.get("ops")):
            ins("         " + text + "\n", kind)
        _xfer(v.get("xfer"), "         ")
        _map_link(v.get("map"), "         🗺️ 在地图查看改端航路\n")

    if plan.aip_routes:
        ins("\n  📜 AIP 航路\n", "section")
        if getattr(plan, "aip_alt_note", None):
            ins("  ℹ️ " + plan.aip_alt_note + "\n", "warn")
        _dists = plan.aip_route_dists or []
        _maps = plan.aip_maps or []
        _alts = getattr(plan, "aip_alts", None) or []
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
            _et = getattr(plan, "aip_etas", None) or []
            if i - 1 < len(_et) and _et[i - 1]:
                ins("       └ " + flight_time_text(_et[i - 1]) + "\n", "muted")
            _xf = getattr(plan, "aip_xfers", None) or []
            _xfer(_xf[i - 1] if i - 1 < len(_xf) else None, "       ")
            _map_link(_maps[i - 1] if i - 1 < len(_maps) else None)
            _alt(_alts[i - 1] if i - 1 < len(_alts) else None)

    if plan.generated_route:
        ins("\n  🧭 生成航路（本地导航数据，非官方 AIP）\n", "section")
        ins("  " + plan.generated_route + "\n", "aip")
        _gd = _dev(plan.generated_route_dist)
        if _gd:
            ins("  " + _gd + "\n", "muted")
        if getattr(plan, "gen_eta", None):
            ins("  " + flight_time_text(plan.gen_eta) + "\n", "muted")
        for text, kind in rule_basis_lines(getattr(plan, "generated_route_ops", None)):
            ins("  " + text + "\n", kind)
        _xfer(getattr(plan, "gen_xfer", None), "  ")
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
        # F29：核验与否必须如实说——把未核验的说成已核验，等于把「冒充真实航班号」的风险瞒下来
        if getattr(plan, "sim_callsign_verified", False):
            ins("     ✅ 已核验：该号不与现实航班号重合（连飞不会与真实航班撞号）\n", "success")
        else:
            ins("     ⚠️ 未能核验该号是否与现实航班号重合（断网/限流），连飞前请自行核对\n", "warn")

    ins("\n  🔗 查看 FlightAware 完整排班表:\n", "label")
    ins("  ", "label"); ins(plan.url + "\n", "link", ("url", plan.url))

    if plan.simbrief_url:
        ins("\n  🛩️ SimBrief 一键签派 : ", "label")
        ins("点击生成并查看simbrief计划（需登录）\n", "sblink", ("simbrief", plan.simbrief_url))
    return out


def error_spans(msg):
    return [Span(f"❌ 发生错误：{msg}\n", "warn")]


# ================= 地图（MapModel）=================
# 画法仿 Navigraph Charts：航段按 SID / enroute / STAR 分色，航点按 机场 / 导航台 / 航点 / 程序内的点 分符号，
# 航路名沿线写在线上。图元一多就互相压住 → 每个标签 / 符号都算好「放大到第几级起才不和更要紧的图元重叠」
# （min zoom，见 _declutter），UI 随缩放显隐。图元的像素尺寸定在这里（避让要按它算），map_view 照着画——两边必须一致。

MAP_APT_D = 22              # 机场符号直径（逻辑像素）
MAP_SYM_D = 18              # 航点 / 导航台符号直径
MAP_DOT_D = 13              # SID/STAR 程序里的终端区点（ITE16 这类，不在航路串里）：只画圆点
MAP_LBL_H = 18              # 航点名标签高
MAP_LBL_GAP = 3             # 标签与符号外沿的间隙
MAP_LBL_FONT = 11
MAP_LEG_H = 14              # 航路名标签高（压在航路线上）
MAP_LEG_FONT = 9
MAP_LETTER_SP = 0.8         # 标签字距（Navigraph 的标签字距很开）

# 微软雅黑 UI 粗体的字宽（em，PIL 实测）；数字一律 0.62，其它少见字符按 0.75 估
_GLYPH_EM = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ -.",
                     (.75, .68, .67, .79, .57, .56, .77, .82, .34, .47, .69, .55, 1.03,
                      .85, .82, .66, .82, .70, .60, .63, .78, .71, 1.08, .70, .65, .65, .30, .44, .29)))


def map_label_w(text, font=MAP_LBL_FONT):
    """标签外框宽（逻辑像素）= 字宽 + 字距 + 左右各 6（含 1 像素边框）。map_view 按它定框宽，_declutter 按它算重叠。"""
    em = sum(_GLYPH_EM.get(c, .62 if c.isdigit() else .75) for c in text)
    return math.ceil(em * font + MAP_LETTER_SP * len(text)) + 12


def map_sym_d(sym):
    """符号直径：机场 / 程序里的点 / 其余（航点、VOR、NDB）。"""
    return MAP_APT_D if sym == "apt" else (MAP_DOT_D if sym == "dot" else MAP_SYM_D)


def plan_maps(plan):
    """本次规划里所有【可看地图】的航路：[(coords, title)] —— 各 AIP 行 + 生成航路，跳过没几何的。
    它们做成同一个地图窗口里的标签页（每点一次 🗺️ 开一个新的地图窗口，见 ui_flet/map_window.py）。"""
    out = []
    alts = getattr(plan, "aip_alts", None) or []
    for i, mp in enumerate(getattr(plan, "aip_maps", None) or []):
        if mp and mp[0]:
            out.append(mp)
        v = alts[i] if i < len(alts) else None
        if v and v.get("map") and v["map"][0]:
            out.append(v["map"])                         # 改端版紧跟原 AIP（同一个对象：_open_map 按 is 匹配标签页）
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


def _merc(lat, lon):
    """Web-Mercator 世界坐标（0–1；x 向东、y 向南）。屏幕像素 = 它 × 256 × 2^zoom（与 flutter_map 同一投影）。"""
    s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
    return (lon + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def _unmerc(x, y):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y)))), x * 360.0 - 180.0


def _dir_deg(p, q):
    """(lat, lon) p→q 在屏幕上的方向角（度；0=向右、90=向下）。墨卡托保角，所以与缩放级无关。"""
    (x1, y1), (x2, y2) = _merc(*p), _merc(*q)
    return math.degrees(math.atan2(y2 - y1, x2 - x1))


_SIDES = (("up", -90.0), ("right", 0.0), ("down", 90.0), ("left", 180.0))


def _label_side(dirs):
    """航点名放在符号哪一侧：默认上方（Navigraph 的放法）；航路线正从那一侧进出（夹角 < 50°）就依次改试右、下、左。
    dirs = 从该点指向前后相邻点的屏幕方向角。"""
    for side, a in _SIDES:
        if all(abs((a - d + 180.0) % 360.0 - 180.0) >= 50.0 for d in dirs):
            return side
    return "up"


def _label_box(side, w, r):
    """标签外框相对符号中心的像素偏移 (左, 上, 右, 下)；r = 符号半径。"""
    h, g = MAP_LBL_H, MAP_LBL_GAP
    return {"up": (-w / 2, -(r + g + h), w / 2, -(r + g)), "down": (-w / 2, r + g, w / 2, r + g + h),
            "right": (r + g, -h / 2, r + g + w, h / 2), "left": (-(r + g + w), -h / 2, -(r + g), h / 2)}[side]


_ZOOM_LEVELS = [z / 2.0 for z in range(4, 31)]      # 避让按 2.0 … 15.0 每半级算一次
_CELL = 64.0                                         # 查重叠用的网格边长（像素）


def _declutter(items):
    """按缩放避让，返回每项的 min zoom（缩放 ≥ 它才显示）。
    items 按重要性从高到低排好，每项 (x, y, box, forced, parent, need)：x/y = 锚点的墨卡托世界坐标；
    box = 外框相对锚点的像素偏移 (左, 上, 右, 下)；forced = 永远显示（但照样挡住后面的）；
    parent = 依附的项（航点名依附它的符号：符号没显示，名字也不显示）；
    need = (世界长度, 所需像素)：航路名要等它那段线在屏幕上够长才放得下。
    从高缩放往低扫，每级按重要性逐个放，与已放的重叠就不放；某级放不下，往下各级也都不放——放大时只增不减，
    不会忽隐忽现。（墨卡托下图元间距随缩放 ×2、图元尺寸是固定像素，所以某级不重叠的到更高级也不重叠。）"""
    n = len(items)
    zmin = [0.0 if it[3] else None for it in items]
    alive = [True] * n
    for lv in reversed(_ZOOM_LEVELS):
        s = 256.0 * 2.0 ** lv
        grid, placed = {}, [False] * n
        for i, (x, y, (l, t, r, b), forced, parent, need) in enumerate(items):
            if not alive[i]:
                continue
            box = (x * s + l, y * s + t, x * s + r, y * s + b)
            cells = [(cx, cy) for cx in range(int(box[0] // _CELL), int(box[2] // _CELL) + 1)
                     for cy in range(int(box[1] // _CELL), int(box[3] // _CELL) + 1)]
            if not forced and ((parent is not None and not placed[parent])
                               or (need and need[0] * s < need[1])
                               or any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3]
                                      for c in cells for o in grid.get(c, ()))):
                alive[i] = False
                continue
            placed[i] = True
            for c in cells:
                grid.setdefault(c, []).append(box)
            if not forced:
                zmin[i] = lv
    return [z if z is not None else _ZOOM_LEVELS[-1] + 0.5 for z in zmin]


def route_legs(coords, route_str):
    """每条【腿】一个标注：`[{lat, lon, label, angle, seg, wlen}]`，label = 航路名(Y56…) / SID·STAR 程序名 / `DCT`。

    航路串里**能在坐标序列里找到同名航点**的 token = 换路点，其余 token = 名称（airway 或程序名）。
    按顺序把换路点对到坐标下标；两个换路点之间的所有坐标（`_parse_aip_route` 沿 airway 加密出来的
    中间点）同属一条腿 → 共用「该换路点之前那个名称」；没有名称就是直飞 `DCT`。

    这套规则对 SID/STAR 也自动成立：程序的航点不在航路串里（串里只有程序名），
    于是它们整段落进「程序名」那一腿——离场是首腿、进场是末腿。

    标注放在这条腿里【最长那一段】的中点（墨卡托中点，正落在屏幕上的直线上）；angle = 该段在屏幕上的走向
    （度，折到 ±90° 以内，字不倒着）；seg = 该段所属段（sid / enr / star，配色用）；
    wlen = 该段的墨卡托世界长度（避让时判断线够不够长、放不放得下字）。

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

    # 全段预览：AIP 串开头 / 结尾常把 SID / STAR 机体逐点展开（NANKO NIKEX KYOTO…），程序段里就切出一串 DCT 小腿——
    # 它们都是程序的一部分，并成一条腿、标程序名（SID 名在串首那条腿上，STAR 名在串尾那条腿上）
    segs = [c[3] if len(c) > 3 else "enr" for c in coords]
    merged = []                                   # [(起, 止, 名称, 所属段)]
    for a, b, name in spans:
        kinds = set(segs[a + 1:b + 1])
        kind = kinds.pop() if len(kinds) == 1 else "enr"
        if merged and kind in ("sid", "star") and merged[-1][3] == kind:
            pa, _pb, pname, _k = merged[-1]
            merged[-1] = (pa, b, (pname or name) if kind == "sid" else (name or pname), kind)
        else:
            merged.append((a, b, name, kind))

    world = [_merc(c[1], c[2]) for c in coords]
    legs = []
    for a, b, name, _kind in merged:
        k = max(range(a, b), key=lambda j: math.dist(world[j], world[j + 1]))
        (x1, y1), (x2, y2) = world[k], world[k + 1]
        lat, lon = _unmerc((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
        ang = ang - 180.0 if ang > 90.0 else (ang + 180.0 if ang <= -90.0 else ang)
        legs.append({"lat": lat, "lon": lon, "label": name or _DCT, "angle": ang, "seg": segs[k + 1],
                     "wlen": math.dist(world[k], world[k + 1])})
    return legs


_RWY_IDENT = re.compile(r"^\d{2}[LRC]?$")


def map_model(coords, title="", kinds=None):
    """航路坐标 → 地图的纯数据，UI 只负责把它翻译成具体图元：
      lines    [{seg, points}]  航段折线；seg = sid / enr / star（只有全段预览分得出 SID/STAR，其余全是 enr）
      markers  [{lat, lon, ident, label, tier, sym, style, side, sym_z, lbl_z}]
               tier 0=起降机场 / 1=换路点（ident 在航路串里，程序里的点除外）/ 2=加密中间点与程序里的点；
               sym = apt / VOR / NDB / fix / dot（dot = SID·STAR 程序里的终端区点）；style = 配色 apt / wpt / sid / star；
               side = 标签在符号哪一侧；sym_z / lbl_z = 符号 / 标签从第几级缩放起显示（见 _declutter）
      legs     [{lat, lon, label, angle, seg, z}]  每条腿的航路名（沿线写；见 route_legs）
      bounds / center / zoom（初始视野）/ title
    coords = [(ident, lat, lon[, seg]), …]（带 seg 的来自 procedures.full_route_coords(tagged=True)）；
    kinds = 与 coords 同序的导航台类型（'VOR' / 'NDB' / None，见 nav_kinds），不给就全按航点画。"""
    if not coords:
        return None
    n = len(coords)
    route = _route_from_title(title)
    majors = set(route.split())
    ends = (title or "").split("  ", 1)[0].split("→")      # 标题头 'RJTT→RJFF'
    segs = [c[3] if len(c) > 3 else "enr" for c in coords]
    kinds = list(kinds or [])
    pts = [(c[1], c[2]) for c in coords]

    # 一段航段归它【终点】所属的段：SID 末点（交接点）之后那段算 enroute、进入 STAR 的那段算 STAR
    lines = []
    for i in range(1, n):
        if lines and lines[-1]["seg"] == segs[i]:
            lines[-1]["points"].append(pts[i])
        else:
            lines.append({"seg": segs[i], "points": [pts[i - 1], pts[i]]})

    # 程序里的点（Navigraph 画成程序色的圆点）：SID / STAR 段上除交接点（SID 末点）外的点——包括 AIP 串开头
    # 逐点展开的 SID 机体（NANKO NIKEX KYOTO…，它们虽在航路串里，却是程序的一部分）。重要性按加密中间点算。
    sid_exit = max((i for i in range(n) if segs[i] == "sid"), default=-1)
    markers = []
    for i, c in enumerate(coords):
        end = i in (0, n - 1)
        proc = not end and segs[i] in ("sid", "star") and i != sid_exit
        tier = 0 if end else (1 if c[0] in majors and not proc else 2)
        nav = kinds[i] if i < len(kinds) else None
        label = c[0]
        if end and _RWY_IDENT.match(c[0]) and len(ends) == 2:    # 全段预览的首尾是跑道头（'34R'）→ 'RJTT 34R'
            label = "%s %s" % (ends[0] if i == 0 else ends[1], c[0])
        dirs = [_dir_deg(pts[i], pts[j]) for j in (i - 1, i + 1) if 0 <= j < n and pts[j] != pts[i]]
        markers.append({"lat": c[1], "lon": c[2], "ident": c[0], "label": label, "tier": tier,
                        "sym": "apt" if end else (nav or ("dot" if proc else "fix")),
                        "style": "apt" if end else (segs[i] if proc else "wpt"), "side": _label_side(dirs)})
    legs = route_legs(coords, route)

    # 避让的重要性：机场 > 换路点 > 航路名 > 加密中间点（同档内符号先于名字）。机场的符号与名字永远显示。
    world = [_merc(*p) for p in pts]
    items, slots, sym_at = [], [], {}

    def add(target, key, xy, box, forced=False, parent=None, need=None):
        items.append((xy[0], xy[1], box, forced, parent, need))
        slots.append((target, key))
        return len(items) - 1

    def add_sym(i, forced):
        r = map_sym_d(markers[i]["sym"]) / 2.0
        sym_at[i] = add(markers[i], "sym_z", world[i], (-r, -r, r, r), forced)

    def add_lbl(i, forced):
        m = markers[i]
        box = _label_box(m["side"], map_label_w(m["label"]), map_sym_d(m["sym"]) / 2.0)
        add(m, "lbl_z", world[i], box, forced, parent=sym_at[i])

    by_tier = [[i for i in range(n) if markers[i]["tier"] == t] for t in (0, 1, 2)]
    for i in by_tier[0]:
        add_sym(i, True)
    for i in by_tier[0]:
        add_lbl(i, True)
    for i in by_tier[1]:
        add_sym(i, False)
    for i in by_tier[1]:
        add_lbl(i, False)
    for lg in legs:
        w, h, a = map_label_w(lg["label"], MAP_LEG_FONT), MAP_LEG_H, math.radians(lg["angle"])
        bw, bh = abs(w * math.cos(a)) + abs(h * math.sin(a)), abs(w * math.sin(a)) + abs(h * math.cos(a))
        add(lg, "z", _merc(lg["lat"], lg["lon"]), (-bw / 2, -bh / 2, bw / 2, bh / 2),
            need=(lg.pop("wlen"), w + MAP_APT_D + 4))         # 线段要容得下字 + 两端各半个符号
    for i in by_tier[2]:
        add_sym(i, False)
    for i in by_tier[2]:
        add_lbl(i, False)
    for (target, key), z in zip(slots, _declutter(items)):
        target[key] = z

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
    return {"lines": lines, "markers": markers, "legs": legs,
            "bounds": bounds, "center": center, "zoom": zoom, "title": title}


def nav_kinds(coords):
    """coords 里每个点是不是导航台：['VOR' / 'NDB' / None, …]，地图据此选符号（router.nav_kind：只查已建好的航路图）。"""
    return [router.nav_kind(c[0], c[1], c[2]) for c in coords or []]


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


IAP_ONLY_TAG = "无STAR·走IAP"


def runway_items(rows, wind, in_use=None, prefer=()):
    """跑道行 → 显示项（长度[米] + 风分量 + 适航），并排序。
    rows=[(rwy_id, length_ft, labels)]；返回 [{disp, rwy, labels, ok, hw, in_use}]。

    `in_use` = 实测正在使用的跑道集合（ntrack，仅羽田）。命中的标「✅在用」并**排在最前**——
    于是「按风默认」自然落在实测在用的跑道里。其余跑道仍在下拉里可选（用户永远能改选）。

    风况合规时**能衔接程序的跑道排在只有 IAP 的前面**：只有前者全部超限，才默认选后者。
    否则风向略偏就会默认丢掉 STAR（RJSS 西北风默认到 VOR/DME RWY30，而不是有 STAR 的 RW27）。"""
    used = set(in_use or ())
    any_proc = any(r[2] for r in rows)
    items = []
    for rwy_id, length_ft, labels in rows:
        it = {"rwy": rwy_id, "len": length_ft, "labels": list(labels), "in_use": rwy_id in used}
        _set_item_wind(it, wind, any_proc)
        items.append(it)
    items.sort(key=lambda it: (not it["in_use"], not it["ok"], not it["labels"], -it["hw"],
                               it["rwy"] not in prefer, procedures._rw_sort_key(it["rwy"])))
    return items


def _set_item_wind(it, wind, any_proc):
    """按风算一条跑道的显示文字和风况。实时天气刷新时也走这里（不重新排序）。"""
    short = it["rwy"].replace("RW", "")
    parts = [short] + (["%.0fm" % (it["len"] * 0.3048)] if it["len"] else [])   # 东亚习惯：显示层用米（数据底层为英尺）
    wdesc, ok, hw = wind_desc(wind, it["rwy"])
    if wdesc:
        parts.append(wdesc)
    if it["in_use"]:
        parts.append("✅在用")
    if any_proc and not it["labels"]:
        parts.append(IAP_ONLY_TAG)
    it.update(disp=" · ".join(parts), ok=ok, hw=hw)


def live_section(snap, querying=False):
    """实时标签页一个机场的文字（snap = LiveService.snapshot）。"""
    snap = snap or {}
    wx = snap.get("wx") or {}
    src = {"swim": "✅ 真实 ATIS（SWIM）", "generated": "⚠️ 生成 ATIS（非官方）"}.get(
        snap.get("source"), "暂无 ATIS")
    at = snap.get("fetched_at")
    title = "%s  ·  %s" % (snap.get("icao", ""), src)
    if snap.get("report_z"):
        title += " %s" % snap["report_z"]
    if snap.get("received_at"):
        title += "  ·  查询于 %s" % time.strftime("%H:%M", time.localtime(snap["received_at"]))
    if at:
        title += "  ·  天气刷新 %s" % time.strftime("%H:%M", time.localtime(at))
    if querying:
        title += "  ·  查询中…"
    raw = wx.get("metar_raw")
    metar = ("METAR %s" % raw) if raw else "METAR 暂无"
    if wx.get("source") == "grid":
        metar += "\n《Open-Meteo 模型合成·非实测》"
    taf = wx.get("taf_raw") or ""
    return {"title": title, "atis": snap.get("atis") or "—", "metar": metar, "taf": taf}


def live_status_text(fenix_on, swim_on, has_creds, swim_status):
    """实时标签页的服务状态行。"""
    fenix = "Fenix：已开启（在 MCDU 的 ATIS 页把 NETWORK 选 BEYONDATC）" if fenix_on else "Fenix：未开启"
    if not swim_on:
        swim = "SWIM：未开启"
    elif not has_creds:
        swim = "SWIM：请先保存账号"
    else:
        swim = {"bad_login": "SWIM：账号或密码错误，请重新保存",
                "auth": "SWIM：登录被拒，稍后再试",
                "no_creds": "SWIM：无法读取账号，请重新保存",
                "error": "SWIM：查询出错，下次查询重试",
                "ok": "SWIM：已就绪",
                "none": "SWIM：已就绪，点「更新 ATIS」或在 MCDU 按 SEND 查询"}.get(swim_status, "SWIM：已就绪")
    return "%s    %s" % (fenix, swim)


def live_blocked_text(swim_on, has_creds):
    """点「更新 ATIS」但查不了时的提示。开关和账号都没问题时，只剩账号或密码错误这一种情况。"""
    if not swim_on:
        return "无法查询 SWIM：请打开「SWIM 真实 ATIS」开关"
    if not has_creds:
        return "无法查询 SWIM：请先保存账号"
    return "无法查询 SWIM：账号或密码错误，请重新输入并保存"


def filter_labels(all_labels, typed):
    """SID/STAR 可搜索下拉：输入即过滤（无命中则回退全量）。"""
    t = (typed or "").strip().lower()
    if not t:
        return list(all_labels)
    return [v for v in all_labels if t in v.lower()] or list(all_labels)


def _hhmm(m):
    return "%02d:%02d" % (int(m) // 60 % 24, int(m) % 60)


def _dur(minutes):
    m = int(round(minutes))
    return "%d:%02d" % (m // 60, m % 60)


def default_eta_model(plan):
    """面板没拿到 controller 的 EtaModel 时（旧测试桩）：按表单机型的剖面、无风。"""
    perf, aid, fb = perf_of(((getattr(plan, "sb_base", None) or {}).get("actype")) or "")
    return eta_mod.EtaModel(plan.dep, plan.arr, perf, None, aid, fb)


def flight_time_text(r):
    """结果卡每条航路下的一行（F36）：EOBT → 落地的预计时长 + 巡航高度 + 巡航段平均顺 / 逆风。"""
    if not r:
        return ""
    if r.get("source") == "legacy":
        return "⏱️ 预计 EOBT → 落地约 %s（机型库缺失，按旧算法粗估）" % _dur(r["block"])
    w = r.get("wind_kt") or 0.0
    wind = ("巡航平均%s风 %.0f kt" % ("顺" if w > 0 else "逆", abs(w))) if r.get("source") == "wind" and abs(w) >= 1 \
        else ("无风估算" if r.get("source") != "wind" else "巡航基本无风")
    return "⏱️ 预计 EOBT → 落地约 %s（FL%03d · %s）" % (_dur(r["block"]), r["fl"], wind)


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


# EOBT 默认值与星期推算放在 operations（生成航路按运行规则定端点也要用同一份），这里保留原名
EOBT_PREP_MIN = operations.EOBT_PREP_MIN
default_eobt_min = operations.default_eobt_min
eobt_weekday = operations.eobt_weekday


def aip_label(i, c):
    """AIP 航路下拉紧凑标签：[序号] 时段 · 距离 · 航路首…尾；改端行标「🌙改端」。"""
    restr = ("🌙改端 · " if c.get("variant") else "") + (c.get("restr") or "全时段")
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
        # EOBT 默认＝当前 JST + 30 分钟（可改）。规划时已按它定了生成航路的端点 → 用同一个值，免得分钟跳变对不上
        nm = proc.get("eobt_min")
        if nm is None:
            nm = default_eobt_min()
        self.eobt = "%02d%02d" % (nm // 60, nm % 60)
        self.plan_eobt_min = nm                          # 规划时的默认 EOBT：没改过 = 按「现在」规划，实测照用
        self.regen_request = None                       # 要按新的 EOBT / 开关重算：{kind: gen|aip, eobt, apply_ops}（UI 取走后清空）
        # AIP 改端：规划时的 AIP 原始行、原候选、判据（operations.aip_key）；改 EOBT / 开关后判据变了就后台重算
        self.aip_rows = proc.get("aip_rows")
        self.aip_orig = proc.get("aip_orig")
        self.aip_key = proc.get("aip_key")
        self.aip_strict = bool(proc.get("aip_strict"))
        self.aip_gc = proc.get("aip_gc") or self.dist_nm
        # F36：ETA 模型（机型剖面 + 高空风），与 controller 规划时同一个对象；没有（旧测试桩）→ 按默认机型、无风
        self.eta_model = proc.get("eta") or default_eta_model(plan)

        self.sel_idx = 0
        self.items = {"dep": [], "arr": []}              # [{disp,rwy,labels,ok,hw}]
        self.sel_rwy = {"dep": None, "arr": None}        # rwy_id ("RW34L") | None
        self.proc_labels = {"dep": [], "arr": []}
        self.sel_proc = {"dep": "", "arr": ""}
        self.ops_label = {"dep": "", "arr": ""}
        self.circling = None                             # 目视盘旋规则生效时：{盘旋落地的跑道: 飞进近的跑道}
        self.select_candidate(proc.get("selected", 0))

    # ---- 只读派生 ----
    @property
    def nt_label(self):
        """实测运用状况的显示文字（随 EOBT 变：改到报文覆盖时段之外会注明「按运行规则」）。"""
        return {"dep": self._nt_text("dep"), "arr": self._nt_text("arr")}
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

    def update_wx(self, icao, wx):
        """实时天气刷新：更新天气文字和跑道风况。不改用户选的跑道和程序，也不重新匹配规则。
        → 有没有更新到这个面板。"""
        changed = False
        for side, code in (("dep", self.dep_icao), ("arr", self.arr_icao)):
            if code != icao or not wx:
                continue
            if side == "dep":
                self.dep_wx = wx
            else:
                self.arr_wx = wx
            self.wind[side] = wx.get("wind")
            self.sky[side] = weather.parse_sky(wx.get("metar_raw"))
            any_proc = any(it["labels"] for it in self.items[side])
            for it in self.items[side]:
                _set_item_wind(it, self.wind[side], any_proc)
            changed = True
        return changed

    def aip_labels(self):
        return [aip_label(i, c) for i, c in enumerate(self.candidates)]

    def _circling_from_rwy(self):
        """当前选的落地跑道属于生效中的目视盘旋规则 → 飞进近的跑道；否则 None（用户改选别的跑道后就不算盘旋了）。"""
        return (self.circling or {}).get(self.sel_rwy["arr"])

    def _iap_only(self, side):
        """当前选中的跑道只能走 IAP：本侧别的跑道有程序，唯独它没有。"""
        items = self.items[side]
        sel = next((i for i in items if i["rwy"] == self.sel_rwy[side]), None)
        return bool(sel) and not sel["labels"] and any(i["labels"] for i in items)

    def hint_text(self):
        notes = proc_notes(self.candidates[self.sel_idx]) if self.candidates else []
        if self._iap_only("arr") and not self._circling_from_rwy():
            rwy = self.sel_rwy["arr"]
            names = [a["name"] for a in procedures.enumerate_approaches(self.arr_icao, self.dat_path)
                     if a["runway"] == rwy or not a["runway"]]
            notes.append("%s 没有能衔接本航路的 STAR，请按 IAP 进近（%s）" % (rwy, " / ".join(names)))
        return ("ℹ️ " + "；".join(notes)) if notes else ""

    def proc_hint(self, side):
        """SID/STAR 下拉框为空时的占位文字。"""
        if self._iap_only(side):
            return "（该跑道无可衔接的 STAR）"
        return "（该机场无 SID）" if side == "dep" else "（该机场无 STAR）"

    def summary_text(self):
        dep_rwy = (self.sel_rwy["dep"] or "").replace("RW", "") or "—"
        arr_rwy = (self.sel_rwy["arr"] or "").replace("RW", "") or "—"
        text = ("已选：%s 跑道 %s / %s    →    %s 跑道 %s / %s"
                % (self.dep_icao, dep_rwy, self.sel_proc["dep"].strip() or "—",
                   self.arr_icao, arr_rwy, self.sel_proc["arr"].strip() or "—"))
        src = self._circling_from_rwy()
        if src:
            a = src.replace("RW", "")
            text += ("\n⚠️ 推荐 %s 跑道目视盘旋 %s 落地。SimBrief 无法匹配 %s 跑道与 %s 跑道进近程序，"
                     "因此会退回 %s 跑道匹配 %s 跑道进近程序。" % (a, arr_rwy, arr_rwy, a, a, a))
        return text

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
        rwy = lambda s: (self.sel_rwy[s] or "").replace("RW", "") or None   # 裸跑道号（SimBrief origrwy/destrwy）
        # SimBrief 不接受目视盘旋：落地跑道配不上另一条跑道的进近程序，链接要用飞进近的那条跑道
        dest = (self._circling_from_rwy() or "").replace("RW", "") or rwy("arr")
        return _planner_simbrief_url(self.sb_base, self.route_str(), eobt_utc_min(self.eobt),
                                     origrwy=rwy("dep"), destrwy=dest)

    def preview_coords(self):
        """当前选定的 SID + enroute + STAR 全段坐标（供地图预览）；失败/不足 → (None, 提示串)。"""
        if not self.candidates:
            return None, "无航路候选"
        c = self.candidates[self.sel_idx]
        try:
            coords = procedures.full_route_coords(                 # tagged：每点带所属段，地图给 SID/STAR 另配色
                self.dep_icao, self.sel_proc["dep"].strip(), self.sel_rwy["dep"],
                self.arr_icao, self.sel_proc["arr"].strip(), self.sel_rwy["arr"],
                c.get("pts"), self.dat_path, tagged=True)
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
        """EOBT 改动：开关开则按新时段重选（含重置按风默认），否则仅影响 SimBrief 派生值。
        生成航路的端点是按规划时的运行规则定的；新时段下生效的规则变了 → 置 regen_request，由 UI 重新生成航路。"""
        self.eobt = text or ""
        if self.apply_ops:
            self.select_candidate(self.sel_idx)
        self._check_regen()                               # AIP 的时段与规则开关无关，关着也要查

    def set_apply_ops(self, on):
        """勾/取消「按运行规则预选」：重跑当前候选（开→应用规则、关→回按风预选）。
        生成航路也跟着换：关掉＝不按规则定端点（最短），打开＝按规则。"""
        self.apply_ops = bool(on)
        self.select_candidate(self.sel_idx)
        self._check_regen()

    def replace_generated(self, candidate):
        """重新生成的航路换进来：保留 EOBT 与开关，按新候选重填跑道 / 程序并应用规则。"""
        if not self.candidates:
            return
        self.candidates[self.sel_idx] = candidate
        self.select_candidate(self.sel_idx)

    def replace_candidates(self, candidates, key):
        """AIP 改端重算后整体换候选（与 AIP 行一一对应）：保留选中的行号；该行航路没变就不重选，
        用户已选的跑道 / 程序不被打乱。"""
        if not candidates:
            return
        old = self.base_route
        self.candidates = list(candidates)
        self.aip_key = key
        idx = self.sel_idx if self.sel_idx < len(self.candidates) else 0
        if self.candidates[idx].get("route") != old:
            self.select_candidate(idx)
        else:
            self.sel_idx = idx

    # ---- 内部：生成航路按运行规则定端点 ----
    def _is_generated(self):
        return bool(self.candidates) and bool(self.candidates[self.sel_idx].get("generated"))

    def _is_variant(self):
        return bool(self.candidates) and bool(self.candidates[self.sel_idx].get("variant"))

    def _eobt_min(self):
        j = eobt_jst_min(self.eobt)
        return j if j is not None else default_eobt_min()          # EOBT 被清空 → 同默认值

    def eobt_is_default(self):
        """EOBT 还是规划时的默认值（= 按「现在」规划，实测照用）。"""
        j = eobt_jst_min(self.eobt)
        return j is None or j == self.plan_eobt_min

    def decision_eta(self, c=None):
        """候选 c（默认当前选中）用来选规则的 ETA（JST 当日分钟）：按它自己的航路几何 + 高度条件（F36，与 controller 同口径）。
        不含 SID / STAR 本身（那是规则按 ETA 选出来的），SID / STAR 多出的平均航程由剖面的终端区余量吸收。"""
        c = c if c is not None else (self.candidates[self.sel_idx] if self.candidates else {})
        return self.eta_model.eta_jst(self._eobt_min(), c.get("pts") or None, alt_cond=c.get("alt") or None)

    def _ctxs_at(self, eta_jst):
        return operations.rule_contexts(self._eobt_min(), None, self.wind["dep"], self.sky["dep"],
                                        self.wind["arr"], self.sky["arr"], eta_jst=eta_jst)

    def _ops_ctxs(self):
        """(离场 ctx, 到达 ctx)：离场按 EOBT、到达按当前候选自己的 ETA。与 controller 生成航路 / 改端时用同一个函数。"""
        return self._ctxs_at(self.decision_eta())

    def _ends(self, dctx, actx):
        """(离场, 到达) 端点限定：与 controller.rule_endpoints 同一套（实测是否算数：离场按 EOBT、到达按 ETA）。"""
        return tuple(operations.route_endpoints(self.ops_data, icao, side, ctx, self._measured(icao, ctx), self.dat_path)
                     for side, icao, ctx in (("dep", self.dep_icao, dctx), ("arr", self.arr_icao, actx)))

    def ends_key(self):
        """当前 EOBT / 开关下，生成航路两端该受的限定 → (离场 fixes, 到达 fixes)，不限定的一侧为 None。
        与 controller.plan 生成航路时的算法相同（operations.route_endpoints），比较它就知道要不要重新生成。"""
        if not self.apply_ops or not self.candidates:
            return (None, None)
        return tuple(frozenset(e["fixes"]) if e else None for e in self._ends(*self._ops_ctxs()))

    def _aip_key(self):
        """当前 EOBT / 开关下的 AIP 改端判据（与 controller.aip_alternatives 同一套：operations.aip_fit / aip_key）。
        每行按自己原 AIP 的几何算 ETA、按该 ETA 选到达端的规则。"""
        dep_jst = self._eobt_min()
        etas = [self.decision_eta(c) for c in self.aip_orig]
        ed, eas = None, [None] * len(etas)
        if self.apply_ops:
            memo = {}
            for k, e in enumerate(etas):
                if e not in memo:
                    memo[e] = self._ends(*self._ctxs_at(e))
                ed, eas[k] = memo[e][0], memo[e][1]
        return operations.aip_key(operations.aip_fit(self.aip_rows, dep_jst, etas, ed, eas), ed, eas)

    def _check_regen(self):
        """生效的规则 / AIP 适用性变了 → 置 regen_request，由 UI 在后台重算（生成航路重生成 / AIP 重新改端）。"""
        if not self.candidates:
            return
        if self._is_generated():
            if self.ends_key() != self.candidates[self.sel_idx].get("ends_key"):
                self.regen_request = {"kind": "gen", "eobt": self.eobt, "apply_ops": self.apply_ops}
            return
        if not (self.aip_rows and self.aip_orig) or self.aip_key is None:
            return
        try:
            key = self._aip_key()
        except Exception as e:                            # noqa: BLE001
            print("⚠️ AIP 适用性判定失败:", e)
            return
        if key != self.aip_key:
            self.regen_request = {"kind": "aip", "eobt": self.eobt, "apply_ops": self.apply_ops}

    # ---- 内部：实测运用状况（ntrack）----
    def _icao(self, side):
        return self.dep_icao if side == "dep" else self.arr_icao

    def _measured(self, icao, ctx=None):
        """这个机场的实测此刻还算不算数 → 实测 cfg 或 None（用户定）：
        EOBT 还是规划时的默认值 = 按「现在」规划，实测照用；用户改了 EOBT → 只在报文覆盖的时段内算数，
        离场按 EOBT、到达按 ETA（ctx = 这一端的规则上下文；不给 → 按当前候选算）。operations.measured_at 与 controller 同一套。
        用于选规则、定端点、AIP 改端判据、✅在用标记；显示文字照旧列出实测（另注明未采用）。"""
        cfg = self.ntrack.get(icao)
        if not cfg or self.eobt_is_default():
            return cfg or None
        if ctx is None:
            dctx, actx = self._ops_ctxs() if self.candidates else self._ctxs_at(self._eobt_min())
            ctx = dctx if icao == self.dep_icao else actx
        return operations.measured_at(cfg, ctx, False)

    def _nt_cfg(self, side):
        return self._measured(self._icao(side))

    def _nt_rwys(self, side):
        """该侧【实测在用】的跑道集合；无数据 / 不在报文覆盖时段内 → None。离场看 DEP、到达看 LDG。"""
        cfg = self._nt_cfg(side)
        if not cfg:
            return None
        return set(cfg.get("dep" if side == "dep" else "ldg") or ()) or None

    def _nt_text(self, side):
        icao = self._icao(side)
        cfg = self.ntrack.get(icao)
        if not cfg:
            return ""
        t = cfg.get("time_jst", "")
        if self._measured(icao):
            off = ""
        elif side == "dep":
            off = "（EOBT %s 不在该报文覆盖的时段内，未采用，按运行规则）" % (self.eobt or "—")
        else:
            off = "（ETA %s 不在该报文覆盖的时段内，未采用，按运行规则）" % _hhmm(self.decision_eta())
        if side == "dep":
            return "✈️ %s 实测运用（%s）：离场 %s%s" % (
                icao, t, "/".join(r.replace("RW", "") for r in cfg.get("dep") or []), off)
        iaps = self.nt_iaps.get(icao) or []
        return "✈️ %s 实测运用（%s）：落地 %s · 进近 %s%s%s" % (
            icao, t, "/".join(r.replace("RW", "") for r in cfg.get("ldg") or []),
            " / ".join(cfg.get("approaches") or []) or "—",
            ("（IAP %s）" % " ".join(iaps)) if iaps else "", off)

    # ---- 内部 ----
    def _fill_rwy(self, side, rows):
        # 实测在用的跑道标 ✅ 并排最前 → 按风默认自然落在其中；其余跑道仍可选（用户能改选）
        items = runway_items(rows, self.wind[side], in_use=self._nt_rwys(side),
                             prefer=operations.preferred_runways(self.ops_data, self._icao(side)))
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
        if side == "dep" and self.candidates:
            # 日本 SID 常以航路开头的直飞点命名（`MAIKO OSRIX GUMID SOUJA…` ⇒ SID `MAIKO1`）。
            # 多条 SID 共用同一过渡时（FERRY7/IWAYA3/MAIKO1/RINKU1 都是 .SOUJA），别按字母序默认给 FERRY7——
            # 偏好【名字（去版本号）与前导直飞点相符】的那条，且按其在航路里出现得越靠前越优先。
            route = (self.candidates[self.sel_idx] or {}).get("route", "")
            lead = []
            for t in route.split():
                if any(c.isdigit() for c in t):          # 遇航路名即止（前导直飞点扫完了）
                    break
                lead.append(t)
            stem = lambda l: l.split(".")[0].rstrip("0123456789")   # `MAIKO1.SOUJA` → `MAIKO`
            hit = sorted((l for l in labels if stem(l) in lead), key=lambda l: lead.index(stem(l)))
            if hit:
                return hit[0]
        return labels[0]

    def _apply_ops(self):
        """v1.6.0：按 operation.json 运行规则预选跑道/SID·STAR（覆盖按风默认）。
        离场按 EOBT、到达按 ETA(=EOBT+航程) 匹配时段；关开关或无规则 → 保持按风预选。"""
        self.ops_label = {"dep": "", "arr": ""}
        self.circling = None
        if not self.apply_ops or not self.ops_data or not self.candidates:
            return
        c = self.candidates[self.sel_idx]
        dctx, actx = self._ops_ctxs()
        self._apply_ops_side("dep", self.dep_icao, c.get("dep_rows", []), dctx, "SID")
        self._apply_ops_side("arr", self.arr_icao, c.get("arr_rows", []), actx, "STAR")

    def _measured_text(self, side):
        """实测构型的简短说明：离场「离场 05/34R」，进场「落地 34L（ILS Y RWY34L）」。"""
        nt = self._nt_cfg(side) or {}
        if side == "dep":
            return "离场 %s" % "/".join(r.replace("RW", "") for r in nt.get("dep") or [])
        apps = " / ".join(nt.get("approaches") or [])
        return "落地 %s%s" % ("/".join(r.replace("RW", "") for r in nt.get("ldg") or []),
                              ("（%s）" % apps) if apps else "")

    def _apply_ops_side(self, side, icao, rows, ctx, proc_name):
        """单侧应用：命中规则 → 设跑道 + 级联程序 + 设 SID/STAR + 标注；无规则不动、无命中给提示。

        有实测运用状况（SWIM 真实 ATIS / 羽田 ntrack）时，把它作为 `config` 传给规则引擎：
        **实测定「用哪些跑道 + 什么进近」（硬约束，进场要对上具体进近），规则定「在其中怎么选 + 配什么 SID/STAR」**。
        一条规则都对不上 → 以实测为主（在实测跑道里按风预选）。
        风闸/天气闸由实测取代、时段/星期闸门保留——详见 operations.select_rule 的说明。
        生成航路只在【此刻生效的那组规则】里选（operations.active_rules）：航路的端点就是按这组规则定的。"""
        nt = self._nt_cfg(side)
        rules = operations.airport_rules(self.ops_data, icao)
        if not rules:
            return
        try:
            iaps = procedures.enumerate_approaches(icao, self.dat_path) if nt else []
            cfg = operations.measured_config(nt, side, iaps)
            if self._is_generated() or self._is_variant():   # 端点按这组规则定的：只在其中选
                active, _cfgs, _rows = operations.airport_active_rules(self.ops_data, icao, ctx, nt, self.dat_path)
                if active[side]:
                    rules = [r for r, _w in active[side]]
            sel = operations.select_rule(rules, side, ctx, rows, config=cfg,
                                         prefer=operations.preferred_runways(self.ops_data, icao))
        except Exception as e:                            # noqa: BLE001
            print("⚠️ 运行规则匹配失败:", e)
            return
        if sel and cfg and sel[2] is None and ((sel[0].get(side) or {}).get(
                "sids" if side == "dep" else "stars")):
            sel = None              # 有实测、规则的程序却接不上这条航路（航路走的是没有规则对上的实测跑道）→ 以实测为主
        if not sel:
            self.ops_label[side] = (
                ("🎯 %s 实测%s 未匹配到运行规则 → 按实测运行（已在实测跑道中按风预选）"
                 % (icao, self._measured_text(side)))
                if nt else
                ("🎯 %s 运行规则：当前风/时段/天气无【可用】匹配规则"
                 "（超限跑道已排除，保持按风合规跑道）" % icao))
            return
        rule, rwy, proc_label = sel
        pairs = operations.circling_pairs(rule, side, rows, proc_label)
        if pairs:
            self.circling = pairs
            for it in self.items[side]:                   # STAR 服务的是进近那条跑道，盘旋落地的各跑道下拉框里也要有
                if proc_label and it["rwy"] in pairs and proc_label not in it["labels"]:
                    it["labels"].insert(0, proc_label)
                    it["disp"] = it["disp"].replace(" · " + IAP_ONLY_TAG, "")
        item = next((i for i in self.items[side] if i["rwy"] == rwy), None)
        if item:
            self.sel_rwy[side] = rwy
            self._fill_proc(side, item)                   # 级联该跑道路线相符的 SID/STAR（预选首个）
        if proc_label:
            self.sel_proc[side] = proc_label              # 规则程序命中航路端点→用它；否则保留路线相符首个
        iap = ""
        if side == "arr":
            # IAP 也以实测为准：ntrack 明确给出当前在用的进近，比规则里录的更准
            iaps = (self.nt_iaps.get(icao) if nt else None) or (rule.get("arr") or {}).get("iaps") or []
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

    def __init__(self, candidates, sel_idx=0, strict=False, eta_model=None):
        self.candidates = list(candidates)
        self.sel_idx = sel_idx
        self.strict = bool(strict)
        self.eta_model = eta_model                       # F36：每行按自己的几何 + 高度条件（或用户填的 FL）算 ETA
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
            route = ("🌙改端 %s（原 AIP %s）" % (c.get("route", ""), c.get("aip_route", ""))
                     if c.get("variant") else c.get("route", ""))
            use = timed.describe_restriction(c.get("restr", ""))
            if c.get("variant"):
                use = "按运行规则改端（原 AIP：%s）" % use
            row = ["●" if i == self.sel_idx else "○", route, c.get("restr") or "-",
                   c.get("alt") or "-", c.get("aircraft") or "-", use, dist]
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
        # 改端行就是为本时段改的：它原 AIP 的时段不再作数（restr 置空），高度 / 机型照旧判
        cands = [dict(c, restr="") if c.get("variant") else c for c in self.candidates]
        self.verdicts = []
        for c in cands:
            eobt_utc = eta_utc = None
            if eobt is not None:
                eobt_utc = (eobt - 540) % 1440
                if self.eta_model:                         # 每行自己的 ETA（以前所有行共用选中行的）
                    eta_utc = (self.eta_model.eta_jst(eobt, c.get("pts") or None, alt_cond=c.get("alt") or None,
                                                      fl=fl) - 540) % 1440
                else:
                    eta_utc = timed.plan_times_utc(eobt, c.get("dist"))[1]
            self.verdicts += timed.filter_candidates([c], eobt_utc, eta_utc, self.cat, fl)
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
        self.airport_runways = []                        # 当前机场的物理跑道（RW..）
        self.preferred = []                              # 当前机场的 preferred_runways（机场级，不属于某条规则）

    # ---- 查 ----
    def existing_airports(self):
        return operations.airports(self.all)

    def existing_text(self):
        aps = self.existing_airports()
        return ("已有规则：" + " ".join(aps)) if aps else "（暂无机场规则；键入 ICAO + 载入即可新建）"

    def preferred_items(self):
        """优先跑道勾选项 [(显示, 跑道, 是否已选)]。"""
        return [(r.replace("RW", ""), r, r in self.preferred) for r in self.airport_runways]

    def toggle_preferred(self, rwy):
        """切换一条优先跑道。同向跑道只留一条：两条都优先等于没有优先。"""
        if rwy in self.preferred:
            self.preferred.remove(rwy)
        else:
            num = procedures._rw_sort_key(rwy)[0]
            self.preferred = [r for r in self.preferred if procedures._rw_sort_key(r)[0] != num] + [rwy]
        self.dirty = True

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
        self.airport_runways = rwys
        self.preferred = list((self.all.get(icao) or {}).get("preferred_runways") or [])
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
            entry = {**(self.all.get(self.icao) or {}), "rules": list(self.rules)}
            entry.pop("preferred_runways", None)
            if self.preferred:
                entry["preferred_runways"] = list(self.preferred)
            self.all[self.icao] = entry

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
