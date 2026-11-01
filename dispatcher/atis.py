# ================= 生成式 ATIS（F33）=================
# 没有真实 ATIS 时（SWIM 未覆盖的机场、未登录、报文过期）由 METAR + 运行规则生成一份。
# 思路是「模板 + 变量」：atis_template.json 定版式，变量为空的行整行删掉。版式跟随真实 ATIS。
#
# 只写 METAR 里有的东西：真实 ATIS 的 35KM、云型 CU/SC、A3011 这类细节 METAR 没有。
# 英寸气压不从 hPa 换算（Q1019 算出约 A3009，真实是 A3011，差 0.02～0.03）。

import json
import math
import os
import re
import threading

from . import ntrack, operations, procedures, weather
from .config import get_real_run_path

_TEMPLATE_FILE = "atis_template.json"
_DEFAULT_TEMPLATE = ["ATIS {icao} {letter}", "M{hhmm}", "{apch}", "{ldg}", "{dep}", "{using}",
                     "{note}", "M", "{wx}", "{tag}", "{model}"]
_TAG = "GENERATED NOT OFFICIAL"
_MODEL_TAG = "MODEL WX NOT OBSERVED"
_NOTE_WIND = "RWY BY WIND"
_VOR_MAX_NM = 200                # 最近 VOR 超过这个距离就不修正磁差

_WIND_RE = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)$")
_VAR_RE = re.compile(r"^(\d{3})V(\d{3})$")
_VIS_RE = re.compile(r"^(?:\d{4}(?:NDV|[NSEW]{1,2})?|\d+(?:/\d+)?SM)$")
_WX_RE = re.compile(r"^(?:\+|-|VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?"
                    r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)*$")
_CLOUD_RE = re.compile(r"^(?:(?:FEW|SCT|BKN|OVC|VV)(?:\d{3}|///)(?:CB|TCU|///)?|//////(?:CB|TCU)?"
                       r"|NSC|NCD|CLR|SKC)$")
_TEMP_RE = re.compile(r"^M?\d{2}/(?:M?\d{2}|//)$")
_QNH_RE = re.compile(r"^(?:Q\d{4}|A\d{4})$")
_TIME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")
_STOP = {"RMK", "TEMPO", "BECMG", "NOSIG", "FCST"}

_nav_lock = threading.Lock()
_vor_cache = {}                  # earth_nav.dat 路径 → [(lat, lon, 磁差)]
_letter_lock = threading.Lock()
_letters = {}                    # 机场 → (字母, 内容键)


def parse_metar(raw):
    """METAR → {time, ddhhmm, hhmm, wind, var, vis, wx, clouds, temp, qnh}。遇 RMK / 趋势组即停。
    解析不了的片段跳过（RVR、跑道状况等）。时间取不到 → None。"""
    toks = (raw or "").replace("=", " ").split()
    m = {"ddhhmm": None, "hhmm": None, "wind": None, "var": None, "vis": None,
         "wx": [], "clouds": [], "temp": None, "qnh": None}
    for t in toks:
        if t in _STOP:
            break
        tm = _TIME_RE.match(t)
        if tm and not m["ddhhmm"]:
            m["ddhhmm"], m["hhmm"] = t, tm.group(2) + tm.group(3)
        elif _WIND_RE.match(t) and not m["wind"]:
            m["wind"] = t
        elif _VAR_RE.match(t):
            m["var"] = t
        elif t == "CAVOK":
            m["vis"] = t
        elif _VIS_RE.match(t) and m["vis"] is None:
            m["vis"] = t
        elif _CLOUD_RE.match(t):
            m["clouds"].append(t)
        elif _TEMP_RE.match(t):
            m["temp"] = t
        elif _QNH_RE.match(t):
            m["qnh"] = t
        elif t not in ("METAR", "SPECI", "COR", "AUTO") and len(t) >= 2 and _WX_RE.match(t) \
                and not t.isdigit():
            m["wx"].append(t)
    return m if m["ddhhmm"] else None


