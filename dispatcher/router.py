# ================= 本地 A* 航路生成（无 AIP 航路时的兜底，F15）=================
# 纯标准库：解析程序自带 NavData 的 earth_fix/earth_nav/earth_awy 建航路图，
# A* 寻路在两机场间生成一条「入航点 → 航路 → 出航点」的参考航路串。
# 仅航路段，不含 SID/STAR（那需解析 CIFP 程序数据）；生成结果为算法产出、非官方 AIP。
# 生成后做连贯性检查：若存在大锐角（接近掉头）转弯则标注「可能有问题，请自行检查」。
#
# 设计要点：
#  - 节点键 = (ident, region)：ident 全球重复，必须带区域码区分。
#  - 解析时按日本 bounding box 过滤节点，图只剩几千条边 → A* 亚毫秒。
#  - 懒加载：首次 generate 才解析 ~28MB 导航数据并缓存（冷启动不变）；解析在后台线程跑。

import os
import json
import math
import heapq
import threading

from .config import get_real_run_path

# 日本范围（含周边 FIR 交接区，box 略宽以保连通性）：(lat_min, lat_max, lon_min, lon_max)
JP_BBOX = (24.0, 46.0, 122.0, 149.0)
_R_NM = 3440.065                 # 地球半径（NM），与 routing.calculate_distance_nm 一致
_MAX_TURN_DEG = 100.0            # 转向角超过此值视为大锐角转弯（接近掉头），可调
_MAX_ENTRY_NM = 120.0           # 机场到最近航路点的最大直飞接入距离（几何兜底用）
_K_ENTRY = 5                    # 取最近的 K 个航路点作入/出航候选
_TRAD_AIRWAY_PENALTY = 1.15     # 「优先 RNAV」：A* 搜索时纯传统航路(整段无 RNAV 名)的边权乘此系数，软优先 RNAV(Y/Z 等)；
                                #   仅作用于选路、不进显示距离(后者由 haversine 重算)；RNAV 太绕(>15%)时仍回退传统。待测后再调。
_RNAV_PREFIXES = frozenset("QTYZLMNP")   # ICAO Annex 11 航路命名：国内 RNAV=Q/T/Y/Z、区域 RNAV=L/M/N/P（其余 H/J/V/W,A/B/G/R=传统）
# 「高频空中走廊」加权：A* 对越少被官方航路实飞的有向航段越加罚，软偏好真实常用走廊（直接 A* 与桥接补接段共用）。
#   乘子恒 ≥1 → 启发 h 仍可采纳；只抬搜索权重、不进显示距离（后者仍 haversine 重算）。热度=该有向段被多少机场对实飞。待测后再调。
_TRUNK_POP = 4                  # 被 ≥ 此数机场对实飞 → 干线走廊，搜索不加罚
_MINOR_CORRIDOR_PENALTY = 1.08  # 1..(_TRUNK_POP-1) 对实飞 → 轻度使用，轻罚
_OFFTRUNK_PENALTY = 1.25        # 从未被官方航路实飞的航段 → 偏离走廊，较重罚（仍可走，仅软优先）
_SMOOTH_RATIO = 1.05            # A* 后处理「航路连续性」：某子段能由【单一 RNAV 航路】在 ≤此倍×当前子路长直达 → 收编成单航路，
                                #   消除「离开一条航路又在下游接回 / 并行航路绕行」这类不自然走法（如离开 Y102 走 Y10/Y125 再接回 Y102）。待测后再调。
# 「转弯成本」：A* 只算距离时，掉头是【免费】的——它会乐意用一个 100° 发夹弯去换几海里的距离，
#   于是抄近道抄出「飞过去又拐回来」的航路（实测 RJOA→RJTH：GUPER 转 70° 紧接 MOE 转 100°）。
#   真实航路几乎不这么飞。故把转弯本身计入搜索代价：超过 _TURN_FREE_DEG 的部分按每度折算若干 NM。
#   代价 ≥0 且启发 h 仍是大圆距离（真实代价 ≥ 距离）→ A* 的 h 依然可采纳。
_TURN_FREE_DEG = 30.0           # 30° 以内是正常转向，不罚
_TURN_NM_PER_DEG = 0.30         # 超出部分每度折算的等效海里数（100° 的发夹弯 ≈ 罚 21 NM）
DEBUG_CORRIDOR = False          # 调试开关：True 时 generate_route 为每条结果打印走廊段构成（干线/轻度/未飞），GUI 日志/控制台可见

_ARR = ("__ARR__", "")          # A* 的虚拟终点（坐标 = 到达机场）


# ---- 几何 ----

def haversine_nm(lat1, lon1, lat2, lon2):
    """两点大圆距离（NM）。"""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2.0) ** 2
    return _R_NM * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _bearing(lat1, lon1, lat2, lon2):
    """从点1到点2的初始大圆方位角（度，0–360）。"""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _in_bbox(lat, lon):
    return JP_BBOX[0] <= lat <= JP_BBOX[1] and JP_BBOX[2] <= lon <= JP_BBOX[3]


def _is_rnav(name):
    """ICAO Annex 11 航路命名：RNAV 航路首字母 ∈ Q/T/Y/Z(国内) ∪ L/M/N/P(区域)；其余为传统(H/J/V/W,A/B/G/R)。"""
    return bool(name) and name[0] in _RNAV_PREFIXES


# ---- NavData 同级文件定位（镜像 navdata.find_navdata_file 的 NavData/ 锚定）----

def _navdata_dir(dat_path=None):
    """导航数据目录：已知 earth_aptmeta.dat 路径时取其目录，否则 <运行目录>/NavData。"""
    if dat_path:
        return os.path.dirname(dat_path)
    return os.path.join(get_real_run_path(), "NavData")


def _find_sibling(filename, dat_path=None):
    """在 NavData 目录里找同级 .dat 文件（直查 + 浅层 ≤2 层兜底递归）。找不到返回 None。"""
    nav_dir = _navdata_dir(dat_path)
    direct = os.path.join(nav_dir, filename)
    if os.path.exists(direct):
        return direct
    if os.path.isdir(nav_dir):
        try:
            for root, dirs, files in os.walk(nav_dir):
                if root[len(nav_dir):].count(os.sep) >= 2:
                    dirs[:] = []
                    continue
                if filename in files:
                    return os.path.join(root, filename)
        except Exception:
            pass
    return None


# ---- 解析（line.split()，逐行 try/except 容错，跳过 header 'I'/版本行/空行）----

def _parse_fixes(path, nodes):
    """earth_fix.dat：`lat lon ident terminal region ...`；只取 enroute（terminal=='ENRT'）且在 bbox 内。"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            p = line.split()
            if len(p) < 5 or p[3] != "ENRT":          # 先用 ENRT 廉价过滤掉大量终端区航路点
                continue
            try:
                lat, lon = float(p[0]), float(p[1])
            except ValueError:
                continue
            if _in_bbox(lat, lon):
                nodes[(p[2], p[4])] = (lat, lon, "fix")


def _parse_navaids(path, nodes):
    """earth_nav.dat：行码 col0=2(NDB)/3(VOR)；lat col1、lon col2；
    ident/region 定位为锚定 'ENRT'（ident=前一、region=后一，回退 p[7]/p[9]）。bbox 过滤。
    若该 (ident,region) 已是 fix 则保留 fix（坐标基本一致）。"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            p = line.split()
            if len(p) < 11 or p[0] not in ("2", "3"):
                continue
            try:
                lat, lon = float(p[1]), float(p[2])
            except ValueError:
                continue
            if not _in_bbox(lat, lon):
                continue
            try:
                e = p.index("ENRT")
                ident, region = p[e - 1], p[e + 1]
            except (ValueError, IndexError):
                ident, region = p[7], p[9]
            key = (ident, region)
            if key in nodes:                          # 已有 fix 同名同区 → 保留 fix
                continue
            nodes[key] = (lat, lon, "NDB" if p[0] == "2" else "VOR")


def _parse_airways(path, nodes, adj, oneway):
    """earth_awy.dat：`id1 reg1 t1 id2 reg2 t2 dir lowhigh base top name`。
    两端点都在 nodes 才建边；权=大圆距离；dir N=双向 / F=正向(1→2) / B=反向(2→1)；多名用 '-' 连。
    `oneway`：收集任一航段标了方向(F/B)的航路名 —— 这类航路是单向的；earth_awy 在它与双向航路
    共挂的段上会统一标 N(丢了单向信息)，故下游标名时不能把它当双向用(见 _safe_names)。整文件扫(不限 bbox)。"""
    for_count = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            p = line.split()
            if len(p) < 11:
                continue
            direction = p[6]
            names = p[10].split("-")
            if direction in ("F", "B"):          # 单向航路：有方向限制即记（全文件，不受 bbox 限制）
                oneway.update(names)
            k1, k2 = (p[0], p[1]), (p[3], p[4])
            n1, n2 = nodes.get(k1), nodes.get(k2)
            if not n1 or not n2:
                continue
            cost = haversine_nm(n1[0], n1[1], n2[0], n2[1])
            if direction in ("N", "F"):
                adj.setdefault(k1, []).append((k2, cost, names))
            if direction in ("N", "B"):
                adj.setdefault(k2, []).append((k1, cost, names))
            for_count += 1
    return for_count


# ---- 图容器 + 懒加载缓存 ----

