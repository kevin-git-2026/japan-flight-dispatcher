# ================= 分时段 AIP 航路选择（F21，v1.4.1）=================
# 一条航线常有多条官方 AIP 航路，按【运行时段(EOBT/ETA)、机型、巡航高度】区分（日本夜间为减噪
# 常走不同/更长的航路）。本模块把 routes.csv 的 Time Restriction / Altitude / Aircraft 列解析出来，
# 供 GUI 在弹窗里让用户按 EOBT + 机型 + 巡航高度确认一条（严格模式自动定唯一；否则列出供手动选）。
#
# 设计取舍（复活 v1.4.0 曾删除的分时段代码的【可靠子集】）：
#   · 时间列可解析、可靠 → 用来过滤。Time Restriction: 'EOBT/ETA HHMM-HHMM'(UTC，可跨午夜 lo>hi)，
#     复合 'EOBT a-b &ETA c-d'(AND)。GUI 起飞时间为 JST → 匹配需 JST−9h=UTC。用 EOBT(撤轮挡)比时段；
#     起飞≈EOBT+滑出，到达 ETA=起飞+航程÷巡航速度。
#   · Altitude / Aircraft 是脏自由文本（'FL180-FL230' 区间 / 'A120-' / 'for AP west of 139E' / 'only for RJCW'…），
#     **不由程序凭空精筛**；改为按【用户给定的参考机型/高度】判属，返回 True/False/None(无法判)，
#     None 只展示、不硬选。因参考值由用户给，连 'FL180-FL230' 区间也能正确判属（修当初反向解析 bug）。

import re

CRUISE_KT = 450.0                 # ETA 推算用的粗略巡航地速（机型库无速度字段，估算够用）
_TAXI_MIN = 15                    # 起飞≈EOBT+滑出，粗略 15min

_TIME_PAT = re.compile(r'(EOBT|ETA)\s*(\d{2})(\d{2})\s*-\s*(\d{2})(\d{2})')   # 固定 4 位 HHMM，支持 '&ETA2145-1344'
_ALT_RANGE = re.compile(r'(?:FL)?\s*(\d{2,3})\s*-\s*(?:FL)?\s*(\d{2,3})', re.I)   # 'FL180-FL230' 区间
_ALT_ONE = re.compile(r'(?:FL|A)\s*(\d{2,3})\s*([+\-])', re.I)                    # 'FL240+' / 'FL230-' / 'A120-'


