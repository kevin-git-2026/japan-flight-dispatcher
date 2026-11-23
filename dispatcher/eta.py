# ================= F36 ETA 推算：机型性能剖面 + 高空风 =================
# 旧算法 ETA = EOBT + 15 分滑出 + 航程 ÷ 450 kt（timed.plan_times_utc）：不算爬升 / 下降、不算风、只用大圆。
# 对照 FlightAware 排班（2026-11-22）从 EOBT 到落地普遍偏早 15–35 分钟；冬季西风带上东西向差得更多
# （当日 250 hPa 关东上空 245° / 120 kt：RJTT→ROAH 巡航段 +39 分、ROAH→RJTT −21 分）。
# 而 ETA 决定到达端用日间还是深夜规则、AIP 时段是否适用、F35 改端是否触发。
#
# 本模块沿航路几何按机型剖面（aircrafts.json 每条的 perf，aircraft.perf_of）分段积分：
#   爬升：FL100 以下按 clb[0] CAS、以上按 clb[1] CAS，到 Mach clb[2] 后保持 Mach；爬升率按 roc 三段；
#   巡航：喷气按 Mach crz（ISA 换真空速），螺旋桨 crz 直接是真空速；
#   下降：Mach des[0] → CAS des[1] → FL100 以下 des[2]，下降率按 rod 两段；最后 10 NM 按 app 速度；
#   另加 term 分钟的终端区余量（吸收 SID / STAR 比直线多出的航程，按排班校准）。
# 每一步取当时的位置、高度、时刻的高空风（weather.fetch_winds_aloft → Winds）；地速 = √(TAS² − 侧风²) + 顺风分量。
# 纯计算：零 GUI、零网络。

import bisect
import datetime
import math

from . import timed

TAXI_MIN = timed._TAXI_MIN       # 滑出（与旧算法相同）
MAG_VAR_W = 8.0                  # 日本磁差约 5–10°W：磁航迹 ≈ 真航迹 + 8°（只用于东单西双）
STEP_FT = 1000                   # 爬升 / 下降的积分步长
CRUISE_STEP_NM = 20.0            # 巡航段积分步长
FINAL_NM = 10.0                  # 最后进近段（约 3000 ft 起的 3° 下滑）按 perf.app
FINAL_FT = 3000
MIN_FL = 30                      # 短程降高度的下限（百英尺）

_R_NM = 3440.065
_A0 = 661.4786                   # 海平面音速 kt
_P0 = 1013.25


# ---------- ISA ----------
def isa(alt_ft):
    """ISA 标准大气 → (温度 K, 气压 hPa)。"""
    h = max(0.0, float(alt_ft)) * 0.3048
    if h <= 11000.0:
        t = 288.15 - 0.0065 * h
        return t, _P0 * (t / 288.15) ** 5.255876
    return 216.65, 226.3206 * math.exp(-(h - 11000.0) / 6341.62)


def hpa_to_ft(hpa):
    """气压高度：hPa → ft（ISA 反算；250 hPa ≈ 34000 ft）。"""
    if hpa >= 226.3206:
        t = 288.15 * (hpa / _P0) ** (1 / 5.255876)
        h = (288.15 - t) / 0.0065
    else:
        h = 11000.0 - 6341.62 * math.log(hpa / 226.3206)
    return h / 0.3048


def _sound_kt(t_k):
    return 38.967854 * math.sqrt(t_k)


def tas_from_mach(mach, alt_ft):
    return mach * _sound_kt(isa(alt_ft)[0])


def tas_from_cas(cas, alt_ft):
    """CAS → TAS（可压缩流公式，ISA）。250 KIAS@FL100 ≈ 290 KTAS。"""
    t, p = isa(alt_ft)
    qc = _P0 * ((1 + 0.2 * (cas / _A0) ** 2) ** 3.5 - 1)
    mach = math.sqrt(5 * ((qc / p + 1) ** (2 / 7.0) - 1))
    return mach * _sound_kt(t)


# ---------- 几何 ----------
def _hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _R_NM * math.asin(min(1.0, math.sqrt(a)))


def _brg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _latlon(p):
    """坐标项 → (lat, lon)：接受 (ident, lat, lon[, 段]) 与 (lat, lon)。"""
    if len(p) >= 3 and isinstance(p[0], str):
        return float(p[1]), float(p[2])
    return float(p[0]), float(p[1])


def gc_points(a, b, step_nm=50.0):
    """大圆上每 step_nm 一点（含两端）。a / b 为 (lat, lon)。"""
    d = _hav(a[0], a[1], b[0], b[1])
    n = max(1, int(math.ceil(d / step_nm)))
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    ang = d / _R_NM
    if ang < 1e-9:
        return [tuple(a), tuple(b)]
    out = []
    for i in range(n + 1):
        f = i / float(n)
        k1, k2 = math.sin((1 - f) * ang) / math.sin(ang), math.sin(f * ang) / math.sin(ang)
        x = k1 * math.cos(la1) * math.cos(lo1) + k2 * math.cos(la2) * math.cos(lo2)
        y = k1 * math.cos(la1) * math.sin(lo1) + k2 * math.cos(la2) * math.sin(lo2)
        z = k1 * math.sin(la1) + k2 * math.sin(la2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return out


class Path:
    """航路几何（折线）：按沿航迹距离 s 取位置与航迹。两点之间按直线插值（段都很短）。"""

    def __init__(self, coords):
        pts = []
        for c in coords or ():
            p = _latlon(c)
            if not pts or _hav(pts[-1][0], pts[-1][1], p[0], p[1]) > 0.01:
                pts.append(p)
        if len(pts) == 2 and _hav(pts[0][0], pts[0][1], pts[1][0], pts[1][1]) > 60:
            pts = gc_points(pts[0], pts[1], 50.0)             # 只有两端（大圆口径）→ 沿大圆加密，取风取航迹都准些
        self.pts = pts
        self.cum = [0.0]
        for i in range(1, len(pts)):
            self.cum.append(self.cum[-1] + _hav(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
        self.total = self.cum[-1] if pts else 0.0

    def at(self, s):
        """→ (lat, lon, 航迹°)。"""
        pts = self.pts
        if len(pts) < 2:
            return (pts[0][0], pts[0][1], 0.0) if pts else (0.0, 0.0, 0.0)
        s = min(max(s, 0.0), self.total)
        i = max(1, min(bisect.bisect_left(self.cum, s), len(pts) - 1))
        a, b = pts[i - 1], pts[i]
        seg = self.cum[i] - self.cum[i - 1]
        f = (s - self.cum[i - 1]) / seg if seg > 0 else 0.0
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, _brg(a[0], a[1], b[0], b[1])


def resample(coords, step_nm=50.0):
    """几何每 step_nm 取一点（含两端）→ [(lat, lon)]：高空风的取样点。"""
    p = Path(coords)
    if p.total <= 0:
        return [tuple(x) for x in p.pts]
    n = max(1, int(math.ceil(p.total / step_nm)))
    return [p.at(p.total * i / n)[:2] for i in range(n + 1)]


# ---------- 高空风 ----------
class Winds:
    """weather.fetch_winds_aloft 的结果 → 按（位置, 高度, 时刻）插值的风。
    data = {model, levels:[hPa…], pts:[(lat, lon, t0, u, v)]}；u / v = [层][小时] 的东向 / 北向分量（kt），
    t0 = 该点序列第 0 小时的 UTC 时刻（epoch 小时）。水平取最近两点反距离加权，垂直按气压高度线性，时间线性。"""

    def __init__(self, data):
        self.model = data.get("model") or ""
        self.levels = list(data.get("levels") or ())
        self.lv_ft = [hpa_to_ft(x) for x in self.levels]
        self.pts = [p for p in data.get("pts") or () if p[3] and p[4]]

    def __bool__(self):
        return bool(self.pts and self.levels)

    def _series(self, p, lvl, th):
        _lat, _lon, t0, u, v = p
        n = len(u[lvl])
        x = min(max(th - t0, 0.0), n - 1.0)
        i = int(x)
        j = min(i + 1, n - 1)
        f = x - i
        return (u[lvl][i] + (u[lvl][j] - u[lvl][i]) * f, v[lvl][i] + (v[lvl][j] - v[lvl][i]) * f)

    def _at_point(self, p, alt_ft, th):
        lv = self.lv_ft
        if alt_ft <= lv[0]:
            return self._series(p, 0, th)
        if alt_ft >= lv[-1]:
            return self._series(p, len(lv) - 1, th)
        k = bisect.bisect_left(lv, alt_ft)
        f = (alt_ft - lv[k - 1]) / (lv[k] - lv[k - 1])
        u1, v1 = self._series(p, k - 1, th)
        u2, v2 = self._series(p, k, th)
        return u1 + (u2 - u1) * f, v1 + (v2 - v1) * f

    def at(self, lat, lon, alt_ft, th):
        """→ (u, v) kt。th = UTC 时刻（epoch 小时）；None → 序列里最早那一小时。"""
        if not self:
            return 0.0, 0.0
        if th is None:
            th = self.pts[0][2]
        cl = math.cos(math.radians(lat))
        best = sorted(((((p[0] - lat) * 60.0) ** 2 + ((p[1] - lon) * 60.0 * cl) ** 2, p) for p in self.pts),
                      key=lambda x: x[0])[:2]
        if len(best) == 1 or best[0][0] < 1.0:
            return self._at_point(best[0][1], alt_ft, th)
        w1, w2 = 1.0 / math.sqrt(best[0][0]), 1.0 / math.sqrt(best[1][0])
        u1, v1 = self._at_point(best[0][1], alt_ft, th)
        u2, v2 = self._at_point(best[1][1], alt_ft, th)
        return (u1 * w1 + u2 * w2) / (w1 + w2), (v1 * w1 + v2 * w2) / (w1 + w2)


def _gs(tas, trk, winds, lat, lon, alt_ft, th):
    """→ (地速, 顺风分量)。无风 → (TAS, 0)。"""
    if not winds:
        return tas, 0.0
    u, v = winds.at(lat, lon, alt_ft, th)
    tr = math.radians(trk)
    along = u * math.sin(tr) + v * math.cos(tr)
    cross = min(abs(u * math.cos(tr) - v * math.sin(tr)), 0.9 * tas)
    return max(math.sqrt(tas * tas - cross * cross) + along, 0.3 * tas), along


# ---------- 巡航高度 ----------
def _alt_bounds(cond):
    """AIP 高度条件 → (下限, 上限)（百英尺；无 → None）。与 timed.alt_matches 用同一套写法。"""
    c = (cond or "").strip()
    if not c:
        return None, None
    m = timed._ALT_RANGE.search(c)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return min(lo, hi), max(lo, hi)
    m = timed._ALT_ONE.search(c)
    if m:
        lvl = int(m.group(1))
        return (lvl, None) if m.group(2) == "+" else (None, lvl)
    return None, None


def _level_ok(fl, east):
    """东单西双（ICAO 半圆规则；FL290–410 为 RVSM 2000 ft 间隔，其上 4000 ft）。"""
    if fl < 290:
        return (fl // 10) % 2 == (1 if east else 0) and fl % 10 == 0
    if fl <= 410:
        return (fl - 290) % 20 == (0 if east else 10)
    return (fl - 410) % 40 == (40 if east else 20) % 40


def cruise_fl(dist_nm, track_deg, perf, alt_cond=None, user_fl=None):
    """巡航高度（百英尺）：用户给的 > 按航程的典型高度（喷气 100 + 0.6 × 航程，螺旋桨更低，均不超过 perf.fl）；
    再受 AIP 高度条件收窄、不超过 perf.ceil，按磁航迹取东单西双。"""
    ceil = int(perf.get("ceil") or 410)
    if user_fl:
        return max(MIN_FL, min(int(user_fl), ceil))
    cls = perf.get("cls")
    k, b = (0.6, 100) if cls == "jet" else ((0.6, 50) if cls == "turboprop" else (0.3, 30))
    fl = min(float(perf.get("fl") or 350), b + k * dist_nm)
    lo, hi = _alt_bounds(alt_cond)
    if lo is not None:
        fl = max(fl, lo)
    if hi is not None:
        fl = min(fl, hi)
    fl = int(min(fl, ceil)) // 10 * 10
    east = (track_deg + MAG_VAR_W) % 360 < 180
    for cand in range(fl, MIN_FL - 1, -10):
        if _level_ok(cand, east) and (lo is None or cand >= lo):
            return cand
    for cand in range(fl, ceil + 1, 10):
        if _level_ok(cand, east) and (hi is None or cand <= hi):
            return cand
    return max(fl, MIN_FL)


# ---------- 剖面积分 ----------
def _jet_tas(cas, mach, alt):
    tas = tas_from_cas(cas, alt)
    return min(tas, tas_from_mach(mach, alt)) if mach else tas


def _climb(path, perf, top_ft, winds, th0):
    """爬升：→ (水平距离 NM, 分钟, Σ顺风×分钟)。th0 = 起飞时刻（epoch 小时）或 None。"""
    cas_lo, cas_hi, mach = perf["clb"]
    roc = perf["roc"]
    s = t = wsum = 0.0
    alt = 0.0
    while alt < top_ft - 1e-6:
        a2 = min(alt + STEP_FT, top_ft)
        mid = (alt + a2) / 2
        r = roc[0] if mid < 10000 else (roc[1] if mid < 24000 else roc[2])
        tas = _jet_tas(cas_lo if mid < 10000 else cas_hi, mach, mid)
        dt = (a2 - alt) / float(r)
        lat, lon, trk = path.at(s)
        gs, along = _gs(tas, trk, winds, lat, lon, mid, None if th0 is None else th0 + t / 60.0)
        s += gs * dt / 60.0
        t += dt
        wsum += along * dt
        alt = a2
    return s, t, wsum


def _descent(path, perf, top_ft, winds, th_land):
    """下降（从终点往回推）：→ (水平距离 NM, 分钟, Σ顺风×分钟)。th_land = 落地时刻的估计。"""
    mach, cas_hi, cas_lo = perf["des"]
    rod = perf["rod"]
    total = path.total
    fin = min(FINAL_NM, total)
    lat, lon, trk = path.at(total - fin / 2)
    gs, along = _gs(float(perf.get("app") or 140), trk, winds, lat, lon, FINAL_FT / 2, th_land)
    t = fin / gs * 60.0
    s, wsum = fin, along * t
    alt = float(FINAL_FT)
    while alt < top_ft - 1e-6:
        a2 = min(alt + STEP_FT, top_ft)
        mid = (alt + a2) / 2
        r = rod[0] if mid >= 10000 else rod[1]
        tas = _jet_tas(cas_lo if mid < 10000 else cas_hi, mach, mid)
        dt = (a2 - alt) / float(r)
        lat, lon, trk = path.at(total - s)
        gs, along = _gs(tas, trk, winds, lat, lon, mid, None if th_land is None else th_land - t / 60.0)
        s += gs * dt / 60.0
        t += dt
        wsum += along * dt
        alt = a2
    return s, t, wsum


def _cruise_tas(perf, alt):
    crz = float(perf.get("crz") or 0.78)
    return tas_from_mach(crz, alt) if crz < 3 else crz


def estimate(eobt_jst, coords, perf, fl=None, winds=None, th_eobt=None, alt_cond=None, taxi_min=TAXI_MIN):
    """一条航路的 ETA → dict：
      eta_jst / eta_utc（当日分钟）、block（EOBT → 落地 分钟）、taxi / climb / cruise / descent / term（分钟）、
      fl（百英尺）、wind_kt（巡航段的平均顺风分量，逆风为负）、dist_nm、source（'wind' / 'nowind'）。
    coords = 航路几何（含两端机场；只给两端 = 大圆）；th_eobt = EOBT 的 UTC 时刻（epoch 小时，取风用）。"""
    path = Path(coords)
    total = path.total
    th_off = None if th_eobt is None else th_eobt + taxi_min / 60.0
    trk = _brg(path.pts[0][0], path.pts[0][1], path.pts[-1][0], path.pts[-1][1]) if len(path.pts) > 1 else 0.0
    want = cruise_fl(total, trk, perf, alt_cond, fl)
    rough = total / _cruise_tas(perf, want * 100) * 60.0      # 落地时刻的粗估（下降段取风用）
    th_land = None if th_off is None else th_off + rough / 60.0
    lvl = want
    while True:
        c = _climb(path, perf, lvl * 100, winds, th_off)
        d = _descent(path, perf, lvl * 100, winds, th_land)
        if c[0] + d[0] <= total or lvl <= MIN_FL:
            break
        lvl -= 10                                            # 短程：爬不到就降高度
    climb_t, descent_t = c[1], d[1]
    cruise_t = wsum = 0.0
    s, s_end = c[0], total - d[0]
    if s_end < s:                                            # 连最低高度都放不下爬升 + 下降 → 按比例压缩
        k = total / (c[0] + d[0]) if (c[0] + d[0]) > 0 else 1.0
        climb_t, descent_t = climb_t * k, descent_t * k
    tas = _cruise_tas(perf, lvl * 100)
    while s < s_end - 1e-6:
        ds = min(CRUISE_STEP_NM, s_end - s)
        lat, lon, ctrk = path.at(s + ds / 2)
        th = None if th_off is None else th_off + (climb_t + cruise_t) / 60.0
        gs, along = _gs(tas, ctrk, winds, lat, lon, lvl * 100, th)
        dt = ds / gs * 60.0
        cruise_t += dt
        wsum += along * dt
        s += ds
    term = float(perf.get("term") or 0)
    block = taxi_min + climb_t + cruise_t + descent_t + term
    eta_jst = int(round(eobt_jst + block)) % 1440
    if cruise_t > 0:
        wind_kt = wsum / cruise_t
    else:
        wind_kt = (c[2] + d[2]) / (c[1] + d[1]) if (c[1] + d[1]) > 0 else 0.0
    return {"eta_jst": eta_jst, "eta_utc": (eta_jst - 540) % 1440, "block": block, "taxi": float(taxi_min),
            "climb": climb_t, "cruise": cruise_t, "descent": descent_t, "term": term, "fl": lvl,
            "wind_kt": wind_kt, "dist_nm": total, "source": "wind" if winds else "nowind"}


def legacy(eobt_jst, coords):
    """没有性能数据时（机型库缺失）：旧算法，形状同 estimate。"""
    total = Path(coords).total
    eu, au = timed.plan_times_utc(eobt_jst, total)
    eta_jst = (au + 540) % 1440
    return {"eta_jst": eta_jst, "eta_utc": au, "block": (eta_jst - eobt_jst) % 1440, "taxi": float(TAXI_MIN),
            "climb": 0.0, "cruise": total / timed.CRUISE_KT * 60.0, "descent": 0.0, "term": 0.0, "fl": None,
            "wind_kt": 0.0, "dist_nm": total, "source": "legacy"}


def _ckey(coords):
    return tuple((round(a, 3), round(b, 3)) for a, b in (_latlon(c) for c in coords or ()))


class EtaModel:
    """一次规划用的 ETA 模型：机型剖面 + 高空风 + 「今天」的日期（EOBT 只有钟点）。
    plan()、面板、F21 弹窗、后台重算都用同一个对象（按 EOBT + 几何 + 高度缓存），判断前后一致。"""

    def __init__(self, dep, arr, perf, winds=None, ac_id=None, fallback=False, now_utc=None):
        self.dep = (dep.lat_dd, dep.lon_dd)
        self.arr = (arr.lat_dd, arr.lon_dd)
        self.perf = perf
        self.winds = winds if winds else None
        self.ac_id = ac_id
        self.fallback = bool(fallback)                   # 机型留空 / 查不到，按默认机型估
        now = now_utc or datetime.datetime.now(datetime.timezone.utc)
        jst = now + datetime.timedelta(hours=9)
        midnight = datetime.datetime(jst.year, jst.month, jst.day, tzinfo=datetime.timezone.utc)
        self._day0_h = midnight.timestamp() / 3600.0 - 9.0   # 今天 JST 0 点的 UTC 时刻（epoch 小时）
        self._now_min, self._now_wd = jst.hour * 60 + jst.minute, jst.isoweekday()
        self._cache = {}

    @property
    def gc(self):
        return [self.dep, self.arr]

    def th_eobt(self, eobt_jst):
        """EOBT（JST 当日分钟）→ UTC 时刻（epoch 小时）。日期与运行规则的星期判断一致（operations.eobt_weekday）。"""
        from .operations import eobt_weekday
        tomorrow = eobt_weekday(eobt_jst, self._now_min, self._now_wd) != self._now_wd
        return self._day0_h + eobt_jst / 60.0 + (24.0 if tomorrow else 0.0)

    def estimate(self, eobt_jst, coords=None, alt_cond=None, fl=None):
        coords = coords or self.gc
        key = (int(eobt_jst), _ckey(coords), alt_cond or "", fl)
        hit = self._cache.get(key)
        if hit is None:
            if self.perf is None:
                hit = legacy(eobt_jst, coords)
            else:
                hit = estimate(eobt_jst, coords, self.perf, fl=fl, winds=self.winds,
                               th_eobt=self.th_eobt(eobt_jst), alt_cond=alt_cond)
            if len(self._cache) > 4000:
                self._cache.clear()
            self._cache[key] = hit
        return hit

    def eta_jst(self, eobt_jst, coords=None, alt_cond=None, fl=None):
        return self.estimate(eobt_jst, coords, alt_cond, fl)["eta_jst"]
