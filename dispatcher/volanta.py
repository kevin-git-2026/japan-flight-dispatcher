# ================= Volanta 已飞航线读取模块 (F11) =================
# 获取用户在 Volanta(fly.volanta.app)记录的已飞航线，供随机规划按已飞次数加权（优先未飞航线）。
# 主路径：用浏览器里 Volanta 的登录会话(本机 localStorage 里的 Orbx token)直接调 /api/v1/Flights，
#   一次拿到全部航班，解析出起降对后并入【单一数据文件 volanta_data.json】——完整、准确、高效。
#   （已移除早期"对 IndexedDB 全盘正则扫描提取航线"的脆弱做法：V8 去重 + 网页懒加载导致它常不完整。
#     `_read_leveldb_text` 予以保留，现仅用于读 localStorage leveldb 以取出登录令牌。）
# 所有 Volanta 持久化数据(同步偏好 + 已飞累积库 + 最后拉取时间)统一存 volanta_data.json，
#   取代早期分散的 volanta_config.txt / volanta_flown.json / volanta_flights.json(首次运行自动迁移合并)。
# 纯标准库实现；任何环节失败都优雅降级为「不启用」，绝不影响主流程。

import os
import re
import csv
import json
import time
import urllib.request

from .config import get_real_run_path


def _read_leveldb_text(db_dir):
    """读取一个 LevelDB 目录下所有 .log/.ldb 文件，拼接为 latin1 字符串。
    浏览器运行时文件可能被占用：优先共享读(Windows 上 CPython 默认允许)，
    失败则复制到临时文件再读。"""
    parts = []
    try:
        names = os.listdir(db_dir)
    except Exception:
        return ""
    for name in names:
        if not (name.endswith(".log") or name.endswith(".ldb")):
            continue
        path = os.path.join(db_dir, name)
        data = None
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            # 兜底：被独占锁定时，复制一份再读
            try:
                import tempfile, shutil
                tmp = os.path.join(tempfile.gettempdir(), "volanta_tmp_" + name)
                shutil.copyfile(path, tmp)
                with open(tmp, "rb") as f:
                    data = f.read()
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            except Exception:
                data = None
        if data:
            parts.append(data.decode("latin1", errors="ignore"))
    return "".join(parts)


# ---- 统一数据文件 volanta_data.json：偏好 + 已飞累积库 + 最后拉取时间，三合一 ----
# 结构：{"preference": "auto"|"ask", "fetched_at": <unix秒>, "flown": {"DEP|ARR": 次数, ...}}
#   - flown 只增不减(航线键取并集)，是加权抽线的依据；
#   - fetched_at 记最后一次成功调 API 的时间(供 skip_if_fresh 与「数据更新于」显示)；
#   - 不再在磁盘上保留 /api/v1/Flights 的原始大响应(~900KB 的 summarisedPositions 用不到)。

_DATA_FILENAME = "volanta_data.json"
_LEGACY_FILES = ("volanta_config.txt", "volanta_flown.json", "volanta_flights.json")


def _data_path():
    """volanta_data.json 路径，锚定到运行目录。"""
    return os.path.join(get_real_run_path(), _DATA_FILENAME)


def _flown_from_jsonable(obj):
    """把 {"DEP|ARR": n} 还原为 {(dep,arr): n}，跳过脏数据。"""
    out = {}
    for k, v in (obj or {}).items():
        p = str(k).split("|")
        if len(p) == 2 and isinstance(v, int) and v > 0:
            out[(p[0].upper(), p[1].upper())] = v
    return out


def _merge_authoritative(store, auth):
    """并集；auth 有的航线用 auth 的(权威/准确)次数，其余保留 store 的。航线键绝不丢失。"""
    return {k: (auth[k] if k in auth else store.get(k, 0)) for k in (set(store) | set(auth))}


def _merge_max(store, other):
    """并集；非权威来源(CSV/旧库)只用 max 抬高次数，不下调、不丢键。"""
    return {k: max(store.get(k, 0), other.get(k, 0)) for k in (set(store) | set(other))}