# ---------- 时间解析 ----------
def parse_hhmm(s):
    """'08:30' / '0830' / '8:30' → 当天分钟数(0-1439)；非法/空 → None。"""
    m = re.match(r'^\s*(\d{1,2})\s*[:：]?\s*(\d{2})\s*$', s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if (h <= 23 and mi <= 59) else None


def parse_time_restriction(s):
    """Time Restriction → [('EOBT', lo, hi), ('ETA', lo, hi), ...]（UTC 当天分钟）。
    支持复合 'EOBT a-b &ETA c-d' 与 csv 多行引号字段；无法解析/空 → []。"""
    return [(kind, int(h1) % 24 * 60 + int(m1), int(h2) % 24 * 60 + int(m2))
            for kind, h1, m1, h2, m2 in _TIME_PAT.findall(s or "")]


def _in_window(t, lo, hi):
    """环形闭区间 [lo,hi] 判断，支持跨午夜(lo>hi，如 2115-1329 表示 21:15→次日 13:29)。"""
    return lo <= t <= hi if lo <= hi else (t >= lo or t <= hi)


def route_matches_time(restr, eobt, eta):
    """无时段限制→True；有 EOBT 段→需 eobt 落入；有 ETA 段→需 eta 落入；复合(都有)→AND。
    eobt/eta 均为 None(未给时刻) → 跳过时间过滤(视作命中)。"""
    wins = parse_time_restriction(restr)
    if not wins:
        return True
    if eobt is None and eta is None:
        return True
    for kind, lo, hi in wins:
        t = eobt if kind == "EOBT" else eta
        if t is None or not _in_window(t, lo, hi):
            return False
    return True


def plan_times_utc(eobt_jst_min, dist_nm, taxi_min=_TAXI_MIN, cruise_kt=CRUISE_KT):
    """GUI 输入的 EOBT(JST) → 分时段匹配用的 UTC 时刻。
    EOBT_utc = EOBT_jst − 9h；起飞 ≈ EOBT + 滑出；ETA = 起飞 + 航程÷巡航速度。
    返回 (eobt_utc_min, eta_utc_min)，均取模 1440(当天分钟)。"""
    eobt_utc = (int(eobt_jst_min) - 9 * 60) % 1440
    enroute = (dist_nm / cruise_kt * 60.0) if (dist_nm and cruise_kt) else 0.0
    eta_utc = (eobt_utc + (taxi_min or 0) + enroute) % 1440
    return eobt_utc, int(round(eta_utc)) % 1440


# ---------- 高度 / 机型：按【用户参考值】判属（True/False/None=无法判） ----------
def parse_fl(s):
    """'FL340' / '34000' / '340' → 百英尺整数(340)｜None。用户巡航高度输入归一。"""
    m = re.search(r'(\d{2,5})', s or "")
    if not m:
        return None
    n = int(m.group(1))
    return n // 100 if n >= 1000 else n


def alt_matches(cond, user_fl):
    """AIP 高度限制 cond 对用户巡航高度 user_fl(百英尺) 是否适用。空限制→True；无 user_fl→None(不判)；
    非标准写法(如 '13000ft'、裸 'FL240')→None(只展示)。因 user_fl 由用户给，区间可正确判属。"""
    c = (cond or "").strip()
    if not c:
        return True
    if user_fl is None:
        return None
    m = _ALT_RANGE.search(c)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return lo <= user_fl <= hi
    m = _ALT_ONE.search(c)
    if m:
        lvl, sign = int(m.group(1)), m.group(2)
        return user_fl >= lvl if sign == "+" else user_fl <= lvl
    return None


def aircraft_matches(cond, user_cat):
    """AIP 机型限制 cond 对用户机型类别 user_cat∈{'JET','PROP'} 是否适用。空→True；无 user_cat→None；
    只认 'JET'/'DH8D'/'PROP'；复杂/地理/机场条件('for PROP except DH8D'/'for AP west of 139E'/'only for RJCW')→None。"""
    c = (cond or "").strip().upper()
    if not c:
        return True
    if not user_cat:
        return None
    if c == "JET":
        return user_cat == "JET"
    if c in ("PROP", "DH8D"):
        return user_cat == "PROP"
    return None                                    # 复杂/地理/机场条件 → 无法判，交用户


# ---------- 候选罗列 / 判定 / 唯一解 / 用途描述 ----------
def annotate_routes(rows):
    """AIP 原始行 [DEP,DEST,Time,Alt,Aircraft,Route,Remarks] → 每行
    {route, restr(规整空白), alt, aircraft}（不含判定）。"""
    out = []
    for r in rows:
        out.append({
            "route": ((r[5] if len(r) > 5 else "") or "").strip(),
            "restr": " ".join(((r[2] if len(r) > 2 else "") or "").split()),
            "alt": ((r[3] if len(r) > 3 else "") or "").strip(),
            "aircraft": ((r[4] if len(r) > 4 else "") or "").strip(),
        })
    return out


def filter_candidates(annotated, eobt_utc, eta_utc, user_cat, user_fl):
    """按用户给的 EOBT(→eobt/eta_utc) + 机型类别 + 巡航高度，为每条候选给判定串：
    'no'(任一硬性不符) / 'unknown'(有无法判的脏条件、无硬性冲突) / 'match'(时段命中且机型/高度适用或无限制)。"""
    verdicts = []
    for c in annotated:
        tv = route_matches_time(c["restr"], eobt_utc, eta_utc)     # bool
        av = alt_matches(c["alt"], user_fl)                        # True/False/None
        cv = aircraft_matches(c["aircraft"], user_cat)             # True/False/None
        if tv is False or av is False or cv is False:
            verdicts.append("no")
        elif av is None or cv is None:
            verdicts.append("unknown")
        else:
            verdicts.append("match")
    return verdicts


def resolve_unique(verdicts):
    """恰有一条 'match' → 其 idx；0 或多条 match → None（不可唯一确定）。严格模式自动确认用。"""
    idxs = [i for i, v in enumerate(verdicts) if v == "match"]
    return idxs[0] if len(idxs) == 1 else None


def _fmt_hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


def _daynight_tag(wins):
    """据首个时段窗（转 JST）粗判 日间/夜间：JST 窗中点落在 06:00–18:00 → 日间，否则 夜间。"""
    _kind, lo, hi = wins[0]
    jlo, jhi = (lo + 540) % 1440, (hi + 540) % 1440
    span = (jhi - jlo) % 1440
    mid = (jlo + span / 2.0) % 1440
    return "日间" if 6 * 60 <= mid < 18 * 60 else "夜间"


def describe_restriction(restr):
    """人读用途串：无限制→'全天'；否则 '日间/夜间 · EOBT 06:15-22:29 JST'（时段转 JST 显示，标签仅辅助）。"""
    wins = parse_time_restriction(restr)
    if not wins:
        return "全天"
    segs = ["%s %s-%s JST" % (kind, _fmt_hhmm((lo + 540) % 1440), _fmt_hhmm((hi + 540) % 1440))
            for kind, lo, hi in wins]
    return "%s · %s" % (_daynight_tag(wins), " ".join(segs))
