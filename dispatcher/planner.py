# ================= 航线规划结果计算（计算 / 渲染解耦）=================
# 把「一次航线规划的计算」从渲染中剥离：build_flight_plan() 只算数据（含 FlightAware 抓取、
# 无排班时的模拟呼号），返回结构化 FlightPlan；GUI(gui.py) 据此渲染。
# 这样将来换 GUI 框架时只需重写渲染层，计算逻辑（连同网络抓取）完全复用、不动。纯标准库。

import re
import random
from dataclasses import dataclass, field
from urllib.parse import urlencode

from .flightaware import fetch_real_flights_with_filter
from .airlines import pick_sim_airline
from .aircraft import find_aircraft_id


@dataclass
class FlightPlan:
    """一次规划的结构化结果（渲染无关）。dep/arr 为 Airport，渲染层用其 scenery_label()/is_military/has_scenery。"""
    dep: object                                          # 出发 Airport
    arr: object                                          # 到达 Airport
    dist_nm: float                                       # 大圆距离(NM)
    flown_count: int = 0                                 # Volanta 已飞次数(>0 时提示)
    aip_routes: list = None                              # AIP 航路行列表(或 None)
    real_flights: list = field(default_factory=list)     # FlightAware 排班字符串(最多 5 条)
    is_exact: bool = False                               # True=完美匹配 / False=仅参考排班
    sim_callsign: str = None                             # 无排班时降级生成的模拟呼号(否则 None)
    url: str = ""                                        # FlightAware 完整排班表链接
    generated_route: str = None                          # 无 AIP 时本地 A* 生成的航路串(否则 None)
    generated_route_warn: str = None                     # 生成航路若有大锐角转弯的提示(否则 None)
    simbrief_url: str = ""                               # SimBrief 一键派遣预填链接(F16)
    generated_route_dist: float = None                   # 生成航路总长(NM)，用于较大圆偏差显示
    aip_route_dists: list = None                         # 各 AIP 航路长(NM)，与 aip_routes 顺序对应
    aip_maps: list = None                                # 每条 AIP 航路的 (coords, 标题)，与 aip_routes 一一对应（供分别开图）
    gen_map: object = None                               # 生成航路的 (coords, 标题)


# ---- SimBrief 一键派遣链接（F16）----

def _normalize_actype(s):
    """用户机型输入 → SimBrief 用的 aircraft_id（查 aircrafts.json 机型库）；查不到则大写透传（SimBrief 容错）。"""
    s = (s or "").strip()
    if not s:
        return ""
    return find_aircraft_id(s) or s.upper()


def _split_callsign(s):
    """'ANA123' / 'ANA0123' → ('ANA','123')；取不出→('','')。供 SimBrief 的 airline/fltnum 参数用。"""
    m = re.match(r'\s*([A-Za-z]{2,3})\s*0*(\d{1,4})', s or "")
    return (m.group(1).upper(), m.group(2)) if m else ("", "")


def _build_simbrief_url(orig, dest, airline, fltnum, actype, route=None):
    """拼 SimBrief 一键派遣预填链接（custom options，公开预填、用用户自己浏览器登录态，无需任何凭据）。
    必填 orig/dest/type；airline/fltnum 可选；route 为空则 SimBrief 用自己数据库的推荐航路。"""
    params = {"orig": orig, "dest": dest}
    if actype:
        params["type"] = actype
    if airline:
        params["airline"] = airline
    if fltnum:
        params["fltnum"] = fltnum
    if route:
        params["route"] = route                          # F16 分时段：填入按起飞时间选出的官方航路，补 SimBrief 时段盲区
    return "https://dispatch.simbrief.com/options/custom?" + urlencode(params)


def build_flight_plan(dep_obj, arr_obj, route_dist, route_details,
                      user_airline="", user_aircraft="", user_time_range="", flown_count=0,
                      generated_route=None, generated_route_warn=None, timed_route=None,
                      generated_route_dist=None, aip_route_dists=None,
                      aip_maps=None, gen_map=None):
    """计算一次规划结果（含 FlightAware 抓取、无排班时降级生成模拟呼号）。无任何打印/UI 依赖。
    模拟呼号的随机数字在此【算一次】，保证显示一致。timed_route(F16)：分时段选出的官方航路串，
    非空则填进 SimBrief 链接的 route 参数（否则留空让 SimBrief 自己算）。"""
    is_exact, real_flights = fetch_real_flights_with_filter(
        dep_obj.code, arr_obj.code, user_airline, user_aircraft, user_time_range)
    url = f"https://flightaware.com/live/findflight?origin={dep_obj.code}&destination={arr_obj.code}"
    sim_callsign = None
    if not real_flights:
        # 无真实排班时降级：按航线两端所在大区挑一个合理航司（用户指定则优先用户的）
        code = user_airline or pick_sim_airline(dep_obj.code, arr_obj.code)
        sim_callsign = f"{code}{random.randint(11, 899)}"
    # F16：SimBrief 一键派遣链接——航司/航班号取真实排班首条或模拟呼号，机型规范化为 ICAO
    cs = real_flights[0].split()[0] if real_flights else (sim_callsign or "")
    sb_airline, sb_fltnum = _split_callsign(cs)
    if not sb_airline and user_airline:
        sb_airline = user_airline.upper()
    simbrief_url = _build_simbrief_url(dep_obj.code, arr_obj.code, sb_airline, sb_fltnum,
                                       _normalize_actype(user_aircraft), timed_route)
    return FlightPlan(
        dep=dep_obj, arr=arr_obj, dist_nm=route_dist, flown_count=flown_count or 0,
        aip_routes=route_details, real_flights=real_flights, is_exact=is_exact,
        sim_callsign=sim_callsign, url=url,
        generated_route=generated_route, generated_route_warn=generated_route_warn,
        simbrief_url=simbrief_url,
        generated_route_dist=generated_route_dist, aip_route_dists=aip_route_dists,
        aip_maps=aip_maps, gen_map=gen_map,
    )


# ---- 共享输入解析（GUI 使用；与 CLI app.py 的内联解析口径一致，避免漂移）----

def parse_runway_ft(text, default=5900.0):
    """解析跑道长度输入：'1800m' → 英尺；'5900ft' → 英尺；空/无单位/非法 → default(=5900ft)。
    与 app.py 内联解析一致：仅识别 m / ft 后缀，裸数字按缺省处理。"""
    s = (text or "").strip().lower().replace(" ", "")
    if not s:
        return default
    try:
        if s.endswith("m"):
            return float(s[:-1]) * 3.28084
        if s.endswith("ft"):
            return float(s[:-2])
    except Exception:
        pass
    return default


def parse_dist(min_str, max_str, default_min=200.0, default_max=450.0):
    """解析航程区间：空 → 默认(200/450)；非法 → 默认；min>max 自动交换。返回 (min, max)。"""
    def _f(v, d):
        try:
            return float(v) if str(v).strip() else d
        except Exception:
            return d
    lo, hi = _f(min_str, default_min), _f(max_str, default_max)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi
