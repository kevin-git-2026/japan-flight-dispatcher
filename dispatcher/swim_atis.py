# ================= SWIM 真实 ATIS（F33）=================
# 日本 SWIM「ATIS情報リクエストサービス」的报文：解析 + 线程安全的本机存储。
# 本模块不联网、不接触账号、密码和 Cookie；查询由 swim_client 负责，用户发起一次才查一次。
#
# 报文样本（2330Z）：
#   ATIS RJTT O / M2330 / (APCH)ILS Z RWY34L/ILS Z RWY34R / LDG RWY 34L/34R / DEP RWY 05/34R /
#   DEP FREQ 126.0 FM RWY05. / 120.8 FM RWY34R, / … / M / 312330Z 35011KT 35KM … Q1019/A3011=
# 第一行的 O 才是 ATIS 字母，M2330 和单独一行的 M 每期都不变。

import datetime
import re
import threading
import time

# SWIM 页面列出的全部机场（用户 2026-10-31 实测的 23 场）
SWIM_AIRPORTS = ("RJCC", "RJCH", "RJSS", "RJAA", "RJTT", "RJSN", "RJGG", "RJOO", "RJBB", "RJBE",
                 "RJOA", "RJOT", "RJOM", "RJOK", "RJFF", "RJFS", "RJFU", "RJFT", "RJFO", "RJFM",
                 "RJFK", "ROAH", "ROIG")

MAX_AGE_SEC = 5400               # 报文超过 90 分钟不再当真实 ATIS 用（一般 30 或 60 分钟更新一次）

_RWY_GROUP_RE = re.compile(
    r"\b(LDG|DEP|USING)\s+RWY\s*(\d\d[LRC]?(?:\s*(?:/|,|AND)\s*(?:RWY\s*)?\d\d[LRC]?)*)")
_RWY_RE = re.compile(r"\d\d[LRC]?")
_TIME_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
_HEAD_RE = re.compile(r"^ATIS\s+(\w{4})\s+([A-Z])\b")
_STOP_RE = re.compile(r"^(LDG\s|DEP\s|USING\s)")


def clean_lines(text):
    """规整成 MCDU 能显示的行：去行首尾空白和空行，去掉非 ASCII 和显示不了的 #。其余原文不改。"""
    out = []
    for ln in (text or "").splitlines():
        ln = "".join(c for c in ln if " " <= c <= "~" and c != "#").strip()
        if ln:
            out.append(ln)
    return out


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def report_time(text, now=None):
    """报文里的观测时间 DDHHMMZ → UTC datetime。报文只有日时分，月份取离 now 最近的那个。取不到 → None。"""
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    now = now or _utcnow()
    day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y, mo = now.year, now.month
    for _ in range(2):
        try:
            t = datetime.datetime(y, mo, day, hour, minute)
        except ValueError:                               # 这个月没有这一天 → 再往前一个月
            t = None
        if t is not None and t <= now + datetime.timedelta(days=1):
            return t
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    return None


def parse_report(text, now=None):
    """一条 ATIS 报文 → {icao, letter, time, approaches, ldg, dep, lines, raw}；不是 ATIS → None。"""
    lines = clean_lines(text)
    if not lines:
        return None
    head = _HEAD_RE.match(lines[0])
    if not head:
        return None
    apch, in_apch = [], False
    for ln in lines[1:]:
        if ln.startswith("(APCH)"):
            apch.append(ln[len("(APCH)"):])
            in_apch = True
        elif in_apch and not _STOP_RE.match(ln):         # 进近串换行时接上
            apch.append(ln)
        else:
            in_apch = False
    ldg, dep = [], []
    for kind, group in _RWY_GROUP_RE.findall("\n".join(lines)):
        rwys = ["RW" + r for r in _RWY_RE.findall(group)]
        if kind in ("LDG", "USING"):
            ldg.extend(r for r in rwys if r not in ldg)
        if kind in ("DEP", "USING"):
            dep.extend(r for r in rwys if r not in dep)
    approaches = [a.strip() for a in " ".join(apch).split("/") if a.strip()]
    return {"icao": head.group(1), "letter": head.group(2), "time": report_time(text, now),
            "approaches": approaches, "ldg": ldg, "dep": dep,
            "lines": lines, "raw": "\n".join(lines)}


def parse_response(obj, now=None):
    """SWIM 的 JSON → (error_code, [report…])。每个机场取最新一条；字段名兼容 atisInfo / atisinfo。"""
    if not isinstance(obj, dict):
        return "invalid", []
    info = obj.get("error_info") or [{}]
    code = str(info[0].get("error_code", "")) if isinstance(info[0], dict) else ""
    reports = []
    for item in obj.get("data") or []:
        if not isinstance(item, dict):
            continue
        texts = item.get("atisInfo") or item.get("atisinfo") or []
        rep = parse_report(texts[0], now) if texts and isinstance(texts[0], str) else None
        if rep:
            reports.append(rep)
    return code, reports


class AtisStore:
    """最新报文和查询状态。查询线程、HTTP 线程、UI 都会读写，全部加锁。报文只在内存。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._reports = {}
        self._status = "none"            # none / ok / no_creds / bad_login / auth / error

    def set_status(self, status):
        with self._lock:
            self._status = status

    def status(self):
        with self._lock:
            return self._status

    def put_response(self, obj):
        """一次查询的结果 → 这次有报文的机场列表。"""
        _code, reports = parse_response(obj)
        now = time.time()
        done = []
        with self._lock:
            self._status = "ok"
            for r in reports:
                if r["icao"] in SWIM_AIRPORTS:
                    r["received_at"] = now
                    self._reports[r["icao"]] = r
                    done.append(r["icao"])
        return done

    def report(self, icao, now=None):
        """未过期的最新报文；没有 → None。"""
        with self._lock:
            r = self._reports.get(icao)
        if not r or not r.get("time"):
            return None
        age = ((now or _utcnow()) - r["time"]).total_seconds()
        return r if age <= MAX_AGE_SEC else None

    def config(self, icao, now=None):
        """未过期报文的实测构型，形状同 ntrack.fetch_latest，规划面板直接复用。没有 → None。"""
        r = self.report(icao, now)
        if not r or not (r["ldg"] or r["dep"]):
            return None
        return {"approaches": r["approaches"], "ldg": r["ldg"], "dep": r["dep"], "raw": r["raw"],
                "time_jst": "SWIM %s" % r["time"].strftime("%d%H%MZ"), "source": "SWIM"}


store = AtisStore()
