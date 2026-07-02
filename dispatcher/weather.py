# ================= 机场天气 METAR + TAF（F20，v1.4.1）=================
# 从 NOAA tgftp 取原始 METAR(观测) 与 TAF(预报)，配合跑道朝向算逆/顺风与侧风分量，
# 为「选跑道」做决策支持（不替用户拍板）。纯标准库 urllib，超时 + 优雅失败，按 URL 短缓存。
#   METAR: …/data/observations/metar/stations/<ICAO>.TXT
#   TAF:   …/data/forecasts/taf/stations/<ICAO>.TXT
# 两者第 1 行为时间戳、其后为报文（TAF 可能跨多物理行，合并成整段）。

import re
import math
import time
import urllib.request

_METAR_URL = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/%s.TXT"
_TAF_URL = "https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/%s.TXT"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JPNFlightDispatcher/1.4.1"
_TTL = 600                       # 缓存 10 分钟（METAR/TAF 约每小时更新）
_CACHE = {}                      # url -> (fetched_at, (timestamp, body)) ；仅缓存成功

_MAX_TAILWIND_KT = 10.0          # 日本运行经验判据：顺风 ≤ 此值 可落（可调）
_MAX_CROSSWIND_KT = 30.0         # 侧风 ≤ 此值 可落（可调）
_MPS_TO_KT = 1.94384

# 风组：dddff[Gff](KT|MPS)，dd d=风向(或 VRB)、ff=风速、G=阵风
_WIND_RE = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\b")


def _fetch_noaa(url, timeout=5):
    """GET NOAA TXT → (时间戳, 报文体)；报文体=第 2 行起合并。超时/失败/格式不符 → None。仅缓存成功。"""
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "ignore")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) >= 2:
            result = (lines[0], " ".join(lines[1:]))
            _CACHE[url] = (time.time(), result)
            return result
    except Exception:
        pass
    return None


def fetch_metar(icao):
    """→ (raw_metar, obs_time) 或 None。"""
    r = _fetch_noaa(_METAR_URL % icao.upper())
    return (r[1], r[0]) if r else None


def fetch_taf(icao):
    """→ (raw_taf, issue_time) 或 None（raw 为多行合并后的整段 TAF）。"""
    r = _fetch_noaa(_TAF_URL % icao.upper())
    return (r[1], r[0]) if r else None


def parse_wind(metar_raw):
    """从 METAR 取风组 → (dir_deg:int | 'VRB' | None, speed_kt:int, gust_kt:int|None)。
    `00000KT`=静风→(0,0,None)；VRB→('VRB',spd,…)；MPS 换算成 KT。无风组 → (None,0,None)。"""
    if not metar_raw:
        return (None, 0, None)
    m = _WIND_RE.search(metar_raw)
    if not m:
        return (None, 0, None)
    d, spd, gust, unit = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    if unit == "MPS":
        spd = round(spd * _MPS_TO_KT)
        gust = round(int(gust) * _MPS_TO_KT) if gust else None
    else:
        gust = int(gust) if gust else None
    direction = "VRB" if d == "VRB" else int(d)
    return (direction, spd, gust)


def runway_wind(rwy_heading, wind_dir, wind_speed):
    """→ (headwind_kt, crosswind_kt)：逆风带符号(正=逆风/负=顺风)、侧风取绝对值。
    跑道朝向缺失 / 风向非数值(VRB) / 静风 → (0,0)（各跑道无差别）。"""
    if rwy_heading is None or not isinstance(wind_dir, (int, float)) or not wind_speed:
        return (0.0, 0.0)
    delta = math.radians((wind_dir - rwy_heading + 540) % 360 - 180)   # 有符号夹角 [-180,180]
    return (wind_speed * math.cos(delta), abs(wind_speed * math.sin(delta)))


def runway_ok(headwind, crosswind):
    """日本运行经验判据：顺风(=−headwind) ≤ 10kt 且 侧风 ≤ 30kt 即可落。"""
    return (-headwind) <= _MAX_TAILWIND_KT and crosswind <= _MAX_CROSSWIND_KT
