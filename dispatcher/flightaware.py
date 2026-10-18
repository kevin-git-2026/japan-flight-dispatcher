# ================= 智能匹配与解析辅助 + FlightAware 现实排班爬虫 =================
# 机型模糊匹配、起飞时间区间解析，以及抓取并过滤 FlightAware findflight 页面排班。
# （与拆分前版本保持完全一致；脆弱耦合点为 FA.findflight.resultsContent 正则）
#
# 本模块有两条【互相独立】的 FlightAware 抓取：
#   ① fetch_real_flights_with_filter —— 按【航线】查排班（findflight 页，内嵌 JSON）
#   ② flight_number_exists           —— 按【航班号】查该号现实中是否存在（ident 页，看 <title>）F29
#   （①的解析另以 findflight_direct_idents 暴露，供 tools/build_flight_blocks.py 离线生成航班号表 F30）

import re
import json
import time
import urllib.request

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_TIMEOUT = 6


# ---- 智能匹配与解析辅助函数 ----

def is_aircraft_match(user_input, ac_type):
    if not user_input: return True
    u = user_input.upper().replace(" ", "").replace("-", "")
    a = ac_type.upper().replace(" ", "").replace("-", "")
    if u in a or a in u: return True
    u_strip = re.sub(r'^[AB]', '', u)
    a_strip = re.sub(r'^[AB]', '', a)
    if u_strip and a_strip and (u_strip in a_strip or a_strip in u_strip): return True
    digits_u = re.findall(r'\d+', u_strip)
    digits_a = re.findall(r'\d+', a_strip)
    if digits_u and digits_a:
        if len(digits_u[0]) >= 2 and len(digits_a[0]) >= 2:
            if digits_u[0][:2] == digits_a[0][:2]: return True
    return False


def time_to_minutes(time_str):
    if not time_str: return None
    match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)?', time_str, re.IGNORECASE)
    if not match: return None
    hour, minute, ampm = int(match.group(1)), int(match.group(2)), match.group(3)
    if ampm:
        ampm = ampm.upper()
        if ampm == "PM" and hour != 12: hour += 12
        elif ampm == "AM" and hour == 12: hour = 0
    return hour * 60 + minute


def parse_user_time_range(range_str):
    try:
        if not range_str or '-' not in range_str: return None
        sh, sm = map(int, range_str.split('-')[0].strip().split(':'))
        eh, em = map(int, range_str.split('-')[1].strip().split(':'))
        return (sh * 60 + sm, eh * 60 + em)
    except Exception: return None


# ---- 核心 JSON 过滤爬虫 ----
# findflight 页的解析（脆弱耦合点：内嵌的 FA.findflight.resultsContent）只维护这一份：
# 运行时的 fetch_real_flights_with_filter 与离线的 tools/build_flight_blocks.py（生成 flight_blocks.json）共用。

_FINDFLIGHT_URL = "https://flightaware.com/live/findflight?origin=%s&destination=%s"
_FINDFLIGHT_JSON = re.compile(r'FA\.findflight\.resultsContent\s*=\s*(\[\{.*?\}\])\s*;', re.DOTALL)