class AirwayGraph:
    def __init__(self, nodes, adj, oneway=None):
        self.nodes = nodes                 # {(ident,region): (lat,lon,kind)}
        self.adj = adj                     # {(ident,region): [(邻点key, 距离NM, [airway名]), ...]}
        self.oneway = oneway or set()      # 单向航路名集合（含 F/B 段）；标名时不当双向用
        self.legal_seg = None              # {(u_key,v_key): {airway名}} —— 从 AIP 实飞航路学到的「合法正向」航段（懒加载）
        self.seg_pop = None                # {(u_key,v_key): int} —— 该有向航段被多少机场对实飞（走廊热度，懒加载，供 A* 加权）
        self.dep_heads = None              # {icao: {head_key: 次数}} —— 各机场从官方航路学到的真实离场过渡点（航路首个图航点）
        self.arr_tails = None              # {icao: {tail_key: 次数}} —— 各机场从官方航路学到的真实进场过渡点（航路末个图航点）
        self.node_items = list(nodes.items())   # 供最近点线性扫
        self.outset = set(adj)             # 有出边的点（可作入航起点）
        self.inset = {nb for nbrs in adj.values() for (nb, _c, _n) in nbrs}  # 有进边的点（可作出航终点/可达）
        self.radj = None                   # 懒建反向邻接（见 _radj）：进场门常是航路【终端】(0 出边，如成田 RUTAS)，
                                           #   要查「traffic 从哪条航路进这个门」只能反查前驱
        # ident → key（解析 AIP 航路串里的航点用；同名优先取 RJ/RO 区域）
        self.by_ident = {}
        for (ident, region) in nodes:
            if ident not in self.by_ident or region in ("RJ", "RO"):
                self.by_ident[ident] = (ident, region)


_GRAPH = None
_LOCK = threading.Lock()


def get_graph(dat_path=None):
    """构建并缓存航路图（首次调用解析导航数据，约几秒；之后命中缓存）。任何失败都返回空图、不抛。"""
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    with _LOCK:
        if _GRAPH is not None:
            return _GRAPH
        nodes, adj, oneway = {}, {}, set()
        try:
            fixp = _find_sibling("earth_fix.dat", dat_path)
            navp = _find_sibling("earth_nav.dat", dat_path)
            awyp = _find_sibling("earth_awy.dat", dat_path)
            if not (fixp and awyp):
                print("ℹ️ 未找到 earth_fix.dat / earth_awy.dat，本地航路生成不可用。")
            else:
                print("🧭 首次生成航路：正在解析导航数据，请稍等…")
                _parse_fixes(fixp, nodes)
                if navp:
                    _parse_navaids(navp, nodes)
                edges = _parse_airways(awyp, nodes, adj, oneway)
                print(f"🧭 导航数据就绪：{len(nodes)} 个航路点 / {edges} 条航段（日本范围）。")
        except Exception as e:
            print(f"⚠️ 导航数据读取失败（本地航路生成将不可用）: {e}")
            nodes, adj, oneway = {}, {}, set()
        _GRAPH = AirwayGraph(nodes, adj, oneway)
        return _GRAPH


# ---- 入/出航点选择 + A* ----

def _nearest_nodes(lat, lon, graph, k=_K_ENTRY, max_nm=_MAX_ENTRY_NM,
                   require_outbound=False, require_inbound=False, toward=None):
    """机场附近的航路点中挑 k 个接入/接出候选（几何兜底：该机场无官方门/STAR/本场 VOR 时才用）。
    require_outbound→只取有出边的点（入航用）；require_inbound→只取有进边的点（出航用，须可达；剔除孤立的进近 VOR 等）。
    toward=(lat,lon) 给定时，按「接入距离 + 该点到 toward 的大圆」排序——优先朝目标方向的接入点，
    避免选到「机场背后」的点导致航路一开头就大角度掉头。返回 [(key, 直飞距离NM), ...]。

    ⚠️ **已知短板（收紧过、反而更差，故维持现状）**：`max_nm`=120 NM 偏大——无官方端点的军用/小场会把
       上百海里外的网点当「进场点」（RJAK 取到 104 NM 外海上的 ELNIS、RJTF 取到 119 NM 外的 MORIZ），
       A* 为够到它把整条航路拧歪（RJBD→RJAK 大圆 277 NM 却飞 430 NM、UTIBO 处 113° 掉头）。
       但把首选半径收到 60 NM 实测**净亏**：修好 RJBD→RJAK / RJDA→RJTF 两条，却把
       RJAN→RJKA(+160 NM) / RJOI→RJTA(+90 NM) 打坏，200 对总距 +388 NM。
       根因在 A* 的代价把「端点→机场」当直线 DCT **白算**（长直飞段不受罚），单靠收半径治不了，留待后续。"""
    cands = []
    for key, (nlat, nlon, _kind) in graph.node_items:
        if require_outbound and key not in graph.outset:
            continue
        if require_inbound and key not in graph.inset:
            continue
        d = haversine_nm(lat, lon, nlat, nlon)
        if d <= max_nm:
            score = d + (haversine_nm(nlat, nlon, toward[0], toward[1]) if toward else 0.0)
            cands.append((score, d, key))
    cands.sort(key=lambda t: t[0])
    return [(key, d) for _score, d, key in cands[:k]]


def _astar(graph, starts, exit_dct, glat, glon):
    """A*：starts={入航key: 起始g(机场→入航直飞)}；exit_dct={出航key: 出航→机场直飞}；
    虚拟终点 _ARR 在到达机场处。启发 h=到机场大圆距离（可采纳）。
    返回 (path_keys, names_seq)：path 为航路点序列，names_seq[i] 为 path[i]→path[i+1] 所用 airway 名列表。无解返回 None。

    ⚠️ 状态是 **(前一点, 当前点)** 而不是单个点——只有记住上一段的航向，才算得出转弯角。
       这是转弯成本的前提；节点态 A* 里发夹弯是免费的，它会用 100° 掉头去换几海里（见 _TURN_* 常量）。"""
    counter = 0
    openh = []
    g = {}
    came = {}                              # 状态 (prev,key) -> (前驱状态, 该段 airway 名列表) ; 入航态为 None
    for key, g0 in starts.items():
        st = (None, key)
        g[st] = g0
        nlat, nlon, _ = graph.nodes[key]
        heapq.heappush(openh, (g0 + haversine_nm(nlat, nlon, glat, glon), counter, st))
        counter += 1
        came[st] = None
    while openh:
        _f, _c, st = heapq.heappop(openh)
        prev, key = st
        if key == _ARR:
            # 回溯：_ARR 的前驱态停在出航点（该段为直飞、无 airway 名）
            path, names_seq = [], []
            s = came[st][0]
            while s is not None and came.get(s) is not None:
                path.append(s[1])
                names_seq.append(came[s][1])
                s = came[s][0]
            if s is not None:
                path.append(s[1])          # 入航点（came 为 None）
            path.reverse()
            names_seq.reverse()
            return path, names_seq
        cur_g = g[st]
        sp = graph.seg_pop
        plat = plon = None
        if prev is not None:
            plat, plon, _ = graph.nodes[prev]
        klat, klon, _ = graph.nodes[key]
        # 沿航路扩展（搜索权重乘子恒 ≥1、转弯罚 ≥0 → 启发可采纳；真实航程仍由 _finish 按 haversine 重算）：
        #   ①「优先 RNAV」整段无 RNAV 名的纯传统边 ×_TRAD_AIRWAY_PENALTY；
        #   ②「高频走廊」越少被官方实飞的有向段越加罚（干线不罚）→ 软偏好真实常用走廊；
        #   ③「转弯成本」大角度转向按度折算海里加罚 → 掉头不再免费（否则会抄出发夹弯航路）。
        for (nb, cost, names) in graph.adj.get(key, ()):
            if nb == prev:
                continue                   # 不原路折回
            f = 1.0 if any(_is_rnav(n) for n in names) else _TRAD_AIRWAY_PENALTY
            if sp:
                pop = sp.get((key, nb), 0)
                f *= 1.0 if pop >= _TRUNK_POP else (_MINOR_CORRIDOR_PENALTY if pop else _OFFTRUNK_PENALTY)
            add = cost * f
            if prev is not None:
                nlat, nlon, _ = graph.nodes[nb]
                turn = abs((_bearing(klat, klon, nlat, nlon)
                            - _bearing(plat, plon, klat, klon) + 180) % 360 - 180)
                if turn > _TURN_FREE_DEG:
                    add += (turn - _TURN_FREE_DEG) * _TURN_NM_PER_DEG
            ng = cur_g + add
            nst = (key, nb)
            if nst not in g or ng < g[nst]:
                g[nst] = ng
                came[nst] = (st, names)
                nlat, nlon, _ = graph.nodes[nb]
                heapq.heappush(openh, (ng + haversine_nm(nlat, nlon, glat, glon), counter, nst))
                counter += 1
        # 若为出航候选，提供到达机场的虚拟直飞边
        if key in exit_dct:
            ng = cur_g + exit_dct[key]
            est = (key, _ARR)
            if est not in g or ng < g[est]:
                g[est] = ng
                came[est] = (st, None)
                heapq.heappush(openh, (ng, counter, est))   # h(_ARR)=0
                counter += 1
    return None


# ---- 航路串格式化 + 连贯性检查 ----

def _safe_names(names, oneway, legal):
    """本段(u→v)在该方向【确定合法】的 airway 名。
    安全集 = 完全双向(不在 oneway) ∪ 本段已学合法正向(在 legal=该 (u,v) 由 AIP 实飞学到的航路集)。
    单向且(本段无 AIP 证据 / 学到的是反向)的航路不安全、不用于标名（避免标出逆向航路串）。"""
    return [n for n in names if n not in oneway or n in legal]


