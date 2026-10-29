# ================= VATJPN「空港別移管高度表」→ transfer_points.json（进/离场移管点）=================
# 数据源：VATJPN 把移管表发布成 Google 表格（2PACX 发布链接，见 PUB_ID）。发布页 `…/pubhtml` 列出每个标签页的
# 名称与 gid，每个 AIRAC 一页（通常同时挂着最近三期）；单页的 `pubhtml/sheet?gid=…` 是一张 HTML 表：
#   行号 | 空港 | 到着(移管点等, 高度, 移管元, 移管先) | 空港 | 出発(…)
# ICAO 列是 rowspan 合并的；到着 / 出発串在 colspan=4 的格里，多条用「 / 」分隔；追加的到着串是整行 colspan=4 的格
# （名古屋小牧 RJNA 型）。CSV 导出会丢掉合并信息，分不清「追加的到着串」与「移管点行」，所以必须解析 HTML。
#
# 两处用它：
#   · 主程序启动时 `refresh()`——按【本机导航数据的 AIRAC】选期、有变化才重建 transfer_points.json；
#   · tools/build_transfer_points.py——开发者手动重建（另外还重写 skill 文档 reference/transfer_points.md）。
#
# ⚠️ STAR 配对按【本机 NavData 的 CIFP】现推：换导航数据 AIRAC 后 STAR 会改名（AIRAC 2610 里 RJOO 的
#   IKOMAE / IKOMAN 已变成 AGPU1R / ROKKO1），旧名留在 json 里面板就配不上——所以本机导航数据周期一变就要重建，
#   json 的 `_vatjpn.navdata` 记着上次是按哪一期推的。
# 只读网络（docs.google.com）；除 transfer_points.json 外不写任何东西。

import datetime
import html
import json
import os
import re
import urllib.request
from html.parser import HTMLParser

from . import procedures
from .config import get_real_run_path
from .navdata import airac_start

PUB_ID = "2PACX-1vSNm44y4pYdLTvZMvnLddLv3sMer3M0uxR6lI4d4qze84p7zjlpzulsGvdkQkGzdDcVczZMXB0C5kiO"
BASE = "https://docs.google.com/spreadsheets/d/e/%s/pubhtml"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"            # 通用 UA，同 ntrack / flightaware（Google 表格对 UA 无要求）
_TIMEOUT = 10
SCHEMA = 2                       # json 结构版本：2 = 加了 arr_xfer / dep_xfer（移管行）。记录里的版本不同 → 启动时重建
_MIN_KEEP = 0.8                 # 新表解析出的机场数 < 原有的 80% → 多半是表格式变了，不覆盖
_MIN_AIRPORTS = 10               # 原文件缺失时的下限（现表 56 个）

_ICAO = re.compile(r"^R[JO][A-Z]{2}$")
_AIRWAY = re.compile(r"^[A-Z]{1,2}\d{1,4}$")                   # Y10 / V28 / A1 / G597 / R211……（点名不带数字或是 3 字母 + 数字）
_POINT = re.compile(r"^[A-Z][A-Z0-9]{1,4}$")
_REGION = re.compile(r"進入管制(区|空域)$")
_AIRAC = re.compile(r"AIRAC\s*(\d{4})")


