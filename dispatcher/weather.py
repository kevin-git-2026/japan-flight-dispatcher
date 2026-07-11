# ================= 机场天气 METAR + TAF（F20，v1.4.1）=================
# 从 NOAA tgftp 取原始 METAR(观测) 与 TAF(预报)，配合跑道朝向算逆/顺风与侧风分量，
# 为「选跑道」做决策支持（不替用户拍板）。纯标准库 urllib，超时 + 优雅失败，按 URL 短缓存。
#   METAR: …/data/observations/metar/stations/<ICAO>.TXT
#   TAF:   …/data/forecasts/taf/stations/<ICAO>.TXT
# 两者第 1 行为时间戳、其后为报文（TAF 可能跨多物理行，合并成整段）。

import re
import math
import time
import json
import datetime
import urllib.parse
import urllib.request

_METAR_URL = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/%s.TXT"
_TAF_URL = "https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/%s.TXT"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JPNFlightDispatcher/1.6.0"
_TTL = 600                       # 缓存 10 分钟（METAR/TAF 约每小时更新）
_CACHE = {}                      # url -> (fetched_at, (timestamp, body)) ；仅缓存成功

# ---- F22 网格天气回退（Open-Meteo）：METAR 缺测/过期时合成一条标准格式 METAR 串 ----
_OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
_GRID_TTL = 1800                 # 网格缓存 30 分钟（模型每 1–3h 更新）
_GRID_CACHE = {}                 # (rlat,rlon) -> (fetched_at, grid|None)
_METAR_STALE_SEC = 7200          # 观测 > 2h 视为过期 → 回退网格（可调）
_GRID_FIELDS = ("wind_speed_10m,wind_direction_10m,wind_gusts_10m,visibility,"
                "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
                "pressure_msl,temperature_2m,dew_point_2m,relative_humidity_2m,"
                "weather_code,precipitation")
_grid_credited = [False]         # 首次用到网格时打印 CC-BY 署名一次

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