def _load_vors(dat_path):
    path = os.path.join(os.path.dirname(dat_path), "earth_nav.dat")
    with _nav_lock:
        if path in _vor_cache:
            return _vor_cache[path]
    vors = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                p = line.split()
                if len(p) > 9 and p[0] == "3":
                    try:
                        vors.append((float(p[1]), float(p[2]), float(p[6])))
                    except ValueError:
                        pass
    except OSError:
        pass
    with _nav_lock:
        _vor_cache[path] = vors
    return vors


def mag_correction(lat, lon, dat_path):
    """METAR 真北风 → ATIS 磁北风要加的度数：最近 VOR 的磁差四舍五入到 10°，恰好 5° 取 0。
    羽田 2300Z 实测和真实 ATIS 一致（33010KT → 34010KT）。"""
    best, best_d = None, _VOR_MAX_NM
    for vlat, vlon, dec in _load_vors(dat_path):
        d = math.hypot((lat - vlat) * 60, (lon - vlon) * 60 * math.cos(math.radians(lat)))
        if d < best_d:
            best, best_d = dec, d
    if best is None:
        return 0
    mag = int((abs(best) + 4.999) // 10) * 10            # 5→0、6→10、8→10
    return mag if best < 0 else -mag


def _apply_mag(wind, var, corr):
    """风组 / 变化区间加磁差。静风和 VRB 不变。"""
    def turn(d):
        return (d + corr - 1) % 360 + 1                  # 0 记作 360
    w = _WIND_RE.match(wind) if wind else None
    out = wind or ""
    if w and w.group(1) != "VRB" and int(w.group(2)) > 0 and int(w.group(1)) > 0:
        out = "%03d%s%s%s" % (turn(int(w.group(1))), w.group(2),
                              ("G" + w.group(3)) if w.group(3) else "", w.group(4))
    v = _VAR_RE.match(var) if var else None
    if v:
        out += " %03dV%03d" % (turn(int(v.group(1))), turn(int(v.group(2))))
    return out


def _vis_text(vis):
    if vis == "9999":
        return "10KM"
    return vis or ""


def wx_line(metar, corr=0):
    m = metar
    parts = [m["ddhhmm"], _apply_mag(m["wind"], m["var"], corr), _vis_text(m["vis"])]
    parts += m["wx"] + m["clouds"]
    parts += [m["temp"] or "", m["qnh"] or ""]
    return " ".join(p for p in parts if p)


def letter_for(icao, key, metar):
    """ATIS 字母：首次按 METAR 时间取值，之后内容变化才加一。只在内存里，重启后重新取值。"""
    with _letter_lock:
        cur = _letters.get(icao)
        if cur and cur[1] == key:
            return cur[0]
        if cur:
            letter = chr((ord(cur[0]) - 65 + 1) % 26 + 65)
        else:
            tm = _TIME_RE.match(metar["ddhhmm"])
            day, hour, minute = int(tm.group(1)), int(tm.group(2)), int(tm.group(3))
            letter = chr((day * 48 + hour * 2 + minute // 30) % 26 + 65)
        _letters[icao] = (letter, key)
        return letter


def _apch_name(name):
    """CIFP 合成名 → ATIS 写法：RNAV → RNP。"""
    return re.sub(r"\bRNAV\b", "RNP", name or "")


def _rwy_text(rwys):
    return "/".join(r.replace("RW", "") for r in rwys)


def _by_wind(rows, wind):
    """没有规则时按风选一条：风况合规、有程序、逆风大、跑道长。"""
    def key(row):
        rwy, length, labels = row
        if isinstance(wind[0], (int, float)) and wind[1]:
            hw, cw = weather.runway_wind(procedures.runway_heading_deg(rwy), wind[0], wind[1])
        else:
            hw, cw = 0.0, 0.0
        return (not weather.runway_ok(hw, cw), not labels, -round(hw, 1), -(length or 0))
    return min(rows, key=key)[0] if rows else None


def operating_config(icao, wx, dat_path, ops_data):
    """当前构型 → {approaches, ldg, dep, note}：羽田实测 > 机场运行规则 > 按风。"""
    cfg = ntrack.fetch_latest(icao) if ntrack.supports(icao) else None
    if cfg:
        return {"approaches": cfg["approaches"], "ldg": cfg["ldg"], "dep": cfg["dep"], "note": ""}

    wind = wx.get("wind") or (None, 0, None)
    dep_rows = procedures.matching_choices(icao, dat_path, [], "dep")[0]
    arr_rows = procedures.matching_choices(icao, dat_path, [], "arr")[0]
    rules = operations.airport_rules(ops_data, icao)
    if rules:
        try:
            jst, wd = weather.now_jst()
            layers, vis = weather.parse_sky((wx.get("metar_raw") or "").split(" RMK ")[0])
            ctx = {"jst_min": jst, "weekday": wd, "wind": wind, "sky_layers": layers, "vis_m": vis}
            prefer = operations.preferred_runways(ops_data, icao)
            dep = operations.select_rule(rules, "dep", ctx, dep_rows, prefer=prefer)
            arr = operations.select_rule(rules, "arr", ctx, arr_rows, prefer=prefer)
            if dep and arr:
                names = {a["ident"]: a["name"]
                         for a in procedures.enumerate_approaches(icao, dat_path)}
                idents = (arr[0].get("arr") or {}).get("iaps") or []
                return {"approaches": [_apch_name(names.get(i, i)) for i in idents],
                        "ldg": [arr[1]], "dep": [dep[1]], "note": ""}
        except Exception as e:                            # noqa: BLE001
            print("⚠️ ATIS 匹配运行规则失败:", e)
    rwy = _by_wind(arr_rows, wind)
    return {"approaches": [], "ldg": [rwy] if rwy else [], "dep": [rwy] if rwy else [],
            "note": _NOTE_WIND if rwy else ""}


def load_template():
    """atis_template.json 的 lines；文件缺失或损坏 → 内置默认值。"""
    try:
        with open(os.path.join(get_real_run_path(), _TEMPLATE_FILE), encoding="utf-8") as f:
            lines = json.load(f).get("lines")
        if isinstance(lines, list) and lines and all(isinstance(x, str) for x in lines):
            return lines
    except (OSError, ValueError, AttributeError):
        pass
    return list(_DEFAULT_TEMPLATE)


def render(template, values):
    """填模板：含 {变量} 且变量全为空的行整行删掉；结果只留 MCDU 能显示的字符。"""
    out = []
    for line in template:
        names = re.findall(r"\{(\w+)\}", line)
        if names and not any(values.get(n) for n in names):
            continue
        text = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), "")), line).strip()
        text = "".join(c for c in text if " " <= c <= "~" and c != "#")
        if text:
            out.append(text)
    return "\n".join(out)


def generate(icao, wx, lat, lon, dat_path, ops_data, template=None):
    """生成式 ATIS 文本；没有可用 METAR → None。wx = weather.resolve_airport_wx 的结果。"""
    metar = parse_metar((wx or {}).get("metar_raw"))
    if not metar:
        return None
    cfg = operating_config(icao, wx, dat_path, ops_data)
    line = wx_line(metar, mag_correction(lat, lon, dat_path))
    ldg, dep = _rwy_text(cfg["ldg"]), _rwy_text(cfg["dep"])
    same = bool(ldg) and ldg == dep
    apch = "/".join(cfg["approaches"])
    values = {"icao": icao, "hhmm": metar["hhmm"], "wx": line, "note": cfg["note"], "tag": _TAG,
              "apch": ("(APCH)" + apch) if apch else "",
              "ldg": "" if same else (("LDG RWY " + ldg) if ldg else ""),
              "dep": "" if same else (("DEP RWY " + dep) if dep else ""),
              "using": ("USING RWY " + ldg) if same else "",
              "model": _MODEL_TAG if wx.get("source") == "grid" else ""}
    values["letter"] = letter_for(icao, "%s|%s|%s|%s" % (line, apch, ldg, dep), metar)
    return render(template or load_template(), values)