def _read_legacy_data():
    """从旧的三个分散文件读出 (data, had_legacy)：volanta_config.txt→偏好、volanta_flown.json→累积库、
    volanta_flights.json→权威次数 + fetched_at。供 volanta_data.json 不存在时首次迁移合并。"""
    run = get_real_run_path()
    had = False
    pref = "ask"
    try:
        with open(os.path.join(run, "volanta_config.txt"), "r", encoding="utf-8") as f:
            had = True
            if f.read().strip().lower() == "auto":
                pref = "auto"
    except Exception:
        pass
    flown = {}
    try:
        with open(os.path.join(run, "volanta_flown.json"), "r", encoding="utf-8") as f:
            flown = _flown_from_jsonable(json.load(f))
            had = True
    except Exception:
        pass
    fetched = 0.0
    fj = os.path.join(run, "volanta_flights.json")
    if os.path.exists(fj):
        had = True
        auth = _load_volanta_json(fj)
        if auth:
            flown = _merge_authoritative(flown, auth)
        try:
            fetched = os.path.getmtime(fj)
        except Exception:
            pass
    return {"preference": pref, "fetched_at": fetched, "flown": flown}, had


def _normalize_data(raw):
    """把 volanta_data.json 原始内容规范化为 {preference, fetched_at, flown(元组键)}，全程容错。"""
    raw = raw if isinstance(raw, dict) else {}
    pref = "auto" if str(raw.get("preference", "")).strip().lower() == "auto" else "ask"
    try:
        fetched = float(raw.get("fetched_at") or 0)
    except Exception:
        fetched = 0.0
    return {"preference": pref, "fetched_at": fetched, "flown": _flown_from_jsonable(raw.get("flown"))}


def _save_data(data):
    """原子写 volanta_data.json(先写 .tmp 再 replace，避免坏写损坏累积库)。成功返回 True。"""
    try:
        out = {
            "preference": data.get("preference", "ask"),
            "fetched_at": float(data.get("fetched_at") or 0),
            "flown": {f"{d}|{a}": int(c) for (d, a), c in (data.get("flown") or {}).items()},
        }
        run = get_real_run_path()
        tmp = os.path.join(run, _DATA_FILENAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, _data_path())
        return True
    except Exception:
        return False


def _load_data():
    """读取 volanta_data.json → {preference, fetched_at, flown}。文件不存在时：
    若有旧的分散文件则迁移合并、删旧文件(只发生一次)；否则返回内存默认(不建文件，避免给纯非用户留垃圾)。"""
    path = _data_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _normalize_data(json.load(f))
        except Exception:
            return {"preference": "ask", "fetched_at": 0.0, "flown": {}}
    data, had_legacy = _read_legacy_data()
    if had_legacy and _save_data(data):
        run = get_real_run_path()
        for fn in _LEGACY_FILES:                 # 数据已并入 volanta_data.json，清掉旧文件去臃肿
            try:
                os.remove(os.path.join(run, fn))
            except Exception:
                pass
        print("🔄 已将旧的 Volanta 数据文件合并为单一的 volanta_data.json。")
    return data


def volanta_auto_enabled():
    """同步偏好是否为「自动」：用户曾选 Y → True(以后启动静默自动同步、不再询问)。
    未设置/为 ask(含旧版日期内容，已在迁移时归一)→ False(每次启动询问一次，不锁死)。"""
    return _load_data()["preference"] == "auto"


def enable_volanta_auto():
    """把同步偏好记为「自动」：用户选 Y 后以后启动自动同步、不再询问(删 volanta_data.json 即恢复每次询问)。"""
    data = _load_data()
    data["preference"] = "auto"
    _save_data(data)


def set_volanta_auto(enabled):
    """设置同步偏好：enabled=True → 'auto'(以后静默自动同步)；False → 'ask'(每次启动询问)。
    供 GUI「自动同步」复选框双向控制(enable_volanta_auto 只能开、不能关)。"""
    data = _load_data()
    data["preference"] = "auto" if enabled else "ask"
    _save_data(data)


