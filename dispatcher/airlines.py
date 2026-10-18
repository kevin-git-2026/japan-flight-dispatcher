# ================= 航司运营网络（模拟呼号航司选择）=================
# 当 FlightAware 查不到任何真实排班、需要降级生成"模拟呼号"时，用航司运营网络挑一个
# 网络上合理覆盖该航线两端的航司，避免"北海道的 AIR DO 飞福冈-冲绳"这类离谱组合。
#
# ★ 数据与逻辑分离：航司/机场区域数据放在运行目录的 airlines.json（首次运行自动生成），
#   本模块只负责"读取数据文件 + 分配航司"。要新增/删除/修改航司（如新开的航司、VATSIM 虚航），
#   或调整机场区域，直接编辑 airlines.json 即可，无需改代码、无需重新打包。
#   文件损坏或缺字段时自动回退到下面的内置默认（_DEFAULT_DATA），保证程序永远能跑。
#
# ★ 数据结构（便于增删改查）：
#   - airlines 是"以航司 ICAO 为键的对象"：增=加键、删=删键、改=改 regions、查=按键直取，键唯一防重复。
#   - 每家航司 regions 是大区列表；全国干线用 ["ALL"]。
#   - airport_regions 把机场 ICAO 前缀映射到大区，少数歧义机场用 overrides 覆盖。

import os
import json
import random
from types import SimpleNamespace

from . import regions
from .config import get_real_run_path
from .routing import calculate_distance_nm

_DATA_FILENAME = "airlines.json"
_NATIONWIDE_TAG = "ALL"          # regions 含此标签 = 全国干线网络，处处可飞
_FALLBACK_NATIONWIDE = ["ANA", "JAL"]   # 极端兜底（数据里啥都没有时）

# 内置默认数据：首次运行写出此文件供用户编辑；文件损坏/缺字段时也回退到它。
_DEFAULT_DATA = {
    "_comment": (
        "航司运营网络：FlightAware 无真实排班时，按航线两端所在大区挑选合理航司生成模拟呼号。"
        "可自行增/删/改：airlines 下每个键是航司 ICAO 代码，regions 是其运营大区列表"
        "（全国干线用 [\"ALL\"]）。可用大区："
        "HOKKAIDO / TOHOKU / KANTO / CHUBU / KANSAI / CHUGOKU_SHIKOKU / KYUSHU / OKINAWA。"
        "airport_regions.by_prefix 是机场 ICAO 前缀→大区，overrides 覆盖个别歧义机场。"
    ),
    "airport_regions": {
        "by_prefix": {
            "RJC": "HOKKAIDO",                        # 北海道主群（新千岁RJCC/函馆RJCH/钏路RJCK/带广RJCB/女满别RJCM/稚内RJCW…）
            "RJE": "HOKKAIDO",                        # 北海道离岛/北部（利尻RJER/纹别RJEB/旭川RJEC）—— 同属北海道
            "RJS": "TOHOKU",                          # 东北+新潟（仙台RJSS/秋田RJSK/青森RJSA/山形RJSC/福岛RJSF/庄内RJSY/新潟RJSN）
            "RJA": "KANTO", "RJT": "KANTO",           # 关东/东京（成田RJAA/羽田RJTT/百里RJAH/大岛RJTO）
            "RJN": "CHUBU", "RJG": "CHUBU",           # 中部（中部国际RJGG/小松RJNK/富山RJNT/静冈RJNS/隐岐RJNO/小牧RJNA）
            "RJB": "KANSAI",                          # 关西（关西国际RJBB/神户RJBE/南纪白滨RJBD）
            "RJO": "CHUGOKU_SHIKOKU",                 # 中国/四国（广岛RJOA/冈山RJOB/出云RJOC/米子RJOH/松山RJOM/高松RJOT/德岛RJOS/高知RJOK）
            "RJF": "KYUSHU", "RJD": "KYUSHU",         # 九州（福冈RJFF/北九州RJFR/熊本RJFT/长崎RJFU/宫崎RJFM/鹿儿岛RJFK/大分RJFO/佐贺RJFS/壹岐RJDB）
            "RO": "OKINAWA"                           # 冲绳/琉球（那霸ROAH/石垣ROIG/宫古ROMY/久米岛ROKJ…）2 字前缀
        },
        "overrides": {
            "RJOO": "KANSAI"                          # 大阪伊丹（RJO 前缀里唯一属关西）
        }
    },
    "airlines": {
        "ANA": {"name": "All Nippon Airways 全日空", "regions": ["ALL"]},
        "JAL": {"name": "Japan Airlines 日本航空", "regions": ["ALL"]},
        "ADO": {"name": "AIR DO 北海道国际航空", "regions": ["HOKKAIDO", "KANTO"]},
        "SNJ": {"name": "Solaseed Air ソラシドエア", "regions": ["KYUSHU", "OKINAWA", "KANTO", "CHUBU", "KANSAI"]},
        "SFJ": {"name": "StarFlyer スターフライヤー", "regions": ["KYUSHU", "KANTO", "CHUBU", "KANSAI"]},
        "SKY": {"name": "Skymark Airlines スカイマーク", "regions": ["KANTO", "HOKKAIDO", "KYUSHU", "OKINAWA", "KANSAI", "CHUBU"]},
        "APJ": {"name": "Peach Aviation ピーチ", "regions": ["KANSAI", "KANTO", "HOKKAIDO", "KYUSHU", "OKINAWA", "TOHOKU", "CHUBU"]},
        "JJP": {"name": "Jetstar Japan ジェットスター", "regions": ["KANTO", "KANSAI", "HOKKAIDO", "KYUSHU", "OKINAWA", "CHUBU"]}
    }
}


