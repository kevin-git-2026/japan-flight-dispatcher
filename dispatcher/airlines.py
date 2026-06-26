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

from .config import get_real_run_path

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
