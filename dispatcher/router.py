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
_BRIDGE_TOLERANCE = 1.25        # Rule5：借的 AIP 桥接航路总程 ≤ 最优 A* 的此倍数才采用（官方航路可略长，不该长太多；待测后再调）
_OVERSHOOT_NM = 20.0           # Rule5：桥接航路若中途比末端更近目的地(过此值)→「冲过头」，不借
_BRIDGE_HEAD_NM = 50.0         # Rule5 双端：dep 到所借 AIP 航路头点的最大 DCT 直连距离（离场点离 AIP 起点够近才连，待测后再调）
_TRAD_AIRWAY_PENALTY = 1.15     # 「优先 RNAV」：A* 搜索时纯传统航路(整段无 RNAV 名)的边权乘此系数，软优先 RNAV(Y/Z 等)；
                                #   仅作用于选路、不进显示距离(后者由 haversine 重算)；RNAV 太绕(>15%)时仍回退传统。待测后再调。
_RNAV_PREFIXES = frozenset("QTYZLMNP")   # ICAO Annex 11 航路命名：国内 RNAV=Q/T/Y/Z、区域 RNAV=L/M/N/P（其余 H/J/V/W,A/B/G/R=传统）

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
                print("🧭 首次生成航路：正在构建本地航路图（解析导航数据，稍候）…")
                _parse_fixes(fixp, nodes)
                if navp:
                    _parse_navaids(navp, nodes)
                edges = _parse_airways(awyp, nodes, adj, oneway)
                print(f"🧭 航路图就绪：{len(nodes)} 个航路点 / {edges} 条航段（日本范围）。")
        except Exception as e:
            print(f"⚠️ 航路图构建失败（已忽略，本地航路生成将不可用）: {e}")
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
        # 沿航路扩展（「优先 RNAV」：整段无 RNAV 名的纯传统边按 _TRAD_AIRWAY_PENALTY 加罚——
        #   只抬高搜索权重以软优先 RNAV，真实航程仍由 _finish 按 haversine 重算，启发 h≤真权、可采纳。）
        for (nb, cost, names) in graph.adj.get(key, ()):
            ng = cur_g + (cost if any(_is_rnav(n) for n in names) else cost * _TRAD_AIRWAY_PENALTY)
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

def _departure_candidates(dep, graph, dat_path, toward):
    """离场端候选 [(key, 机场→该点直飞NM)]：SID 出口 ∪ 本场 VOR（须有出边）→ 几何兜底（朝 toward）。"""
    sid, _st, _iaf, vor = _cifp_endpoints(dep.code, dat_path)
    ek = [k for k in (sid | vor) if k in graph.outset]
    cands = [(k, haversine_nm(dep.lat_dd, dep.lon_dd, graph.nodes[k][0], graph.nodes[k][1])) for k in ek]
    if not cands:
        cands = _nearest_nodes(dep.lat_dd, dep.lon_dd, graph, require_outbound=True, toward=toward)
    return cands


def _arrival_candidates(arr, graph, dat_path, toward):
    """进场端候选 [(key, 该点→机场直飞NM)]：STAR 入口 → 本场 VOR → IAF/IF（须有进边）→ 几何兜底。"""
    _sd, star, iaf, vor = _cifp_endpoints(arr.code, dat_path)
    xk = ([k for k in star if k in graph.inset]
          or [k for k in vor if k in graph.inset]
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


def _learn_airway_directions(graph, aip_data):
    """扫 routes.csv 全部官方航路，按【航段】学习每条航路被实飞证实的合法正向。
    解析 `FIX 航路 FIX …`（斜杠并联 `Y14/Y122/V30` = 该段可走其一）→ 对每个 `prev --W--> next`
    用 _trace_airway 展开出实际经过的中间航段 → 记 legal[(u,v)] ∋ W。官方发布航路永不逆向用航路，
    故这是单向方向的可靠真值来源；earth_awy 把「单向 RNAV + 双向航路共挂」段统一标 N 丢掉的信息，由此补回。
    返回 {(u_key,v_key): {airway名}}。"""
    legal = {}
    traced = {}                                   # (prev,next,W) -> seg 缓存，避免重复 Dijkstra
    for row in aip_data:
        if len(row) < 6:
            continue
        rs = row[5].strip()
        if not rs:
            continue
        prev, pend = None, None                   # prev=上一航点 key；pend=其后到下一航点之间的并联 airway 名
        for tok in rs.split():
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
                prev, pend = key, None
            else:                                 # airway 名（斜杠并联拆开，仅留含数字的航路名，跳过噪声）
                grp = [a for a in tok.split("/") if any(c.isdigit() for c in a)]
                if grp:
                    pend = grp
    return legal


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
                seg = _learn_airway_directions(graph, aip_data)
                print(f"🧭 已从 AIP 航路学习航路方向：{len(seg)} 个合法正向航段。")
        except Exception as e:
            print(f"⚠️ 航路方向学习失败（已忽略，退回保守标名）: {e}")
            seg = {}
        graph.legal_seg = seg
        return seg


# ---- 公开 API ----

def _try_aip_bridge(dep, arr, graph, aip_data, airports, dat_path, max_near_nm=100.0):
    """Rule 5：借「D' → A'」的官方 AIP 航路中段 + A* 补接 arr 端，D'/A' 为 dep/arr 自己或其附近机场。
      - **单端**(D'==dep)：dep 精确，借 dep→A' 的官方 AIP（A' 是 arr 的 ≤max_near_nm 替身）。
      - **双端**(D'≠dep，dep 附近 ≤max_near_nm)：借 D'→A' 的官方 AIP 中段，dep DCT 接到其头点——
        **仅当该 AIP 航路头点离 dep ≤_BRIDGE_HEAD_NM**（离场点离 AIP 起点够近，直连才合理）。
        典型：RJFR→RJEC 借 RJFF→RJCC 的西线（头点 BUTUR 离 RJFR 仅 30nm）。
    三道闸全过才采用：① 干净(无大锐角 suspect) ② 不冲过头(overshoot) ③（调用方）≤_BRIDGE_TOLERANCE×最优。
    只返回最优(总航程最短)的【干净】桥接，否则 None（调用方退回纯 A* 最优方案）。"""
    bycode = {a.code: a for a in airports}
    arr_cands = _arrival_candidates(arr, graph, dat_path, toward=(dep.lat_dd, dep.lon_dd))
    if not arr_cands:
        return None
    exit_dct = {k: d for k, d in arr_cands}
    best = None
    for row in aip_data:
        if len(row) < 6:
            continue
        dp = bycode.get(row[0].strip().upper())                        # D'（出发端机场）
        ap = bycode.get(row[1].strip().upper())                        # A'（到达端机场）
        rs = row[5].strip()
        if not dp or not ap or not rs:
            continue
        if dp.code == dep.code and ap.code == arr.code:
            continue                                                   # = Rule0 直连，不在此处理
        # arr 侧：A' 须是 arr 自己或其 ≤max_near_nm 的替身
        if haversine_nm(arr.lat_dd, arr.lon_dd, ap.lat_dd, ap.lon_dd) > max_near_nm:
            continue
        # dep 侧：D' 须是 dep 自己，或 dep 的 ≤max_near_nm 替身
        if dp.code != dep.code and haversine_nm(dep.lat_dd, dep.lon_dd, dp.lat_dd, dp.lon_dd) > max_near_nm:
            continue
        mid_path, mid_names = _parse_aip_route(rs, graph)
        if not mid_path:
            continue
        # 双端(D'≠dep)：dep 须能 DCT 直连到所借 AIP 的头点（离得够近才算合理衔接）
        if dp.code != dep.code:
            hlat, hlon, _ = graph.nodes[mid_path[0]]
            if haversine_nm(dep.lat_dd, dep.lon_dd, hlat, hlon) > _BRIDGE_HEAD_NM:
                continue
        tail = mid_path[-1]                                            # A' 的到达端(AIP 航路尾)
        segC = _astar(graph, {tail: 0.0}, exit_dct, arr.lat_dd, arr.lon_dd)
        if not segC:
            continue                                                   # A' 接不到 arr → 跳过
        full_path = mid_path + segC[0][1:]
        # 冲过头检查：航路对目的地的最近点若出现在中途(而非末端) → 「过站再折返」，不借
        dists = [haversine_nm(graph.nodes[k][0], graph.nodes[k][1], arr.lat_dd, arr.lon_dd) for k in full_path]
        if min(dists) + _OVERSHOOT_NM < dists[-1]:
            continue
        result = _finish(graph, dep, arr, full_path, mid_names + segC[1], source="aip_bridge")
        if not result or result["suspect"]:                           # 借了出现锐角弯 → 这条不借
            continue
        if best is None or result["dist_nm"] < best["dist_nm"]:
            best = result
    return best


def _direct_route(dep, arr, graph, dat_path):
    """case 1–4：在 SID/STAR/VOR/IAF 端点间直接 A* 的最优航路。无解 → None。"""
    dep_cands = _departure_candidates(dep, graph, dat_path, toward=(arr.lat_dd, arr.lon_dd))
    arr_cands = _arrival_candidates(arr, graph, dat_path, toward=(dep.lat_dd, dep.lon_dd))
    if not dep_cands or not arr_cands:
        return None
    res = _astar(graph, {k: d for k, d in dep_cands}, {k: d for k, d in arr_cands},
                 arr.lat_dd, arr.lon_dd)
    if not res:
        return None
    return _finish(graph, dep, arr, res[0], res[1], source="generated")


def generate_route(dep_airport, arr_airport, dat_path=None, aip_data=None, airports=None):
    """无直连 AIP 航路时，用本地导航数据生成一条参考航路。
    先算【最优】= SID/STAR/VOR/IAF 端点间直接 A*（case 1–4）；再试 Rule 5 桥接（借 dep→「arr 附近机场」
    的官方 AIP + 补接）——仅当桥接【干净】(无大锐角转弯) 且总航程 ≤ 最优 × _BRIDGE_TOLERANCE 时才采用，
    否则用最优。返回 {route_str,fixes,dist_nm,source,suspect,warn} 或 None。异常尽量自保不外抛。"""
    graph = get_graph(dat_path)
    if not graph.adj:
        return None
    _ensure_directions(graph, aip_data)          # 懒加载：首次从 AIP 航路学习合法正向（供标名层避免逆向单向航路）
    optimal = _direct_route(dep_airport, arr_airport, graph, dat_path)
    if aip_data and airports:
        bridged = _try_aip_bridge(dep_airport, arr_airport, graph, aip_data, airports, dat_path)
        if bridged and (optimal is None or bridged["dist_nm"] <= optimal["dist_nm"] * _BRIDGE_TOLERANCE):
            return bridged       # 官方 AIP 桥接：干净 + 不比最优明显绕远 → 首选
    return optimal


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
