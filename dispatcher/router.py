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
_MAX_ENTRY_NM = 120.0           # 机场到最近航路点的最大直飞接入距离
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
    共挂的段上会统一标 N(丢了单向信息)，故下游标名时不能把它当双向用(见 _pick_airway)。整文件扫(不限 bbox)。"""
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
    """机场附近 max_nm 内的航路点中挑 k 个接入/接出候选。require_outbound→只取有出边的点（入航用）；
    require_inbound→只取有进边的点（出航用，须可达；剔除孤立的进近 VOR 等）。
    toward=(lat,lon) 给定时，按「接入距离 + 该点到 toward 的大圆」排序——优先朝目标方向的接入点，
    避免选到「机场背后」的点导致航路一开头就大角度掉头。返回 [(key, 直飞距离NM), ...]。"""
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
    返回 (path_keys, names_seq)：path 为航路点序列，names_seq[i] 为 path[i]→path[i+1] 所用 airway 名列表。无解返回 None。"""
    counter = 0
    openh = []
    g = {}
    came = {}                              # key -> (前驱key, 该段 airway 名列表) ; 入航点为 None
    for key, g0 in starts.items():
        g[key] = g0
        nlat, nlon, _ = graph.nodes[key]
        heapq.heappush(openh, (g0 + haversine_nm(nlat, nlon, glat, glon), counter, key))
        counter += 1
        came[key] = None
    while openh:
        _f, _c, key = heapq.heappop(openh)
        if key == _ARR:
            # 回溯：_ARR 的前驱是出航点（该段为直飞、无 airway 名）
            exit_key = came[_ARR][0]
            path, names_seq = [], []
            k = exit_key
            while k is not None:
                path.append(k)
                link = came.get(k)
                if link is None:
                    break
                prev, names = link
                names_seq.append(names)
                k = prev
            path.reverse()
            names_seq.reverse()
            return path, names_seq
        cur_g = g[key]
        sp = graph.seg_pop
        # 沿航路扩展（搜索权重乘子恒 ≥1 → 启发可采纳；真实航程仍由 _finish 按 haversine 重算）：
        #   ①「优先 RNAV」整段无 RNAV 名的纯传统边 ×_TRAD_AIRWAY_PENALTY；
        #   ②「高频走廊」越少被官方实飞的有向段越加罚（干线不罚）→ 软偏好真实常用走廊（桥接补接段同样受益）。
        for (nb, cost, names) in graph.adj.get(key, ()):
            f = 1.0 if any(_is_rnav(n) for n in names) else _TRAD_AIRWAY_PENALTY
            if sp:
                pop = sp.get((key, nb), 0)
                f *= 1.0 if pop >= _TRUNK_POP else (_MINOR_CORRIDOR_PENALTY if pop else _OFFTRUNK_PENALTY)
            ng = cur_g + cost * f
            if nb not in g or ng < g[nb]:
                g[nb] = ng
                came[nb] = (key, names)
                nlat, nlon, _ = graph.nodes[nb]
                heapq.heappush(openh, (ng + haversine_nm(nlat, nlon, glat, glon), counter, nb))
                counter += 1
        # 若为出航候选，提供到达机场的虚拟直飞边
        if key in exit_dct:
            ng = cur_g + exit_dct[key]
            if _ARR not in g or ng < g[_ARR]:
                g[_ARR] = ng
                came[_ARR] = (key, None)
                heapq.heappush(openh, (ng, counter, _ARR))   # h(_ARR)=0
                counter += 1
    return None


# ---- 航路串格式化 + 连贯性检查 ----

def _pick_airway(names, prev, oneway, legal=()):
    """本段(u→v)多名里选一个 airway 名。
    安全集 = 完全双向(不在 oneway) ∪ 本段已学合法正向(在 legal=该 (u,v) 由 AIP 实飞学到的航路集)——
    这些在 u→v 方向确定合法；单向且(本段无 AIP 证据 / 学到的是反向)的航路不安全、不用于改名（避免逆向串）。
    安全集只增不减，故只会恢复合法的单向 RNAV 标名、绝不重新引入反向。
    优先级：①延续上一段(若仍安全，或安全集无 RNAV) → ②安全集首个 RNAV → ③安全集首名 → ④names[0] 兜底。"""
    safe = [n for n in names if n not in oneway or n in legal]
    rnav = [n for n in safe if _is_rnav(n)]
    if prev and prev in safe and (_is_rnav(prev) or not rnav):
        return prev
    if rnav:
        return rnav[0]
    if safe:
        return safe[0]
    return names[0]


def _format_route(path, names_seq, oneway=frozenset(), legal_seg=None):
    """把 path + 每段 airway 名格式化为 AIP 风格串：仅在 airway 变化处保留换路点。
    每段选一个名——优先【方向合法的】RNAV 航路、其次延续上一段已选名（见 _pick_airway）。
    `oneway`=单向航路名集合；`legal_seg`={(u,v):{航路}}=AIP 实飞学到的合法正向，用来在该方向安全地保留单向 RNAV。"""
    if not path:
        return ""
    legal_seg = legal_seg or {}
    chosen = []
    prev = None
    for i, names in enumerate(names_seq):
        legal = legal_seg.get((path[i], path[i + 1]), ())          # 本段 (u→v) 已学合法正向航路集
        c = _pick_airway(names, prev, oneway, legal)
        chosen.append(c)
        prev = c
    tokens = [path[0][0]]
    for i, awy in enumerate(chosen):
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
        return True, f"在 {worst_at} 附近有约 {worst:.0f}° 大角度转弯"
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


