# ================= 核心计算与比对模块 =================
# 大圆距离(Haversine, NM)、AIP 航路精确匹配，以及按已飞次数加权的随机航线抽取。

import math
import random


def calculate_distance_nm(airport1, airport2):
    # 🌟 使用 Python 原生 math 库
    R_nm = 3440.065
    lat1, lon1 = math.radians(airport1.lat_dd), math.radians(airport1.lon_dd)
    lat2, lon2 = math.radians(airport2.lat_dd), math.radians(airport2.lon_dd)
    dlat, dlon = lat2 - lat1, lon2 - lon1

    a = math.sin(dlat / 2.0)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R_nm * c


def find_aip_route(routes_data, dep_icao, arr_icao):
    return [", ".join(row) for row in routes_data if row[0].strip().upper() == dep_icao.upper() and row[1].strip().upper() == arr_icao.upper()] or None


# 真实航路长校验（问题3）：大圆做粗筛、放宽下限，对选中候选算真实航路长，超界则换一条
_PREFILTER_RELAX = 0.7     # 启用航路长校验时大圆下限放宽系数（真实航路长 ≥ 大圆，避免漏掉略短的）
_LEN_VALIDATE_CAP = 40     # 航路长校验最多尝试的候选数（每次约 1–2ms，封顶防自由规划候选过多）


def get_random_route(airport_list, min_dist, max_dist, aip_routes_data=None, strict_aip=False,
                     fixed_dep=None, fixed_dest=None, flown_counts=None, aip_index=None,
                     require_both_scenery=False, active_sims=None, route_len_fn=None,
                     relax_lower=_PREFILTER_RELAX):
    if len(airport_list) < 2: raise ValueError("可用机场太少，无法生成航线。")
    flown_counts = flown_counts or {}

    ap1_fixed = next((a for a in airport_list if a.code == fixed_dep), None) if fixed_dep else None
    ap2_fixed = next((a for a in airport_list if a.code == fixed_dest), None) if fixed_dest else None
    if fixed_dep and not ap1_fixed: raise ValueError(f"出发机场 {fixed_dep} 不在可用列表中。")
    if fixed_dest and not ap2_fixed: raise ValueError(f"目的机场 {fixed_dest} 不在可用列表中。")

    # 出发/到达候选池：固定端只保留该机场，否则全部机场
    pool_dep = [ap1_fixed] if fixed_dep else airport_list
    pool_dest = [ap2_fixed] if fixed_dest else airport_list

    # 严格 AIP 模式：用 set 索引 O(1) 过滤，避免枚举时对每对线性遍历 aip_routes_data
    if strict_aip and aip_index is None and aip_routes_data:
        aip_index = {(r[0].strip().upper(), r[1].strip().upper()) for r in aip_routes_data if len(r) >= 2}

    # 启用真实航路长校验时放宽大圆下限（真实航路长 ≥ 大圆，避免漏掉大圆略低于 min 但实长达标的航线）
    pre_min = min_dist * relax_lower if route_len_fn else min_dist

    # 枚举所有满足约束(距离区间 + 可选 AIP)的候选航线，按已飞次数加权：
    #   权重 w = 1/(count+1)**2 —— 未飞(count=0)权重 1.0，飞得越多权重越低但恒 >0(软优先)
    # 同时按「军用端数量」分层(0=两端民用 / 1=一端军用 / 2=两端军用)，供「优先民用」选择。
    tiers = {0: ([], []), 1: ([], []), 2: ([], [])}
    for ap1 in pool_dep:
        for ap2 in pool_dest:
            if ap1.code == ap2.code: continue
            dist = calculate_distance_nm(ap1, ap2)
            if not (pre_min <= dist <= max_dist): continue
            # 需求 B：仅在两端都已安装地景的机场间抽线（按所选模拟器；未检测 None 时恒 True，过滤自然失效）
            if require_both_scenery and not (ap1.has_scenery_for(active_sims) and ap2.has_scenery_for(active_sims)): continue
            if strict_aip and (ap1.code, ap2.code) not in (aip_index or set()): continue
            count = flown_counts.get((ap1.code, ap2.code), 0)
            w = 1.0 / (count + 1) ** 2
            mil = (1 if ap1.is_military else 0) + (1 if ap2.is_military else 0)
            tiers[mil][0].append((ap1, ap2, dist, count))
            tiers[mil][1].append(w)

    # 优先民用：从「军用端最少」的非空层里按已飞次数加权抽线，避免随到可能没有民航助航
    #   设施/进近、未必可飞的军用机场（哪怕它未飞、权重更高）。
    #   普通情况都落在第 0 层(两端民用)；约束太严或用户固定了军用端时，才退到第 1 层
    #   (另一端仍优先民用)、再退到第 2 层(两端军用)。
    candidates, weights = [], []
    for mil in (0, 1, 2):
        if tiers[mil][0]:
            candidates, weights = tiers[mil]
            break
    if not candidates:
        raise RuntimeError("未能找到与要求匹配的航线。")

    def _finish(cand):
        ap1, ap2, dist, count = cand
        route = find_aip_route(aip_routes_data, ap1.code, ap2.code) if aip_routes_data else None
        return ap1, ap2, dist, route, count

    # 不校验航路长：按已飞次数加权单抽（flown_counts 为空时权重相等 = 均匀随机，与原行为一致）
    if route_len_fn is None:
        return _finish(random.choices(candidates, weights=weights, k=1)[0])

    # 校验真实航路长(问题3)：加权无放回抽样，取首个真实航路长 ∈ [min,max] 的候选；
    #   都不达标则取最接近区间的那条（best-effort，不让规划失败）。
    w = list(weights)
    best = None                                       # (gap, cand, real_len)
    for _ in range(min(len(candidates), _LEN_VALIDATE_CAP)):
        if sum(w) <= 0: break
        i = random.choices(range(len(candidates)), weights=w, k=1)[0]
        w[i] = 0.0
        real_len = route_len_fn(candidates[i][0], candidates[i][1])
        if real_len is None: continue                 # 连不通/无航路 → 跳过
        if min_dist <= real_len <= max_dist:
            return _finish(candidates[i])
        gap = (min_dist - real_len) if real_len < min_dist else (real_len - max_dist)
        if best is None or gap < best[0]:
            best = (gap, candidates[i], real_len)
    if best is not None:
        print("ℹ️ 未找到航路长正好在 %.0f–%.0f NM 的航线，已选最接近的（实长约 %.0f NM）。"
              % (min_dist, max_dist, best[2]))
        return _finish(best[1])
    return _finish(random.choices(candidates, weights=weights, k=1)[0])