def _findflight_data(dep_icao, arr_icao):
    """抓 findflight 页、取出内嵌 JSON（每条 = 一个航段 leg）。页面里找不到 → None；网络异常照常抛出。"""
    req = urllib.request.Request(_FINDFLIGHT_URL % (dep_icao, arr_icao), headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
        html = response.read().decode('utf-8', errors='ignore')
    json_match = _FINDFLIGHT_JSON.search(html)
    return json.loads(json_match.group(1)) if json_match else None


def _direct_legs(flights_data):
    """只留「直飞」：findflight 每条结果是一个航段(leg)，带 origin/destination(IATA)。中转联程被拆成多段——
    首段 dep→中转城市(带 connectionCity)、后续段→到达(带 layoverDuration)。旧代码不看起止，把某一段误当直飞。
    两端 IATA 直接从数据自身推：dep=首条结果的 origin；arr=第一条【行程】最后一段的 destination
    （行程按段依次排列，下一条行程又从 dep 起，故 origin 再次==dep 即换了行程）。缺该字段(FA 改版)则不过滤、保持旧行为。"""
    has_od = bool(flights_data) and "origin" in flights_data[0] and "destination" in flights_data[0]
    if not has_od:
        return list(flights_data or [])
    dep_iata = flights_data[0].get("origin")
    arr_iata = flights_data[0].get("destination")
    for fl in flights_data[1:]:
        if fl.get("origin") == dep_iata:                    # 下一条行程开始 → 第一条行程结束
            break
        arr_iata = fl.get("destination")                    # 续段：全程到达随之推进到本段目的地
    return [fl for fl in flights_data                       # 跳过中转联程的分段（本段起止≠全程起止）
            if fl.get("origin") == dep_iata and fl.get("destination") == arr_iata]


def _clean_html(raw_html):
    if not raw_html: return ""
    return re.sub(r'<[^>]+>', '', raw_html.replace("&nbsp;", " ")).strip()


def _ident_of(flight):
    """航段 → 航班号（如 'JAL501'）：取 flightIdent 链接里的文字，没有链接就去掉 HTML 标签。"""
    ident_raw = flight.get("flightIdent", "")
    ident_match = re.search(r'>([A-Za-z0-9]+)</a>', ident_raw)
    return ident_match.group(1).upper() if ident_match else _clean_html(ident_raw).upper()


def findflight_direct_idents(dep_icao, arr_icao):
    """按航线查真·直飞航班 → {航班号: 起飞时刻文本}（如 {'JAL501': '06:35AM JST'}）。
    供 tools/build_flight_blocks.py 生成航班号表（F30）；运行时不调用。
    页面里找不到排班 JSON（无航班 / 改版）→ None；网络异常照常抛出，调用方据此计失败。"""
    flights_data = _findflight_data(dep_icao, arr_icao)
    if flights_data is None:
        return None
    out = {}
    for flight in _direct_legs(flights_data):
        ident = _ident_of(flight)
        if ident and ident not in out:
            out[ident] = _clean_html(flight.get("flightDepartureTime", ""))
    return out


def fetch_real_flights_with_filter(dep_icao, arr_icao, target_airline, filter_aircraft=None, filter_time_range=None):
    try:
        flights_data = _findflight_data(dep_icao, arr_icao)
        if flights_data is None: return False, []
        matched_flights, other_flights, seen = [], [], set()

        time_limits = parse_user_time_range(filter_time_range)

        for flight in _direct_legs(flights_data):           # 只留真·直飞（见 _direct_legs）
            flt = _ident_of(flight)

            if not flt: continue
            if target_airline and not flt.startswith(target_airline): continue
            if flt in seen: continue

            ac_type = flight.get("aircraftType", "").strip() or "未公布"
            dep_time_clean = _clean_html(flight.get("flightDepartureTime", ""))
            arr_time_clean = _clean_html(flight.get("flightArrivalTime", ""))
            times = f"{dep_time_clean} -> {arr_time_clean}" if dep_time_clean else "起降时间暂缺"
            flight_str = f"{flt} (机型: {ac_type} | 时间: {times})"
            seen.add(flt)

            is_match = True
            if filter_aircraft and not is_aircraft_match(filter_aircraft, ac_type): is_match = False
            if time_limits and is_match:
                dep_min = time_to_minutes(dep_time_clean)
                if dep_min is not None and not (time_limits[0] <= dep_min <= time_limits[1]): is_match = False

            if is_match: matched_flights.append(flight_str)
            else: other_flights.append(flight_str)
            if len(matched_flights) >= 5: break

        if matched_flights: return True, matched_flights[:5]
        elif other_flights: return False, other_flights[:5]
        return False, []
    except Exception: return False, []


# ---- F29：按航班号查「该号现实中是否存在」（供模拟呼号避开真实航班号）----
#
# 判据在 ident 页的 <title>，位于页面【最前 16KB】：
#   真实存在 → '<title>NH995 (ANA995) All Nippon Flight Tracking and History - FlightAware</title>'
#   查无此号 → '<title>Unknown Flight - FlightAware</title>'
# 注意两件实测出来的事：
#   ① 该页【不存在也返回 HTTP 200】，只能靠 title 区分，不能靠状态码。
#   ② 整页 456–536KB，而 `Range` 头【无效】（服务器忽略、照样回整页）→ 必须用 read(N) 从 socket 主动截断，
#      否则每核验一个号就白下 500KB。
#
# ⚠️ 三态返回、且「认不出的页面」必须是 None 而【不是】空号：
#   实测并发抓取会被限流，限流页的 title 既不是 Unknown Flight、也不是正常航班页；
#   若把它当成空号，就会把真实航班号当空号发给用户——正是本功能要修的 bug。
#   反方向（把空号误判成占用）只是多试一个号，无害。故判定从严：只有明确认出才下结论。

_IDENT_URL = "https://www.flightaware.com/live/flight/%s"
_IDENT_HEAD_BYTES = 16384        # 只读页面前 16KB —— title 一定在里面
_IDENT_TIMEOUT = 8
_IDENT_TTL = 86400               # 「某航班号是否存在」变化极慢，缓存一天足够
_IDENT_CACHE = {}                # ident -> (fetched_at, bool)；只缓存有结论的（None 不入缓存）

_T_UNKNOWN = "Unknown Flight"                                   # 查无此号
_T_REAL = re.compile(r"Flight\s+Tracking\s+and\s+History", re.I)  # 正常航班页的固定尾巴


def flight_number_exists(ident):
    """该航班号现实中是否存在（FlightAware 认不认得这个号）。
    True=真实存在（别用） / False=查无此号（可用） / None=判不了（限流·断网·改版，绝不当成可用）。"""
    key = (ident or "").strip().upper()
    if not key:
        return None
    hit = _IDENT_CACHE.get(key)
    if hit and time.time() - hit[0] < _IDENT_TTL:
        return hit[1]
    try:
        req = urllib.request.Request(_IDENT_URL % key, headers={'User-Agent': _UA})
        with urllib.request.urlopen(req, timeout=_IDENT_TIMEOUT) as r:
            head = r.read(_IDENT_HEAD_BYTES).decode('utf-8', errors='ignore')
    except Exception:
        return None
    m = re.search(r"<title>(.*?)</title>", head, re.S | re.I)
    if not m:
        return None                                  # 连 title 都没有 → 不是我们认识的页面
    title = m.group(1).strip()
    if _T_UNKNOWN.lower() in title.lower():
        result = False
    elif _T_REAL.search(title):
        result = True
    else:
        return None                                  # 限流页/改版页 → 判不了，不缓存
    _IDENT_CACHE[key] = (time.time(), result)
    return result