_GATE_DIR_MARGIN_NM = 30.0      # 官方门方向过滤余量：到对端的距离 ≤「机场到对端」+ 此值才保留（丢掉另一侧/反向、会逼绕道的门）
_GATE_GIVEUP_RATIO = 1.4        # 离场门质量门控：用官方离场头算出的航路若 >此倍×大圆（或含大锐角）→ 弃门重算（退本场 VOR/SID/几何）


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


def _arrival_candidates(arr, graph, dat_path, toward):
    """进场端候选 [(key, 该点→机场直飞NM)]：把【官方航路学到的真实进场过渡点】∪【VATJPN 移管表官方进场门】∪【CIFP STAR 入口】
    并集（enroute 级进场点，须有进边），按航向过滤后交给 A* 自选最优；都空时再回退【本场 VOR】→【IAF/IF】→几何。
    学到尾点是融合核心（直接取官方航路真实落地接入点）；用并集保证不因它不全/反向而漏选，又保留「enroute 门优先于进近 VOR/IAF」。"""
    _sd, star, iaf, vor = _cifp_endpoints(arr.code, dat_path)
    learned = [k for k in _learned_tails(graph, arr.code) if k in graph.inset]
    gates = [k for k in (graph.by_ident.get(g) for g in _transfer_points().get(arr.code, {}).get("arr", []))
             if k and k in graph.inset]
    enroute = list(dict.fromkeys(learned + gates + [k for k in star if k in graph.inset]))
    prim = _dir_filter(enroute, arr.lat_dd, arr.lon_dd, toward, graph)
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
            "source": source, "suspect": suspect, "warn": warn, "coords": geo}


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
    用于平滑时拒绝把路收编到反向的单向 RNAV 航路（与 _pick_airway 的「安全集」同口径）。"""
    legal_seg = graph.legal_seg or {}
    for u, v in zip(seg, seg[1:]):
        if awy in graph.oneway and awy not in legal_seg.get((u, v), ()):
            return False
    return True


def _smooth_airway_continuity(graph, path, names_seq):
    """A* 后处理「航路连续性」平滑：把能由【单一 RNAV 航路】近乎等长直达的子段收编成单航路。
    消除走廊加权/A* 偶发的「离开一条航路 → 走平行航路 → 下游又接回同一航路」这类不自然走法
    （典型：明明 Y102 直连 HPE→METEL→SDE，却被收编成 HPE→Y10→VINAR→Y125→METEL→Y102→SDE）。
    判据：子段 path[i..j] 存在单一 RNAV 航路 W 直达，W 实际几何长 ≤ 当前子段 ×_SMOOTH_RATIO，且 W 在该方向合法。
    贪心从 i 起优先收编更远的 j（一次抹掉更多绕行）；纯几何 + 方向合法，不引入反向单向航路。
    返回平滑后的 (path, names_seq)。"""
    if not path or len(path) < 3:
        return path, names_seq
    out_path, out_names = [path[0]], []
    n = len(path)
    i = 0
    while i < n - 1:
        found = None
        for j in range(n - 1, i + 1, -1):                       # j≥i+2：至少跨过一个中间点才有「收编」意义
            sub_len = _path_length_nm(graph, path[i:j + 1])
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
                if best_awy is None or slen < best_len:         # 多条 RNAV 候选取更短
                    best_awy, best_seg, best_len = awy, seg, slen
            if best_awy:
                found = (j, best_seg, best_awy)
                break
        if found:
            j, seg, awy = found
            for k in seg[1:]:
                out_path.append(k)
                out_names.append([awy])
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
        # 真实进/离场过渡点：仅当航路串【首/末 token 本身就是航点】才记（按出现次数计权）——
        # 以航路名开头/结尾的行（如 'Y14 HWE …'、'… Y106/Y124/V22'）其首/末个【解析航点】常在中途/远端，
        # 若当离/进场点会让 A* 退化成「DCT 直飞 200nm 到中途点」的坏串，故跳过。
        head = graph.by_ident.get(toks[0]) if toks else None
        tail = graph.by_ident.get(toks[-1]) if toks else None
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


def _direct_route(dep, arr, graph, dat_path):
    """case 1–4：SID/STAR/VOR/IAF/官方门 端点间直接 A*。
    离场门质量门控：用官方离场头算出的航路若含大锐角(suspect) 或 >_GATE_GIVEUP_RATIO×大圆 → 弃门重算
    （退回 SID/本场 VOR/几何，即允许「本场台离场 + VOR 程序」），取更优者。无解 → None。"""
    arr_cands = _arrival_candidates(arr, graph, dat_path, toward=(dep.lat_dd, dep.lon_dd))
    if not arr_cands:
        return None
    exit_dct = {k: d for k, d in arr_cands}

    def _run(use_dep_gate):
        dep_cands = _departure_candidates(dep, graph, dat_path, toward=(arr.lat_dd, arr.lon_dd), use_gates=use_dep_gate)
        if not dep_cands:
            return None
        res = _astar(graph, {k: d for k, d in dep_cands}, exit_dct, arr.lat_dd, arr.lon_dd)
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
    return best


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
