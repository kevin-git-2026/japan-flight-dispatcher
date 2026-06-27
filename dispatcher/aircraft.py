# ================= 机型库（SimBrief 机型参数）=================
# 读取运行目录的 aircrafts.json（从 SimBrief 抓取、精简剥隐私后的机型库），
# 提供：机型查找（用户输入 ICAO/简写/名字 → SimBrief aircraft_id）+ GUI 下拉选项。
# 纯标准库；文件缺失/损坏一律降级为空库（机型查找回退为原样透传），不崩。

import os
import re
import json

from .config import get_real_run_path

_DB = None          # [{"id","icao","name","engines","pax","cargo","search"}, ...]
_BY_CODE = None     # {ICAO或id(大写): id}

# 常见简写消歧：裸数字/口语简写易匹配到老型号或多型，这里给最常见默认（值为 SimBrief id；
# 命中后仍校验该 id 在库内，不在则继续模糊匹配）。精确 ICAO/id 在查表前已 cover。
_ALIAS = {
    "737": "B738", "739": "B739", "73g": "B737", "max": "B38M", "max8": "B38M", "737max": "B38M",
    "320": "A320", "319": "A319", "318": "A318", "neo": "A20N", "320neo": "A20N", "321neo": "A21N",
    "777": "B77W", "773": "B77W", "787": "B789", "767": "B763", "757": "B752", "747": "B748",
    "350": "A359", "351": "A35K", "380": "A388", "330": "A333", "340": "A343", "220": "BCS3",
    "q400": "DH8D", "dash8": "DH8D", "dhc8": "DH8D", "crj": "CRJ7", "crj700": "CRJ7", "crj900": "CRJ9",
    "atr": "AT76", "atr72": "AT76", "e170": "E170", "e190": "E190",
}


def _path():
    return os.path.join(get_real_run_path(), "aircrafts.json")


def load_aircraft_db():
    """加载机型库（缓存）。文件缺失/损坏 → 空库（不抛）。"""
    global _DB, _BY_CODE
    if _DB is not None:
        return _DB
    db = []
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            db = [a for a in raw if a.get("id")]
    except Exception:
        db = []
    _DB = db
    _BY_CODE = {}
    for a in db:
        for k in (a.get("id"), a.get("icao")):
            if k:
                _BY_CODE.setdefault(k.upper(), a["id"])   # 同 ICAO 多型时，先入库的(客机)优先
    return db


def find_aircraft_id(query):
    """用户输入(ICAO / SimBrief id / 数字简写 / 名字片段) → SimBrief aircraft_id；查不到返回 None。"""
    load_aircraft_db()
    if not _DB or not query:
        return None
    q = str(query).strip()
    if not q:
        return None
    qu = q.upper()
    if qu in _BY_CODE:                                     # 1) 精确 id / icao
        return _BY_CODE[qu]
    al = _ALIAS.get(q.lower().replace(" ", "").replace("-", ""))
    if al and al.upper() in _BY_CODE:                      # 2) 常见简写消歧（裸数字易撞老型号）
        return _BY_CODE[al.upper()]
    ql = q.lower()
    for a in _DB:                                          # 2) 名字 / 搜索串包含
        if ql in (a.get("name") or "").lower() or ql in (a.get("search") or ""):
            return a["id"]
    digits = re.sub(r"\D", "", q)                          # 3) 数字简写(737/320)：匹配 icao 含该数字
    if len(digits) >= 3:
        for a in _DB:
            if digits in (a.get("icao") or ""):
                return a["id"]
    return None


def aircraft_choices():
    """供 GUI 下拉/搜索：[(显示串, id, 搜索blob小写), ...]，按库内顺序。
    显示串形如 'A320 — A320-200'（货机加 [货]）；搜索 blob 含 icao/name/厂商别名，供输入过滤。"""
    load_aircraft_db()
    out = []
    for a in _DB:
        label = "%s — %s" % (a.get("icao") or a.get("id"), a.get("name") or "")
        if a.get("cargo"):
            label += " [货]"
        blob = (label + " " + (a.get("search") or "")).lower()
        out.append((label, a["id"], blob))
    return out
