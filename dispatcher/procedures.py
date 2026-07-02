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

from .router import haversine_nm, _navdata_dir

_NM_TO_FT = 6076.12
_PROC_CACHE = {}                 # icao -> {"SID":{name:{...}}, "STAR":{...}}
_RWY_CACHE = {}                  # icao -> {rwy_id:(lat,lon)}


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
    body = rwy_id.replace("RW", "").strip()
    try:
        return (int(body[:2]), body[2:])
    except ValueError:
        return (99, body)


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
            labels = set()
            for t, (tf, tt) in d["trans"].items():         # SID 接过渡末点 tt / STAR 接过渡首点 tf
                if (tt if is_dep else tf) == endpoint:
                    labels.add("%s.%s" % (name, t))
            if not labels:                                 # 未命中过渡端 → 裸程序（本场交付点：过渡另一端；无过渡则 body 端点）
                if d["trans"]:
                    bare = {(tf if is_dep else tt) for tf, tt in d["trans"].values()}
                else:
                    bare = d["body_term"] if is_dep else d["body_first"]
                if endpoint in bare:
                    labels.add(name)
            if labels:
                matched = True
                for rw in (d["runways"] or all_rw):
                    rw_labels.setdefault(rw, set()).update(labels)

    if not matched:                                         # 回退：列全部程序（所有 SID.TRANS / 裸名）
        for name, d in procs.items():
            labels = {"%s.%s" % (name, t) for t in d["trans"]} or {name}
            for rw in (d["runways"] or all_rw):
                rw_labels.setdefault(rw, set()).update(labels)

    rows = [(rw, runway_length_ft(rw, runways), sorted(rw_labels[rw]))
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
    out = []
    for it in list(sid_wp) + mid + list(star_wp):
        if not out or out[-1][0] != it[0]:
            out.append(it)
    return out
