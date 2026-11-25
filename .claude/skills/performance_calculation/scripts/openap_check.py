#!/usr/bin/env python3
"""OpenAP / opentop 对照脚本（performance_calculation skill 用）。

只在开发用的独立 venv 里运行；本项目的程序（dispatcher/、exe）不依赖它，也不打包它。
它只读地导入本项目的纯标准库模块 dispatcher.eta / dispatcher.aircraft，把 OpenAP 的结果与本项目的
ETA 剖面并列对照。除 --json 指定的路径外不写任何文件（opentop 导入时带进的 matplotlib 会写字体缓存，
脚本把 MPLCONFIGDIR 默认指到系统临时目录）。

    python -m venv <venv>
    <venv>\\Scripts\\python -m pip install --only-binary=:all: openap==2.6.2 opentop==2.7.0
    <venv>\\Scripts\\python -B .claude/skills/performance_calculation/scripts/openap_check.py env

子命令：env / fleet / grid / flight / matrix。说明见 ../reference/openap.md、opentop.md、validation.md。
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import contextlib  # noqa: E402
import importlib  # noqa: E402
import importlib.metadata as md  # noqa: E402
import inspect  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import warnings  # noqa: E402
from pathlib import Path  # noqa: E402

TESTED = {"openap": "2.6.2", "opentop": "2.7.0"}
ROOT = Path(__file__).resolve().parents[4]          # 仓库根：scripts → skill → skills → .claude → 根
FT, KT, G0 = 0.3048, 0.514444, 9.80665
KGMIN_PER_BOEING = 100 * 0.45359237 / 60            # Boeing CI 的单位 100 lb/h → kg/min（≈0.756）
VENV_HELP = (
    "需要在独立的 venv 里装 OpenAP（不要装进打包 exe 用的全局 Python）：\n"
    "  python -m venv <venv>\n"
    "  <venv>\\Scripts\\python -m pip install --only-binary=:all: -r "
    ".claude/skills/performance_calculation/scripts/requirements-lock.txt\n"
    "  <venv>\\Scripts\\python -B .claude/skills/performance_calculation/scripts/openap_check.py env")


QUIET = ("Wave drag is experimental", "using synonym")      # OpenAP 每次实例化都会发的提示，不算问题


def quiet_warnings():
    for msg in QUIET:
        warnings.filterwarnings("ignore", message=".*%s.*" % msg)


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def note(msg):
    print("⚠️ " + msg)


# ---------------------------------------------------------------- 依赖与本项目模块
_MODS = {}


def mods(top=False):
    """按需导入 openap（及 opentop）。缺了打印 venv 配置方法并以 2 退出；版本或环境不对时警告。"""
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "openap_check_mpl"))
    for name in (("openap", "opentop") if top else ("openap",)):
        if name in _MODS:
            continue
        try:
            _MODS[name] = importlib.import_module(name)
        except ImportError:
            die(2, "缺少依赖 %s。\n%s" % (name, VENV_HELP))
        importlib.import_module("openap.drag")             # openap/drag.py、prop.py 导入时 simplefilter("once")，
        importlib.import_module("openap.prop")             # 会盖掉先设的过滤，所以导入后再设一次
        quiet_warnings()
        ver = md.version(name)
        if ver != TESTED[name]:
            note("%s 版本 %s，与实测版本 %s 不同：结果可能与 validation.md 不一致" % (name, ver, TESTED[name]))
        if name == "openap" and sys.prefix == sys.base_prefix:
            note("当前不在 venv 里：OpenAP 不要装进打包 exe 用的全局 Python")
    return _MODS


def project():
    """只读地导入本项目的纯标准库模块。"""
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))                  # 追加在末尾：venv 里的包优先
    try:
        from dispatcher import aircraft, eta
    except ImportError as e:
        die(2, "导入本项目模块失败（%s）：脚本须放在仓库的 .claude/skills/performance_calculation/scripts/ 下" % e)
    for name in ("_level_ok", "MAG_VAR_W", "_brg", "_hav", "cruise_fl", "estimate", "isa", "hpa_to_ft",
                 "tas_from_mach", "tas_from_cas"):
        if not hasattr(eta, name):
            die(2, "dispatcher.eta 里找不到 %s：eta.py 改过，请同步本脚本" % name)
    return eta, aircraft


_APTS = {}


def coords(code, source="auto"):
    """ICAO（或 'lat,lon'）→ ((lat, lon), 来源)。auto：本机 NavData 有就用它，否则用 OpenAP 自带的机场表。"""
    if "," in code:
        lat, lon = (float(x) for x in code.split(","))
        return (lat, lon), "坐标"
    code = code.upper()
    if source in ("auto", "navdata"):
        if not _APTS:
            project()
            from dispatcher.data import load_airports_from_navigraph
            from dispatcher.navdata import find_navdata_file
            with contextlib.redirect_stdout(io.StringIO()):
                dat = find_navdata_file()
                if dat:
                    _APTS.update({a.code: (a.lat_dd, a.lon_dd) for a in load_airports_from_navigraph(dat, None, 0)})
        if code in _APTS:
            return _APTS[code], "navdata"
        if source == "navdata":
            die(2, "本机 NavData 里找不到 %s" % code)
    from openap import nav
    ap = nav.airport(code)
    if not ap:
        die(2, "找不到机场 %s" % code)
    return (ap["lat"], ap["lon"]), "openap"


def gc(a, b):
    eta, _ = project()
    return eta._hav(a[0], a[1], b[0], b[1]), eta._brg(a[0], a[1], b[0], b[1])


def east_of(track):
    eta, _ = project()
    return (track + eta.MAG_VAR_W) % 360 < 180


def legal_at_or_below(alt_ft, east):
    """alt_ft 及以下最近的东单西双合法高度层（百英尺）。"""
    eta, _ = project()
    fl = int(alt_ft // 100) // 10 * 10
    while fl >= 30:
        if eta._level_ok(fl, east):
            return fl
        fl -= 10
    return None


def legal_near(alt_mean, alt_max, east):
    """opentop 的连续高度 → 一个合法高度层：不高于巡航最高点、最接近巡航均高的那层。
    （按 TOC 取会偏低：TOC 之后它还在以 ≤ 500 fpm 慢慢爬。）"""
    hi = legal_at_or_below(alt_max, east)
    if hi is None:
        return None
    lo = legal_at_or_below(hi * 100 - 100, east)
    return min((x for x in (hi, lo) if x is not None), key=lambda fl: abs(fl * 100 - alt_mean))


# ---------------------------------------------------------------- 成本指数换算
def ci_k(top=None):
    """opentop obj_ci 的默认价格 → k（kg/min）：ci:N 等效于「燃油 + k·N/(100−N)·分钟」。"""
    if top is None:
        return 25 / (0.8 / 0.82)
    p = inspect.signature(top.CompleteFlight.obj_ci).parameters
    return p["time_price"].default / (p["fuel_price"].default / 0.82)


def ci_to_weight(ci_kgmin, k):
    return 100.0 * ci_kgmin / (ci_kgmin + k)


def weight_to_ci(n, k):
    return math.inf if n >= 100 else k * n / (100.0 - n)


# ---------------------------------------------------------------- OpenAP 核心模型
def needs_synonym(ac):
    """机体 / 阻力 / WRAP 任一没有自有数据就要 use_synonym=True：FuelFlow 内部会建 WRAP，并把 use_synonym 一并传过去，
    所以 A359（机体、阻力都自有，WRAP 没有）不加它连 FuelFlow 都建不起来。"""
    base = Path(mods()["openap"].__file__).parent / "data"
    lo = ac.lower()
    return not all((base / f / (lo + ext)).exists() for f, ext in (("aircraft", ".yml"), ("dragpolar", ".yml"),
                                                                  ("wrap", ".txt")))


class Model:
    """一个机型的 OpenAP 核心模型：推力 / 阻力 / 油耗（波阻默认开，与 opentop 一致）。
    synonym=None：按 needs_synonym 自动决定（结果只作量级参考时会标出）。"""

    def __init__(self, oa, ac, wave_drag=True, synonym=None, eng=None):
        if synonym is None:
            synonym = needs_synonym(ac)
        self.synonym = synonym
        kw = {"use_synonym": synonym}
        self.oa, self.type = oa, ac.upper()
        self.ac = oa.prop.aircraft(ac, **kw)
        self.ff = oa.FuelFlow(ac, eng=eng, wave_drag=wave_drag, **kw)
        self.drag = oa.Drag(ac, wave_drag=wave_drag, **kw)
        self.thrust = oa.Thrust(ac, eng=eng, **kw)
        self.n_eng = int(self.ac["engine"]["number"])
        self.t_max = float(self.ff.engine["max_thrust"]) * self.n_eng
        self.mmo = float(self.ac["mmo"])
        self.ceil_ft = float(self.ac["limits"]["ceiling"]) / FT
        self.wave = wave_drag

    def evaluate(self, mass, fl, mach, dT=0.0, wind=None):
        """FL × Mach 网格（numpy）→ 各量。ff kg/h、tas / gs kt、roc 为最大巡航推力下的剩余爬升率 fpm。"""
        import numpy as np
        aero = self.oa.aero
        F, M = np.meshgrid(np.asarray(fl, float), np.asarray(mach, float), indexing="ij")
        shape = F.shape
        # 一律用一维数组调 OpenAP：它的 ndarrayconvert 会把 (N, 1) 的结果 squeeze 成 (N,)，
        # FuelFlow.enroute 内部再与 (N, 1) 的输入广播成 (N, N)，结果就错了
        f1, m1 = F.ravel(), M.ravel()
        alt = f1 * 100.0
        tas = np.asarray(aero.mach2tas(m1, alt * FT, dT=dT), float) / KT
        ff = np.asarray(self.ff.enroute(mass=mass, tas=tas, alt=alt, dT=dT), float).reshape(-1) * 3600.0
        drag = np.asarray(self.drag.clean(mass=mass, tas=tas, alt=alt, dT=dT), float).reshape(-1)
        thrust = np.asarray(self.thrust.cruise(tas=tas, alt=alt, dT=dT), float).reshape(-1)
        roc = (thrust - drag) * tas * KT / (mass * G0) / FT * 60.0
        rho = np.asarray(aero.density(alt * FT, dT=dT), float)
        cl = mass * G0 / (0.5 * rho * (tas * KT) ** 2 * float(self.ac["wing"]["area"]))
        w = np.asarray(wind(f1), float) if wind else 0.0
        gs = tas + w
        out = {"fl": f1, "mach": m1, "tas": tas, "gs": gs, "ff": ff, "drag": drag, "thrust": thrust, "roc": roc,
               "cl": cl, "x": drag / self.t_max, "sr": tas / ff, "gsr": gs / ff}
        return {k: np.broadcast_to(v, f1.shape).reshape(shape) for k, v in out.items()}


def parse_wind(spec):
    """'300:-40,340:-60' → FL 的线性插值函数（kt，正 = 顺风），两端外取端点值。"""
    if not spec:
        return None
    import numpy as np
    pts = sorted((float(a), float(b)) for a, b in (p.split(":") for p in spec.split(",")))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return lambda F: np.interp(F, xs, ys)


def grid_analysis(m, mass, mach_ref=None, ci_list=(0, 10, 30, 60, 100), boeing=(), fl_min=200, fl_max=None,
                  mach_min=0.60, mach_step=0.005, resid_roc=300.0, dT=0.0, wind=None, east=None, cl_max=None,
                  h_max_fl=None):
    """核心模型网格分析 → dict。约束：Mach ≤ MMO−0.02、高度 ≤ OpenAP ceiling（与 h_max）、剩余爬升率 ≥ resid_roc、
    （可选）1.3 g 下 CL ≤ cl_max。OPT 在 mach_ref 上按 100 ft 分辨率找；ECON 在 1000 ft 层 × Mach 网格上找。"""
    import numpy as np
    top_ft = min(m.ceil_ft, (h_max_fl or 1e9) * 100.0)
    fl_hi = int(min(fl_max or 1e9, top_ft / 100.0))
    mach_hi = m.mmo - 0.02
    if mach_ref is None:
        try:
            mach_ref = float(m.oa.WRAP(m.type).cruise_mach()["default"])
        except Exception:  # noqa: BLE001
            mach_ref = float(m.ac["cruise"]["mach"])
    mach_ref = min(mach_ref, mach_hi)
    res = {"type": m.type, "synonym": m.synonym, "mass": mass, "wave": m.wave, "mach_ref": mach_ref,
           "ceil_fl": m.ceil_ft / 100.0, "mmo": m.mmo}

    # OPT / MAX：mach_ref 上的 1D 扫描（100 ft）
    fl_fine = np.arange(fl_min, fl_hi + 0.001, 1.0)
    e1 = m.evaluate(mass, fl_fine, [mach_ref], dT, wind)
    sr, roc = e1["sr"][:, 0], e1["roc"][:, 0]
    ok = roc >= resid_roc
    if cl_max:
        ok &= e1["cl"][:, 0] * 1.3 <= cl_max
    res["max0_fl"] = float(fl_fine[roc >= 0].max()) if (roc >= 0).any() else None
    res["max300_fl"] = float(fl_fine[roc >= 300].max()) if (roc >= 300).any() else None
    res["opt_free_fl"] = float(fl_fine[int(np.nanargmax(sr))])          # 只看比航程、不计推力约束的 OPT
    res["opt_fl"] = res["thrust_ratio_opt"] = None              # 参考 Mach 上可能没有满足剩余爬升率的高度
    for d in (20, 40):
        for sgn in ("m", "p"):
            res["sr_loss_%s%d" % (sgn, d * 100)] = None
    if ok.any():
        i_opt = int(np.argmax(np.where(ok, sr, -1)))
        res["opt_fl"] = float(fl_fine[i_opt])
        res["opt_capped"] = bool(i_opt == len(fl_fine) - 1 or not ok[min(i_opt + 1, len(ok) - 1)])
        res["thrust_ratio_opt"] = float(e1["x"][i_opt, 0])
        for d in (20, 40):                                        # fl_fine 步长 100 ft：d 步 = d×100 ft
            for sgn in (-1, 1):
                j = i_opt + sgn * d
                key = "sr_loss_%s%d" % ("m" if sgn < 0 else "p", d * 100)
                res[key] = float(1 - sr[j] / sr[i_opt]) if 0 <= j < len(sr) and ok[j] else None

    # 2D：1000 ft 层 × Mach
    fls = np.arange(math.ceil(fl_min / 10) * 10, fl_hi + 0.001, 10.0)
    machs = np.arange(mach_min, mach_hi + 1e-9, mach_step)
    e2 = m.evaluate(mass, fls, machs, dT, wind)
    feas = e2["roc"] >= resid_roc
    if cl_max:
        feas &= e2["cl"] * 1.3 <= cl_max
    if east is not None:
        project()
        legal = np.array([project()[0]._level_ok(int(f), east) for f in fls])
        feas &= legal[:, None]
    res["x_gt_1"] = bool((e2["x"][feas] > 1).any())
    # OPT 层上的 MRC / LRC（按 1000 ft 取整到 OPT 附近的层）
    res["mrc_mach"] = res["lrc_mach"] = None
    k = int(np.argmin(np.abs(fls - res["opt_fl"]))) if res["opt_fl"] is not None else None
    row_ok = e2["roc"][k] >= resid_roc if k is not None else np.zeros(1, bool)
    if row_ok.any():
        srk = np.where(row_ok, e2["sr"][k], -1)
        j = int(np.argmax(srk))
        res["mrc_mach"] = float(machs[j])
        lrc = machs[(srk >= 0.99 * srk[j]) & (machs >= machs[j])]
        res["lrc_mach"] = float(lrc.max()) if lrc.size else None
    econ = {}
    cis = [(c, "%g" % c) for c in ci_list] + [(b * KGMIN_PER_BOEING, "B%g" % b) for b in boeing]
    for ci, label in cis:
        cost = np.where(feas, (e2["ff"] + 60.0 * ci) / e2["gs"], np.inf)   # 每海里成本（kg 当量）
        if not np.isfinite(cost).any():
            econ[label] = None
            continue
        a, b = np.unravel_index(int(np.argmin(cost)), cost.shape)
        econ[label] = {"ci_kgmin": ci, "fl": float(fls[a]), "mach": float(machs[b]),
                       "kg_per_nm": float(cost[a, b] - 60.0 * ci / e2["gs"][a, b]), "at_mach_cap": bool(b == len(machs) - 1)}
    res["econ"] = econ
    return res


# ---------------------------------------------------------------- opentop
def run_flight(ac, orig, dest, m0=0.85, tow=None, payload=None, objective="fuel", custom_ci=None, h_max_fl=None,
               nodes=None, max_iter=3000, synonym=None, engine=None, return_failed=False, coord_src="auto"):
    """opentop CompleteFlight 一次 → dict（失败时 df=None、带原因）。synonym=None：自动（见 needs_synonym）。"""
    top = mods(top=True)["opentop"]
    if synonym is None:
        synonym = needs_synonym(ac)
    a, sa = coords(orig, coord_src)
    b, sb = coords(dest, coord_src)
    dist, trk = gc(a, b)
    h_max_m = None if h_max_fl is None else h_max_fl * 100 * FT

    class Flight(top.CompleteFlight):
        # 2.7.0 的 CompleteFlight.trajectory() 不收 h_max；init_conditions 读 kwargs['h_max']，缺省 = ceiling
        def init_conditions(self, **kw):
            if h_max_m is not None:
                kw["h_max"] = h_max_m
            super().init_conditions(**kw)

    with warnings.catch_warnings(record=True) as wl:
        warnings.simplefilter("always")
        acd = mods()["openap"].prop.aircraft(ac, use_synonym=synonym)
        if tow is not None:
            m0 = tow / float(acd["mtow"])
        opt = Flight(ac, a, b, m0=m0, engine=engine, use_synonym=synonym, payload=payload)
        opt.setup(nodes=nodes, max_iter=max_iter)
        obj = objective
        if custom_ci is not None:
            obj = (lambda x, u, dt, **kw: opt.obj_fuel(x, u, dt, **kw) + custom_ci * dt / 60.0)
        t0 = time.perf_counter()
        r = opt.trajectory(obj, result_object=True, return_failed=return_failed)
        sec = time.perf_counter() - t0
    out = {"ac": ac.upper(), "type": ac.upper() + ("（同义映射）" if synonym else ""), "orig": orig.upper(),
           "dest": dest.upper(),
           "coords": "%s/%s" % (sa, sb),
           "gc_nm": dist, "track": trk, "m0_kg": float(opt.mass_init), "m0_frac": float(opt.mass_init) / float(acd["mtow"]),
           "mtow": float(acd["mtow"]), "mlw": float(acd["mlw"]), "oew": float(acd["oew"]),
           "ceil_ft": float(acd["limits"]["ceiling"]) / FT, "mmo": float(acd["mmo"]),
           "objective": "fuel+%g·min" % custom_ci if custom_ci is not None else str(objective),
           "h_max_fl": h_max_fl, "payload": payload, "nodes": int(opt.nodes), "sec": sec,
           "status": getattr(r, "status", None), "iters": getattr(r, "iters", None),
           "success": bool(getattr(r, "success", False)),
           "warnings": sorted({str(w.message) for w in wl
                               if any(s in (w.filename or "").replace("\\", "/") for s in ("/opentop/", "/openap/"))
                               and not any(q in str(w.message) for q in QUIET)})}
    df = r.df
    if df is None or len(df) == 0:
        out["df"] = None
        return out
    out.update(summarize(df, trk))
    out["m0_kg"] = float(df["mass"].iloc[0])                  # payload 模式下起飞质量是解出来的
    out["m0_frac"] = out["m0_kg"] / out["mtow"]
    # 起飞太重时 opentop 不一定报错：它会低空慢飞把多余的油烧掉，刚好落在 MLW 下（实测 B772 0.85 MTOW）
    out["mlw_tight"] = payload is None and out["land_kg"] >= 0.995 * out["mlw"]
    out["df"] = df
    return out


def summarize(df, track):
    """opentop 结果 DataFrame → 摘要（巡航段 = TOC..TOD：首个爬升率 ≤ 500 fpm 且 > FL150 的点到最后一个 ≥ −500 fpm 的点）。"""
    import numpy as np
    alt = df["altitude"].to_numpy(float)
    vs = df["vertical_rate"].to_numpy(float)
    mach = df["mach"].to_numpy(float)
    mass = df["mass"].to_numpy(float)
    ts = df["ts"].to_numpy(float)
    lat, lon = df["latitude"].to_numpy(float), df["longitude"].to_numpy(float)
    eta, _ = project()
    seg = [0.0] + [eta._hav(lat[i - 1], lon[i - 1], lat[i], lon[i]) for i in range(1, len(lat))]
    cum = np.cumsum(seg)
    high = np.where(alt > 15000)[0]
    if high.size:
        lo, hi = high[0], high[-1]
        toc = next((i for i in range(lo, hi + 1) if vs[i] <= 500), lo)
        tod = next((i for i in range(hi, lo - 1, -1) if vs[i] >= -500), hi)
    else:
        toc = tod = int(np.argmax(alt))
    cr = slice(toc, max(tod, toc) + 1)
    east = east_of(track)
    return {"fuel_kg": float(mass[0] - mass[-1]), "air_min": float(ts[-1] - ts[0]) / 60.0,
            "toc_nm": float(cum[toc]), "tod_to_go_nm": float(cum[-1] - cum[tod]), "path_nm": float(cum[-1]),
            "alt_toc": float(alt[toc]), "alt_mean": float(alt[cr].mean()), "alt_max": float(alt.max()),
            "legal_fl": legal_near(float(alt[cr].mean()), float(alt.max()), east), "east": east,
            "mach_mean": float(mach[cr].mean()), "mach_min": float(mach[cr].min()), "mach_max": float(mach[cr].max()),
            "cruise_vs_max": float(np.abs(vs[cr]).max()), "land_kg": float(mass[-1])}


def eta_compare(ac, a, b):
    """同一大圆下本项目 eta 的巡航高度与空中时间（爬升 + 巡航 + 下降，不含滑行与 term）。"""
    eta, aircraft = project()
    with contextlib.redirect_stdout(io.StringIO()):
        aircraft.load_aircraft_db()
        perf, aid, fb = aircraft.perf_of(ac)
    if perf is None:
        return None
    r = eta.estimate(0, [a, b], perf)
    return {"perf_id": aid, "fallback": fb, "fl": r["fl"], "air_min": r["climb"] + r["cruise"] + r["descent"],
            "perf_ceil": perf.get("ceil"), "perf_crz": perf.get("crz")}


def rough_landing(m, mass, dist):
    """预检用的粗估：FL350 / M0.78 巡航油耗 × 航程时间。"""
    e = m.evaluate(mass, [350], [0.78])
    return mass - float(e["ff"][0, 0]) * dist / float(e["tas"][0, 0])


# ---------------------------------------------------------------- env
def env_checks(sig=False):
    """→ [(编号, 说明, 通过?, 实际值)]。编号与 reference/openap.md 的 API 表对应。"""
    oa = mods(top=True)["openap"]
    top = _MODS["opentop"]
    eta, _ = project()
    import numpy as np
    rows = []

    def chk(cid, desc, fn):
        try:
            ok, val = fn()
        except Exception as e:  # noqa: BLE001
            ok, val = False, "%s: %s" % (type(e).__name__, e)
        rows.append((cid, desc, bool(ok), val))

    chk("E01", "版本与环境", lambda: (all(md.version(k) == v for k, v in TESTED.items()) and sys.prefix != sys.base_prefix,
                                  "python %s, openap %s, opentop %s, casadi %s, venv=%s" % (
                                      sys.version.split()[0], md.version("openap"), md.version("opentop"),
                                      md.version("casadi"), sys.prefix != sys.base_prefix)))

    def e02():
        a = oa.prop.aircraft("A320")
        need = ("mtow", "mlw", "oew", "mfc", "mmo", "ceiling", "limits", "cruise", "engine", "wing")
        return all(k in a for k in need), "mtow %s, mlw %s, oew %s, mfc %s, mmo %s, ceiling %s m" % (
            a["mtow"], a["mlw"], a["oew"], a["mfc"], a["mmo"], a["ceiling"])
    chk("E02", "prop.aircraft('A320') 的键", e02)

    def e03():
        e = oa.prop.engine(oa.prop.aircraft("A320")["engine"]["default"])
        return all(k in e for k in ("max_thrust", "ff_to", "ff_co", "ff_app", "ff_idl")), "%s: max_thrust %.0f N" % (
            e["name"], e["max_thrust"])
    chk("E03", "prop.engine(默认发动机) 的推力与 ICAO 油耗字段", e03)

    def e04():
        a = oa.aero
        v = [a.mach2tas(0.78, 11000) / KT, a.cas2tas(290 * KT, 9000) / KT, a.crossover_alt(290 * KT, 0.78) / FT,
             a.h_isa(25000.0) / FT]
        return all(np.isfinite(v)), "M0.78@11km %.1f kt, 290KCAS@9km %.1f kt, crossover %.0f ft, 250hPa %.0f ft" % tuple(v)
    chk("E04", "aero 速度换算（SI 输入）", e04)

    def e05():
        t = oa.Thrust("A320")
        c1, c2 = float(t.cruise(tas=450, alt=30000)), float(t.cruise(tas=450, alt=39000))
        return c1 > c2 > 0, "cruise FL300 %.0f N > FL390 %.0f N" % (c1, c2)
    chk("E05", "Thrust.cruise 为最大可用推力（N，全机），随高度递减", e05)

    def e06():
        d0 = float(oa.Drag("A320").clean(mass=65000, tas=470, alt=37000))
        d1 = float(oa.Drag("A320", wave_drag=True).clean(mass=65000, tas=470, alt=37000))
        return d1 >= d0 > 0, "M0.82 级 FL370：无波阻 %.0f N，有波阻 %.0f N" % (d0, d1)
    chk("E06", "Drag.clean，wave_drag=True 时阻力不小于默认", e06)

    def e07():
        f = oa.FuelFlow("A320")
        a, b = float(f.enroute(mass=60000, tas=450, alt=36000)), float(f.enroute(mass=70000, tas=450, alt=36000))
        return 0 < a < b, "60 t %.3f kg/s < 70 t %.3f kg/s" % (a, b)
    chk("E07", "FuelFlow.enroute 返回 kg/s，随质量增大", e07)

    def e08():
        w = oa.WRAP("A320").cruise_alt()
        return set(w) >= {"default", "minimum", "maximum"}, "cruise_alt %s km（%s–%s）" % (
            w["default"], w["minimum"], w["maximum"])
    chk("E08", "WRAP.cruise_alt 返回分布（km）", e08)

    def e09():
        try:
            oa.prop.aircraft("AT76")
            plain = "无需同义映射"
        except Exception as e:  # noqa: BLE001
            plain = type(e).__name__
        syn = oa.prop.aircraft("AT76", use_synonym=True)["aircraft"]
        return plain != "无需同义映射", "不加 use_synonym：%s；加了：%s" % (plain, syn)
    chk("E09", "prop / Drag / FuelFlow 同义映射须 use_synonym=True", e09)

    def e10():
        ps = inspect.signature(top.CompleteFlight.__init__).parameters
        ts = inspect.signature(top.CompleteFlight.trajectory).parameters
        ok = {"m0", "payload", "dT"} <= set(ps) and "step_climb" not in ts and "h_max" not in ts
        return ok, "__init__ 有 m0/payload/dT；trajectory 无 step_climb、无 h_max"
    chk("E10", "opentop.CompleteFlight 签名（2.7.0）", e10)

    def e11():
        k = ci_k(top)
        return abs(k - 25.625) < 1e-9, "time_price/(fuel_price/0.82) = %.3f kg/min → ci:30 ≈ %.1f kg/min，Airbus CI 30 ≈ ci:%.1f" % (
            k, weight_to_ci(30, k), ci_to_weight(30, k))
    chk("E11", "obj_ci 默认价格与换算常数", e11)

    def e12():
        a = oa.aero
        worst = 0.0
        for ft in (0, 10000, 20000, 30000, 36089, 41000):
            h = ft * FT
            t_e, p_e = eta.isa(ft)
            pairs = [(t_e, float(a.temperature(h))), (p_e * 100, float(a.pressure(h))),
                     (eta.tas_from_mach(0.78, ft), float(a.mach2tas(0.78, h)) / KT),
                     (eta.tas_from_cas(290, ft), float(a.cas2tas(290 * KT, h)) / KT)]
            worst = max(worst, max(abs(x - y) / abs(y) for x, y in pairs))
        for hpa in (850, 700, 500, 300, 250, 200):
            worst = max(worst, abs(eta.hpa_to_ft(hpa) - float(a.h_isa(hpa * 100.0)) / FT) / (float(a.h_isa(hpa * 100.0)) / FT))
        return worst <= 0.001, "最大相对偏差 %.4f%%" % (worst * 100)
    chk("E12", "openap.aero 与本项目 eta 的 ISA / 速度换算偏差 ≤ 0.1%", e12)

    def e13():
        ap = oa.nav.airport("RJTT")
        return bool(ap), "RJTT %.4f, %.4f" % (ap["lat"], ap["lon"])
    chk("E13", "openap.nav.airport（OpenAP 自带机场表）", e13)

    if sig:
        print("签名：")
        for obj, name in ((oa.prop.aircraft, "prop.aircraft"), (oa.prop.engine, "prop.engine"),
                          (oa.aero.mach2tas, "aero.mach2tas"), (oa.aero.cas2tas, "aero.cas2tas"),
                          (oa.aero.crossover_alt, "aero.crossover_alt"), (oa.aero.h_isa, "aero.h_isa"),
                          (oa.Thrust.__init__, "Thrust"), (oa.Thrust.cruise, "Thrust.cruise"),
                          (oa.Thrust.climb, "Thrust.climb"), (oa.Drag.__init__, "Drag"),
                          (oa.Drag.clean, "Drag.clean"), (oa.FuelFlow.__init__, "FuelFlow"),
                          (oa.FuelFlow.enroute, "FuelFlow.enroute"), (oa.WRAP.__init__, "WRAP"),
                          (oa.FlightGenerator.__init__, "FlightGenerator"), (oa.nav.airport, "nav.airport"),
                          (top.CompleteFlight.__init__, "opentop.CompleteFlight"),
                          (top.CompleteFlight.setup, "CompleteFlight.setup"),
                          (top.CompleteFlight.trajectory, "CompleteFlight.trajectory"),
                          (top.Cruise.__init__, "opentop.Cruise"), (top.CompleteFlight.obj_ci, "obj_ci")):
            print("  %-26s %s" % (name, inspect.signature(obj)))
    return rows


def cmd_env(args):
    rows = env_checks(args.sig)
    for cid, desc, ok, val in rows:
        print("%s %s  %s：%s" % ("✅" if ok else "❌", cid, desc, val))
    return 0 if all(r[2] for r in rows) else 1


# ---------------------------------------------------------------- fleet
FLEET = ("A319 A320 A20N A321 A21N B737 B738 B38M B739 B763 B772 B77W B788 B789 A359 E170 E190 CRJ7 CRJ9 "
         "DH8D AT76").split()


def _syn_map(folder):
    import pandas as pd
    p = Path(mods()["openap"].__file__).parent / "data" / folder / "_synonym.csv"
    return dict(pd.read_csv(p).itertuples(index=False, name=None)) if p.exists() else {}


def coverage(t):
    """一个机型在 OpenAP 各数据里的来源：自有 / 同义→X / 通用 / 无。只读包内数据，不复制。"""
    import pandas as pd
    oa = mods()["openap"]
    base = Path(oa.__file__).parent / "data"
    lo = t.lower()

    def src(folder, ext):
        if (base / folder / (lo + ext)).exists():
            return "自有"
        s = _syn_map(folder).get(lo)
        return "同义→%s" % s.upper() if s else "无"
    fits = {str(x).lower() for x in pd.read_csv(base / "fuel" / "fuel_models.csv")["typecode"]}
    row = {"type": t, "prop": src("aircraft", ".yml"), "drag": src("dragpolar", ".yml"), "wrap": src("wrap", ".txt")}
    row["fuel"] = "自有拟合" if lo in fits else ("通用曲线" if row["prop"] != "无" else "无")
    try:
        a = oa.prop.aircraft(t, use_synonym=row["prop"] != "自有")
        row.update(ceil_fl=round(float(a["limits"]["ceiling"]) / FT / 100), mmo=a["mmo"], engine=a["engine"]["default"],
                   mtow=a["mtow"], mlw=a["mlw"])
    except Exception:  # noqa: BLE001
        row.update(ceil_fl=None, mmo=None, engine=None, mtow=None, mlw=None)
    eta, aircraft = project()
    with contextlib.redirect_stdout(io.StringIO()):
        aircraft.load_aircraft_db()
        perf, aid, fb = aircraft.perf_of(t)
    row.update(proj_id=None if fb else aid, proj_ceil=perf.get("ceil") if perf and not fb else None,
               proj_crz=perf.get("crz") if perf and not fb else None)
    return row


def cmd_fleet(args):
    rows = [coverage(t.upper()) for t in (args.types or FLEET)]
    print("| 机型 | 机体 | 阻力极曲线 | 油耗 | WRAP | OpenAP ceiling | MMO | 默认发动机 | 本项目 perf.ceil / crz |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["type"], r["prop"], r["drag"], r["fuel"], r["wrap"], "FL%s" % r["ceil_fl"] if r["ceil_fl"] else "—",
            r["mmo"] or "—", r["engine"] or "—",
            "FL%s / %s" % (r["proj_ceil"], r["proj_crz"]) if r["proj_ceil"] else "—（不在机型库）"))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


# ---------------------------------------------------------------- grid
def _mass_arg(m, args):
    if args.mass:
        return float(args.mass)
    return float(args.mass_frac) * float(m.ac["mtow"])


def cmd_grid(args):
    oa = mods()["openap"]
    m = Model(oa, args.type, wave_drag=not args.no_wave_drag, synonym=args.synonym, eng=args.engine)
    cis = [float(x) for x in args.ci_airbus.split(",")] if args.ci_airbus else []
    boe = [float(x) for x in args.ci_boeing.split(",")] if args.ci_boeing else []
    east = None if args.track is None else east_of(args.track)
    wind = parse_wind(args.wind)
    if args.mass_sweep:
        a, b, n = args.mass_sweep.split(":")
        masses = [float(m.ac["mtow"]) * (float(a) + (float(b) - float(a)) * i / max(int(n) - 1, 1))
                  for i in range(int(n))]
    else:
        masses = [_mass_arg(m, args)]
    results = []
    for mass in masses:
        r = grid_analysis(m, mass, args.mach, cis, boe, args.fl_min, args.fl_max, resid_roc=args.resid_roc, dT=args.dT,
                          wind=wind, east=east, cl_max=args.cl_max, h_max_fl=args.h_max)
        results.append(r)
        print_grid(r)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return 0


def _fl(x):
    return "—" if x is None else "FL%03d" % round(x)


def _pct(x):
    return "—" if x is None else "%.1f%%" % (x * 100)


def print_grid(r):
    print("%s%s  质量 %.0f kg  波阻%s  参考 Mach %.3f  OpenAP ceiling FL%03d  MMO %.2f" % (
        r["type"], "（同义映射，只作量级参考）" if r.get("synonym") else "", r["mass"], "开" if r["wave"] else "关", r["mach_ref"], round(r["ceil_fl"]), r["mmo"]))
    if r.get("opt_fl") is None:
        print("  参考 Mach 上没有满足剩余爬升率的高度（不计推力约束的 OPT %s，MAX 0 fpm %s）" % (
            _fl(r.get("opt_free_fl")), _fl(r.get("max0_fl"))))
    else:
        print("  OPT %s%s（不计推力约束 %s）；MAX（剩余爬升率 0 / 300 fpm）%s / %s；OPT 处推力比 %.2f" % (
            _fl(r["opt_fl"]), "（顶到约束）" if r.get("opt_capped") else "", _fl(r.get("opt_free_fl")),
            _fl(r["max0_fl"]), _fl(r["max300_fl"]), r["thrust_ratio_opt"]))
        print("  OPT 附近 SR 损失：−2000 ft %s，+2000 ft %s，−4000 ft %s，+4000 ft %s；MRC %s，LRC %s" % (
            _pct(r["sr_loss_m2000"]), _pct(r["sr_loss_p2000"]), _pct(r["sr_loss_m4000"]), _pct(r["sr_loss_p4000"]),
            "M%.3f" % r["mrc_mach"] if r.get("mrc_mach") else "—", "M%.3f" % r["lrc_mach"] if r.get("lrc_mach") else "—"))
    for label, e in r["econ"].items():
        if e is None:
            print("  CI %s：无可行点" % label)
            continue
        print("  CI %-5s（%.1f kg/min）→ ECON %s M%.3f%s，%.2f kg/NM" % (
            label, e["ci_kgmin"], _fl(e["fl"]), e["mach"], "（Mach 顶到 MMO−0.02）" if e["at_mach_cap"] else "",
            e["kg_per_nm"]))


# ---------------------------------------------------------------- flight
def cmd_flight(args):
    top = mods(top=True)["opentop"]
    oa = _MODS["openap"]
    k = ci_k(top)
    objective, custom = "fuel", None
    if args.objective:
        objective = args.objective
    if args.ci_airbus is not None or args.ci_boeing is not None:
        ci = args.ci_airbus if args.ci_airbus is not None else args.ci_boeing * KGMIN_PER_BOEING
        objective = "ci:%.4f" % ci_to_weight(ci, k)
    if args.custom_ci is not None:
        custom = args.custom_ci
    if objective.startswith("ci:") and float(objective[3:]) >= 100:
        die(2, "ci:N 须 < 100（N ≥ 100 时燃油权重为 0 或负）；要最短时间请用 --objective time")
    a, _ = coords(args.orig, args.coords)
    b, _ = coords(args.dest, args.coords)
    dist, _trk = gc(a, b)
    if args.payload is None:
        m = Model(oa, args.type, synonym=args.synonym, eng=args.engine)
        tow = args.tow or args.m0 * float(m.ac["mtow"])
        land = rough_landing(m, tow, dist)
        if land > float(m.ac["mlw"]):
            note("粗估落地质量 %.0f kg > MLW %.0f kg：opentop 会以「final mass … larger than MLW」失败，请降低 --m0 / --tow"
                 % (land, float(m.ac["mlw"])))
    r = run_flight(args.type, args.orig, args.dest, args.m0, args.tow, args.payload, objective, custom, args.h_max,
                   args.nodes, args.max_iter, args.synonym, args.engine, args.return_failed, args.coords)
    print_flight(r, k)
    runs = [r]
    if args.snap and r.get("df") is not None and r.get("legal_fl"):
        r2 = run_flight(args.type, args.orig, args.dest, args.m0, args.tow, args.payload, objective, custom,
                        r["legal_fl"], args.nodes, args.max_iter, args.synonym, args.engine, args.return_failed,
                        args.coords)
        print("— 贴到合法高度层 FL%03d 后重解：" % r["legal_fl"])
        print_flight(r2, k)
        if r2.get("df") is not None:
            print("  较连续高度多耗油 %.0f kg（%.1f%%）" % (r2["fuel_kg"] - r["fuel_kg"], (r2["fuel_kg"] / r["fuel_kg"] - 1) * 100))
        runs.append(r2)
    if args.compare_eta:
        e = eta_compare(args.type, a, b)
        if e is None:
            print("本项目 eta：机型库没有性能数据")
        else:
            print("本项目 eta（同一大圆、无风）：FL%03d，空中 %.0f 分钟%s；opentop 巡航均高 FL%03d，空中 %.0f 分钟" % (
                e["fl"], e["air_min"], "（机型按 %s 估）" % e["perf_id"] if e["fallback"] else "",
                round(r.get("alt_mean", 0) / 100) if r.get("df") is not None else 0, r.get("air_min") or 0))
    if args.json:
        Path(args.json).write_text(json.dumps([{k_: v for k_, v in x.items() if k_ != "df"} for x in runs],
                                              ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return 0 if all(x.get("df") is not None for x in runs) else 1


def describe_objective(obj, k):
    if obj.startswith("ci:"):
        n = float(obj[3:])
        ci = weight_to_ci(n, k)
        return "ci:%g（≈ 航司 CI %.1f kg/min = Boeing %.0f）" % (n, ci, ci / KGMIN_PER_BOEING)
    return obj


def print_flight(r, k):
    print("%s %s→%s  大圆 %.0f NM  坐标 %s  起飞质量 %.0f kg（%.2f MTOW）  目标 %s%s  节点 %d" % (
        r["type"], r["orig"], r["dest"], r["gc_nm"], r["coords"], r["m0_kg"], r["m0_frac"],
        describe_objective(r["objective"], k), "  高度上限 FL%03d" % r["h_max_fl"] if r["h_max_fl"] else "",
        r["nodes"]))
    print("  求解：%s，%s 次迭代，%.1f 秒" % (r["status"], r["iters"], r["sec"]))
    for w in r["warnings"]:
        print("  opentop 警告：%s" % w)
    if r.get("df") is None:
        print("  ❌ 无结果")
        return
    print("  航程油耗 %.0f kg，空中 %.1f 分钟，TOC 距起点 %.0f NM，航迹 %.0f NM" % (
        r["fuel_kg"], r["air_min"], r["toc_nm"], r["path_nm"]))
    print("  巡航高度 TOC %.0f / 均 %.0f / 最高 %.0f ft（%s方向、接近巡航均高的合法层：%s）；Mach %.3f（%.3f–%.3f）；巡航最大 |VS| %.0f fpm" % (
        r["alt_toc"], r["alt_mean"], r["alt_max"], "东" if r["east"] else "西", _fl(r["legal_fl"]), r["mach_mean"],
        r["mach_min"], r["mach_max"], r["cruise_vs_max"]))
    print("  落地质量 %.0f kg（MLW %.0f）" % (r["land_kg"], r["mlw"]))
    if r.get("mlw_tight"):
        print("  ⚠️ 落地质量贴着 MLW：多半是起飞太重、优化器靠低空慢飞烧油来满足 MLW 的病态解，结果不可用；请降低起飞质量")


# ---------------------------------------------------------------- matrix
ROUTES = [("RJTT", "RJOO"), ("RJTT", "RJFF"), ("RJTT", "RJCC"), ("RJTT", "ROAH"), ("RJCC", "ROAH")]


def _t3_cases():
    c = []
    for o, d in ROUTES:
        c.append(("B1", "A320", o, d, dict(m0=0.85)))
    for ci in (10, 30, 60, 100):
        c.append(("B2", "A320", "RJTT", "RJFF", dict(m0=0.85, ci_airbus=ci)))
    c.append(("B2", "A320", "RJTT", "RJFF", dict(m0=0.85, objective="time")))
    c.append(("B2c", "A320", "RJTT", "RJFF", dict(m0=0.85, custom_ci=30)))
    for m0 in (0.75, 0.95):
        c.append(("B3", "A320", "RJTT", "RJFF", dict(m0=m0)))
    for t, m0 in (("B738", 0.85), ("E190", 0.85), ("B789", 0.70), ("A359", 0.70), ("B772", 0.70)):
        c.append(("B4", t, "RJTT", "RJFF", dict(m0=m0)))
        c.append(("B4", t, "RJTT", "RJFF", dict(m0=m0, ci_airbus=30)))
    for fl in (360, 300):
        c.append(("B5", "A320", "RJTT", "RJFF", dict(m0=0.85, h_max=fl)))
    c.append(("B6", "A320", "RJTT", "RJFF", dict(payload=15000)))
    c.append(("B7", "B772", "RJTT", "RJFF", dict(m0=0.85)))
    return c


def _t2_cases():
    out = []
    for t, fr in (("A320", (0.75, 0.85, 0.95)), ("B738", (0.75, 0.85, 0.95)), ("E190", (0.75, 0.85, 0.95)),
                  ("B789", (0.60, 0.70, 0.80)), ("A359", (0.60, 0.70, 0.80)), ("B772", (0.60, 0.70, 0.80))):
        for f in fr:
            for wave in (True, False):
                out.append((t, f, wave))
    return out


def _eea_table(path):
    """EEA/EMEP 2023 航空附表（Annex 1 master emissions calculator，© EUROCONTROL）→ {机型: [(NM, CCD kg)]}。
    只在本机读取，用于对照；表本身不复制、不入库。"""
    try:
        import openpyxl
    except ImportError:
        note("读 EEA 表需要 openpyxl（只用于 T4）：<venv>\\Scripts\\python -m pip install openpyxl")
        return None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    tab = {}
    for r in wb["database"].iter_rows(min_row=3, values_only=True):
        if r[1] and r[9] == "CCD" and isinstance(r[13], (int, float)):
            tab.setdefault(r[1].split("-")[0], []).append((float(r[11]), float(r[13]), r[10]))
    return {k: sorted(v) for k, v in tab.items()}


def _interp(pts, x):
    for (x0, y0, f0), (x1, y1, f1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0), (f0, f1)
    return None, None


# Airbus《Getting to Grips with Aircraft Performance》（2002）公开示例里的几个数，仅作对照（不是完整表格）：
#   p.148 图 F27（A320 FCOM 巡航表，M.78、ISA）：各重量在 FL290–FL390 里比航程最大的层、FL350 的 NM/1000 kg；
#   p.158 爬升例：松刹车 74 t、ISA、250 / 300 kt / M.78 爬到 FL330 → 23 分钟、146 NM、1,840 kg；
#   p.167 下降例：下降顶点 65 t、ISA、M.78 / 300 / 250 kt，FL370 → 1,500 ft → 16.2 分钟、98 NM、121 kg。
GTG_A320_BEST_FL = {54: 390, 62: 370, 66: 350, 70: 350, 74: 330}
GTG_A320_SR_FL350 = {54: 208.4, 62: 194.1, 66: 186.0, 70: 176.1, 74: 165.7}
GTG_CLIMB = {"mass": 74000, "top_ft": 33000, "min": 23.0, "nm": 146.0, "kg": 1840.0}
GTG_DESCENT = {"mass": 65000, "top_ft": 37000, "bottom_ft": 1500, "min": 16.2, "nm": 98.0, "kg": 121.0}


def schedule_burn(m, mass, alt_from, alt_to, perf, climb=True):
    """按本项目 eta 的速度计划与升降率（perf.clb / des、roc / rod）逐 1000 ft 积分，油耗用 OpenAP FuelFlow。
    → (分钟, NM, kg)。只用于和 Airbus 爬升 / 下降例子对照。"""
    eta, _ = project()
    step = 1000.0 if climb else -1000.0
    alt, t_min, nm, kg = float(alt_from), 0.0, 0.0, 0.0
    while (alt < alt_to - 1e-6) if climb else (alt > alt_to + 1e-6):
        a2 = min(alt + step, alt_to) if climb else max(alt + step, alt_to)
        mid = (alt + a2) / 2
        if climb:
            cas_lo, cas_hi, mach = perf["clb"]
            r = perf["roc"][0] if mid < 10000 else (perf["roc"][1] if mid < 24000 else perf["roc"][2])
        else:
            mach, cas_hi, cas_lo = perf["des"]
            r = perf["rod"][0] if mid >= 10000 else perf["rod"][1]
        tas = eta._jet_tas(cas_lo if mid < 10000 else cas_hi, mach, mid)
        dt = abs(a2 - alt) / float(r)
        ff = float(m.ff.enroute(mass=mass, tas=tas, alt=mid, vs=r if climb else -r)) * 60.0   # kg/min
        t_min += dt
        nm += tas * dt / 60.0
        kg += ff * dt
        mass -= ff * dt
        alt = a2
    return t_min, nm, kg


def _lto_air(oa, t, synonym):
    """ICAO 标准 LTO 的空中部分（起飞 0.7 + 爬升 2.2 + 进近 4.0 分钟）油量，按 OpenAP 的发动机数据。"""
    a = oa.prop.aircraft(t, use_synonym=synonym)
    e = oa.prop.engine(a["engine"]["default"])
    return int(a["engine"]["number"]) * 60 * (e["ff_to"] * 0.7 + e["ff_co"] * 2.2 + e["ff_app"] * 4.0)


def cmd_matrix(args):
    groups = set(args.case or ["T0", "T1", "T2", "T3", "T4"])
    if args.list:
        print("T0 环境与 API（env 全部检查）；T1 机型覆盖；T2 核心网格（6 型 × 3 质量 × 波阻开/关）；")
        print("T3 opentop：" + "，".join("%s %s %s→%s %s" % (i, t, o, d, kw) for i, t, o, d, kw in _t3_cases()))
        print("T4 公开资料对照（WRAP 区间、Airbus GTG 巡航表 / 爬升 / 下降例子、Boeing 757 例子、EEA 表需 --eea）")
        return 0
    rc = 0
    k = ci_k()
    if "T0" in groups:
        print("\n## T0 环境与 API\n")
        rows = env_checks()
        print("| 编号 | 检查 | 结果 | 实际 |\n| --- | --- | --- | --- |")
        for cid, desc, ok, val in rows:
            print("| %s | %s | %s | %s |" % (cid, desc, "✅" if ok else "❌", val))
            rc |= 0 if ok else 1
        oa = _MODS["openap"]
        v = float(oa.FuelFlow("A320").enroute(mass=60000, tas=250, alt=30000))
        print("\nT0.3 README 示例输入（A320、60 t、250 kt、FL300、平飞）：FuelFlow.enroute = %.3f kg/s" % v)
    if "T1" in groups:
        print("\n## T1 机型覆盖\n")
        cmd_fleet(argparse.Namespace(types=None, json=None))
    t2 = {}
    if "T2" in groups or "T4" in groups:
        oa = mods()["openap"]
        for t, f, wave in _t2_cases():
            m = Model(oa, t, wave_drag=wave)
            t2[(t, f, wave)] = grid_analysis(m, f * float(m.ac["mtow"]), boeing=(20, 50))
    if "T2" in groups:
        print("\n## T2 核心网格（剩余爬升率 300 fpm、Mach ≤ MMO−0.02、无风、ISA）\n")
        print("| 机型 | 质量 | 波阻 | 参考 Mach | OPT | OPT（不计推力） | MAX 0 / 300 fpm | ceiling | SR 损失 −/+2000 ft | SR 损失 −/+4000 ft | MRC / LRC |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for (t, f, wave), r in t2.items():
            print("| %s%s | %.0f t（%.2f MTOW） | %s | %.3f | %s%s | %s | %s / %s | FL%03d | %s / %s | %s / %s | %s / %s |" % (
                t, "（同义）" if r.get("synonym") else "", r["mass"] / 1000, f, "开" if wave else "关", r["mach_ref"],
                _fl(r.get("opt_fl")), "*" if r.get("opt_capped") else "", _fl(r.get("opt_free_fl")), _fl(r["max0_fl"]),
                _fl(r["max300_fl"]), round(r["ceil_fl"]),
                _pct(r.get("sr_loss_m2000")), _pct(r.get("sr_loss_p2000")), _pct(r.get("sr_loss_m4000")),
                _pct(r.get("sr_loss_p4000")), "M%.3f" % r["mrc_mach"] if r.get("mrc_mach") else "—",
                "M%.3f" % r["lrc_mach"] if r.get("lrc_mach") else "—"))
        print("\n（* = OPT 顶到剩余爬升率或 ceiling 约束）\n")
        labels = ["0", "10", "30", "60", "100", "B20", "B50"]
        print("| 机型 | 质量 | " + " | ".join("CI %s" % x for x in labels) + " |")
        print("| --- | --- | " + " | ".join("---" for _ in labels) + " |")
        for (t, f, wave), r in t2.items():
            if not wave:
                continue
            cells = []
            for lb in labels:
                e = r["econ"].get(lb)
                cells.append("—" if not e else "%s M%.3f%s" % (_fl(e["fl"]), e["mach"], "*" if e["at_mach_cap"] else ""))
            print("| %s | %.2f MTOW | %s |" % (t, f, " | ".join(cells)))
        print("\n（波阻开；CI 单位 kg/min，B20 / B50 = Boeing 100 lb/h；* = Mach 顶到 MMO−0.02）\n")
        print("验收：")
        checks = []
        for (t, f, wave), r in t2.items():
            seq = [r["econ"][x] for x in ("0", "10", "30", "60", "100") if r["econ"].get(x)]
            checks.append(("%s %.2f %s ECON Mach 随 CI 不降" % (t, f, "开" if wave else "关"),
                           all(b_["mach"] >= a_["mach"] - 1e-9 for a_, b_ in zip(seq, seq[1:]))))
            checks.append(("%s %.2f %s CI 最优 FL 随 CI 不升（容差一层）" % (t, f, "开" if wave else "关"),
                           all(b_["fl"] <= a_["fl"] + 10 for a_, b_ in zip(seq, seq[1:]))))
            if r["max0_fl"] and r["max300_fl"]:
                checks.append(("%s %.2f %s MAX(300) ≤ MAX(0) ≤ ceiling" % (t, f, "开" if wave else "关"),
                               r["max300_fl"] <= r["max0_fl"] <= r["ceil_fl"] + 1e-9))
        types = sorted({x[0] for x in t2})
        for t in types:
            fr = sorted({x[1] for x in t2 if x[0] == t})
            for wave in (True, False):
                opts = [t2[(t, f, wave)]["opt_fl"] for f in fr]
                checks.append(("%s 波阻%s OPT 随质量不升" % (t, "开" if wave else "关"),
                               all(o2 is None or o1 is None or o2 <= o1 + 1e-9 for o1, o2 in zip(opts, opts[1:]))))
            for f in fr:
                on, off = t2[(t, f, True)]["opt_fl"], t2[(t, f, False)]["opt_fl"]
                checks.append(("%s %.2f 开波阻的 OPT ≤ 不开" % (t, f), on is None or off is None or on <= off + 1e-9))
        bad = [c for c in checks if not c[1]]
        print("  %d 项，未通过 %d 项%s" % (len(checks), len(bad), "：" + "；".join(c[0] for c in bad) if bad else ""))
        rc |= 1 if bad else 0
    t3 = []
    if "T3" in groups or "T4" in groups:
        if args.skip_opentop:
            print("\n（--skip-opentop：跳过 T3）")
        else:
            sel = set(args.only or [])
            for cid, t, o, d, kw in _t3_cases():
                if sel and cid not in sel:
                    continue
                r = run_flight(t, o, d, m0=kw.get("m0", 0.85), payload=kw.get("payload"),
                               objective=("ci:%.4f" % ci_to_weight(kw["ci_airbus"], k)) if "ci_airbus" in kw
                               else kw.get("objective", "fuel"), custom_ci=kw.get("custom_ci"), h_max_fl=kw.get("h_max"))
                r["id"], r["kw"] = cid, kw
                r["eta"] = eta_compare(t, *(coords(x)[0] for x in (o, d)))
                t3.append(r)
                print("  … %s %s %s→%s %s：%s，%.0f 秒" % (cid, t, o, d, r["objective"], r["status"], r["sec"]),
                      file=sys.stderr)
    if "T3" in groups and t3:
        print("\n## T3 opentop CompleteFlight（无风、ISA；高度为连续量）\n")
        print("| 编号 | 机型 | 航线 | 大圆 NM | 起飞质量 | 目标 | 状态 / 秒 | 油耗 kg | 空中 min | TOC NM | 巡航高度 TOC / 均 / 高（ft） | 合法层 | Mach 均（范围） | 落地 / MLW | eta FL / min |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in t3:
            e = r["eta"]
            et = "FL%03d / %.0f" % (e["fl"], e["air_min"]) if e else "—"
            if r.get("df") is None:
                print("| %s | %s | %s→%s | %.0f | %.0f kg（%.2f） | %s | %s / %.0f | ❌ 无结果：%s | | | | | | | %s |" % (
                    r["id"], r["type"], r["orig"], r["dest"], r["gc_nm"], r["m0_kg"], r["m0_frac"],
                    describe_objective(r["objective"], k), r["status"], r["sec"], "；".join(r["warnings"]) or "—", et))
                continue
            print("| %s | %s | %s→%s | %.0f | %.0f kg（%.2f） | %s | %s / %.0f | %.0f | %.1f | %.0f | %.0f / %.0f / %.0f | %s | %.3f（%.3f–%.3f） | %.0f / %.0f%s | %s |" % (
                r["id"], r["type"], r["orig"], r["dest"], r["gc_nm"], r["m0_kg"], r["m0_frac"],
                describe_objective(r["objective"], k) + ("，上限 FL%03d" % r["h_max_fl"] if r["h_max_fl"] else "")
                + ("，payload %.0f kg" % r["payload"] if r["payload"] else ""), r["status"], r["sec"], r["fuel_kg"],
                r["air_min"], r["toc_nm"], r["alt_toc"], r["alt_mean"], r["alt_max"], _fl(r["legal_fl"]),
                r["mach_mean"], r["mach_min"], r["mach_max"], r["land_kg"], r["mlw"],
                " ⚠️ 贴 MLW（病态解）" if r.get("mlw_tight") else "", et))
            if r["status"] not in ("Solve_Succeeded", "Solved_To_Acceptable_Level") and r["id"] != "B7":
                rc |= 1
        print("\n验收（有结果的用例）：")
        checks = []
        for r in t3:
            if r.get("df") is None:
                continue
            tag = "%s %s %s %s" % (r["id"], r["ac"], r["orig"] + "→" + r["dest"], r["objective"])
            top = min(r["ceil_ft"], (r["h_max_fl"] or 1e9) * 100.0)
            checks += [(tag + " 求解成功", r["status"] in ("Solve_Succeeded", "Solved_To_Acceptable_Level")),
                       (tag + " 高度 ≤ min(ceiling, h_max)", r["alt_max"] <= top + 50),
                       (tag + " Mach ≤ MMO", r["mach_max"] <= r["mmo"] + 1e-3),
                       (tag + " 落地 ≤ MLW", r["land_kg"] <= r["mlw"] + 1),
                       (tag + " 落地不贴 MLW（非病态解）", not r["mlw_tight"] or r["id"] == "B7")]
        bad = [c for c in checks if not c[1]]
        print("  %d 项，未通过 %d 项%s" % (len(checks), len(bad), "：" + "；".join(c[0] for c in bad) if bad else ""))
        rc |= 1 if bad else 0
        far = ["%s %s %s→%s %s（eta %.0f / opentop %.0f 分钟）" % (r["id"], r["ac"], r["orig"], r["dest"], r["objective"],
                                                              r["eta"]["air_min"], r["air_min"])
               for r in t3 if r.get("df") is not None and r.get("eta") and not r["mlw_tight"]
               and abs(r["eta"]["air_min"] / r["air_min"] - 1) > 0.10]
        print("与本项目 eta 的空中时间相差 > 10%%（只报告）：%s" % ("；".join(far) if far else "无"))
        ci30 = next((r for r in t3 if r["id"] == "B2" and r["kw"].get("ci_airbus") == 30 and r.get("df") is not None), None)
        cust = next((r for r in t3 if r["id"] == "B2c" and r.get("df") is not None), None)
        if ci30 and cust:
            df_ = abs(ci30["fuel_kg"] / cust["fuel_kg"] - 1)
            dt_ = abs(ci30["air_min"] / cust["air_min"] - 1)
            ok = df_ <= 0.005 and dt_ <= 0.005
            print("\n换算验证（B2 Airbus CI 30 ↔ B2c 燃油 + 30×分钟）：油耗差 %.2f%%，时间差 %.2f%% → %s" % (
                df_ * 100, dt_ * 100, "✅" if ok else "❌"))
            rc |= 0 if ok else 1
    if "T4" in groups:
        oa = mods()["openap"]
        print("\n## T4 公开资料对照（只报告，不判定）\n")
        print("| 机型 | WRAP 巡航高度众数（区间） | 核心模型 OPT（0.85 / 0.70 MTOW，波阻开） |")
        print("| --- | --- | --- |")
        for t in ("A320", "B738", "E190", "B789", "A359", "B772"):
            try:
                w = oa.WRAP(t).cruise_alt()
                wt = "FL%03d（FL%03d–FL%03d）" % (round(w["default"] * 1000 / FT / 100), round(w["minimum"] * 1000 / FT / 100),
                                               round(w["maximum"] * 1000 / FT / 100))
            except Exception as e:  # noqa: BLE001
                wt = "—（%s）" % type(e).__name__
            f = 0.85 if t in ("A320", "B738", "E190") else 0.70
            r = t2.get((t, f, True))
            print("| %s | %s | %s |" % (t, wt, _fl(r["opt_fl"]) if r else "—"))
        a320 = Model(oa, "A320", wave_drag=True)
        print("\nAirbus GTG p.148 A320 巡航表（M.78、ISA）vs OpenAP 核心模型（M0.78、波阻开、剩余爬升率 300 fpm）：\n")
        print("| 重量 | 表中比航程最大的层（FL290–390，每 2000 ft） | OpenAP OPT | FL350 NM/1000 kg：表 | OpenAP | 比值 |")
        print("| --- | --- | --- | --- | --- | --- |")
        for w, best in GTG_A320_BEST_FL.items():
            r = grid_analysis(a320, w * 1000.0, mach_ref=0.78, ci_list=())
            e = a320.evaluate(w * 1000.0, [350], [0.78])
            sr = float(e["sr"][0, 0]) * 1000
            print("| %d t | FL%03d | %s | %.1f | %.1f | %.2f |" % (w, best, _fl(r["opt_fl"]), GTG_A320_SR_FL350[w], sr,
                                                                  sr / GTG_A320_SR_FL350[w]))
        eta, aircraft = project()
        with contextlib.redirect_stdout(io.StringIO()):
            aircraft.load_aircraft_db()
            perf = aircraft.perf_of("A320")[0]
        path = eta.Path([(35.0, 133.0), (35.0, 143.0)])
        c = eta._climb(path, perf, GTG_CLIMB["top_ft"], None, None)
        cb = schedule_burn(a320, GTG_CLIMB["mass"], 0, GTG_CLIMB["top_ft"], perf, climb=True)
        d = eta._descent(path, perf, GTG_DESCENT["top_ft"], None, None)
        db = schedule_burn(a320, GTG_DESCENT["mass"], GTG_DESCENT["top_ft"], GTG_DESCENT["bottom_ft"], perf, climb=False)
        print("\n| Airbus 例子（GTG） | Airbus | 本项目 eta（A320 perf） | eta 剖面 + OpenAP 油耗 |")
        print("| --- | --- | --- | --- |")
        print("| p.158 爬升 74 t → FL330 | %.0f 分钟 / %.0f NM / %.0f kg | %.1f 分钟 / %.0f NM | %.1f 分钟 / %.0f NM / %.0f kg |" % (
            GTG_CLIMB["min"], GTG_CLIMB["nm"], GTG_CLIMB["kg"], c[1], c[0], cb[0], cb[1], cb[2]))
        print("| p.167 下降 65 t FL370 → 1500 ft | %.1f 分钟 / %.0f NM / %.0f kg | %.1f 分钟 / %.0f NM（到地面，含最后 10 NM 进近） | %.1f 分钟 / %.0f NM / %.0f kg |" % (
            GTG_DESCENT["min"], GTG_DESCENT["nm"], GTG_DESCENT["kg"], d[1], d[0], db[0], db[1], db[2]))
        m = Model(oa, "B752", wave_drag=True)
        r0 = grid_analysis(m, 0.80 * float(m.ac["mtow"]), ci_list=(0, 999))
        print("\nBoeing 757 例子：CI 0 → 最大时 OPT 由 FL328 降到 FL268（−6000 ft）。核心模型 B752（0.80 MTOW，波阻开）："
              "CI 0 的 ECON 层 %s、CI 999 kg/min 的 ECON 层 %s（Mach %.3f）" % (
                  _fl(r0["econ"]["0"]["fl"]), _fl(r0["econ"]["999"]["fl"]), r0["econ"]["999"]["mach"]))
        if args.eea:
            tab = _eea_table(args.eea)
            if tab:
                print("\nEEA/EMEP 2023 航空附表（CCD + ICAO 标准 LTO 的空中部分，按大圆插值）vs opentop 燃油目标：\n")
                print("| 机型 | 航线 | 大圆 NM | EEA CCD kg（表中巡航层） | LTO 空中 kg | EEA 合计 | opentop kg | 比值 |")
                print("| --- | --- | --- | --- | --- | --- | --- | --- |")
                for r in t3:
                    if r.get("df") is None or r["objective"] != "fuel" or r["h_max_fl"] or r["payload"] or r["id"] not in ("B1", "B4"):
                        continue
                    pts = tab.get(r["ac"])
                    if not pts:
                        continue
                    ccd, fls = _interp(pts, r["gc_nm"])
                    if ccd is None:
                        continue
                    lto = _lto_air(oa, r["ac"], needs_synonym(r["ac"]))
                    tot = ccd + lto
                    print("| %s | %s→%s | %.0f | %.0f（FL%s–FL%s） | %.0f | %.0f | %.0f | %.2f |" % (
                        r["ac"], r["orig"], r["dest"], r["gc_nm"], ccd, fls[0], fls[1], lto, tot, r["fuel_kg"],
                        r["fuel_kg"] / tot))
                print("\n（EEA 表的质量与载重假设不明；opentop 起飞质量见 T3。数值 © EUROCONTROL，仅作对照。）")
    if args.json and t3:
        Path(args.json).write_text(json.dumps([{k_: v for k_, v in x.items() if k_ != "df"} for x in t3],
                                              ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return rc


# ---------------------------------------------------------------- CLI
def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    quiet_warnings()
    p = argparse.ArgumentParser(prog="openap_check", description="OpenAP / opentop 对照（开发用 venv 专用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("env", help="版本 + 文档里每个 API 调用的冒烟检查")
    e.add_argument("--sig", action="store_true", help="另打印各函数签名")

    f = sub.add_parser("fleet", help="日本机队在 OpenAP 里的数据覆盖")
    f.add_argument("--types", nargs="*", help="机型（ICAO），缺省为常用的 21 种")
    f.add_argument("--json")

    g = sub.add_parser("grid", help="核心模型：FL × Mach 网格上的 OPT / MAX / MRC / LRC / 各 CI 的 ECON")
    g.add_argument("type")
    mg = g.add_mutually_exclusive_group(required=True)
    mg.add_argument("--mass", type=float, help="质量 kg")
    mg.add_argument("--mass-frac", type=float, help="质量占 MTOW 的比例")
    mg.add_argument("--mass-sweep", help="a:b:n，按 MTOW 比例从 a 到 b 取 n 个质量（看阶梯爬升点）")
    g.add_argument("--mach", type=float, help="OPT / MAX 用的参考 Mach（缺省 WRAP 巡航 Mach）")
    g.add_argument("--ci-airbus", default="0,10,30,60,100", help="CI 列表，kg/min")
    g.add_argument("--ci-boeing", help="CI 列表，100 lb/h")
    g.add_argument("--fl-min", type=int, default=200)
    g.add_argument("--fl-max", type=int)
    g.add_argument("--h-max", type=int, help="高度上限 FL")
    g.add_argument("--resid-roc", type=float, default=300.0, help="最小剩余爬升率 fpm（REC MAX 口径 300）")
    g.add_argument("--track", type=float, help="真航迹°：只在东单西双的合法层里找 ECON（磁差按 eta.MAG_VAR_W）")
    g.add_argument("--wind", help="风廓线 'FL:kt,…'，正 = 顺风，例 '300:-40,380:-80'")
    g.add_argument("--cl-max", type=float, help="抖振 CL 上限（自备假设；按 1.3 g 检查 CL×1.3 ≤ 它）")
    g.add_argument("--dT", type=float, default=0.0, help="ISA 温度偏差 K")
    g.add_argument("--no-wave-drag", action="store_true")
    g.add_argument("--synonym", action="store_const", const=True, default=None,
                   help="强制同义映射（缺省按数据自动判断，结果只作量级参考）")
    g.add_argument("--engine")
    g.add_argument("--json")

    fl = sub.add_parser("flight", help="opentop CompleteFlight 一次")
    fl.add_argument("type")
    fl.add_argument("orig")
    fl.add_argument("dest")
    mm = fl.add_mutually_exclusive_group()
    mm.add_argument("--m0", type=float, default=0.85, help="起飞质量占 MTOW 的比例（opentop 缺省 0.85）")
    mm.add_argument("--tow", type=float, help="起飞质量 kg")
    mm.add_argument("--payload", type=float, help="业载 kg（opentop 解出起飞质量；落地质量下限 = OEW + 业载）")
    ob = fl.add_mutually_exclusive_group()
    ob.add_argument("--objective", help="fuel / time / ci:N（N 为 0–100 的权重）")
    ob.add_argument("--ci-airbus", type=float, help="航司 CI，kg/min（自动换算成 ci:N）")
    ob.add_argument("--ci-boeing", type=float, help="航司 CI，100 lb/h（自动换算成 ci:N）")
    ob.add_argument("--custom-ci", type=float, help="自定义目标「燃油 + X·分钟」（验证换算用）")
    fl.add_argument("--h-max", type=int, help="高度上限 FL")
    fl.add_argument("--snap", action="store_true", help="贴到最接近巡航均高（不高于最高点）的合法层，再以它为上限重解")
    fl.add_argument("--compare-eta", action="store_true", help="并列本项目 eta 的 FL 与空中时间")
    fl.add_argument("--coords", choices=("auto", "navdata", "openap"), default="auto")
    fl.add_argument("--nodes", type=int)
    fl.add_argument("--max-iter", type=int, default=3000)
    fl.add_argument("--synonym", action="store_const", const=True, default=None, help="强制同义映射（缺省自动）")
    fl.add_argument("--engine")
    fl.add_argument("--return-failed", action="store_true")
    fl.add_argument("--json")

    mx = sub.add_parser("matrix", help="跑 validation.md 的实测矩阵并输出其表格")
    mx.add_argument("--case", nargs="*", choices=("T0", "T1", "T2", "T3", "T4"))
    mx.add_argument("--only", nargs="*", help="T3 只跑这些编号（B1 … B7）")
    mx.add_argument("--list", action="store_true")
    mx.add_argument("--skip-opentop", action="store_true")
    mx.add_argument("--eea", help="EEA/EMEP 2023 航空附表 xlsx 的本机路径（T4，需 openpyxl）")
    mx.add_argument("--json")

    args = p.parse_args(argv)
    return {"env": cmd_env, "fleet": cmd_fleet, "grid": cmd_grid, "flight": cmd_flight, "matrix": cmd_matrix}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