def _format_route(path, names_seq, oneway=frozenset(), legal_seg=None):
    """把 path + 每段 airway 名格式化为 AIP 风格串：仅在 airway 变化处保留换路点。

    ⚠️ 标名按「**能连续覆盖最多航段**」选，而不是逐段贪心挑 RNAV —— 真实飞行计划就是这么写的：
       一条航路能一直飞到哪儿，就写到哪儿。逐段贪心会把一条完整航路切碎：实测 XAC→…→HCE 明明整段
       都是 V18，只因中间某几段还并挂着平行的 Y588/Y587，就被切成
       `XAC Y588 MOE V18 GYOGN Y587 SANGO V18 HCE`（物理路径对、串却面目全非）。
       RNAV 优先降为**同覆盖长度时的次级判据**（选路层的 RNAV 偏好由 _TRAD_AIRWAY_PENALTY 负责，与标名无关）。

    某段 names 为空 = 该段【不在任何航路上】的真 DCT 直飞腿（如 VATJPN 到着尾段里那些离网的点）
    → 只列航点、不写航路名。

    `oneway`=单向航路名集合；`legal_seg`={(u,v):{航路}}=AIP 实飞学到的合法正向，用来在该方向安全地保留单向 RNAV。"""
    if not path:
        return ""
    legal_seg = legal_seg or {}
    n = len(names_seq)
    safe_at = [_safe_names(names_seq[i] or (), oneway, legal_seg.get((path[i], path[i + 1]), ()))
               for i in range(n)]

    chosen = [None] * n
    i = 0
    while i < n:
        cands = safe_at[i] or list(names_seq[i] or ())           # 安全集为空 → 兜底用原名
        if not cands:                                            # 该段无任何航路名 → 真 DCT 腿
            chosen[i] = None
            i += 1
            continue
        best, best_run = None, -1
        for a in cands:
            run = 0                                              # 这条航路从 i 起能连续覆盖几段
            while i + run < n and a in (safe_at[i + run] or names_seq[i + run] or ()):
                run += 1
            if run > best_run or (run == best_run and _is_rnav(a) and not _is_rnav(best)):
                best, best_run = a, run                          # 覆盖更长者胜；同长则 RNAV 胜
        for k in range(i, i + max(best_run, 1)):
            chosen[k] = best
        i += max(best_run, 1)

    tokens = [path[0][0]]
    for i, awy in enumerate(chosen):
        if awy is None:                                          # DCT 腿：只列航点
            tokens.append(path[i + 1][0])
            continue
        if i == 0 or awy != chosen[i - 1]:
            tokens.append(awy)                                   # 进入新 airway
        is_junction = (i == len(chosen) - 1) or (chosen[i + 1] != awy)
        if is_junction:
            tokens.append(path[i + 1][0])                        # 换路点 / 出航点
    return " ".join(tokens)