def _data_path():
    """airlines.json 路径，锚定到运行目录（与 installed_scenery.json 等同级）。"""
    return os.path.join(get_real_run_path(), _DATA_FILENAME)


def _normalize(raw):
    """把原始 JSON 规范化为 (airlines, by_prefix, overrides)：
    airlines: dict{ICAO(大写) -> set(大区大写)}；by_prefix/overrides: dict{大写 -> 大区大写}。
    脏数据(类型不对/空值)直接跳过，不抛异常。"""
    raw = raw if isinstance(raw, dict) else {}
    reg = raw.get("airport_regions") or {}
    by_prefix = {str(k).upper(): str(v).upper() for k, v in (reg.get("by_prefix") or {}).items()}
    overrides = {str(k).upper(): str(v).upper() for k, v in (reg.get("overrides") or {}).items()}
    airlines = {}
    for code, info in (raw.get("airlines") or {}).items():
        if not isinstance(info, dict):
            continue
        regs = info.get("regions")
        if isinstance(regs, str):            # 容错：regions 写成字符串也接受
            regs = [regs]
        regs = {str(r).upper() for r in (regs or []) if str(r).strip()}
        if code and regs:
            airlines[str(code).upper()] = regs
    return airlines, by_prefix, overrides


# 内置默认规范化一份，作为各段缺失时的兜底
_DEF_AIRLINES, _DEF_PREFIX, _DEF_OVERRIDES = _normalize(_DEFAULT_DATA)


def _load_airline_data():
    """读取运行目录的 airlines.json；缺失则写出默认文件供编辑；损坏/缺段则回退内置默认。
    返回 (airlines, by_prefix, overrides)。"""
    path = _data_path()
    raw = None
    created = False
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            print(f"⚠️ 航司数据文件 {_DATA_FILENAME} 解析失败，本次改用内置默认航司规则。")
            raw = None
    else:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_DEFAULT_DATA, f, ensure_ascii=False, indent=2)
            created = True
        except Exception:
            pass

    airlines, by_prefix, overrides = _normalize(raw)
    if not airlines:                          # 航司段缺失/全脏 → 回退默认航司
        airlines = dict(_DEF_AIRLINES)
    if not by_prefix:                         # 区域段缺失 → 回退默认区域映射（含 overrides）
        by_prefix, overrides = dict(_DEF_PREFIX), dict(_DEF_OVERRIDES)

    if created:
        print(f"🛫 已生成航司规则文件 {_DATA_FILENAME}（无真实排班时按航线挑合理航司，可自行增删改）。")
    return airlines, by_prefix, overrides


# 进程内缓存（启动加载一次；编辑文件后需重启生效）
_cache = None


def _get_data():
    global _cache
    if _cache is None:
        _cache = _load_airline_data()
    return _cache


def init_airline_data():
    """启动时调用一次：加载（首次运行时生成）airlines.json，返回航司数量。"""
    airlines, _, _ = _get_data()
    return len(airlines)


def airport_region(icao):
    """把机场 ICAO 映射到地理大区：先查 override，再试 3 字前缀(RJC/RJE/RJF…)，再试 2 字前缀(RO→冲绳)。
    识别不了返回 None。"""
    _, by_prefix, overrides = _get_data()
    if not icao:
        return None
    code = icao.upper()
    if code in overrides:
        return overrides[code]
    if code[:3] in by_prefix:
        return by_prefix[code[:3]]
    if code[:2] in by_prefix:
        return by_prefix[code[:2]]
    return None


# ---- F29：挑一个【不与现实航班号重合】的模拟呼号 ----
#
# 为什么必须核验：模拟呼号原本是 `code + randint(11, 899)`，零核验。实测 11–899 这一段
# 【几乎被真实航班号织满】（ANA 抽样约 90–100% 是真号），也就是说过去生成的呼号几乎每个都在
# 冒充真实航班——连飞时会干扰到正挂着该号飞的人。而且【不存在"安全号段"】（连 9000–9999 都有
# 50% 被占），纯离线的号段启发式解决不了，只能逐个联网核验。
#
# 为什么坚持短号：4 位号连飞时管制不好记、机组不好读，所以短号段要【搜够】再放宽（用户明确要求）。
# 实测各航司短号空号率差异极大且方向相反（ANA 空在低位、JAL 空在高位），故【不做子号段偏置】
# ——样本量不足以支撑，随机无放回撒 + 足够预算即可。
#
# 网络在调用方：本模块保持纯计算，谓词由 planner 注入（同 routing.get_random_route(accept_fn=…) 的范式）。

_SHORT_LO, _SHORT_HI = 11, 899        # 2–3 位（优先，管制/机组友好）
_WIDE_LO, _WIDE_HI = 1000, 9999       # 4 位（仅短号段搜尽后的兜底）
_SHORT_TRIES = 18                     # 短号段预算：给足（实测 ANA 期望约 10 次）才谈得上"不轻易放宽"
_WIDE_TRIES = 8
_UNKNOWN_GIVEUP = 3                   # 连续这么多次"判不了"→ 认定限流/离线，立刻停手，别空转


# ---- F30（v2.0.1_hotfix3）：模拟呼号「像真的」——按日本航司的航班号范式排候选 ----
#
# 实测范式（1,628 个真实航班，详见 .claude/skills/route_planning/reference/flight_numbering.md）：
#   ① 奇偶 = 走向：从「上位城市」出发为奇数（下り）、飞往上位城市为偶数（上り），跨航司几乎 100% 成立。
#      上位 = 四个始发枢纽 东京 > 大阪 > 名古屋 > 福冈；其余机场（含札幌、那霸）按离东京远近，远端为「下」。
#      航司例外：Peach 以大本营关西为上位；天马的神户不按枢纽算。全量 1,978 个号校验见 tools/build_flight_blocks.py。
#   ② 每条航线占一个号段、按起飞时刻递增；JAL 等的百位数还编码地区（5xx 北海道、9xx 冲绳…）。
#   ③ 空号藏在号段之间、甚至号段内部的空洞里：ANA 宫崎段 602–618 内的 606/607/611/615/616 都是空号。
# 做法（用户提出的「锁定百位，十位个位自由变、个位奇偶由走向定」）：锁定该航司该类航线的「对口百位」，
#   奇偶按走向，再【精确】扣掉 flight_blocks.json 里实测出现过的号码——不能按区间扣，否则③里的空洞会被一并扔掉。
#   剩下的小集合（通常 9–20 个）在 18 次预算内几乎能穷举：ANA 6xx 奇数剩 9 个、其中 3 个空号（33%，盲抽约 5%）。
# 这里只决定【先查哪些号】：是否「已核验」仍只认 exists_fn（FlightAware）的结论——可信点只有一个。

_BLOCKS_FILENAME = "flight_blocks.json"
# 奇偶用的「上位」等级：只有四个始发枢纽城市（从它们出发 = 奇数）；其余机场——【含札幌、那霸】——一律按
# 离东京远近：远的一端是「下」，飞往它为奇数（实测仙台/广岛/松山→札幌、熊本/松山→那霸都是奇数，
# 札幌、那霸是线路的远端而不是枢纽；把它们排进上位表曾让全量校验掉到 94.9%）。
_CITY_RANK = {"RJTT": 0, "RJAA": 0, "RJAH": 0,          # 东京（羽田 / 成田 / 茨城——天马把茨城当东京）
              "RJOO": 1, "RJBB": 1, "RJBE": 1,          # 大阪（伊丹 / 关西 / 神户）
              "RJGG": 2, "RJNA": 2,                     # 名古屋（中部 / 小牧）
              "RJFF": 3}                                # 福冈
_OTHER_RANK = 9
_AIRLINE_RANK_OVERRIDE = {"APJ": {"RJBB": -1},          # Peach 以大本营关西为上位（关西→成田为奇数）
                          "SKY": {"RJBE": _OTHER_RANK}}  # 天马的神户不按枢纽算（仙台→神户为奇数）
# 城市类别：供「对口百位」按（上位端城市, 下位端地区）匹配同类航线用，与奇偶等级无关——札幌、那霸仍各成一类
_CITY_CLASS_OF = {"RJTT": "TYO", "RJAA": "TYO", "RJAH": "TYO", "RJOO": "OSA", "RJBB": "OSA", "RJBE": "OSA",
                  "RJGG": "NGO", "RJNA": "NGO", "RJFF": "FUK", "RJCC": "SPK", "ROAH": "OKA"}
_CLASS_ZH = {"TYO": "东京", "OSA": "大阪", "NGO": "名古屋", "FUK": "福冈", "SPK": "札幌", "OKA": "那霸"}
_TOKYO = SimpleNamespace(lat_dd=35.5523, lon_dd=139.7798)   # 羽田：等级相同时按离东京远近定上下位
_STYLE_LO, _STYLE_HI = 10, 999                          # 按范式选号时用全部 2–3 位号（9xx 冲绳线等也是真实惯用段）
_MIN_HUNDRED_USE, _MIN_HUNDRED_SHARE = 3, 0.10          # 同类航线在某百位里至少用了这么多号，才算「对口百位」
# 在一个锁定百位里连续撞上这么多个真号 → 换下一段。取 5 不取 3（用户定）：3 太容易漏掉小段里的空号——
# ANA 6xx 奇数 9 个候选里有 3 个空号，前 3 个全是真号的概率 24%、前 5 个才 4.8%；真塞满的段最多白查 5 次就离开。
_GROUP_GIVEUP = 5
_BLOCKS = None                                          # flight_blocks.json 懒加载缓存


def _rank(code, icao):
    over = _AIRLINE_RANK_OVERRIDE.get((code or "").upper(), {})
    icao = (icao or "").upper()
    return over.get(icao, _CITY_RANK.get(icao, _OTHER_RANK))


def _city_class(icao):
    """机场所在城市类别（TYO/OSA/NGO/FUK/SPK/OKA，其它 None）。按机场本身定，不受航司覆盖影响
    （Peach 把关西排到东京之上，但关西仍属大阪）。"""
    return _CITY_CLASS_OF.get((icao or "").upper())


def senior_junior(code, dep, arr):
    """→ (上位端, 下位端)：城市等级高者为上位；等级相同按离东京远近（近者为上位）；仍分不出 → None。
    dep/arr 需有 code / lat_dd / lon_dd（即 model.Airport）。"""
    rd, ra = _rank(code, dep.code), _rank(code, arr.code)
    if rd != ra:
        return (dep, arr) if rd < ra else (arr, dep)
    try:
        dd, da = calculate_distance_nm(_TOKYO, dep), calculate_distance_nm(_TOKYO, arr)
    except Exception:                                   # noqa: BLE001
        return None
    if abs(dd - da) < 1:
        return None
    return (dep, arr) if dd < da else (arr, dep)


def direction_parity(code, dep, arr):
    """走向 → 航班号奇偶：从上位城市出发 = 1（奇数，下り），飞往上位城市 = 0（偶数，上り），判不了 = None。"""
    sj = senior_junior(code, dep, arr)
    return None if sj is None else (1 if sj[0] is dep else 0)


def _blocks():
    """读运行目录的 flight_blocks.json（随包静态数据，tools/build_flight_blocks.py 生成）→
    {航司: {"numbers": 全部实测号码, "routes": [(上位端类别, 下位端地区, 该航线号码), …]}}。缺失 / 损坏 → {}。"""
    global _BLOCKS
    if _BLOCKS is None:
        out = {}
        try:
            with open(os.path.join(get_real_run_path(), _BLOCKS_FILENAME), "r", encoding="utf-8") as f:
                raw = json.load(f)
            for r in raw.get("routes") or []:
                code = str(r.get("airline") or "").upper()
                raw_nums = (r.get("down") or []) + (r.get("up") or []) + (r.get("numbers") or [])  # 新格式分方向
                nums = frozenset(n for n in raw_nums
                                 if isinstance(n, int) and not isinstance(n, bool) and 0 < n < 10000)
                if code and nums:
                    a = out.setdefault(code, {"numbers": set(), "routes": []})
                    a["numbers"] |= nums
                    a["routes"].append((r.get("senior_class"), r.get("junior_region"), nums))
        except Exception:                               # noqa: BLE001
            out = {}
        _BLOCKS = out
    return _BLOCKS


def _hundreds_label(h):
    return "10–99" if h == 0 else "%dxx" % (h // 100)


def callsign_style(code, dep, arr):
    """按日本航司的航班号范式，给模拟呼号排好候选顺序（纯计算，不联网，绝不抛异常）。
    → None（无从判断，调用方走旧的均匀抽号）或
      {"parity": 1/0/None, "short": [候选组…], "wide": [候选组…], "why": 一行依据}
    每个候选组由 pick_sim_callsign 组内打乱、按组的先后依次查：
      先是「对口百位」各一组（同类航线用得最多的百位在前），最后一组是该号段里其余号；
      所有组都只含奇偶正确、且不在实测真号里的号——已知的真号一个都不去查。"""
    try:
        return _callsign_style(code, dep, arr)
    except Exception:                                   # noqa: BLE001
        return None


def _callsign_style(code, dep, arr):
    code = (code or "").upper()
    parity = direction_parity(code, dep, arr)
    data = _blocks().get(code)
    if parity is None and not data:
        return None
    known = data["numbers"] if data else frozenset()
    sj = senior_junior(code, dep, arr)
    s_cls = _city_class(sj[0].code) if sj else None
    j_reg = regions.region_of(sj[1].lat_dd, sj[1].lon_dd) if sj else None

    def ok(n):
        return n not in known and (parity is None or n % 2 == parity)

    def groups(lo, hi):
        """→ (候选组列表, 对口百位, 匹配到的层级)。依次放宽：(类别,地区) → (任意,地区) → (类别,任意)。"""
        usage, level = {}, None                         # 百位 → 同类航线在这个百位里用了几个号
        if data:
            for want_s, want_r, lv in ((s_cls, j_reg, "exact"), (None, j_reg, "region"), (s_cls, None, "class")):
                if lv != "exact" and want_s is None and want_r is None:
                    continue
                for rs, rr, nums in data["routes"]:
                    if (want_s is None or rs == want_s) and (want_r is None or rr == want_r):
                        for n in nums:
                            if lo <= n <= hi:
                                usage[n // 100 * 100] = usage.get(n // 100 * 100, 0) + 1
                if usage:
                    level = lv
                    break
        # 用得最多的百位最像真的，排最前；只沾到零星几个号的百位不锁——多半是邻段的尾巴（如 ANA 九州线里
        # 山口宇部段末尾的 700），锁了反而把一整段没把握的号排到真正对口的 6xx 前面。
        top = max(usage.values()) if usage else 0
        locked = []
        for h, cnt in sorted(usage.items(), key=lambda hc: (-hc[1], hc[0])):
            if cnt < max(_MIN_HUNDRED_USE, top * _MIN_HUNDRED_SHARE):
                continue
            cand = [n for n in range(max(h, lo), min(h + 99, hi) + 1) if ok(n)]
            if cand:
                locked.append((h, cand))
        used = {n for _, cand in locked for n in cand}
        rest = [n for n in range(lo, hi + 1) if ok(n) and n not in used]
        return [cand for _, cand in locked] + ([rest] if rest else []), [h for h, _ in locked], level

    short, short_h, level = groups(_STYLE_LO, _STYLE_HI)
    wide, _wide_h, _ = groups(_WIDE_LO, _WIDE_HI)

    parts = []
    if short_h:
        where = {"exact": "%s线" % j_reg if j_reg else "同类航线", "region": "%s线" % j_reg,
                 "class": "%s线" % _CLASS_ZH.get(s_cls, "")}.get(level, "同类航线")
        parts.append("%s惯用 %s 段" % (where, "/".join(_hundreds_label(h) for h in sorted(short_h))))
    if parity is not None:
        parts.append("从上位城市出发用奇数（下り）" if parity else "飞往上位城市用偶数（上り）")
    why = ("🎭 模拟呼号按 %s 编号规律生成：%s。" % (code, "；".join(parts))) if parts else ""
    return {"parity": parity, "short": short, "wide": wide, "why": why}


def pick_sim_callsign(code, exists_fn=None, on_try=None,
                      short_tries=_SHORT_TRIES, wide_tries=_WIDE_TRIES, style=None):
    """生成模拟呼号，尽量不与现实中真实存在的航班号重合。

    exists_fn(callsign) -> True(真号,换) / False(空号,采用) / None(判不了)；不传 = 完全等同旧行为。
    on_try(callsign, i, total) 可选进度回调（UI 打日志用）。
    style（F30，callsign_style 的返回值）：按航班号范式决定【先查哪些号】——候选组依次查、组内打乱；
      在一组里连续撞上 _GROUP_GIVEUP 个真号就换下一组（最后一组不放弃）。不传 = 旧的均匀随机抽号。
    返回 (callsign, verified)：verified=True 仅当【确实核验过且确认是空号】——判不了/搜不到时
    照样给号但 verified=False，由调用方如实标注，绝不假装核验过。"""
    code = (code or "").upper()
    if not exists_fn:                                   # 无谓词（离线/测试）→ 旧行为
        return "%s%d" % (code, random.randint(_SHORT_LO, _SHORT_HI)), False

    unknown = 0
    spare = None                                        # F30：预算用完时给出的「像真的」4 位号（未核验）
    for lo, hi, budget, groups in ((_SHORT_LO, _SHORT_HI, short_tries, (style or {}).get("short")),
                                   (_WIDE_LO, _WIDE_HI, wide_tries, (style or {}).get("wide"))):
        if groups:                                      # F30：组间保序、组内打乱（组间不重叠，故无重复）
            order = [random.sample(list(g), len(g)) for g in groups]
        else:                                           # 无 style → 旧的均匀无放回抽号（随机序列与旧版一致）
            order = [random.sample(range(lo, hi + 1), min(budget, hi - lo + 1))]
        total = min(budget, sum(len(g) for g in order))
        probed = []
        for gi, group in enumerate(order):
            streak = 0                                  # 本组连续撞上真号的次数
            for n in group:
                if len(probed) >= budget:
                    break
                probed.append(n)
                cs = "%s%d" % (code, n)
                if on_try:
                    on_try(cs, len(probed), total)
                verdict = exists_fn(cs)
                if verdict is False:
                    return cs, True                      # 空号 —— 采用
                if verdict is None:                      # 判不了：既不算撞号、也不清零连撞
                    unknown += 1
                    if unknown >= _UNKNOWN_GIVEUP:       # 限流/断网 → 别空转，给号并如实标注未核验
                        return cs, False
                    continue
                unknown = 0
                streak += 1
                if groups and streak >= _GROUP_GIVEUP and gi < len(order) - 1:
                    break                                # F30：这一段多半塞满了 → 换下一段
            if len(probed) >= budget:
                break
        if groups and lo == _WIDE_LO:                   # 备一个没查过的、最像真的 4 位号（取最前面那组）
            seen = set(probed)
            spare = next((random.choice(rest) for rest in ([n for n in g if n not in seen] for g in groups)
                          if rest), None)
        if lo == _SHORT_LO:                              # 短号段搜尽仍无空号 → 才放宽到 4 位
            print("ℹ️ %s 的 2–3 位号段已被现实航班号占满，改用 4 位号（仍会避开真实航班号）。" % code)
    if spare is not None:                               # F30：仍按范式给一个像真的 4 位号（未核验）
        return "%s%d" % (code, spare), False
    return "%s%d" % (code, random.randint(_WIDE_LO, _WIDE_HI)), False


def pick_sim_airline(dep_icao, arr_icao):
    """按航线两端所在大区，随机挑一个"网络同时覆盖两端"的航司，避免离谱组合。
    全国干线(regions 含 "ALL")始终是候选；任一端区域识别失败/无合适航司则回退到任一已知航司。"""
    airlines, _, _ = _get_data()
    rd, ra = airport_region(dep_icao), airport_region(arr_icao)
    candidates = []
    for code, regs in airlines.items():
        if _NATIONWIDE_TAG in regs:
            candidates.append(code)                       # 全国网络：总是候选
        elif rd and ra and rd in regs and ra in regs:
            candidates.append(code)                       # 区域航司：需同时覆盖两端
    if not candidates:
        candidates = list(airlines.keys()) or _FALLBACK_NATIONWIDE
    return random.choice(candidates)
