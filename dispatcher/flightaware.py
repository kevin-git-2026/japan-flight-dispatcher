# ================= 智能匹配与解析辅助 + FlightAware 现实排班爬虫 =================
# 机型模糊匹配、起飞时间区间解析，以及抓取并过滤 FlightAware findflight 页面排班。
# （与拆分前版本保持完全一致；脆弱耦合点为 FA.findflight.resultsContent 正则）

import re
import json
import urllib.request


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

def fetch_real_flights_with_filter(dep_icao, arr_icao, target_airline, filter_aircraft=None, filter_time_range=None):
    try:
        url = f"https://flightaware.com/live/findflight?origin={dep_icao}&destination={arr_icao}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=6) as response:
            html = response.read().decode('utf-8', errors='ignore')

        json_match = re.search(r'FA\.findflight\.resultsContent\s*=\s*(\[\{.*?\}\])\s*;', html, re.DOTALL)
        if not json_match: return False, []

        flights_data = json.loads(json_match.group(1))
        matched_flights, other_flights, seen = [], [], set()

        def clean_html(raw_html):
            if not raw_html: return ""
            return re.sub(r'<[^>]+>', '', raw_html.replace("&nbsp;", " ")).strip()

        time_limits = parse_user_time_range(filter_time_range)

        for flight in flights_data:
            ident_raw = flight.get("flightIdent", "")
            ident_match = re.search(r'>([A-Za-z0-9]+)</a>', ident_raw)
            flt = ident_match.group(1).upper() if ident_match else clean_html(ident_raw).upper()

            if not flt: continue
            if target_airline and not flt.startswith(target_airline): continue
            if flt in seen: continue

            ac_type = flight.get("aircraftType", "").strip() or "未公布"
            dep_time_clean = clean_html(flight.get("flightDepartureTime", ""))
            arr_time_clean = clean_html(flight.get("flightArrivalTime", ""))
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