def _check_continuity(coords, idents, max_turn=_MAX_TURN_DEG):
    """对 enroute 航路点折线逐内点算转向角，最尖的转角超阈值即记大锐角转弯。
    只看航路段之间的转弯——SID/STAR 衔接处（首/尾点与机场之间）的转向由程序消化，不计入。
    coords / idents 一一对应（都是 enroute 航路点序列）。返回 (suspect, warn)。"""
    worst, worst_at = 0.0, None
    for i in range(1, len(coords) - 1):
        b_in = _bearing(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        b_out = _bearing(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        turn = abs(b_out - b_in) % 360.0
        if turn > 180.0:
            turn = 360.0 - turn
        if turn > worst:
            worst, worst_at = turn, idents[i]
    if worst > max_turn:
        return True, f"在 {worst_at} 附近有约 {worst:.0f}° 大角度转弯，可能非最优/有问题，请自行核对。"
    return False, None


# ---- CIFP 进离场程序端点（SID 出口 / STAR 入口）----

_CIFP_CACHE = {}


def _cifp_endpoints(icao, dat_path=None):
    """解析 CIFP/<ICAO>.dat，提取该机场各类 enroute 衔接点。返回 4 个 {(ident,region)} 集合：
      sid_exits     SID 里 section=='E' 的航路点 —— 离场出口（脱离 SID、接入航路网处）
      star_entries  STAR 里 section=='E' 的航路点 —— 进场入口（航路网交给 STAR 处）
      iaf_if        APPCH 里描述码第 4 位为 'A'(IAF) / 'I'(IF) 的航路点 —— 无 STAR 时的进近衔接点
      vors          APPCH 里 section=='D' 的导航台 —— 本场 / 进近 VOR
    CIFP 行：`<TYPE>:seq,rtype,proc,trans,fix,region,section,sub,desccode,...`（fix=4、region=5、section=6、desccode=8）。
    无文件/无程序 → 空集。按机场缓存。"""
    if icao in _CIFP_CACHE:
        return _CIFP_CACHE[icao]
    sid, star, iaf_if, vors = set(), set(), set(), set()
    try:
        path = os.path.join(_navdata_dir(dat_path), "CIFP", icao + ".dat")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    head, _sep, rest = line.partition(":")
                    if head not in ("SID", "STAR", "APPCH"):
                        continue
                    p = rest.split(",")
                    if len(p) < 7:
                        continue
                    fix, region, section = p[4].strip(), p[5].strip(), p[6].strip()
                    if not fix:
                        continue
                    key = (fix, region)
                    if section == "D":          # 任何程序里的 VHF 导航台 = 本场/进近 VOR（离场进场枢纽）
                        vors.add(key)
                    if head == "SID":
                        if section == "E":      # SID 末端的 enroute 航路点（出口）
                            sid.add(key)
                    elif head == "STAR":
                        if section == "E":      # STAR 首端的 enroute 航路点（入口）
                            star.add(key)
                    else:  # APPCH：取 IAF/IF（描述码第 4 位为 A/I）
                        desc = p[8] if len(p) > 8 else ""
                        if len(desc) >= 4 and desc[3] in ("A", "I"):
                            iaf_if.add(key)
    except Exception:
        pass
    _CIFP_CACHE[icao] = (sid, star, iaf_if, vors)
    return sid, star, iaf_if, vors


# ---- 端点候选（直接 A* 与 Rule 5 桥接共用）----

_TRANSFER = None


def _transfer_points():
    """加载 transfer_points.json（VATJPN 移管表抽取的各机场进场门/离场头），缓存。缺失/损坏 → 空 dict。
    运行目录锚定（源码=项目根，frozen=exe 同级，与 airlines.json 一致）。"""
    global _TRANSFER
    if _TRANSFER is not None:
        return _TRANSFER
    _TRANSFER = {}
    try:
        p = os.path.join(get_real_run_path(), "transfer_points.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                _TRANSFER = json.load(f)
    except Exception:
        _TRANSFER = {}
    return _TRANSFER


def arr_gate_routings(icao, gate):
    """VATJPN「到着」栏里该进场门的全部官方径路 → [{via_fix, via_awy, dct, star, cond}, …]（无则空）。
    `via_fix`/`via_awy` = **来向**（上游接入点/航路）；`dct` = 门后 enroute 该照抄的直飞段；
    `star` = 门后展开点反查出的 STAR；`cond` = 方向/机型/跑道限定（原文）。"""
    return list((_transfer_points().get(icao, {}).get("arr_gates") or {}).get(gate) or ())


def gate_stars(icao, gate):
    """VATJPN 说【这个进场门配哪条 STAR】。

    官方到着栏把门后的 STAR 机体**逐点展开**写了出来——`…KOHWA Y546 AGPUK MIRAI ABENO IKOMA` 里
    `MIRAI ABENO IKOMA` 就是 STAR「IKOMAE」的机体；同理 RJAH 的 `TATSU NAKAH` = STAR「TATSU」。
    拿这串点去反查 CIFP，就能**直接读出**该门配哪条 STAR，不必再靠几何猜
    （RJOO 的 AGPUK 上同时挂着 STAR「AGPUK」与「IKOMAE」，只有这条路能分清）。
    空 = VATJPN 没展开门后径路 / 该门本就没 STAR → 调用方回退按 CIFP 端点匹配。"""
    out = []
    for r in arr_gate_routings(icao, gate):
        for s in (r.get("star") or ()):
            if s not in out:
                out.append(s)
    return out


_GATE_DIR_MARGIN_NM = 30.0      # 官方门方向过滤余量：到对端的距离 ≤「机场到对端」+ 此值才保留（丢掉另一侧/反向、会逼绕道的门）
_GATE_GIVEUP_RATIO = 1.4        # 离场门质量门控：用官方离场头算出的航路若 >此倍×大圆（或含大锐角）→ 弃门重算（退本场 VOR/SID/几何）
_VIA_DIR_TOL_DEG = 90.0         # 来向容差：本次来向 与 VATJPN 给该门的官方上游走廊 夹角 ≤ 此值才算方向相符
_VIA_DIR_MIN_NM = 100.0         # 航程门槛：短于此的航段【不做】来向过滤——
                                # VATJPN 的到着走廊描述的是「从远处进入终端区」的交通；32 NM 的航段全程都在终端区里，
                                # 「门→出发机场」的方位由局部几何主导、根本不代表进场走廊来向。
                                # （实测：RJNG→RJGG 32NM 夹角 124°、RJDU→RJFF 47NM 夹角 92° —— 两条都被误剔了官方门，
                                #   反而绕远、还绕出大锐角；而 141NM 的 RJTT→RJNA、344NM 的 RJNO→RJAA 夹角只有 20–50°，判得很准。）


def _radj(graph):
    """反向邻接 {key: [(前驱key, 距离, [airway名]), …]}，懒建缓存。
    进场门常是航路【终端】（成田 RUTAS/SUPOK/LUBLA 出边=0），要问「traffic 从哪条航路进这个门」只能反查前驱。"""
    if graph.radj is None:
        r = {}
        for u, nbrs in graph.adj.items():
            for (v, c, n) in nbrs:
                r.setdefault(v, []).append((u, c, n))
        graph.radj = r
    return graph.radj


def _via_anchors(graph, icao, gate_key):
    """该进场门的【官方来向锚点】坐标 —— 即 VATJPN「到着」栏写在门前面的那段上游走廊。
    给了上游点（`…MBE Y121 SWING` 的 MBE）就用它；只给航路名（`…Y88 KCC`）则取该航路上**进入本门**的前驱节点。
    这就是「方向」的出处：**门本身的位置不编码来向**（RJNA 的 KCC 就在场内，从哪来都「顺路」），
    编码来向的是它上游那段走廊。"""
    out = []
    for r in arr_gate_routings(icao, gate_key[0]):
        k = graph.by_ident.get(r.get("via_fix")) if r.get("via_fix") else None
        if k and k in graph.nodes:
            out.append((graph.nodes[k][0], graph.nodes[k][1]))
            continue
        awy = r.get("via_awy")
        if not awy:
            continue
        for (u, _c, names) in _radj(graph).get(gate_key, ()):
            if awy in names:
                out.append((graph.nodes[u][0], graph.nodes[u][1]))
    return out


def _via_dir_ok(graph, arr, gate_key, toward):
    """本次来向是否与 VATJPN 给该门的官方走廊相符（夹角 ≤ `_VIA_DIR_TOL_DEG`）。
    无 toward / 航段过短(见 `_VIA_DIR_MIN_NM`) / 该门无 via 数据 / 锚点解析不出 → 不判（放行，退回原几何过滤）。"""
    if not toward:
        return True
    if haversine_nm(arr.lat_dd, arr.lon_dd, toward[0], toward[1]) < _VIA_DIR_MIN_NM:
        return True                                              # 短程：全程都在终端区，来向无意义
    anchors = _via_anchors(graph, arr.code, gate_key)
    if not anchors:
        return True
    glat, glon = graph.nodes[gate_key][0], graph.nodes[gate_key][1]
    b_dep = _bearing(glat, glon, toward[0], toward[1])            # 本次实际来向（门 → 出发地）
    for a in anchors:
        d = abs(b_dep - _bearing(glat, glon, a[0], a[1])) % 360.0
        if min(d, 360.0 - d) <= _VIA_DIR_TOL_DEG:
            return True
    return False


def _dir_filter(gates, ref_lat, ref_lon, toward, graph):
    """只留「朝 toward 方向有进展」的官方门：该门到 toward 的距离 ≤ 机场到 toward 的距离 + 余量。
    丢掉位于机场另一侧/反方向的门（这类移管表里多是其它运行方向的门），避免被强制绕道。toward 缺失则不过滤。"""
    if not gates or not toward:
        return gates
    rd = haversine_nm(ref_lat, ref_lon, toward[0], toward[1])
    return [k for k in gates if haversine_nm(graph.nodes[k][0], graph.nodes[k][1], toward[0], toward[1]) <= rd + _GATE_DIR_MARGIN_NM]
    # 全被滤掉（门都在反向）→ 返回空，上层回退 CIFP/几何（方向感知），避免被官方门强制绕道


def _learned_heads(graph, icao):
    """该机场从官方航路学到的真实离场过渡点 keys（航路【首个】图航点，如 RJGG 北向 KCC / 东向 BOGON）。无则空 list。"""
    return list((graph.dep_heads or {}).get(icao, {}))


def _learned_tails(graph, icao):
    """该机场从官方航路学到的真实进场过渡点 keys（航路【末个】图航点）。无则空 list。"""
    return list((graph.arr_tails or {}).get(icao, {}))


_ONFIELD_VOR_NM = 15.0          # 本场VOR判定：CIFP section-D 导航台里距机场最近且 ≤此距离的才算「本场台」（场内/跑道旁）；
                                # 排除被程序当中途航点/feeder 引用的远端 VOR（如 RJSY 的 YTE 距场 37nm，是 ZUNDA2 SID 的中途点，非本场台）


def _onfield_vor(airport, graph, vors):
    """本场VOR = CIFP section-D 导航台里【距机场最近且 ≤_ONFIELD_VOR_NM】的那一个（场内/跑道旁，引导回场用）。
    section-D 会收录程序里引用的所有 VOR（含 SID/STAR 中途航点、feeder），不能一概当本场台——必须按坐标取最近的
    （见 skill cifp_format「本场 VOR 辨识」：RJTT 是场内 TTE 而非 feeder VOR）。返回 {key}（单个最近本场台）或空集。"""
    best, bestd = None, _ONFIELD_VOR_NM
    for k in vors:
        d = haversine_nm(airport.lat_dd, airport.lon_dd, graph.nodes[k][0], graph.nodes[k][1])
        if d <= bestd:
            best, bestd = k, d
    return {best} if best else set()


def _departure_candidates(dep, graph, dat_path, toward, use_gates=True):
    """离场端候选 [(key, 机场→该点直飞NM)]：把【官方航路学到的真实离场过渡点】∪【VATJPN 移管表官方离场头】∪【CIFP SID 出口/本场 VOR】
    并集（须有出边），按航向过滤后交给 A* 自选最优。学到端点是「AIP 桥接 × 走廊」融合核心（直接取官方航路真实接入点，
    如 RJGG 北向 KCC，不借邻场程序）；但用并集而非优先级——保证不会因学到端点不全/反向而漏掉更优 CIFP 出口（A* 取最短自然选对）。全空 → 几何兜底。
    use_gates=False 时只用 CIFP（弃学到端点与移管门，退「本场台离场 + VOR 程序」），供质量门控回退。"""
    sid, _st, _iaf, vor = _cifp_endpoints(dep.code, dat_path)
    cifp = [k for k in (sid | _onfield_vor(dep, graph, vor)) if k in graph.outset]   # 本场VOR 只取最近的一个，排除中途点/feeder VOR
    pool = cifp
    if use_gates:
        learned = [k for k in _learned_heads(graph, dep.code) if k in graph.outset]
        gates = [k for k in (graph.by_ident.get(g) for g in _transfer_points().get(dep.code, {}).get("dep", []))
                 if k and k in graph.outset]
        pool = list(dict.fromkeys(learned + gates + cifp))
    ek = _dir_filter(pool, dep.lat_dd, dep.lon_dd, toward, graph)
    cands = [(k, haversine_nm(dep.lat_dd, dep.lon_dd, graph.nodes[k][0], graph.nodes[k][1])) for k in ek]
    if not cands:
        cands = _nearest_nodes(dep.lat_dd, dep.lon_dd, graph, require_outbound=True, toward=toward)
    return cands


def _arr_dct_anchors(graph, icao):
    """把 arr_dct 每条【进场门 + DCT 尾段】重锚到路径里第一个【图上可达(inset)】的 fix，返回 {anchor_key: [其后 DCT 尾段 idents]}。
    进场门本身在航路网上(如 RJFM 的 KUE) → anchor 即门、尾段不变；门是【网外终端 fix】(如 RJFF 的 HABOH，region RJDA、无图节点) →
    顺 [门]+尾段 找到第一个可达点(FUGEN)作 anchor、其后(OMUTA OSTEP HONOK)为 DCT 尾段。anchor 供进场候选(A* 能落上去)、尾段供落点后 DCT 补全。
    注：这些是 VATJPN 允许的合法到着，保留为可达候选；是否优先由走廊奖励(_prefer_corridor_arrival)裁决——常用走廊(如 Y25 ISKUP)在容差内胜出、这类作备选。"""
    out = {}
    for gate, tail in _transfer_points().get(icao, {}).get("arr_dct", {}).items():
        path = [gate] + list(tail or [])
        for i, ident in enumerate(path):
            k = graph.by_ident.get(ident)
            if k and k in graph.inset:
                out[k] = path[i + 1:]
                break
    return out


def _arrival_candidates(arr, graph, dat_path, toward):
    """进场端候选 [(key, 该点→机场直飞NM)]：把【官方航路学到的真实进场过渡点】∪【VATJPN 移管表官方进场门】∪【到着 DCT 尾段可达锚点】
    ∪【CIFP STAR 入口】并集（enroute 级进场点，须有进边），按航向过滤后交给 A* 自选最优；都空时再回退【本场 VOR】→【IAF/IF】→几何。
    学到尾点是融合核心（直接取官方航路真实落地接入点）；用并集保证不因它不全/反向而漏选，又保留「enroute 门优先于进近 VOR/IAF」。"""
    _sd, star, iaf, vor = _cifp_endpoints(arr.code, dat_path)
    learned = [k for k in _learned_tails(graph, arr.code) if k in graph.inset]
    gates = [k for k in (graph.by_ident.get(g) for g in _transfer_points().get(arr.code, {}).get("arr", []))
             if k and k in graph.inset]
    anchors = list(_arr_dct_anchors(graph, arr.code))       # 到着 DCT 尾段的可达锚点（门在网外时如 RJFF 的 FUGEN），保留为合法备选
    enroute = list(dict.fromkeys(learned + gates + anchors + [k for k in star if k in graph.inset]))
    prim = _dir_filter(enroute, arr.lat_dd, arr.lon_dd, toward, graph)
    # 官方来向过滤：VATJPN 给了上游走廊的门，来向对不上就剔除（几何过滤对【场内 VOR 型的门】无能为力——
    # RJNA 的 KCC 离场 0.8NM，从任何方向来都「顺路」，只有它上游走廊 MIDER/KMC 才说明它是【西向】的门）。
    # 全被滤光则不滤（数据不全时不至于无解）。
    prim = [k for k in prim if _via_dir_ok(graph, arr, k, toward)] or prim
    xk = (prim
          or [k for k in _onfield_vor(arr, graph, vor) if k in graph.inset]   # 本场VOR 只取最近的一个，排除中途点/feeder VOR
          or [k for k in iaf if k in graph.inset])
    cands = [(k, haversine_nm(graph.nodes[k][0], graph.nodes[k][1], arr.lat_dd, arr.lon_dd)) for k in xk]
    if not cands:
        cands = _nearest_nodes(arr.lat_dd, arr.lon_dd, graph, require_inbound=True, toward=toward)
    return cands


def _path_length_nm(graph, path):
    return sum(haversine_nm(graph.nodes[path[i]][0], graph.nodes[path[i]][1],
                            graph.nodes[path[i + 1]][0], graph.nodes[path[i + 1]][1])
               for i in range(len(path) - 1))


def _finish(graph, dep, arr, path, names_seq, source):
    """由 enroute 航路点序列 + 各段 airway 名，组出返回 dict（总距、连贯性、航路串）。"""
    if not path:
        return None
    total = (haversine_nm(dep.lat_dd, dep.lon_dd, graph.nodes[path[0]][0], graph.nodes[path[0]][1])
             + _path_length_nm(graph, path)
             + haversine_nm(graph.nodes[path[-1]][0], graph.nodes[path[-1]][1], arr.lat_dd, arr.lon_dd))
    coords = [(graph.nodes[k][0], graph.nodes[k][1]) for k in path]
    suspect, warn = _check_continuity(coords, [k[0] for k in path])
    geo = ([(dep.code, dep.lat_dd, dep.lon_dd)]
           + [(k[0], graph.nodes[k][0], graph.nodes[k][1]) for k in path]
           + [(arr.code, arr.lat_dd, arr.lon_dd)])                 # 含起降机场首尾，供画图/测长
    return {"route_str": _format_route(path, names_seq, graph.oneway, graph.legal_seg), "fixes": path, "dist_nm": total,
            "source": source, "suspect": suspect, "warn": warn, "coords": geo,
            "_names": list(names_seq)}     # 各段 airway 名（内部用：补进场尾段后要据此重排航路串）


_ARR_TAIL_BACKTRACK_NM = 8.0    # 进场尾段裁剪：某尾点比已达最近点还远出此值→视作背离本场，从此截断（丢 ROAH NHC→LAVON、RJTL …SHT→TOHNE 这类）


def _star_connect(icao, dat_path):
    """该机场 STAR 与航路的合法衔接点 idents。**延迟 import**：`procedures` 在模块级 import 了本模块（procedures→router），
    这里若在模块级反向 import 就成环；放在调用时导入即可（届时两边都已加载完）。
    ⚠️ 必须复用 `procedures` 的口径，别在这儿另写一份 CIFP 解析——「路由器和面板各自定义程序从哪儿接管」正是病根本身。"""
    try:
        from . import procedures
        return procedures.star_connect_points(icao, dat_path)
    except Exception:
        return set()


def _arrival_tail_keys(graph, arr, endpoint_key, dat_path=None):
    """VATJPN「到着」里进场门之后那串**直飞点**中，本次该照抄进 enroute 的部分 → [key,…]（可空）。
    （尾段按 `_arr_dct_anchors` 的可达锚点查表——落点=某进场门(RJFM KUE)或重锚点(RJFF FUGEN) 时才有。）

    ★ **核心分支：官方表里写出来的「直飞」有两种，必须分开对待**
      ①【真·直飞径路】该点处**接不上任何 STAR**（不少机场压根没 STAR，只有 IAP/雷达引导）→ 这才是真的离网 DCT，
         照抄进 enroute（RJFM `KUE ESKAP KROMA ENBEN MZE`、RJTL、ROAH… 共 25 条）。
      ②【被展开成点列的 STAR】该点处**接得上 STAR** → 官方只是把 transition/机体逐点写了出来；那是**程序**的活，
         不该塞进 enroute（RJAH 的 `TATSU NAKAH` 其实就是 STAR「TATSU」的机体 TATSU→NAKAH）。
      判据 = `procedures.star_connect_points`，**与面板 `matching_choices` 同一套**，于是
      「enroute 收在衔接点」⟺「面板必定匹配得到 STAR」。
      ② 若被误当 ① 处理，末点就越过了 STAR 的接入口 → 面板匹配不上 → 回退「列出全部 STAR」→ 按序默认选中了
      **别的方向**那条（RJAH 实测选中 GOT1、正解是 TATSU，预览图上从 NAKAH 拐去 GOT 再折回本场，绕出一大弯）。

    故：落点处就接得上 → 尾段整段不要（35 条中 6 条）；尾段中途接得上 → 收下该点后截断（另 4 条）。"""
    tail = _arr_dct_anchors(graph, arr.code).get(endpoint_key)
    if not tail:
        return []
    connect = _star_connect(arr.code, dat_path)             # STAR 能从哪些点接管（与面板同源）
    if endpoint_key[0] in connect:                          # 落点处 STAR 已能接管 → 尾段整段是【展开的 STAR】，非直飞径路
        return []
    out, best = [], haversine_nm(graph.nodes[endpoint_key][0], graph.nodes[endpoint_key][1], arr.lat_dd, arr.lon_dd)
    for ident in tail:
        k = graph.by_ident.get(ident)
        if not k or k in out or k == endpoint_key:
            break
        d = haversine_nm(graph.nodes[k][0], graph.nodes[k][1], arr.lat_dd, arr.lon_dd)
        if d > best + _ARR_TAIL_BACKTRACK_NM:                # 背离本场 → 截断（后续尾点丢弃）
            break
        out.append(k)
        if ident in connect:                                 # STAR 从这里接管 → enroute 收在衔接点，其余交给 STAR
            break
        best = min(best, d)
    return out


def _append_arrival_tail(graph, dep, arr, res, dat_path=None):
    """把 VATJPN「到着」尾段（进场门之后的直飞点，如 RJFM 北向 KUE ESKAP KROMA ENBEN MZE）补到生成航路末尾——
    这些点管制会据以引导下降，属真实进场衔接。enroute 连贯性已在 _finish 按航路段算过，尾段（进场消化段）不再计入。

    ⚠️ 尾段的点【不一定离网】：VATJPN 表里同一段常有两种写法——`… UWE Y312 ODE`（用航路名）
       与 `… UWE BONJI ODE`（把航点逐个列出）——而 RJSR 的 BONJI 其实就在 Y312 上，两者是**同一条物理路径**。
       这里原本把尾段一律【裸拼航点】，于是明明整段都在 Y312 上，却被写成 `… Y312 UWE BONJI ODE`
       （几何对、串却把一条航路拆成了 DCT 点列，还与前段的 Y312 自相矛盾）。
       故：尾段每一腿都去图里查真实的 airway 名，有边就带上名字、交给 _format_route 统一成串；
       真正离网的腿（查无此边）names 为空 → _format_route 只列航点，与原行为一致。"""
    if not res or not res.get("fixes"):
        return res
    tail = _arrival_tail_keys(graph, arr, res["fixes"][-1], dat_path)
    if not tail:
        return res
    path = res["fixes"] + tail
    names = list(res.get("_names") or [])
    for a, b in zip(path[len(res["fixes"]) - 1:], tail):          # 从落点起，逐腿查图里的真实边
        e = next((x for x in graph.adj.get(a, ()) if x[0] == b), None)
        names.append(list(e[2]) if e else [])                     # 无边 = 真 DCT 腿 → 空名
    res["fixes"] = path
    res["_names"] = names
    res["dist_nm"] = (haversine_nm(dep.lat_dd, dep.lon_dd, graph.nodes[path[0]][0], graph.nodes[path[0]][1])
                      + _path_length_nm(graph, path)
                      + haversine_nm(graph.nodes[path[-1]][0], graph.nodes[path[-1]][1], arr.lat_dd, arr.lon_dd))
    res["route_str"] = _format_route(path, names, graph.oneway, graph.legal_seg)
    res["coords"] = ([(dep.code, dep.lat_dd, dep.lon_dd)]
                     + [(k[0], graph.nodes[k][0], graph.nodes[k][1]) for k in path]
                     + [(arr.code, arr.lat_dd, arr.lon_dd)])
    return res


def _trace_airway(graph, a, b, awy):
    """从 a 出发、只走名为 awy 的航路边，在图中走到 b，返回完整 fix 序列 [a,…,b]（含中间过渡点）；
    走不通（airway 名对不上 / 不连通）返回 None。Dijkstra 限定边的 airway 名。"""
    if a == b:
        return [a]
    dist = {a: 0.0}
    prev = {a: None}
    pq = [(0.0, 0, a)]
    cnt = 0
    while pq:
        d, _, k = heapq.heappop(pq)
        if k == b:
            path, x = [], b
            while x is not None:
                path.append(x)
                x = prev[x]
            return path[::-1]
        if d > dist.get(k, float("inf")):
            continue
        for (nb, cost, names) in graph.adj.get(k, ()):
            if awy not in names:
                continue
            nd = d + cost
            if nd < dist.get(nb, float("inf")):
                dist[nb] = nd
                prev[nb] = k
                cnt += 1
                heapq.heappush(pq, (nd, cnt, nb))
    return None


def _airways_out(graph, u):
    """u 出边上出现过的所有 airway 名（去重）。"""
    s = set()
    for (_nb, _c, names) in graph.adj.get(u, ()):
        s.update(names)
    return s


def _seg_dir_safe(graph, seg, awy):
    """traced 段每一步 (u→v) 上 awy 在该方向都合法（不在 oneway，或该段已从 AIP 实飞学到合法正向含 awy）——
    用于平滑时拒绝把路收编到反向的单向 RNAV 航路（与 _safe_names 的「安全集」同口径）。"""
    legal_seg = graph.legal_seg or {}
    for u, v in zip(seg, seg[1:]):
        if awy in graph.oneway and awy not in legal_seg.get((u, v), ()):
            return False
    return True


def _min_seg_pop(graph, seg):
    """一段路径里最冷门那一节的走廊热度（被多少机场对官方实飞过）。0 = 含从未被实飞的航段。"""
    sp = graph.seg_pop or {}
    return min((sp.get((seg[i], seg[i + 1]), 0) for i in range(len(seg) - 1)), default=0)


def _smooth_airway_continuity(graph, path, names_seq):
    """A* 后处理「航路连续性」平滑：把能由【单一 RNAV 航路】近乎等长直达的子段收编成单航路。
    消除走廊加权/A* 偶发的「离开一条航路 → 走平行航路 → 下游又接回同一航路」这类不自然走法
    （典型：明明 Y102 直连 HPE→METEL→SDE，却被收编成 HPE→Y10→VINAR→Y125→METEL→Y102→SDE）。
    判据：子段 path[i..j] 存在单一 RNAV 航路 W 直达，W 实际几何长 ≤ 当前子段 ×_SMOOTH_RATIO，且 W 在该方向合法。
    贪心从 i 起优先收编更远的 j（一次抹掉更多绕行）；纯几何 + 方向合法，不引入反向单向航路。

    ⚠️ **走廊守卫**：收编后的路径不得比原路径【更少被官方实飞】。这里原本只看几何长度与方向合法，
       于是会把官方在飞的走廊换成没人飞的平行 RNAV——实测 RJOA→RJTH 就被它把官方唯一的
       `MOE V18 GYOGN SANGO HCE`（59.8 NM，pop=1）换成了 `MOE Y588 EBINE HCE`（58.6 NM，**pop=0，
       1436 条官方航路里一次没飞过**），只为省 1.2 NM。平滑是去绕行，不该拿走廊真实性去换。
    返回平滑后的 (path, names_seq)。"""
    if not path or len(path) < 3:
        return path, names_seq
    out_path, out_names = [path[0]], []
    n = len(path)
    i = 0
    while i < n - 1:
        found = None
        for j in range(n - 1, i + 1, -1):                       # j≥i+2：至少跨过一个中间点才有「收编」意义
            sub = path[i:j + 1]
            sub_len = _path_length_nm(graph, sub)
            sub_pop = _min_seg_pop(graph, sub)                  # 原子段的走廊真实性下限
            best_awy, best_seg, best_len = None, None, None
            for awy in _airways_out(graph, path[i]):
                if not _is_rnav(awy):                           # 仅收编到 RNAV 航路（不把 RNAV 路降级成传统直连，守问题1）
                    continue
                seg = _trace_airway(graph, path[i], path[j], awy)
                if not seg or len(seg) < 2:
                    continue
                slen = _path_length_nm(graph, seg)
                if slen > sub_len * _SMOOTH_RATIO or not _seg_dir_safe(graph, seg, awy):
                    continue
                if _min_seg_pop(graph, seg) < sub_pop:          # ★ 走廊守卫：不拿在飞的走廊换没人飞的平行航路
                    continue
                if best_awy is None or slen < best_len:         # 多条 RNAV 候选取更短
                    best_awy, best_seg, best_len = awy, seg, slen
            if best_awy:
                found = (j, best_seg, best_awy)
                break
        if found:
            j, seg, _awy = found
            # ⚠️ 只改【路径】，不改【名字】：这里原本把该段名列表重写成收编到的那一个航路名（`[awy]`），
            #    把「这几段其实也并挂着别的航路」的事实抹掉了 → 标名层再也看不到那条能连贯覆盖全程的航路，
            #    于是一条完整的 V18 被切成 `XAC Y588 MOE V18 HCE`。名字交给 _format_route 统一选。
            for a, b in zip(seg[:-1], seg[1:]):
                out_path.append(b)
                e = next((x for x in graph.adj.get(a, ()) if x[0] == b), None)
                out_names.append(list(e[2]) if e else [_awy])
            i = j
        else:
            out_path.append(path[i + 1])
            out_names.append(names_seq[i])
            i += 1
    return out_path, out_names


def _parse_aip_route(route_str, graph, densify=True):
    """把官方 AIP 航路串解析成图中的 (fix_path, names)：能在图里找到的 token 当航点，其余当 airway 名。
    densify=True（默认）时，沿标注的 airway 在图里补出两换路点之间的中间过渡点 —— 这样航路距离按
    实际 airway 折线累加（更精确）、画图也更密；某段补不出来则退化为换路点直连。
    返回 ([key,…], [[airway],…])；航点不足 2 个返回 (None, None)。"""
    seq, pending = [], None                      # 先抽出换路点序列 [(key, 该点之前的 airway 名)]
    for tok in route_str.split():
        key = graph.by_ident.get(tok)
        if key:
            seq.append((key, pending))
            pending = None
        else:
            pending = tok
    if len(seq) < 2:
        return None, None
    fix_path, names = [seq[0][0]], []
    for i in range(1, len(seq)):
        a = seq[i - 1][0]
        b, awy = seq[i][0], seq[i][1]
        seg = _trace_airway(graph, a, b, awy) if (densify and awy and awy != "DCT") else None
        if seg and len(seg) > 2:                  # 加密成功：插入中间 fix（跳过已在序列里的 a）
            for k in seg[1:]:
                fix_path.append(k)
                names.append([awy])
        else:                                     # 直连（DCT 或加密失败）
            fix_path.append(b)
            names.append([awy] if awy else ["DCT"])
    return fix_path, names


# ---- 从 AIP 实飞航路学习航路「合法正向」（修正 earth_awy 在共挂 N 段上丢失的单向信息）----

_LEGAL_LOCK = threading.Lock()
_ARR_DCT_ONLY = {}


def _arr_dct_only(icao):
    """该机场【VATJPN 明列的门后直飞点】中，其本身不是移管点的那些 ident。
    这就是「官方直飞径路」与「enroute」的分界线：门(`arr`)属 enroute 交接点，门后的点(`arr_dct`)属进场径路。
    RJFK 的 `ESLIL HIGOH KGE`、RJFM 的 `ESKAP KROMA ENBEN MZE` 落在这里；
    RJSR 的 `ODE` 虽也出现在 `arr_dct[UWE]` 的尾段里，但它**自己就是移管点**，故不在此集合中、照常可学。"""
    if icao not in _ARR_DCT_ONLY:
        tp = _transfer_points().get(icao, {})
        gates = set(tp.get("arr") or ())
        tail = {f for t in (tp.get("arr_dct") or {}).values() for f in (t or ())}
        _ARR_DCT_ONLY[icao] = tail - gates
    return _ARR_DCT_ONLY[icao]


def _enroute_entry(graph, toks, inbound=False, skip=()):
    """航路串这一端【第一个真正落在 enroute 航路网上】的航点 —— 即真实的进/离场过渡点。
    离场端正序传 toks；进场端倒序传 toks 并置 `inbound=True`。

    ⚠️ **官方直飞径路 ≠ enroute**（`skip`）：官方串常把「进场门 + 门后的直飞径路」整条写出来，
       如 `YAMGA KUE ESLIL HIGOH KGE`——VATJPN 移管表白纸黑字写着 RJFK 的门是 **KUE**，
       而 `ESLIL HIGOH KGE` 是**门之后的官方进场径路**（`transfer_points.json` 的 `arr_dct`），属进场、不属 enroute。
       从末尾往回扫会一头扎进这段径路、停在 HIGOH（它恰好还挂在航路网上），于是学出个**终端区里面**的假门。
       故把「VATJPN 明列的门后直飞点、且其本身不是门」的 ident 传进 `skip` 跳过。
       判据是**门籍**、不是「是不是 DCT」——RJSR 的 `… UWE Y312 ODE` 里 ODE **本身就是 VATJPN 列的门**，照学。

    ⚠️ **两端的连通性判据是反向的**：
       离场端要【出边】——航路得从这个点离开；进场端要【入边】——航路得进到这个点。
       不少进场门本身就是航路的**终端**：成田的 RUTAS 是 Y81 的尽头，**出边=0、只有入边**（SUPOK/LUBLA 同）。
       两端若都拿「有出边」去筛，进场门就会被整个跳过、退到上游的过境 enroute 点——实测 RJAA 因此
       把用了 **34 次**的 RUTAS 一条都没学到，反而从 `RJTT→RJAA OPPAR UTIBO RUTAS` 的中间点学出个
       **从未做过成田进场门**的 UTIBO（VATJPN 的成田进场门只有 SWAMP·SUPOK·LUBLA·RUTAS）。
       于是从西边飞成田会一头扎向 `… Y233 UTIBO`，而 STAR 却从东南的 RUTAS 起 —— 交接断裂。

    ⚠️ 也不能直接取首/末 token。官方串常以【带 transition 的 SID/STAR】开头/结尾：
       如 RJOO 的 `MINAC GUJYO Y13 …` —— CIFP 里 `MINAC4` 这条 SID 的机体终点就是 `MINAC`、
       过渡到 `GUJYO`，`Y13` 才是 enroute。**MINAC 属终端区程序，在 enroute 航路图里【本来就没有边】**，
       拿它当离场点会被连通性过滤丢掉，A* 只好退去用本场 VOR（实测 RJOO 退到了 2.2 NM 外的 ITE，
       而 ITE 在 1436 条官方航路里只作过 2 次离场点）。真正的 enroute 入口是过渡终点 `GUJYO`。
       这与 procedures.matching_choices 的口径本来就一致（离场接 transition 的终点），只是学习这一环漏了。

    遇到航路名即停：`Y14 HWE …` 这类以航路名开头的行，其首个航点在中途/远端，
    当离场点会让 A* 退化成「DCT 直飞 200nm 到中途点」的坏串。"""
    onnet = graph.inset if inbound else graph.outset      # 进场看入边 / 离场看出边
    for tok in toks or ():
        if tok in skip:                            # 官方进场径路上的点（门之后）→ 不是 enroute 交接点，继续往外扫
            continue
        if any(c.isdigit() for c in tok):          # 航路名 → 已越过入口
            return None
        key = graph.by_ident.get(tok)
        if key and key in onnet:                   # 落在航路网上（该方向有边）→ 就是它
            return key
    return None                                    # 一路都是终端区点 / 图外点


def _learn_routes(graph, aip_data):
    """扫 routes.csv 全部官方航路，一次学出四样（同一遍解析）：
      legal     {(u,v):{airway}}          —— 实飞证实的合法正向（修 earth_awy 共挂 N 段丢失的单向信息）
      seg_pop   {(u,v):机场对数}           —— 走廊热度（该有向航段被多少不同机场对实飞）
      dep_heads {dep_icao:{head_key:次数}} —— 各机场真实离场过渡点（航路【首个】图航点，如 RJGG 北向用 KCC、东向用 BOGON）
      arr_tails {arr_icao:{tail_key:次数}} —— 各机场真实进场过渡点（航路【末个】图航点）
    解析 `FIX 航路 FIX …`（斜杠并联 `Y14/Y122/V30` = 该段可走其一）→ 对每个 `prev --W--> next` 用
    _trace_airway 展开实际中间航段。官方发布航路永不逆向用航路，故这是单向方向 / 真实端点的可靠真值来源。
    端点学习是「AIP 桥接 × 走廊」融合的关键：离场/进场过渡点不再靠 CIFP 猜，而是直接取官方航路两端的真实接入点。"""
    legal = {}
    seg_pairs = {}                                # (u,v) -> {(dep,arr)} —— 实飞过该有向航段的机场对集合（去重热度）
    traced = {}                                   # (prev,next,W) -> seg 缓存，避免重复 Dijkstra
    dep_heads, arr_tails = {}, {}
    for row in aip_data:
        if len(row) < 6:
            continue
        dep, arr, rs = row[0].strip().upper(), row[1].strip().upper(), row[5].strip()
        if not rs:
            continue
        pair = (dep, arr)
        toks = rs.split()
        prev, pend = None, None                   # prev=上一航点 key；pend=其后到下一航点之间的并联 airway 名
        for tok in toks:
            key = graph.by_ident.get(tok)
            if key:                               # 航点
                if prev and pend:
                    for W in pend:
                        tk = (prev, key, W)
                        if tk not in traced:
                            traced[tk] = _trace_airway(graph, prev, key, W)
                        seg = traced[tk]
                        if seg:
                            for u, v in zip(seg, seg[1:]):
                                legal.setdefault((u, v), set()).add(W)
                                seg_pairs.setdefault((u, v), set()).add(pair)
                prev, pend = key, None
            else:                                 # airway 名（斜杠并联拆开，仅留含数字的航路名，跳过噪声）
                grp = [a for a in tok.split("/") if any(c.isdigit() for c in a)]
                if grp:
                    pend = grp
        # 真实进/离场过渡点 = 航路串两端【第一个真正落在 enroute 航路网上】的点（按出现次数计权）。
        head = _enroute_entry(graph, toks)                                    # 离场端：要有出边
        tail = _enroute_entry(graph, list(reversed(toks)), inbound=True,     # 进场端：要有【入】边（门常是航路终端）
                              skip=_arr_dct_only(arr))                        #        且跳过「门后的官方进场径路」
        if head is not None:
            dep_heads.setdefault(dep, {})[head] = dep_heads.setdefault(dep, {}).get(head, 0) + 1
        if tail is not None:
            arr_tails.setdefault(arr, {})[tail] = arr_tails.setdefault(arr, {}).get(tail, 0) + 1
    return legal, {k: len(v) for k, v in seg_pairs.items()}, dep_heads, arr_tails


def _ensure_directions(graph, aip_data):
    """懒加载并缓存 graph.legal_seg。无 aip_data / 学习失败 → 空 dict（退回保守标名，不抛）。线程安全。"""
    if graph.legal_seg is not None:
        return graph.legal_seg
    with _LEGAL_LOCK:
        if graph.legal_seg is not None:
            return graph.legal_seg
        seg = {}
        try:
            if aip_data:
                seg, graph.seg_pop, graph.dep_heads, graph.arr_tails = _learn_routes(graph, aip_data)
                print(f"🧭 已从 AIP 航路学习：{len(seg)} 航段 / {len(graph.seg_pop or {})} 段走廊 / "
                      f"{len(graph.dep_heads or {})} 机场离场过渡点 / {len(graph.arr_tails or {})} 机场进场过渡点。")
        except Exception as e:
            print(f"⚠️ 航路方向学习失败（已忽略，退回保守标名）: {e}")
            seg = {}
        graph.legal_seg = seg
        return seg


# ---- 航路生成（直接 A* + 公开 API）----
# 注：旧 Rule 5「_try_aip_bridge」（借邻近机场官方 AIP 航路中段 + A* 补接）已删除。
#     端点学习落地后，基于「官方航路真实进/离场端点 + VATJPN 移管门 + 走廊」的直接 A* 即理论最优；
#     桥接只是其近似手段，且会借入【邻场】离场过渡点造成倒飞/绕远（如 RJFM→RJBE 误用鹿児島 MIDAI 头点），故整体移除。完整留底见 revisions.md。


_ARR_CORRIDOR_TOL = 1.15    # 走廊奖励：改落常用门的航路 ≤ 此倍×最短 才换
_CORRIDOR_FREQ_RATIO = 1.5  # 且常用门频次须 >此倍×当前落点频次 才换——避免两门频次相近时按全局频次误换方向
                            # （如 RJTT→RJOO 当前落 IKOMA(24) ≈ 别向的 IZUMI(26)：不换；RJKN→RJFF 当前落 HONOK(非学到,0) → 换 ISKUP(14)）


def _prefer_corridor_arrival(dep, arr, graph, best, exit_dct, run, dat_path=None):
    """把 A* 纯最短选的进场落点，换成【真实 AIP 最常用的方向合规进场门】——学到尾点(arr_tails)带频次=真实落地热度。
    仅当【该门≠当前落点】【它比当前落点(含到着尾段的实际末点)显著更常飞(>`_CORRIDOR_FREQ_RATIO`×)】【改落它的航路 ≤ `_ARR_CORRIDOR_TOL`×最短且不含大锐角】时才换；
    否则保持最短。等价于给常用走廊施奖励、给近场捷径/备选到着施相对惩罚，而不删除后者(仍是合法备选)。"""
    if not best or not best.get("fixes"):
        return best
    tails = (graph.arr_tails or {}).get(arr.code, {})
    cand = _dir_filter([k for k in tails if k in exit_dct], arr.lat_dd, arr.lon_dd, (dep.lat_dd, dep.lon_dd), graph)
    if not cand:
        return best
    top = max(cand, key=lambda k: tails[k])                    # 频次最高的方向合规进场门（南向 RJFF 即 Y25 的 ISKUP）
    tk = _arrival_tail_keys(graph, arr, best["fixes"][-1], dat_path)   # 当前航路(含到着尾段)的实际末点
    final = tk[-1] if tk else best["fixes"][-1]
    if final == top or tails[top] <= tails.get(final, 0) * _CORRIDOR_FREQ_RATIO:   # 已落常用门 / 当前落点同等常飞 → 不换
        return best
    alt = run(True, {top: exit_dct[top]})                      # 强制 A* 落到该常用门
    if alt and not alt["suspect"] and alt["dist_nm"] <= best["dist_nm"] * _ARR_CORRIDOR_TOL:
        return alt
    return best


_STAR_HANDOFF_TOL = 1.20    # 交接偏好：改落 STAR 衔接点的航路 ≤ 此倍×最短 才换（超了说明那个 STAR 不在本方向上）


def _prefer_star_handoff(dep, arr, graph, best, exit_dct, run, dat_path):
    """把 A* 纯最短选的落点，换成【真正能交接给 STAR 的点】——只要绕路在容差内。

    为什么必须有这一层：**A\\* 的代价含「落点→机场」那段直线**，于是【本场 VOR】（就在场内，代价≈0）
    结构性地压过一切真正的进场门，无论你从哪个方向来。RJNA 的 KCC 离场仅 0.8NM：从东京飞来也一头扎向
    `NINOX Y28 KCC`（南线沿海走廊），而 RJNA 的 5 条 STAR **没有一条从 KCC 起**（EXPOHN←SWING、
    EXPOHS←SHIMA、ORIBEE←MAMLA、SHINO←ADGUN）——现实是走内陆 Y20/Y88 从 SWING 进、接 EXPOHN。
    落点换成 STAR 衔接点后，enroute 正好收在 STAR 起点，交接连续（这也正是 `matching_choices` 的匹配条件，
    否则面板只能回退「列出全部 STAR」去猜——RJAH 选中 GOT1 就是这么来的）。

    是【偏好】不是【硬限】：绕路超 `_STAR_HANDOFF_TOL` 或出可疑大转弯 → 保持最短。
    很多机场压根没 STAR（只有 IAP/雷达引导），官方航路也确实收在本场 VOR（`… V18 HCE`）——那些情形本函数直接放行。
    候选来自 `_arrival_candidates`（已按航向过滤），A* 在其中自选最优。"""
    if not best or not best.get("fixes"):
        return best
    connect = _star_connect(arr.code, dat_path)
    if not connect or best["fixes"][-1][0] in connect:          # 没 STAR / 已收在衔接点上 → 不动
        return best
    exits = {k: d for k, d in exit_dct.items() if k[0] in connect}
    if not exits:
        return best
    alt = run(True, exits)                                      # 强制落到（任一）STAR 衔接点，A* 自选
    if alt and not alt["suspect"] and alt["dist_nm"] <= best["dist_nm"] * _STAR_HANDOFF_TOL:
        return alt
    return best


_DCT_MAX_NM = 250.0         # 直飞降级上限：大圆超此距离就不考虑降级（长途本就该走航路网）
_DCT_GIVEUP_RATIO = 1.5     # 航路网结果 > 此倍×大圆 → 视为「航路网帮了倒忙」


def _maybe_dct(dep, arr, graph, dat_path, exit_dct, best):
    """短程直飞降级：**航路网反而绕远时，改给一条直飞（DCT）航路**——并明确标注是降级，请用户自行核对。

    为什么需要：短程航段上端点候选会退化——离场门把你往【反方向】拽，进场门却贴在【出发机场】旁边，
    A* 只能「先飞出去、再飞回来」。实测：
      · RJDU→RJDO 大圆 **45 NM** → 航路网 174 NM（+284%）：唯一的离场候选 KAZSA 离目的地 68 NM（比整段航程还远），
        而进场门 OLE 就在出发机场旁边 1.6 NM 处。
      · RJSR→RJCB 大圆 199 NM → 301 NM：两端都只剩本场 VOR，ODE 在网上唯一的出边指向反方向的 UWE，
        到了 UWE 只能 **174° 掉头**飞回来。
    这不是 A* 算错，是**这种航段本就不该配门**。官方 AIP 表对短途航段的写法正是【直飞】——
    61 条纯 DCT 行全是这类（`RJFE→RJFF 「IKE」`、`RJKA→RJKI 「AME」`、`RJCC→RJCH 「HWE」`，一两个点甚至不给点）。

    触发：大圆 ≤ `_DCT_MAX_NM` 且（航路网 > `_DCT_GIVEUP_RATIO`×大圆 **或** 含大角度掉头）。
    落点：优先取能交接 STAR 的进场点，其次按「出发地→该点→目的地」总距最短——与官方单点写法一致。
    直飞并不更短时保持原航路（不为降级而降级）。"""
    gc = haversine_nm(dep.lat_dd, dep.lon_dd, arr.lat_dd, arr.lon_dd)
    if gc > _DCT_MAX_NM or not exit_dct:
        return best
    if best and not best["suspect"] and best["dist_nm"] <= gc * _DCT_GIVEUP_RATIO:
        return best

    def _thru(k):                                       # 出发地 → 该点 → 目的地 的总距
        la, lo = graph.nodes[k][0], graph.nodes[k][1]
        return (haversine_nm(dep.lat_dd, dep.lon_dd, la, lo)
                + haversine_nm(la, lo, arr.lat_dd, arr.lon_dd))

    connect = _star_connect(arr.code, dat_path)
    k = min(exit_dct, key=lambda x: (x[0] not in connect, _thru(x)))
    d = _thru(k)
    if best and d >= best["dist_nm"]:                   # 直飞并不更好 → 不动
        return best

    why = ("走航路网要绕到 %.0f NM（+%.0f%%）" % (best["dist_nm"], 100 * (best["dist_nm"] / gc - 1))
           + ("、且含大角度掉头" if best["suspect"] else "")) if best else "航路网上找不到合理航路"
    return {
        "route_str": k[0], "fixes": [k], "dist_nm": d, "source": "direct", "suspect": False,
        "warn": ("该航段大圆仅 %.0f NM，%s → 已【降级为直飞(DCT)】，未走航路网。"
                 "短途航段官方 AIP 本就多用直飞写法，但请自行核对。" % (gc, why)),
        "coords": [(dep.code, dep.lat_dd, dep.lon_dd),
                   (k[0], graph.nodes[k][0], graph.nodes[k][1]),
                   (arr.code, arr.lat_dd, arr.lon_dd)],
        "_names": [],
    }


def _direct_route(dep, arr, graph, dat_path):
    """case 1–4：SID/STAR/VOR/IAF/官方门 端点间直接 A*。
    离场门质量门控：用官方离场头算出的航路若含大锐角(suspect) 或 >_GATE_GIVEUP_RATIO×大圆 → 弃门重算
    （退回 SID/本场 VOR/几何，即允许「本场台离场 + VOR 程序」），取更优者。
    进场走廊奖励：A* 纯最短的落点换成真实 AIP 最常用的方向合规进场门（容差内）。
    进场交接偏好：再把落点换成能交接给 STAR 的点（容差内）——否则本场 VOR 会因代价≈0 恒胜。无解 → None。"""
    arr_cands = _arrival_candidates(arr, graph, dat_path, toward=(dep.lat_dd, dep.lon_dd))
    if not arr_cands:
        return None
    exit_dct = {k: d for k, d in arr_cands}

    def _run(use_dep_gate, exits=exit_dct):
        dep_cands = _departure_candidates(dep, graph, dat_path, toward=(arr.lat_dd, arr.lon_dd), use_gates=use_dep_gate)
        if not dep_cands:
            return None
        res = _astar(graph, {k: d for k, d in dep_cands}, exits, arr.lat_dd, arr.lon_dd)
        if not res:
            return None
        sp, sn = _smooth_airway_continuity(graph, res[0], res[1])   # 收编「离开又接回同一/平行航路」的绕行
        return _finish(graph, dep, arr, sp, sn, source="generated")

    best = _run(True)
    if best:
        gc = haversine_nm(dep.lat_dd, dep.lon_dd, arr.lat_dd, arr.lon_dd)
        if best["suspect"] or best["dist_nm"] > gc * _GATE_GIVEUP_RATIO:    # 离场门导致坏航路 → 弃门重算
            alt = _run(False)
            if alt and ((best["suspect"] and not alt["suspect"]) or alt["dist_nm"] < best["dist_nm"]):
                best = alt
    best = _prefer_corridor_arrival(dep, arr, graph, best, exit_dct, _run, dat_path)  # 进场走廊奖励（常用门 ≤1.15×最短则优先）
    best = _prefer_star_handoff(dep, arr, graph, best, exit_dct, _run, dat_path)      # 进场交接偏好（收在 STAR 起点，≤1.20×最短）
    best = _append_arrival_tail(graph, dep, arr, best, dat_path)   # 补 VATJPN 到着尾段（进场门后 DCT 直飞点），方便管制引导
    return _maybe_dct(dep, arr, graph, dat_path, exit_dct, best)   # 短程降级：航路网反而绕远 → 给直飞（并标注）


def _corridor_dbg(graph, res):
    """DEBUG_CORRIDOR：打印一条生成航路的走廊段构成（按 seg_pop 分干线/轻度/未飞），并列出离走廊段。"""
    sp = graph.seg_pop or {}
    fp, nm = _parse_aip_route(res["route_str"], graph)
    if not fp:
        return
    trunk = minor = off = 0
    offsegs = []
    for i in range(len(fp) - 1):
        if nm[i][0] == "DCT":
            continue
        pop = sp.get((fp[i], fp[i + 1]), 0)
        if pop >= _TRUNK_POP:
            trunk += 1
        elif pop:
            minor += 1
        else:
            off += 1
            offsegs.append(f"{fp[i][0]}-{fp[i + 1][0]}")
    print(f"   🛣️ [{res['source']}] 走廊段: 干线 {trunk} / 轻度 {minor} / 未飞 {off}"
          + (f"；离走廊: {' '.join(offsegs)}" if offsegs else ""))


def generate_route(dep_airport, arr_airport, dat_path=None, aip_data=None, airports=None):
    """无直连 AIP 航路时，用本地导航数据生成一条参考航路 = SID/STAR/VOR/IAF/官方门 端点间的直接 A*（case 1–4）。
    端点取自【官方航路学到的真实进/离场过渡点】∪【VATJPN 移管表官方门】∪【CIFP】（见 _departure_candidates/_arrival_candidates），
    enroute 走 RNAV 优先 + 走廊加权 + 方向合法 + 航路连续性平滑。基于这套真值的直接航路即理论最优——
    旧 Rule 5「借邻近机场官方 AIP 航路」桥接是其近似手段，端点学习落地后已删除（它会借入邻场离场过渡点造成倒飞/绕远）。
    返回 {route_str,fixes,dist_nm,source,suspect,warn,coords} 或 None。`airports` 形参保留（向后兼容，现未用）。异常尽量自保不外抛。"""
    graph = get_graph(dat_path)
    if not graph.adj:
        return None
    _ensure_directions(graph, aip_data)          # 懒加载：首次从 AIP 航路学习 合法正向 + 走廊热度 + 真实进离场端点
    res = _direct_route(dep_airport, arr_airport, graph, dat_path)
    if DEBUG_CORRIDOR and res:
        _corridor_dbg(graph, res)
    return res


def route_geometry(dep_airport, arr_airport, route_str, dat_path=None):
    """把航路串解析成带坐标的航点序列 [(ident, lat, lon), ...]，含起降机场首尾。
    用于算航路长度与画图；解析不到坐标的 token（airway 名）自动跳过。图不可用时只返回起降两点。"""
    pts = [(dep_airport.code, dep_airport.lat_dd, dep_airport.lon_dd)]
    graph = get_graph(dat_path)
    if route_str and graph.nodes:
        fix_path, _names = _parse_aip_route(route_str, graph)
        for key in (fix_path or []):
            lat, lon, _kind = graph.nodes[key]
            pts.append((key[0], lat, lon))
    pts.append((arr_airport.code, arr_airport.lat_dd, arr_airport.lon_dd))
    return pts


def route_length_nm(pts):
    """带坐标航点序列 [(ident,lat,lon),...] 的累计大圆长度(NM)。"""
    return sum(haversine_nm(pts[i][1], pts[i][2], pts[i + 1][1], pts[i + 1][2])
               for i in range(len(pts) - 1))