# ---- 云 / 能见度解码（供运行规则「好天门槛」= 云底高 + 能见度）----
# 云组：FEW/SCT/BKN/OVC/VV + 三位百尺基高（/// 未知）；VV=垂直能见度(不定云底)按最密算。
_CLOUD_RE = re.compile(r"\b(FEW|SCT|BKN|OVC|VV)(\d{3}|///)")
_COVER_DENSITY = {"FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}


def parse_sky(metar_raw):
    """从 METAR 解出云层与能见度 → (layers, vis_m)：
    layers=[(density 1-4, base_ft|None)]（FEW/SCT/BKN/OVC/VV，base=三位×100ft，/// → None）；
    vis_m=能见度米（CAVOK→9999 / 风组后首个 4 位米组 / xSM 换算 / 取不到 → None）。"""
    if not metar_raw:
        return ([], None)
    layers = [(_COVER_DENSITY.get(cov), None if base == "///" else int(base) * 100)
              for cov, base in _CLOUD_RE.findall(metar_raw)]
    return (layers, _parse_visibility(metar_raw))


def _parse_visibility(metar_raw):
    """能见度(米)：CAVOK→9999；风组后首个 4 位米组（可带方位后缀）；xSM→×1609。取不到→None。"""
    toks = metar_raw.split()
    if "CAVOK" in toks:
        return 9999
    start = 0
    for i, t in enumerate(toks):
        if _WIND_RE.match(t):
            start = i + 1
            break
    for t in toks[start:]:
        m = re.match(r"^(\d{4})(?:NDV|[NSEW]{1,2})?$", t)
        if m:
            return int(m.group(1))
        f = re.match(r"^(\d+)/(\d+)SM$", t)
        if f:
            return int(round(int(f.group(1)) / int(f.group(2)) * 1609))
        w = re.match(r"^(\d+)SM$", t)
        if w:
            return int(w.group(1)) * 1609
    return None


def ceiling_ft(layers, cover="SCT"):
    """云底(ft) = 云量密度 ≥ cover(FEW/SCT/BKN/OVC 起算) 的各层里最低【已知】基高。
    无该密度以上的云 / 均为未知基高(///) → None（无约束云底；好天门槛按「过/未知」处理）。"""
    dmin = _COVER_DENSITY.get((cover or "SCT").upper(), 2)
    known = [b for d, b in layers if d and d >= dmin and b is not None]
    return min(known) if known else None


# ================= F22 网格天气回退（Open-Meteo → 合成标准 METAR）=================
# METAR 缺测或过期（很多小机场夜间只回白天旧报）时，取机场坐标处的 Open-Meteo 网格模型天气，
# 编码成一条【标准格式 METAR 串】，直接喂 parse_wind 与显示逻辑复用。清楚标注「模型合成·非实测」。
# 取数：主用 models=jma_msm（日本本地 5km，风/温/压/云最准）；其 visibility/gust 恒 null →
#       由保守能见度(默认 9999)与「无阵风不出 G 组」自然处理；仅机场在 MSM 域外才回退 best_match。
# 编码要点（已用真实 RJFK 报文校准）：能见度不可信→保守(默认 9999、绝不臆造雾)；降水强度按速率、
#       毛毛雨(51–55)归并为 RA；云用分层 low/mid/high（勿塌成单一 OVC）、云底未知记 ///。


def _utcnow():
    """当前 UTC 的 naive datetime（避开 utcnow() 弃用，又与 strptime 的 naive 结果可相减）。"""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def now_jst():
    """当前日本标准时 (UTC+9) → (当日分钟 0–1439, ISO 星期 1=周一…7=周日)。供运行规则时段/星期匹配。"""
    t = _utcnow() + datetime.timedelta(minutes=540)
    return t.hour * 60 + t.minute, t.isoweekday()


def metar_age_sec(obs_time):
    """tgftp 首行时间戳 `YYYY/MM/DD HH:MM`(UTC) → 距今秒数；无法解析 → None。"""
    if not obs_time:
        return None
    try:
        t = datetime.datetime.strptime(obs_time.strip()[:16], "%Y/%m/%d %H:%M")
        return (_utcnow() - t).total_seconds()
    except (ValueError, TypeError):
        return None


def _fetch_grid_raw(lat, lon, model, timeout=6):
    """GET Open-Meteo current（model=None → best_match）。→ current dict｜None。"""
    params = {"latitude": "%.4f" % lat, "longitude": "%.4f" % lon,
              "current": _GRID_FIELDS, "wind_speed_unit": "kn",
              "timezone": "GMT", "forecast_days": "1"}
    if model:
        params["models"] = model
    url = _OPENMETEO_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                import gzip
                raw = gzip.decompress(raw)
        cur = json.loads(raw.decode("utf-8", "ignore")).get("current")
        return cur if isinstance(cur, dict) else None
    except Exception:
        return None


def fetch_grid_weather(lat, lon):
    """Open-Meteo 网格天气 → 规整 dict｜None。主用 jma_msm；域外(无风)回退 best_match。按坐标缓存。"""
    key = (round(lat, 3), round(lon, 3))
    hit = _GRID_CACHE.get(key)
    if hit and time.time() - hit[0] < _GRID_TTL:
        return hit[1]
    cur, model = _fetch_grid_raw(lat, lon, "jma_msm"), "jma_msm"
    if not cur or cur.get("wind_speed_10m") is None:        # MSM 域外 → best_match
        alt = _fetch_grid_raw(lat, lon, None)
        if alt and alt.get("wind_speed_10m") is not None:
            cur, model = alt, "best_match"
    grid = None
    if cur and cur.get("wind_speed_10m") is not None:
        grid = {"wind_dir": cur.get("wind_direction_10m"), "wind_kt": cur.get("wind_speed_10m"),
                "gust_kt": cur.get("wind_gusts_10m"), "vis_m": cur.get("visibility"),
                "cloud_low": cur.get("cloud_cover_low"), "cloud_mid": cur.get("cloud_cover_mid"),
                "cloud_high": cur.get("cloud_cover_high"), "cloud_pct": cur.get("cloud_cover"),
                "qnh_hpa": cur.get("pressure_msl"), "temp_c": cur.get("temperature_2m"),
                "dewp_c": cur.get("dew_point_2m"), "rh": cur.get("relative_humidity_2m"),
                "wx_code": cur.get("weather_code"), "precip_mm": cur.get("precipitation"),
                "time_z": cur.get("time"), "model": model}
    _GRID_CACHE[key] = (time.time(), grid)
    return grid


def _enc_time(time_z):
    """`2026-07-02T21:15`(UTC) → (`022115Z`, `2026/07/02 21:15`)；解析失败用当前 UTC。"""
    try:
        t = datetime.datetime.strptime((time_z or "")[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        t = _utcnow()
    return "%02d%02d%02dZ" % (t.day, t.hour, t.minute), t.strftime("%Y/%m/%d %H:%M")


def _enc_wind(grid):
    """风组 dddffKT（向→最近10°、速→取整；0→00000KT；阵风仅 gust−spd≥10kt 才附 Ggg）。"""
    spd = grid.get("wind_kt")
    if spd is None:
        return "/////KT"
    spd = int(round(spd))
    if spd == 0:
        return "00000KT"
    dd = int(round((grid.get("wind_dir") or 0) / 10.0) * 10) % 360
    dd = 360 if dd == 0 else dd
    s = ("%03d%03d" if spd >= 100 else "%03d%02d") % (dd, spd)
    g = grid.get("gust_kt")
    if g is not None and int(round(g)) - spd >= 10:
        g = int(round(g))
        s += ("G%03d" if g >= 100 else "G%02d") % g
    return s + "KT"


def _enc_visibility(grid):
    """能见度：模型此项不可靠 → 仅雾码/强降水时才信其低值，否则一律 9999（绝不臆造雾）。"""
    vis, code = grid.get("vis_m"), grid.get("wx_code")
    trust_low = code in (45, 48, 65, 75, 82, 95, 96, 99)
    if vis is None or not trust_low:
        return "9999"
    vis = int(vis)
    if vis >= 9999:
        return "9999"
    return "%04d" % (vis // 100 * 100 if vis < 5000 else min(vis, 9999) // 1000 * 1000)


def _enc_weather(grid):
    """天气现象：类型取 weather_code、强度按 precip 速率；毛毛雨 51–55 归并为 RA；雾仅 45/48。"""
    code = grid.get("wx_code")
    if code is None:
        return ""
    p = grid.get("precip_mm") or 0
    inten = "-" if p < 2.5 else ("" if p <= 10 else "+")    # 雨/阵雨强度按速率
    g = []
    if code in (51, 53, 55, 61, 63, 65):                   # 毛毛雨归并入雨
        g.append(inten + "RA")
    elif code in (56, 57, 66, 67):                         # 冻性
        g.append(inten + "FZRA")
    elif code in (71, 73, 75):                             # 雪：强度按 code 档（雪强度本按能见度）
        g.append(("-" if code == 71 else "+" if code == 75 else "") + "SN")
    elif code == 77:
        g.append("SG")
    elif code in (80, 81, 82):                             # 阵雨
        g.append(("-" if code == 80 else "+" if code == 82 else "") + "SHRA")
    elif code in (85, 86):
        g.append(("-" if code == 85 else "+") + "SHSN")
    elif code in (95, 96, 99):                             # 雷暴
        g.append(("+TSRA" if code == 99 else inten + "TSRA") + (" GR" if code in (96, 99) else ""))
    if code in (45, 48):                                   # 雾（不由能见度反推）
        g.append("FG")
    return " ".join(g)


def _enc_clouds(grid):
    """云组：分层 low/mid/high 各 %→okta→FEW/SCT/BKN/OVC（云底未知记 ///）；无则用总量；0→NSC。"""
    def cov(pct):
        if pct is None:
            return None
        o = int(round(pct / 12.5))
        if o < 1:
            return None
        return "FEW" if o <= 2 else "SCT" if o <= 4 else "BKN" if o <= 7 else "OVC"
    layers = []
    for pct in (grid.get("cloud_low"), grid.get("cloud_mid"), grid.get("cloud_high")):
        c = cov(pct)
        if c and (c + "///") not in layers:
            layers.append(c + "///")
    if layers:
        return " ".join(layers)
    c = cov(grid.get("cloud_pct"))
    return (c + "///") if c else "NSC"


def _enc_temp(grid):
    """温/露点 TT/TdTd（四舍五入、负值前缀 M）。"""
    t = grid.get("temp_c")
    if t is None:
        return ""
    def f(x):
        x = int(round(x))
        return ("M%02d" % -x) if x < 0 else ("%02d" % x)
    td = grid.get("dewp_c")
    return "%s/%s" % (f(t), f(td) if td is not None else "//")


def grid_to_metar(icao, grid):
    """网格 dict → (标准格式合成 METAR 串, obs_time)。见本文件 F22 编码要点。grid 为空 → None。"""
    if not grid:
        return None
    ztime, obs_time = _enc_time(grid.get("time_z"))
    parts = [icao.upper(), ztime, _enc_wind(grid), _enc_visibility(grid),
             _enc_weather(grid), _enc_clouds(grid), _enc_temp(grid)]
    q = grid.get("qnh_hpa")
    if q is not None:
        parts.append("Q%04d" % int(round(q)))
    parts.append("RMK OPEN-METEO")
    return (" ".join(p for p in parts if p), obs_time)


def resolve_airport_wx(icao, lat, lon):
    """统一天气解析：METAR 新鲜→用实测；缺测/过期→Open-Meteo 网格合成 METAR（清楚标注非实测）。
    → {source:'metar'|'grid'|None, wind:(dir,spd,gust), metar_raw, metar_age_sec, taf_raw, model}。"""
    m = fetch_metar(icao)
    taf = fetch_taf(icao)
    taf_raw = taf[0] if taf else None
    age = metar_age_sec(m[1]) if m else None
    if m and (age is None or age <= _METAR_STALE_SEC):     # 实测新鲜（或时间无法解析→当新鲜）
        return {"source": "metar", "wind": parse_wind(m[0]), "metar_raw": m[0],
                "metar_age_sec": age, "taf_raw": taf_raw, "model": None}
    try:                                                    # 缺测或过期 → 网格
        grid = fetch_grid_weather(lat, lon)
    except Exception:
        grid = None
    syn = grid_to_metar(icao, grid) if grid else None
    if syn:
        if not _grid_credited[0]:
            _grid_credited[0] = True
            print("🌐 天气数据来自 Open-Meteo (CC BY 4.0)")
        return {"source": "grid", "wind": parse_wind(syn[0]), "metar_raw": syn[0],
                "metar_age_sec": age, "taf_raw": taf_raw, "model": grid.get("model")}
    if m:                                                   # 网格也失败 → 退回（哪怕过期的）实测
        return {"source": "metar", "wind": parse_wind(m[0]), "metar_raw": m[0],
                "metar_age_sec": age, "taf_raw": taf_raw, "model": None}
    return {"source": None, "wind": (None, 0, None), "metar_raw": None,
            "metar_age_sec": None, "taf_raw": taf_raw, "model": None}
