# ================= 跑道 + SID/STAR 枚举与端点预筛（F20，v1.4.1）=================
# 从程序自带 NavData/CIFP/<ICAO>.dat 解析：
#   · RWY: 记录 → 各跑道头坐标（算跑道长度）；跑道朝向由跑道号×10 得（CIFP 朝向字段不可靠）。
#   · SID/STAR 记录 → 每条程序服务的跑道、enroute 过渡(TRANS)及其连接航点(section E)。
# 对外：用「生成/AIP 航路的首点(离场)/末点(进场)」预筛出真正接得上本航路的 SID.TRANS / STAR，
#       连同跑道（含长度）交给 GUI 让用户选。纯标准库、按机场懒加载缓存。
#
# 关键 CIFP 字段（`<TYPE>:` 后 split(",")）：proc=p[2]、transition=p[3]、fix=p[4]、region=p[5]、section=p[6]。
#   transition: ① RWnn[L/R/C/B]=跑道专用段（B=平行双跑道两端都服务）；② 非 RW 名=enroute 过渡(标准写法 SID.TRANS 的 TRANS)；③ 空=common 段。
#   section=='E'∪'D' 的 fix = 段内航点（含 VOR）。**接航路的是段的【端点】而非途经点**：
#   一条 enroute 过渡按序列走「离场点(IF/首)→…→enroute 端(末)」——SID 用【末点】接航路(=标准 SID.TRANS 的 TRANS)，
#   过渡首点则是本场 SID 的**离场交付点**(裸 SID 衔接)；STAR 方向相反，用过渡【首点】接航路、末点为进场交付点。
#   （例：RJCC 的 JUGGL2/MKE9/TOBBY9 三条 SID 的 BUTOS/PANSY 过渡都从 TOBBY 起飞——航路 `TOBBY Y10…` 的离场点是
#    TOBBY，应给裸 JUGGL2/MKE9/TOBBY9；若按「TOBBY∈过渡途经点」会误标成 X.BUTOS（冲过 TOBBY 到 BUTOS）。）

import os
import re

from .router import haversine_nm, _navdata_dir

_NM_TO_FT = 6076.12
_PROC_CACHE = {}                 # icao -> {"SID":{name:{...}}, "STAR":{...}}
_RWY_CACHE = {}                  # icao -> {rwy_id:(lat,lon)}
_APPCH_CACHE = {}                # icao -> [进近程序 dict…]（F23）

# CIFP APPCH 类型字母（p[1] / ident 首字母）→ 显示名（F23）
_APPCH_TYPE = {"I": "ILS", "X": "LDA", "D": "VOR/DME", "V": "VOR", "N": "NDB",
               "R": "RNAV", "P": "GPS", "J": "GLS", "B": "LOC BC", "L": "LOC",
               "Q": "NDB/DME", "S": "VOR/DME", "T": "TACAN", "U": "SDF", "G": "GPS"}
_RWNUM_RE = re.compile(r"(\d{2}[LRC]?)")   # 从进近编码名里取跑道（I16L→16L、X22-W→22）


def _cifp_path(icao, dat_path=None):
    return os.path.join(_navdata_dir(dat_path), "CIFP", icao.upper() + ".dat")


def _dms(tok):
    """CIFP 坐标 `N35325647`/`E139454060`（半球 + DDMMSSss，经度多一位度）→ 十进制度；解析失败 None。"""
    if not tok or tok[0] not in "NSEW":
        return None
    try:
        hemi, digits = tok[0], tok[1:]
        dd = 2 if hemi in "NS" else 3
        deg = int(digits[:dd])
        mm = int(digits[dd:dd + 2])
        ss = int(digits[dd + 2:dd + 4])
        hund = int(digits[dd + 4:dd + 6] or "0")
        val = deg + mm / 60.0 + (ss + hund / 100.0) / 3600.0
        return -val if hemi in "SW" else val
    except (ValueError, IndexError):
        return None


