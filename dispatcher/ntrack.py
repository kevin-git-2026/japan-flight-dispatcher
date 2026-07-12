# ================= 国土交通省 ntrack：羽田的【实测】运用状况 =================
# 日本国土交通省的「羽田空港飛行コースホームページ」全公开当前及过去 72 小时的机场运用状况：
# 实际在用的【进近方式 + 落地跑道 + 离场跑道】，每 30 分钟一条。这是权威实测值——
# 比我们靠「时段 + 风 + 天气」规则去【推断】准得多，所以它是进离场预选的**首选依据**。
#
# 取法（已实测）：一次 GET，表格就内联在首页 HTML 里（`<table id='atistable'>`），
# 不走 ajax、不需要 JS、不需要认证——与本项目既有的 jp-routes / FlightAware 抓取同族。
# 站点无 robots.txt 限制，数据全公开。
#
# ⚠️ 只有羽田。ntrack 是羽田专属站（成田是 NAA 另一个站点，将来可另行接入）。
#    其余机场仍走 operation.json 规则引擎。
#
# ⚠️ 这是【当前/过去】的实况，不是预报。用最新一条即可：模拟飞行常调时间但用实时天气，
#    而运用构型正是被实时天气驱动的——所以「此刻的构型」就是用户实际会遇到的那个。
#    （深夜運用这类按钟点切换的除外：那由 operation.json 规则引擎按 EOBT 兜住。）

import html as _html
import re
import time
import urllib.request

_URL = "https://www.ntrack.mlit.go.jp/NtrackTop/show"
_TTL = 1800                      # 30 分钟缓存 —— 正好是它的更新周期
_TIMEOUT = 8
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

AIRPORTS = ("RJTT",)             # 本数据源覆盖的机场

# 「<进近> LDG RWY <落地跑道> DEP RWY <离场跑道>」
# 例：LDA W RWY22/LDA W RWY23 LDG RWY 22/23 DEP RWY 16L/16R
_CFG_RE = re.compile(r"^(?P<app>.*?)\s*LDG\s+RWY\s+(?P<ldg>[\w/]+)\s+DEP\s+RWY\s+(?P<dep>[\w/]+)\s*$",
                     re.I | re.S)
_TABLE_RE = re.compile(r"<table[^>]*id=['\"]atistable['\"].*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)

# MLIT 写 RNP，CIFP 写 RNAV；其余写法（ILS / LDA / LOC / VOR + Y/Z/W 后缀）两边逐字一致。
_ALIAS = ((r"\bRNP\b", "RNAV"),)

_cache = {"at": 0.0, "rows": None}


def supports(icao):
    return (icao or "").upper() in AIRPORTS


def _text(s):
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _rwys(s):
    """`16L/16R` → `['RW16L', 'RW16R']`。"""
    return ["RW" + p.strip().upper() for p in (s or "").split("/") if p.strip()]


def parse_config(text):
    """一条运用状况串 → {approaches, ldg, dep, raw}；不合格式 → None。"""
    m = _CFG_RE.match((text or "").strip())
    if not m:
        return None
    return {"approaches": [a.strip() for a in m.group("app").split("/") if a.strip()],
            "ldg": _rwys(m.group("ldg")),
            "dep": _rwys(m.group("dep")),
            "raw": " ".join((text or "").split())}


def _fetch_rows():
    """整张表（最新在前）：[(时刻 JST, 运用状况串)]。失败 → None。"""
    req = urllib.request.Request(_URL, headers={"User-Agent": _UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        doc = r.read().decode("utf-8", "replace")
    m = _TABLE_RE.search(doc)
    if not m:
        return None
    out = []
    for tr in _ROW_RE.findall(m.group(0)):
        cells = [_text(c) for c in _CELL_RE.findall(tr)]
        if len(cells) >= 2 and cells[0] and cells[1]:
            out.append((cells[0], cells[1]))
    return out or None


def fetch_latest(icao="RJTT"):
    """该机场【当前】的实测运用状况 → {time_jst, approaches, ldg, dep, raw}；不支持/取不到 → None。
    全程 try/except：断网、改版、解析失败都只是返回 None，规划流程照常走规则引擎。"""
    if not supports(icao):
        return None
    now = time.time()
    if _cache["rows"] is None or now - _cache["at"] > _TTL:
        try:
            rows = _fetch_rows()
        except Exception as e:                            # noqa: BLE001
            print("⚠️ 取羽田实测运用状况失败（走运行规则）:", e)
            return None
        if not rows:
            return None
        _cache["rows"], _cache["at"] = rows, now
        print("✈️ 已取到羽田实测运用状况（国土交通省 ntrack）：%s %s" % rows[0])
    t, text = _cache["rows"][0]                            # 最新一条
    cfg = parse_config(text)
    if not cfg:
        return None
    cfg["time_jst"] = t
    return cfg


def _norm(s):
    s = re.sub(r"\s+", " ", (s or "").strip().upper())
    for pat, rep in _ALIAS:
        s = re.sub(pat, rep, s)
    return s


def match_iaps(approaches, iap_list):
    """ntrack 的进近串 → CIFP 的进近 ident。
    `iap_list` = `procedures.enumerate_approaches(icao)` 的返回（含合成显示名 `name`）。
    两边都按 ICAO 图表命名，故【逐字匹配】即可（`LDA W RWY22` → `X22-W`）；唯一别名是 RNP↔RNAV。"""
    by_name = {_norm(a.get("name")): a.get("ident") for a in (iap_list or [])}
    out = []
    for a in approaches or []:
        ident = by_name.get(_norm(a))
        if ident and ident not in out:
            out.append(ident)
    return out