# 引导登录用的落地页：用「地图页 /map」而非「航班页 /flights」——/flights 对【未登录】用户会卡在加载，
# 而 /map 能正常完成登录。登录后 Orbx 令牌(有效约 14 天)写进 localStorage，程序据此直接调 API；
# 14 天内直接用令牌请求(无需再开浏览器)，令牌过期后再次引导到 /map 登录拿新令牌。
_VOLANTA_LOGIN_URL = "https://fly.volanta.app/map"


def _open_volanta_in_browser(url=_VOLANTA_LOGIN_URL):
    """优先用 Edge 打开 Volanta 地图页(/map)让用户登录；找不到 Edge 则回退默认浏览器/非 Windows。
    用 /map 而非 /flights：后者对未登录用户会卡加载。登录后 localStorage 会刷新出有效的 Orbx 令牌。"""
    edge = None
    try:
        import shutil
        edge = shutil.which("msedge")
    except Exception:
        edge = None
    if not edge:
        for p in (
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        ):
            if p and os.path.exists(p):
                edge = p
                break
    if edge:
        try:
            import subprocess
            subprocess.Popen([edge, url])
            return True
        except Exception:
            pass
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def _load_volanta_csv(path):
    """解析 Volanta 官方导出的 CSV(列名灵活匹配)，统计起降对次数，作为兜底数据源。"""
    counts = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            dep_key = arr_key = None
            for col in (reader.fieldnames or []):
                lc = col.strip().lower()
                if dep_key is None and lc in ("origin", "departure", "dep", "from", "departureicao", "origin_icao"):
                    dep_key = col
                if arr_key is None and lc in ("destination", "arrival", "arr", "dest", "to", "arrivalicao", "destination_icao"):
                    arr_key = col
            if not dep_key or not arr_key:
                return counts
            for row in reader:
                dep = (row.get(dep_key) or "").strip().upper()
                arr = (row.get(arr_key) or "").strip().upper()
                if len(dep) == 4 and len(arr) == 4 and dep != arr:
                    counts[(dep, arr)] = counts.get((dep, arr), 0) + 1
    except Exception:
        pass
    return counts


def _counts_from_flights_obj(data):
    """从 /api/v1/Flights 的【已解析 JSON 对象】提取 dict{(dep,arr): count}。
    结构灵活匹配：顶层 list 或被包在 flights/data/... 里；每条取 flight 子对象的 origin/destination ICAO。"""
    counts = {}
    # 顶层一般是 list；也兼容被包在 {"flights":[...]} / {"data":[...]} 里的情形
    flights = data
    if isinstance(data, dict):
        for key in ("flights", "data", "items", "results", "Flights"):
            if isinstance(data.get(key), list):
                flights = data[key]
                break
    if not isinstance(flights, list):
        return counts

    def icao_of(node):
        # 既支持扁平字符串("ROMY")，也支持嵌套对象({"icaoCode":"ROMY",...})
        if isinstance(node, str):
            s = node.strip().upper()
            return s if len(s) == 4 and s.isalnum() else None
        if isinstance(node, dict):
            for k in ("icaoCode", "icao", "icaoIdent", "ident", "code", "airportCode"):
                v = node.get(k)
                if isinstance(v, str) and len(v.strip()) == 4:
                    return v.strip().upper()
        return None

    for item in flights:
        if not isinstance(item, dict):
            continue
        # Volanta /api/v1/Flights：每条是 {"flight": {...}, "summarisedPositions": [...]}，
        # 航班数据在 flight 子对象里；优先取扁平的 originIcao/destinationIcao，回退嵌套 origin/destination
        fl = item.get("flight") if isinstance(item.get("flight"), dict) else item
        dep = icao_of(fl.get("originIcao") or fl.get("origin")
                      or fl.get("departureIcao") or fl.get("departure") or fl.get("from"))
        arr = icao_of(fl.get("destinationIcao") or fl.get("destination")
                      or fl.get("arrivalIcao") or fl.get("arrival") or fl.get("to"))
        if dep and arr and dep != arr:
            counts[(dep, arr)] = counts.get((dep, arr), 0) + 1
    return counts