def _parse_runways(icao, dat_path=None):
    """扫 `RWY:` 记录 → {rwy_id:(lat,lon)}（跑道头坐标，`;` 之后第 1/2 段）。按机场缓存。"""
    icao = icao.upper()
    if icao in _RWY_CACHE:
        return _RWY_CACHE[icao]
    rwys = {}
    try:
        with open(_cifp_path(icao, dat_path), encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.startswith("RWY:"):
                    continue
                rest = line.partition(":")[2]
                body, _sep, coords = rest.partition(";")
                rid = body.split(",")[0].strip()           # "RW16L"
                cp = coords.split(",")
                if rid and len(cp) >= 2:
                    lat, lon = _dms(cp[0].strip()), _dms(cp[1].strip())
                    if lat is not None and lon is not None:
                        rwys[rid] = (lat, lon)
    except Exception:
        pass
    _RWY_CACHE[icao] = rwys
    return rwys


def runway_heading_deg(rwy_id):
    """跑道磁航向 ≈ 跑道号×10（CIFP RWY 朝向字段不可靠，号码才是真值；如 RW16L→160）。失败 None。
    注：METAR 风为真北、跑道号为磁向，日本磁差约 7–8°W，此处忽略（对「是否顶风」判断影响可忽略）。"""
    try:
        num = int(rwy_id.replace("RW", "").strip()[:2])
    except (ValueError, AttributeError):
        return None
    h = (num * 10) % 360
    return 360 if h == 0 else h


def _reciprocal(rwy_id):
    """反向端：`RW16L`↔`RW34R`（号 ±18、L↔R/C↔C/无后缀不变）。失败 None。"""
    body = rwy_id.replace("RW", "").strip()
    try:
        num = int(body[:2])
    except ValueError:
        return None
    suf = body[2:].strip()
    rnum = num + 18 if num <= 18 else num - 18
    rsuf = {"L": "R", "R": "L"}.get(suf, suf)
    return "RW%02d%s" % (rnum, rsuf)


def runway_length_ft(rwy_id, runways):
    """跑道长度 = 该端与反向端两跑道头大圆距离（英尺）。CIFP 长度字段不可靠，故由两端坐标算。缺反向端/坐标 → None。"""
    a = runways.get(rwy_id)
    recip = _reciprocal(rwy_id)
    b = runways.get(recip) if recip else None
    if not a or not b:
        return None
    return haversine_nm(a[0], a[1], b[0], b[1]) * _NM_TO_FT


def _expand_rw(trans):
    """跑道专用 transition → 跑道端列表：`RW34B`→[RW34L,RW34R]；`RW16L`→[RW16L]。"""
    body = trans.replace("RW", "").strip()
    try:
        num = int(body[:2])
    except ValueError:
        return [trans]
    suf = body[2:].strip()
    if suf == "B":
        return ["RW%02dL" % num, "RW%02dR" % num]
    return ["RW%02d%s" % (num, suf)]


def enumerate_procedures(icao, dat_path=None):
    """解析 SID/STAR 记录 → {"SID":{name:{...}}, "STAR":{...}}（按机场缓存）：
      runways    = 该程序所有 RW 段展开的跑道端集；
      trans      = {过渡名: (first_fix, term_fix)}——enroute 过渡的【首点】(IF/起) 与【末点】(终)，
                   连接点取 section E∪D。SID 用末点接航路(=标准 SID.TRANS 的 TRANS)、STAR 用首点接航路；
                   过渡的另一端即本场交付点(裸程序衔接)。
      body_first / body_term = 非过渡段(跑道/common)的首点/末点集，供无过渡时的裸程序衔接。"""
    icao = icao.upper()
    if icao in _PROC_CACHE:
        return _PROC_CACHE[icao]
    out = {"SID": {}, "STAR": {}}
    try:
        with open(_cifp_path(icao, dat_path), encoding="utf-8", errors="ignore") as f:
            for line in f:
                head, _sep, rest = line.partition(":")
                if head not in ("SID", "STAR"):
                    continue
                p = rest.split(",")
                if len(p) < 7:
                    continue
                name, trans, fix, section = p[2].strip(), p[3].strip(), p[4].strip(), p[6].strip()
                if not name:
                    continue
                d = out[head].setdefault(name, {"runways": set(), "_grp": {}})
                if trans.startswith("RW"):
                    d["runways"].update(_expand_rw(trans))
                # 连接点取 section E(enroute) ∪ D(VOR/导航台)——很多端点是 VOR(如 XAC/MKE)，只取 E 会漏
                conn = fix if section in ("E", "D") else None
                if conn:                                    # 按段(gkey)记首/末点：首点固定为第 1 个、末点随序更新
                    gkey = trans or ""                      # RWxx / 具名过渡 / ""(common) / "ALL"
                    g = d["_grp"].setdefault(gkey, {"first": conn, "term": conn})
                    g["term"] = conn
    except Exception:
        pass
    for procs in out.values():                              # 归整：拆 enroute 过渡(具名段) 与 body(跑道/common 段)
        for d in procs.values():
            grp = d.pop("_grp")
            d["trans"], body_first, body_term = {}, set(), set()
            for gkey, g in grp.items():
                if gkey and gkey != "ALL" and not gkey.startswith("RW"):
                    d["trans"][gkey] = (g["first"], g["term"])
                else:
                    body_first.add(g["first"]); body_term.add(g["term"])
            d["body_first"], d["body_term"] = body_first, body_term
    _PROC_CACHE[icao] = out
    return out


def _rw_sort_key(rwy_id):
    body = (rwy_id or "").replace("RW", "").strip()
    try:
        return (int(body[:2]), body[2:])
    except ValueError:
        return (99, body)


def enumerate_approaches(icao, dat_path=None):
    """扫 CIFP `APPCH:` 记录 → 每条进近程序 `{ident, type, suffix, runway, name, trans}`（按机场缓存，F23）。
    ident 是编码名（内嵌跑道）：`I16L`→ILS RWY16L、`D34L`→VOR/DME RWY34L、`X22-W`→LDA W RWY22、`R34L-Y`→RNAV Y RWY34L。
    CIFP 无自然语言名，`name` 由 类型(首字母映射)+后缀+跑道 合成；`trans`=各 IAF 过渡名（APPCH 里的 p[3]）。
    类型 `A` 行是进近过渡(feeder)段，其过渡名并入所属 ident。解析失败 → 空列表（绝不崩）。"""
    icao = icao.upper()
    if icao in _APPCH_CACHE:
        return _APPCH_CACHE[icao]
    procs, order = {}, []
    try:
        with open(_cifp_path(icao, dat_path), encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.startswith("APPCH:"):
                    continue
                p = line.partition(":")[2].split(",")
                if len(p) < 4:
                    continue
                rtype, ident, trans = p[1].strip(), p[2].strip(), p[3].strip()
                if not ident:
                    continue
                d = procs.get(ident)
                if d is None:
                    d = procs[ident] = {"rtypes": set(), "trans": set()}
                    order.append(ident)
                if rtype and rtype != "A":
                    d["rtypes"].add(rtype)
                if trans:
                    d["trans"].add(trans)
    except Exception:
        pass
    out = []
    for ident in order:
        d = procs[ident]
        tletter = ident[0] if (ident and ident[0].isalpha()) else (sorted(d["rtypes"])[0] if d["rtypes"] else "")
        typ = _APPCH_TYPE.get(tletter, tletter or "APP")
        m = _RWNUM_RE.search(ident)
        if m:
            rwytxt = m.group(1)
            runway = "RW" + rwytxt
            suffix = ident[m.end():].lstrip("-").strip()      # 后缀可带 -（X22-W）或直接跟（I34LX）
            name = "%s%s RWY%s" % (typ, (" " + suffix) if suffix else "", rwytxt)
        else:                                                 # 无跑道（盘旋进近，如 VORA）→ 用原始编码名
            runway, suffix, name = None, "", ident
        out.append({"ident": ident, "type": typ, "suffix": suffix,
                    "runway": runway, "name": name, "trans": sorted(d["trans"])})
    out.sort(key=lambda a: (_rw_sort_key(a["runway"] or "RW99"), a["ident"]))
    _APPCH_CACHE[icao] = out
    return out


def _connect_points(d, is_dep):
    """一条程序与航路的【合法衔接点】→ `(过渡衔接点 {trans名: ident}, 裸程序衔接点 {ident…})`。

    口径：**接航路的是过渡的端点**——离场(SID)接过渡【末点】(=TRANS)、进场(STAR)接过渡【首点】(=TRANS)；
    裸程序则接过渡的**另一端**（本场交付点：雷达引导直接切进程序机体），无过渡时用 body 端点。

    抽成函数是因为它是**唯一真源**：`matching_choices`（面板据以匹配）与 `star_connect_points`
    （router 据以判断「航路该在哪儿收手交给程序」）必须用同一套判据。两边各写一份 = RJAH 那个 bug 的病根本身。"""
    trans = {t: (tt if is_dep else tf) for t, (tf, tt) in d["trans"].items()}
    if d["trans"]:
        bare = {(tf if is_dep else tt) for tf, tt in d["trans"].values()}
    else:
        bare = set(d["body_term"] if is_dep else d["body_first"])
    return trans, bare


def star_connect_points(icao, dat_path=None):
    """该机场 STAR 能与航路衔接的【全部点】ident 集合——「航路飞到这儿，剩下的交给 STAR」的那些点。
    与 `matching_choices(kind='arr')` 判据**完全同源**（同一个 `_connect_points`），
    故「enroute 收在这些点之一」⟺「面板一定能匹配到 STAR」，这个等价关系正是 router 需要的保证。

    router 用它做**分支判断**：官方 AIP / VATJPN 移管表里那些直飞点列，到底是
    ①【无 SID/STAR 时的真·直飞径路】（该照抄进 enroute，如 RJFM `KUE ESKAP KROMA ENBEN MZE`），还是
    ②【有 STAR、只是把 transition 展开写成了点列】（不该抄进 enroute，那是程序的活，如 RJAH `TATSU NAKAH`
       其实就是 STAR「TATSU」的机体 TATSU→NAKAH）。见 `router._arrival_tail_keys`。"""
    out = set()
    for d in enumerate_procedures(icao, dat_path)["STAR"].values():
        t, b = _connect_points(d, False)
        out |= set(t.values()) | b
    return out


_ORDER_NEAR_FIXES = 6      # 排序只看靠端点这一段航路（加密后的航点），够表达来向、又不会被航路远端带偏


def _label_order(icao, route_fixes, conn, dat_path):
    """回退列全部程序时的排序键：**按该程序的接入点离「航路靠本端的那一段」多近**由近及远（同距再按字母）。

    为什么不能用字母序：面板默认选中的就是第一条（viewmodel `labels[0]`）。RJAH 有 GOT1 与 TATSU 两条 STAR，
    字母序把**反方向**的 GOT1 排在前面 → 从关西飞来的航班默认套了个往北绕出去、再折回本场的进场。
    为什么不是「离端点最近」：官方航路常收在**本场 VOR**（`… V18 HCE`、`… Y312 ODE`——AIP 表原文如此），
    而本场 VOR 到各条 STAR 起点的距离并不编码来向；编码来向的是**航路本身**——落在来向航路上的接入点距离≈0。
    端点未命中任何程序接入点时只能这样几何猜，是决策支持、不是替用户拍板（用户随时可改选）。
    坐标解析不到 → 退回字母序。"""
    near = [f for f in (route_fixes or [])[:_ORDER_NEAR_FIXES] if f]
    pts = [p for p in (_resolve_fix(f, None, icao, dat_path) for f in near) if p]
    if not conn or not pts:
        return lambda lbl: (0.0, lbl)

    def _dist(lbl):
        best = float("inf")
        for ident in conn.get(lbl) or ():
            c = _resolve_fix(ident, None, icao, dat_path)
            if c:
                best = min(best, min(haversine_nm(p[0], p[1], c[0], c[1]) for p in pts))
        return best

    cache = {}
    return lambda lbl: (cache.setdefault(lbl, _dist(lbl)), lbl)


def sid_star_endpoints(route_str):
    """AIP/生成航路串 → (SID 出口, STAR 入口)，供端点预筛用真实的程序衔接点。
      SID 出口 = 第一个【航路名】之前的那个航点（离场程序在这儿接 enroute）；
      STAR 入口 = 最后一个【航路名】之后的那个航点（enroute 在这儿交给进场程序）；
      纯 DCT（无航路名）→ (首点, 末点)；航路名打头/结尾（罕见）→ 该端 None（退回调用方原有兜底）。

    ⚠️ 为什么不能直接取航路串首/末点：官方串开头常把 **SID 机体逐点展开**（终端区航点），
       如 RJOO→RJFF 的 `TIGER SUMAR AYAME SETOH SOUJA Y281 …`——`TIGER SUMAR AYAME SETOH` 都是
       SID「TIGER2」的机体，真正的 SID 出口是它们之后、第一个航路名 `Y281` 之【前】的 **SOUJA**。
       拿首点 SUMAR 去预筛只会匹配到裸 `TIGER2`（无过渡），拿 SOUJA 才得到正解 `TIGER2.SOUJA`。"""
    toks = [t for t in (route_str or "").split() if t]
    if not toks:
        return None, None
    is_awy = lambda t: any(c.isdigit() for c in t)                    # 航路名含数字，航点不含
    awy = [i for i, t in enumerate(toks) if is_awy(t)]
    if not awy:
        return toks[0], toks[-1]                                      # 纯 DCT
    dep_exit = toks[awy[0] - 1] if awy[0] >= 1 else None              # 第一个航路名前的点
    arr_entry = toks[awy[-1] + 1] if awy[-1] + 1 < len(toks) else None  # 最后一个航路名后的点
    return dep_exit, arr_entry


def matching_choices(icao, dat_path, route_fixes, kind):
    """按航路端点预筛该机场可用跑道 + SID.TRANS / STAR(.TRANS)。
    route_fixes: 朝端点方向的航点 ident 有序列表 —— 离场(kind='dep')传航路【正序】(首点在前)，
                 进场(kind='arr')传【逆序】(末点在前)；route_fixes[0]=该端的航路端点。
    衔接口径：**接航路的是过渡的端点**——离场(SID)接过渡【末点】(=TRANS)，其【首点】是本场离场交付点(裸 SID)；
             进场(STAR)接过渡【首点】(=TRANS)，其【末点】是进场交付点(裸 STAR)。无过渡则用 body 端点。
    返回 (rows, matched)：rows=[(rwy_id, length_ft|None, sorted([label…]))]；
      matched=True 表示按端点筛中；False=端点未命中任何程序端点（端点来自学习/移管/本场VOR），回退列该机场全部程序。
      **无任何 SID/STAR 时**（很多机场无 STAR，只有 IAP/雷达引导）仍返回全部物理跑道、label 为空 []——
      让用户能选跑道，不因无程序就弃选（labels 为空即表示该跑道无 SID/STAR，进近走 IAP）。"""
    procs = enumerate_procedures(icao, dat_path)["SID" if kind == "dep" else "STAR"]
    runways = _parse_runways(icao, dat_path)
    all_rw = set(runways)                                  # 物理跑道（RWY 记录）——服务全跑道(ALL/common)的程序挂到这些上
    for d in procs.values():
        all_rw |= d["runways"]

    route_fixes = [f for f in (route_fixes or []) if f]
    endpoint = route_fixes[0] if route_fixes else None
    is_dep = (kind == "dep")

    rw_labels = {}                                          # rwy_id -> set(label)
    matched = False
    if endpoint:
        for name, d in procs.items():
            tconn, bconn = _connect_points(d, is_dep)      # ← 判据唯一真源（router 用的是同一个）
            labels = {"%s.%s" % (name, t) for t, f in tconn.items() if f == endpoint}
            if not labels and endpoint in bconn:           # 未命中过渡端 → 裸程序（本场交付点）
                labels = {name}
            if labels:
                matched = True
                for rw in (d["runways"] or all_rw):
                    rw_labels.setdefault(rw, set()).update(labels)

    conn = {}                                               # label -> 该标签的衔接点 idents（回退时按【离来向航路的远近】排序用）
    if not matched:                                         # 回退：列全部程序（所有 SID.TRANS / 裸名）
        for name, d in procs.items():
            tconn, bconn = _connect_points(d, is_dep)
            for t, f in tconn.items():
                conn["%s.%s" % (name, t)] = {f}
            if not tconn:
                conn[name] = set(bconn)
            labels = {"%s.%s" % (name, t) for t in d["trans"]} or {name}
            for rw in (d["runways"] or all_rw):
                rw_labels.setdefault(rw, set()).update(labels)

    order = _label_order(icao, route_fixes, conn, dat_path)  # 命中时=字母序（conn 空）；回退时=接入点离来向航路由近及远
    rows = [(rw, runway_length_ft(rw, runways), sorted(rw_labels[rw], key=order))
            for rw in sorted(rw_labels, key=_rw_sort_key)]
    if not rows:                                            # 无任何 SID/STAR（很多机场无 STAR，靠 IAP/雷达引导进近）→
        rows = [(rw, runway_length_ft(rw, runways), [])     # 仍列物理跑道（标签空），让用户能选跑道，别因无程序就弃选
                for rw in sorted(runways, key=_rw_sort_key)]
    return rows, matched


# ================= 全段航路可视化（SID+enroute+STAR 画到地图，F21 续）=================
# 复用 GUI 的 _open_map（吃 [(ident,lat,lon)…]）。enroute 段用 router.route_geometry 已算好的 pts；
# SID/STAR 段在这里按【选定跑道 + 过渡】还原有序航点并解析坐标（含 terminal section-P 航点）。

_LEG_CACHE = {}                  # icao -> {"SID":{name:{gkey:[(ident,region,section)…]}}, "STAR":{...}}
_FIX_INDEX = None                # {(ident,region): {terminal|'ENRT': (lat,lon)}}


def _load_fix_index(dat_path=None):
    """解析 earth_fix.dat(全部，含 terminal) + earth_nav.dat(ENRT 台) → {(ident,region):{area:(lat,lon)}}。一次性缓存。"""
    global _FIX_INDEX
    if _FIX_INDEX is not None:
        return _FIX_INDEX
    idx = {}
    d = _navdata_dir(dat_path)
    try:                                                       # earth_fix: `lat lon ident <terminal/ENRT> region …`
        with open(os.path.join(d, "earth_fix.dat"), encoding="utf-8", errors="ignore") as f:
            for line in f:
                p = line.split()
                if len(p) < 5:
                    continue
                try:
                    lat, lon = float(p[0]), float(p[1])
                except ValueError:
                    continue
                idx.setdefault((p[2], p[4]), {})[p[3]] = (lat, lon)
    except Exception:
        pass
    try:                                                       # earth_nav: VOR/NDB/DME（只取 ENRT 台，SID/STAR 用得到）
        with open(os.path.join(d, "earth_nav.dat"), encoding="utf-8", errors="ignore") as f:
            for line in f:
                p = line.split()
                if len(p) < 11 or p[0] not in ("2", "3"):
                    continue
                try:
                    lat, lon = float(p[1]), float(p[2])
                    e = p.index("ENRT")
                    ident, region = p[e - 1], p[e + 1]
                except (ValueError, IndexError):
                    continue
                idx.setdefault((ident, region), {}).setdefault("ENRT", (lat, lon))
    except Exception:
        pass
    _FIX_INDEX = idx
    return idx


def _resolve_fix(ident, region, airport, dat_path=None):
    """(ident,region) → (lat,lon)；优先本场 terminal 航点、其次 ENRT、再任意；region 缺失回退 RJ/RO。解析不到 → None。"""
    idx = _load_fix_index(dat_path)
    d = idx.get((ident, region)) or idx.get((ident, "RJ")) or idx.get((ident, "RO"))
    if not d:
        return None
    return d.get(airport) or d.get("ENRT") or next(iter(d.values()))


def _parse_legs(icao, dat_path=None):
    """SID/STAR 每条程序、每个段(跑道段/common/过渡)的【有序航点】(ident,region,section)。按机场缓存。"""
    icao = icao.upper()
    if icao in _LEG_CACHE:
        return _LEG_CACHE[icao]
    out = {"SID": {}, "STAR": {}}
    try:
        with open(_cifp_path(icao, dat_path), encoding="utf-8", errors="ignore") as f:
            for line in f:
                head, _sep, rest = line.partition(":")
                if head not in ("SID", "STAR"):
                    continue
                p = rest.split(",")
                if len(p) < 7:
                    continue
                name, trans, fix, region, section = (p[2].strip(), p[3].strip(),
                                                     p[4].strip(), p[5].strip(), p[6].strip())
                if not name or not fix:                        # 无航点的腿(CA/CD 等)跳过
                    continue
                out[head].setdefault(name, {}).setdefault(trans or "", []).append((fix, region, section))
    except Exception:
        pass
    _LEG_CACHE[icao] = out
    return out


def _match_rw_group(legs, rwy_id):
    """选定跑道 rwy_id(如 RW01L) → 服务它的 CIFP 跑道段键(如 RW01B，B=双跑道)。无则 None。"""
    for g in legs:
        if g.startswith("RW") and rwy_id in _expand_rw(g):
            return g
    return None


def procedure_coords(icao, label, rwy_id, kind, dat_path=None):
    """一条 SID/STAR(标准写法 `NAME` 或 `NAME.TRANS`)在选定跑道下的有序航点坐标 [(ident,lat,lon)…]。
    离场：跑道段→common→过渡（起于跑道头）；进场：过渡→common→跑道段（止于跑道头）。解析不到的点跳过。"""
    icao = icao.upper()
    rwys = _parse_runways(icao, dat_path)
    thr = None
    if rwy_id and rwy_id in rwys:
        c = rwys[rwy_id]
        thr = (rwy_id.replace("RW", ""), c[0], c[1])
    name, _dot, trans = (label or "").partition(".")
    legs = _parse_legs(icao, dat_path)["SID" if kind == "dep" else "STAR"].get(name, {})
    rw_group = _match_rw_group(legs, rwy_id) if rwy_id else None
    seq = []

    def _add(g):
        if g is not None and g in legs:
            seq.extend(legs[g])
    if kind == "dep":
        _add(rw_group); _add(""); _add("ALL")
        if trans:
            _add(trans)
    else:
        if trans:
            _add(trans)
        _add("ALL"); _add(""); _add(rw_group)

    coords, prev = [], None
    for ident, region, _section in seq:
        c = _resolve_fix(ident, region, icao, dat_path)
        if c and ident != prev:
            coords.append((ident, c[0], c[1]))
            prev = ident
    if thr:
        coords = ([thr] + coords) if kind == "dep" else (coords + [thr])
    return coords


def full_route_coords(dep, sid_label, dep_rwy, arr, star_label, arr_rwy, enroute_pts, dat_path=None):
    """拼全段坐标：SID 段 + enroute(去掉首尾机场，pts[1:-1]) + STAR 段，按 ident 去相邻重复。供 GUI `_open_map`。"""
    sid_wp = procedure_coords(dep, sid_label, dep_rwy, "dep", dat_path)
    star_wp = procedure_coords(arr, star_label, arr_rwy, "arr", dat_path)
    mid = list(enroute_pts[1:-1]) if enroute_pts and len(enroute_pts) >= 2 else list(enroute_pts or [])
    # ⚠️ AIP 航路串开头/结尾常把 SID/STAR 机体逐点展开（`MAIKO OSRIX GUMID SOUJA …`），
    #    这些点与 SID/STAR 段坐标【重叠】——直接拼会画出「先到 SOUJA、又跳回 MAIKO、再走回 SOUJA」的折返线。
    #    故把 enroute 裁到 [SID 末点 … STAR 首点]：SID 覆盖的前导点、STAR 覆盖的尾随点都交给程序段画。
    ids = [it[0] for it in mid]
    if sid_wp:
        se = sid_wp[-1][0]                                # SID 交接点（末点，如 SOUJA）
        if se in ids:
            k = ids.index(se); mid = mid[k:]; ids = ids[k:]
    if star_wp:
        ss = star_wp[0][0]                               # STAR 交接点（首点，如 KIRIN）
        if ss in ids:
            mid = mid[:ids.index(ss) + 1]
    out = []
    for it in list(sid_wp) + mid + list(star_wp):
        if not out or out[-1][0] != it[0]:
            out.append(it)
    return out