# ---------- 抓取 ----------
def _get(url, timeout=_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def list_tabs(pub, timeout=_TIMEOUT):
    """发布页 → [(名称, gid)]，按页面顺序（最新一期排最前）。"""
    page = _get(BASE % pub, timeout)
    return [(html.unescape(n), g) for n, g in re.findall(r'items\.push\(\{name: "(.*?)", pageUrl: ".*?", gid: "(\d+)"', page)]


def sheet_url(pub, gid):
    return (BASE % pub) + "/sheet?headers=false&gid=" + gid


def tab_airac(name):
    """标签页名里的 AIRAC 号（int）；没有 → None。"""
    m = _AIRAC.search(name or "")
    return int(m.group(1)) if m else None


def tab_label(name, gid):
    a = tab_airac(name)
    return "AIRAC %04d" % a if a is not None else "gid %s" % gid


def pick_tab(tabs, local_cycle):
    """选哪一期 → ((名称, gid) 选中的, (名称, gid) 最新的, 警告 | None)。
    跟随本机导航数据：取「AIRAC ≤ 本机周期」里最新的一期——航路网与 STAR 都来自本机 NavData，门与配对要同属一期；
    本机比所有期都旧 → 取最旧一期；本机周期未知 → 取最新一期。
    标签页名里都没有 AIRAC 号 → 按发布顺序取第一张（VATJPN 把最新一期排最前）。"""
    if not tabs:
        return None, None, None
    num = [(tab_airac(n), -i, n, g) for i, (n, g) in enumerate(tabs) if tab_airac(n) is not None]
    if not num:
        return tabs[0], tabs[0], "标签页名称里没有 AIRAC 号，按发布顺序取第一张「%s」" % tabs[0][0]
    newest = max(num)
    try:
        local = int(local_cycle)
    except (TypeError, ValueError):
        local = None
    if local is None:
        pick = newest
    else:
        older = [t for t in num if t[0] <= local]
        pick = max(older) if older else min(num)
    return pick[2:], newest[2:], None


# ---------- 解析 HTML 表 → 网格 ----------
class _Table(HTMLParser):
    """只收 <td>（行号 <th> 不要）；每格 [rowspan, colspan, 文本]，<br> 记成换行。"""

    def __init__(self):
        super().__init__()
        self.rows, self.cell = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.rows.append([])
        elif tag == "td":
            self.cell = [int(a.get("rowspan") or 1), int(a.get("colspan") or 1), ""]
        elif tag == "br" and self.cell is not None:
            self.cell[2] += "\n"

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.rows[-1].append(self.cell)
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell[2] += data


def grid(page):
    """按 rowspan / colspan 展开成网格：每行 {列号: (文本, colspan, 在合并格里的偏移)}。"""
    t = _Table()
    t.feed(page)
    out, pending = [], {}
    for r, row in enumerate(t.rows):
        g, col = {}, 0
        for rs, cs, text in row:
            while (r, col) in pending:
                g[col] = pending.pop((r, col))
                col += 1
            for k in range(cs):
                g[col + k] = (text.strip(), cs, k)
                for rr in range(1, rs):
                    pending[(r + rr, col + k)] = (text.strip(), cs, k)
            col += cs
        while (r, col) in pending:
            g[col] = pending.pop((r, col))
            col += 1
        out.append(g)
    return out


def sheet_rows(page):
    """网格 → (管制区顺序, {ICAO: {region, arr_cells, dep_cells}})，ICAO 按表中出现顺序。"""
    rows = grid(page)
    hdr = next(g for g in rows if any(c[0] == "到着" for c in g.values()))
    airport_cols = sorted(c for c, v in hdr.items() if v[0] == "空港")
    arr_col = min(c for c, v in hdr.items() if v[0] == "到着")
    dep_col = min(c for c, v in hdr.items() if v[0] == "出発")
    a_icao, d_icao = airport_cols[0], airport_cols[1]
    regions, airports, region = [], {}, None

    def block(g, col):                                         # colspan=4、偏移 0 的整块格 = 串（不是移管点行）
        v = g.get(col)
        return v[0] if v and v[1] >= 4 and v[2] == 0 and v[0] else None

    for g in rows:
        icao = (g.get(a_icao) or ("",))[0]
        text = block(g, arr_col)
        if not icao and text and _REGION.search(text):
            region = text
            if region not in regions:
                regions.append(region)
            continue
        if not _ICAO.match(icao):
            continue
        ap = airports.setdefault(icao, {"region": region, "arr_cells": [], "dep_cells": []})
        if text:
            ap["arr_cells"].append(text)
        dep = block(g, dep_col)
        if dep and (g.get(d_icao) or ("",))[0] == icao:
            ap["dep_cells"].append(dep)
    for side, icol, col in (("arr_groups", a_icao, arr_col), ("dep_groups", d_icao, dep_col)):
        for icao, groups in _side_groups(rows, icol, col).items():
            if icao in airports:
                airports[icao][side] = groups
    return regions, airports


def _side_groups(rows, icao_col, col):
    """一侧（到着 / 出発）的移管组：{ICAO: [{routes, rows}, …]}，按表中顺序。
    表头格（colspan=4）开一个组；其下各行的四个普通格 = 移管点 | 高度 | 移管元 | 移管先。两侧各按自己那列的 ICAO 归属。"""
    out, cur, cur_ap = {}, None, None
    for g in rows:
        icao = (g.get(icao_col) or ("",))[0]
        if not _ICAO.match(icao):
            continue
        cells = [g.get(col + k) or ("", 1, 0) for k in range(4)]
        head = cells[0]
        if head[1] >= 4:
            if head[2] == 0 and head[0] and not _REGION.search(head[0]):
                cur, cur_ap = {"routes": _alternatives(head[0]), "rows": []}, icao
                out.setdefault(icao, []).append(cur)
            continue
        if cur is not None and icao == cur_ap and any(c[0] for c in cells):
            cur["rows"].append(_xfer_row([c[0] for c in cells]))
    return out


def _xfer_row(cells):
    """移管行的四格原文 → {fix, cond?, alt, from, to}。移管点格末尾的「(JET)」类机型条件拆进 cond；
    多行的席位格（`BB W APP (125.00)` 换行 `BB C DEP (120.65)`）用「 / 」连起来；高度格换行只是折行，用空格连。"""
    fix, alt, frm, to = cells

    def lines(s):
        return [x.strip() for x in s.split("\n") if x.strip()]
    fix = " ".join(lines(fix))
    row, m = {}, re.match(r"^(.*?)\s*\(([^)]*)\)$", fix)
    if m and m.group(1):
        fix = m.group(1)
        row["fix"], row["cond"] = fix, m.group(2).strip()
    else:
        row["fix"] = fix
    row["alt"] = " ".join(alt.split())
    row["from"] = " / ".join(lines(frm))
    row["to"] = " / ".join(lines(to))
    return row


# ---------- 串 → 门 / 来向 / 门后点 ----------
def _alternatives(cell):
    """一格里的多条串拆开。分隔符有三种：「/」（括号里的不算：`MARCO… (South / West Bound)`）、换行、
    以及串中间的「…」——AIRAC 2610 起 VATJPN 有时把两条串直接连写：`…BOTAN Y403 KABIL… MYE BOTAN KABIL…`
    （「…」结束前一条）、`…Y753 IZUMI …BOTAN KABIL…`（「…」开始下一条）。"""
    out, cur, depth = [], "", 0
    for ch in cell:
        depth += (ch == "(") - (ch == ")")
        if (ch == "/" and depth <= 0) or ch == "\n":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    parts = [p for s in out + [cur]
             for p in re.split(r"(?<=…)\s+(?=[^\s(])|\s+(?=…\S)", s.strip().replace("...", "…"))]
    return [a for a in (s.strip() for s in parts) if a]


def _tokens(s, warn, where):
    toks = []
    for t in s.split():
        t = t.strip("…・.").strip()
        if not t:
            continue
        if _POINT.match(t) or _AIRWAY.match(t):
            toks.append(t)
        else:
            warn.append("%s：解析不了的词「%s」（%s）" % (where, t, s))
    return toks


def arrival_tokens(alt, warn=None, where=""):
    """一条到着串 → (门及门之前的 token, 门后点列, cond)；拆不出门 → None。
    门 = 最后一个航路名之后的点（没有航路名 → 首点）；末尾括号是条件。"""
    warn = [] if warn is None else warn
    cond = None
    m = re.search(r"\(([^)]*)\)\s*$", alt)
    if m:
        cond, alt = m.group(1).strip() or None, alt[:m.start()]
    if "(" in alt:
        warn.append("%s：串中间有括号，已忽略（%s）" % (where, alt))
        alt = re.sub(r"\([^)]*\)", " ", alt)
    toks = _tokens(alt, warn, where)
    awys = [i for i, t in enumerate(toks) if _AIRWAY.match(t)]
    if awys and awys[-1] + 1 >= len(toks):
        warn.append("%s：航路名后面没有点（%s）" % (where, alt))
        return None
    gi = awys[-1] + 1 if awys else 0
    if gi >= len(toks):
        return None
    return toks[:gi + 1], toks[gi + 1:], cond


def parse_arrival(alt, warn, where):
    """一条到着串 → (门, via_fix, via_awy, 门后点列, cond)。"""
    p = arrival_tokens(alt, warn, where)
    if not p:
        return None
    up, post, cond = p
    awys = [i for i, t in enumerate(up) if _AIRWAY.match(t)]
    via_awy = up[awys[-1]] if awys else None
    via_fix = up[awys[-1] - 1] if awys and awys[-1] > 0 and not _AIRWAY.match(up[awys[-1] - 1]) else None
    return up[-1], via_fix, via_awy, post, cond


def match_arrival(entry, route_str, idents):
    """这条航路对应移管表里的哪个到着组 → {route: 命中的那条到着串, rows: 移管行, full: 是否完全匹配}；
    该机场没有移管组 → None；有但对不上 → {}。
    门必须在航路上；然后「门及门之前的 token 全在航路上」（完全匹配）优先，其次命中多的、到着串更具体（更长）的。
    这样同落 IZUMI 的 `…Y753 IZUMI` / `…Y231 MIRIO Y401 KAINA Y753 IZUMI` / `…BOTAN Y403 …` 能分开。
    门之后的点（STAR 机体 / 门后直飞段）不要求在航路上。`idents` = 加密后的航点（沿航路展开的中间点也算）。"""
    groups = (entry or {}).get("arr_xfer") or []
    if not groups:
        return None
    on = set((route_str or "").split()) | set(idents or ())
    best = None
    for k, grp in enumerate(groups):
        for alt in grp.get("routes") or ():
            p = arrival_tokens(alt)
            if not p or p[0][-1] not in on:
                continue
            up = p[0]
            hit = sum(1 for t in up if t in on)
            key = (hit == len(up), hit, len(up), -k)
            if best is None or key > best[0]:
                best = (key, alt, grp)
    if not best:
        return {}
    return {"route": best[1], "rows": best[2].get("rows") or [], "full": best[0][0]}


def departure_heads(cell, warn, where):
    """出発格 → 离场头（每条的首个点名）。去掉「・」、所有括号（(SUTBA DERBY DEPARTURE) / (for …) / (… Bound)）；
    跳过 For All / For Other 这类分组标签与「XXX DEPARTURE」这类程序标题。"""
    out = []
    for alt in _alternatives(cell):
        s = re.sub(r"\([^)]*\)", " ", alt).strip().lstrip("・").strip()
        if not s or s.lower().startswith("for ") or s.upper().endswith("DEPARTURE"):
            continue
        toks = _tokens(s, warn, where)
        if toks and not _AIRWAY.match(toks[0]):
            out.append(toks[0])
    return out


# ---------- 门后点列 → 直飞段 / STAR（按本机 CIFP 现推）----------
class Stars:
    """一个机场的 STAR：衔接点（与 matching_choices / star_connect_points 同源的 `_connect_points`）+ 每条的有序点列。"""

    def __init__(self, icao, dat):
        procs = procedures.enumerate_procedures(icao, dat)["STAR"]
        legs = procedures._parse_legs(icao, dat)["STAR"]
        self.entry, self.paths = {}, {}
        for name, d in procs.items():
            t, b = procedures._connect_points(d, False)
            for fx in set(t.values()) | b:
                self.entry.setdefault(fx, []).append(name)
            g = legs.get(name, {})
            trans = [k for k in g if k and k != "ALL" and not k.startswith("RW")] or [None]
            rws = [k for k in g if k.startswith("RW")] or [None]
            self.paths[name] = [[f for f, _r, _s in (g.get(tr, []) if tr else []) + g.get("", []) + g.get("ALL", [])
                                 + (g.get(rw, []) if rw else [])] for tr in trans for rw in rws]

    def _contains(self, name, rest):
        for p in self.paths.get(name, ()):
            it = iter(p)
            if all(x in it for x in rest):                       # 依次（子序列）包含
                return True
        return False

    def split(self, gate, post):
        """门后点列 → (dct, star)。只凭「门后展开的点」作证据配 STAR：
          · 门后某个点本身是 STAR 衔接点（RJFM `KUE ESKAP KROMA ENBEN MZE` 的 ENBEN）→ 直飞到它为止（含它），
            配在它衔接的 STAR；其中有经过下一个点的就只留这些；
          · STAR 就从门起飞（RJOO AGPUK、RJAH TATSU）→ 还得经过门后的下一个点才算（AGPUK 起飞的三条 STAR 里
            只有 AGPU1R 经过 MIRAI）；对不上就不算——RJCO `RUMOI SPE` 的 SPE 不在 RUMOI 起飞的 STAR 上，SPE 是真直飞；
          · 门后没有点（`…Y30 SWAMP`）→ 无从判断，不配 STAR（面板会按端点自己匹配）。"""
        seq = [gate] + list(post)
        for i, fx in enumerate(seq):
            names = self.entry.get(fx)
            if not names:
                continue
            rest = seq[i + 1:]
            hit = sorted(n for n in names if rest and self._contains(n, rest[:1]))
            if i == 0:
                if hit:
                    return [], hit
                continue
            return list(post[:i]), hit or sorted(names)
        return list(post), []


# ---------- 组装 ----------
def build_airport(icao, ap, stars, warn):
    arr, gates, dct, dep = [], {}, {}, []
    for cell in ap["arr_cells"]:
        for alt in _alternatives(cell):
            p = parse_arrival(alt, warn, icao)
            if not p:
                continue
            gate, via_fix, via_awy, post, cond = p
            d, star = stars.split(gate, post)
            r = {"via_fix": via_fix, "via_awy": via_awy, "dct": d, "star": star, "cond": cond}
            if gate not in arr:
                arr.append(gate)
            if r not in gates.setdefault(gate, []):
                gates[gate].append(r)
            if d and gate not in dct:
                dct[gate] = d
    for cell in ap["dep_cells"]:
        for h in departure_heads(cell, warn, icao):
            if h not in dep:
                dep.append(h)
    out = {"region": ap["region"], "arr": arr}
    if dep:
        out["dep"] = dep
    if dct:
        out["arr_dct"] = dct
    out["arr_gates"] = gates
    if ap.get("arr_groups"):                                   # 移管行（移管点 / 高度 / 移管元 / 移管先）原文
        out["arr_xfer"] = ap["arr_groups"]
    if ap.get("dep_groups"):
        out["dep_xfer"] = ap["dep_groups"]
    return out


def build(page, old, dat, label, source=None):
    """一张表的 HTML → (新 json, 管制区顺序, 表中机场, 跳过的, 保留的人工条目, 警告)。
    `source` = {pub, gid, tab, navdata}：写进 `_vatjpn`，下次启动据此判断要不要重建。"""
    regions, airports = sheet_rows(page)
    warn, skipped, new = [], [], {}
    for icao in sorted(airports):
        v = build_airport(icao, airports[icao], Stars(icao, dat), warn)
        if not v["arr"] and not v.get("dep"):                  # 空行 / 只有「For All」（小场、非管制场）→ 不录
            skipped.append(icao)
            continue
        new[icao] = v
    manual = [k for k, v in old.items() if isinstance(v, dict) and v.get("_source") and k not in new]
    out = {"_comment": _AIRAC.sub(label, old.get("_comment") or "机场进/离场移管点（VATJPN「空港別移管高度表」%s）" % label)}
    if source:
        out["_vatjpn"] = dict(source, schema=SCHEMA)
    out.update(new)
    for k in manual:
        out[k] = old[k]
    return out, regions, airports, skipped, manual, warn


# ---------- 读写 ----------
def json_path():
    return os.path.join(get_real_run_path(), "transfer_points.json")


def load(path):
    """现有 json；缺失 / 损坏 → {}。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def dumps(data):
    return json.dumps(data, ensure_ascii=False, indent=2)        # 与仓库里的文件同格式：indent=2、无结尾换行


def write_text(path, text):
    """原子写（tmp → os.replace）、UTF-8、LF。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _table_airports(d):
    """VATJPN 表里来的机场（不含 `_` 开头的元数据与 `_source` 人工条目）。"""
    return [k for k, v in d.items() if not k.startswith("_") and isinstance(v, dict) and not v.get("_source")]


def changed_airports(old, new):
    keys = {k for k in list(old) + list(new) if not k.startswith("_")}
    return sorted(k for k in keys if old.get(k) != new.get(k))


# ---------- 主程序启动时 ----------
def _stale_notice(local_cycle, pick, newest):
    """本机导航数据比 VATJPN 最新一期旧、且那一期已经生效 → 提醒文案；否则 None。
    「已经生效」这一条是防 VATJPN 提前发布下期表：那时 Navigraph 多半还没出数据，提醒了用户也无从升级。"""
    try:
        local = int(local_cycle)
    except (TypeError, ValueError):
        return None
    top = tab_airac(newest[0])
    if top is None or top <= local:
        return None
    start = airac_start("%04d" % top)
    if not start or start > datetime.date.today():
        return None
    return ("本机导航数据（AIRAC %s）与最新的 VATJPN 移管数据表（AIRAC %04d）不匹配，请升级导航数据到最新版本。\n\n"
            "本次先用 VATJPN 的 %s 移管表（与本机导航数据相符的最新一期）。\n"
            "请前往 https://navigraph.com/downloads 下载「X-Plane 12」导航数据，"
            "替换程序目录下 NavData 文件夹里的数据后重启即可。") % (local_cycle, top, tab_label(*pick))


def refresh(dat, local_cycle, path=None, timeout=_TIMEOUT):
    """启动时调（后台线程）：按本机导航数据选期，VATJPN 期次 / 本机 AIRAC / 发布 ID 与 `_vatjpn` 记录不一致才重建并写回。
    **绝不抛异常**——任何失败都沿用现有文件。
    返回 {status: fresh|updated|offline|rejected|error, data: 新表（仅 updated；写盘失败也照给）, notice: 提醒文案 | None}。"""
    res = {"status": "error", "data": None, "notice": None}
    try:
        path = path or json_path()
        old = load(path)
        rec = old.get("_vatjpn") if isinstance(old.get("_vatjpn"), dict) else {}
        pub = rec.get("pub") or PUB_ID
        try:
            tabs = list_tabs(pub, timeout)
        except Exception as e:                                  # noqa: BLE001  断网 / 超时 / 被拒
            print("ℹ️ 暂时连不上 VATJPN 移管表（%s），沿用现有数据。" % type(e).__name__)
            res["status"] = "offline"
            return res
        pick, newest, warn = pick_tab(tabs, local_cycle)
        if not pick:
            print("⚠️ VATJPN 移管表发布页里没找到标签页（页面格式可能变了），沿用现有数据。")
            return res
        if warn:
            print("⚠️ " + warn)
        name, gid = pick
        label = tab_label(name, gid)
        res["notice"] = _stale_notice(local_cycle, pick, newest)
        if res["notice"]:
            print("⚠️ " + res["notice"].replace("\n\n", "\n").replace("\n", "\n   "))
        if (rec.get("pub") == pub and rec.get("gid") == gid and rec.get("navdata") == local_cycle
                and rec.get("schema") == SCHEMA and _table_airports(old)):
            print("✅ 移管表已是最新（VATJPN %s）。" % label)
            res["status"] = "fresh"
            return res

        try:
            page = _get(sheet_url(pub, gid), timeout)
        except Exception as e:                                  # noqa: BLE001
            print("ℹ️ 暂时读不到 VATJPN 移管表（%s），沿用现有数据。" % type(e).__name__)
            res["status"] = "offline"
            return res
        try:
            new = build(page, old, dat, label, {"pub": pub, "gid": gid, "tab": name, "navdata": local_cycle})[0]
        except Exception as e:                                  # noqa: BLE001  表头 / 列认不出来
            print("⚠️ VATJPN 移管表的格式可能变了（解析失败：%s），本次未更新，沿用现有数据。" % type(e).__name__)
            res["status"] = "rejected"
            return res
        n_old, n_new = len(_table_airports(old)), len(_table_airports(new))
        if n_new < max(_MIN_AIRPORTS, _MIN_KEEP * n_old):
            print("⚠️ VATJPN 移管表的格式可能变了（解析出 %d 个机场，原有 %d 个），本次未更新，沿用现有数据。" % (n_new, n_old))
            res["status"] = "rejected"
            return res
        res["data"], res["status"] = new, "updated"
        changed = changed_airports(old, new)
        was = rec.get("tab")
        what = "VATJPN %s → %s" % (was, name) if was and was != name else "VATJPN %s" % name
        try:
            write_text(path, dumps(new))
            print("🔄 移管表已重建（%s · STAR 按本机导航数据 AIRAC %s 配对；%s）。"
                  % (what, local_cycle or "?", "%d 个机场有变化" % len(changed) if changed else "内容无变化"))
        except OSError as e:
            print("⚠️ 移管表已重建，但写不进程序目录（%s），本次只在内存里使用。" % type(e).__name__)
        return res
    except Exception as e:                                      # noqa: BLE001  解析 / 构建出错 → 沿用现有
        print("⚠️ 移管表更新失败（%s），沿用现有数据。" % type(e).__name__)
        res["status"], res["data"] = "error", None
        return res