def _load_volanta_json(path):
    """读取并解析用户放入的 volanta_flights.json(DevTools Copy 的 /api/v1/Flights 响应)
    → dict{(dep,arr): count}。文件不存在/损坏 → 空 dict。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _counts_from_flights_obj(json.load(f))
    except Exception:
        return {}


# ---- 通过浏览器登录会话直接拉取完整航班(本机、token 不外传) ----
# 原理：Volanta(母公司 Orbx)登录后，会把一个【Orbx 签发的 API token】(JWT, iss="Orbx")存进浏览器
# localStorage(有效期约 14 天)。取出它，在本机带 Authorization: Bearer 调 Volanta 自家的
# /api/v1/Flights，一次拿到全部航班，解析出起降对后并入 volanta_data.json——无需开发者工具、无需滚动。
#   （注：Firebase idToken(iss=securetoken.google.com)会被 API 以「issuer invalid」拒绝，故不用它。）
# ⚠️ 安全边界：
#   - localStorage 是全站共享的，但本模块【只提取 iss=="Orbx" 且就近出现在 fly.volanta.app 来源附近】
#     的令牌，绝不取用/记录/发送任何其它网站的令牌；
#   - 取到的 token 仅在内存中用于这一次请求：绝不落盘、绝不发往除 api.volanta.app 以外任何地方；
#   - 这与 yt-dlp 的 --cookies-from-browser 同类：读用户【自己】浏览器里【自己】的会话、访问【自己】的数据。

_VOLANTA_API_URL = "https://api.volanta.app/api/v1/Flights"
_JWT_PAT = re.compile(r'(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})')


def _jwt_payload(jwt):
    """本地解出 JWT payload(dict)，不验签；失败返回 None。"""
    try:
        import base64
        p = jwt.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return None


def _localstorage_leveldb_dirs():
    """各浏览器 Profile 的 Local Storage leveldb 目录(localStorage 全站共享于一个库)。"""
    dirs = []
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return dirs
    for base in (("Microsoft", "Edge"), ("Google", "Chrome"), ("BraveSoftware", "Brave-Browser")):
        root = os.path.join(local, *base, "User Data")
        if not os.path.isdir(root):
            continue
        try:
            for prof in os.listdir(root):
                d = os.path.join(root, prof, "Local Storage", "leveldb")
                if os.path.isdir(d):
                    dirs.append(d)
        except Exception:
            continue
    return dirs


def _extract_volanta_api_token(text):
    """从 localStorage 文本里取出 Volanta(Orbx)签发的、未过期、最新的 API token(JWT)。找不到返回 None。
    严格限定：iss=="Orbx"(Volanta 母公司专属) 且其前方就近(3000 字符内)出现 fly.volanta.app 来源标记，
    以确保是 Volanta 的 localStorage 条目——绝不取用其它网站的任何令牌。"""
    best, best_exp = None, 0.0
    for m in _JWT_PAT.finditer(text or ""):
        jwt = m.group(1)
        pl = _jwt_payload(jwt)
        if not pl or str(pl.get("iss")) != "Orbx":      # 仅 Orbx(Volanta)签发的
            continue
        exp = pl.get("exp", 0) or 0
        if exp <= time.time() + 30:                      # 已过期/即将过期跳过
            continue
        window = text[max(0, m.start() - 3000):m.start()].lower()
        if "volanta" not in window:                      # 就近来源校验：确属 fly.volanta.app
            continue
        if exp > best_exp:
            best, best_exp = jwt, exp
    return best


def _fetch_volanta_flights_json(token, timeout=15):
    """用 Orbx API token 在本机调 /api/v1/Flights，返回完整航班 JSON 文本；失败返回 None。
    请求 gzip(标准库可解)以避开 br(无 brotli 解码)。token 只放在本次请求的 Authorization 头。"""
    req = urllib.request.Request(_VOLANTA_API_URL, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://fly.volanta.app",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "ignore")
    except Exception:
        return None


def try_fetch_volanta_json_via_session(timeout=15, skip_if_fresh=0):
    """用浏览器里 Volanta 的登录会话直接拉取完整航班，把解析出的已飞次数并入 volanta_data.json。成功返回 True，否则 None。
    全程本机：读自己浏览器里自己的 Volanta(Orbx)token → 调 Volanta 自家 API → 存自己的飞行数据；token 不落盘、不外传。
    只在解析出 ≥1 条航线时才提交(原子写)，避免拿坏响应污染累积库。
    skip_if_fresh>0 时：若上次成功拉取距今不到该秒数，则跳过联网、直接返回 True(省流量/减少 API 调用)。"""
    data = _load_data()
    if skip_if_fresh and data.get("fetched_at"):
        try:
            if time.time() - float(data["fetched_at"]) < skip_if_fresh:
                return True                # 已足够新，不重复联网
        except Exception:
            pass
    try:
        token = None
        for d in _localstorage_leveldb_dirs():
            token = _extract_volanta_api_token(_read_leveldb_text(d))
            if token:
                break
        if not token:
            return None
        js_text = _fetch_volanta_flights_json(token, timeout=timeout)
        if not js_text:
            return None
        try:
            obj = json.loads(js_text)          # 必须是合法 JSON
        except Exception:
            return None
        counts = _counts_from_flights_obj(obj)
        if not counts:                         # 解析不出任何航线 → 视为坏响应，不提交
            return None
        data["flown"] = _merge_authoritative(data.get("flown") or {}, counts)
        data["fetched_at"] = time.time()
        if _save_data(data):                   # 原子写入，确认落盘成功才算拉取成功
            return True
    except Exception:
        pass
    return None


def load_volanta_flown_routes():
    """读取 volanta_data.json 的已飞累积库，并(若工作目录存在)合并用户放入的可选导入文件，返回 (flown, meta)。
    可选导入(都是用户手动放入的)：
      - volanta_flights.json：DevTools 里 Copy 的 /api/v1/Flights 响应(权威次数)，吸收后【删除】以保持单文件；
      - volanta_flights.csv：Volanta 官方导出(兜底，只 max 抬高)，吸收后【保留】(用户自有文件，不动)。
    合并策略：航线键取并集(只增不减)；权威源用其次数，CSV 只 max。有变化则原子落盘 volanta_data.json。"""
    run_dir = get_real_run_path()
    data = _load_data()
    flown = dict(data.get("flown") or {})
    before = dict(flown)
    fetched = float(data.get("fetched_at") or 0)

    # 可选导入①：用户放入的 volanta_flights.json(权威)
    fj = os.path.join(run_dir, "volanta_flights.json")
    json_imported = False
    if os.path.exists(fj):
        auth = _load_volanta_json(fj)
        if auth:
            flown = _merge_authoritative(flown, auth)
            json_imported = True
            try:
                fetched = max(fetched, os.path.getmtime(fj))
            except Exception:
                pass

    # 可选导入②：volanta_flights.csv(官方导出，兜底，只 max)
    cj = os.path.join(run_dir, "volanta_flights.csv")
    if os.path.exists(cj):
        flown = _merge_max(flown, _load_volanta_csv(cj))

    saved = True
    if flown != before or fetched != float(data.get("fetched_at") or 0):
        data["flown"], data["fetched_at"] = flown, fetched
        saved = _save_data(data)
    if json_imported and saved:                # 已并入 volanta_data.json，删掉导入文件去臃肿(CSV 保留)
        try:
            os.remove(fj)
        except Exception:
            pass

    meta = {
        "routes": len(flown),                  # 去重后的不同有向航线数
        "flights": sum(flown.values()),        # 总飞行次数
        "latest": time.strftime("%Y-%m-%d %H:%M", time.localtime(fetched)) if fetched else None,
    }
    return flown, meta
