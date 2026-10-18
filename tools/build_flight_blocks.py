# ================= 生成 flight_blocks.json（F30：模拟呼号按真实航班号范式选号）=================
# 用法（在项目根目录）：python tools/build_flight_blocks.py [输出路径，默认项目根的 flight_blocks.json]
#
# 串行抓 FlightAware findflight（每次间隔 0.9 秒），记下各航司各航线【实测出现过的确切航班号】，
# 原子写出 flight_blocks.json——运行目录的随包静态数据，同 transfer_points.json。
# 采样哪些航线：运行目录 routes_cache.csv 里【全部官方 AIP 航路的机场对】（现实中在飞的航线一定有 AIP 航路，
# 实测约 311 对、640 次请求、15 分钟左右）∪ 下面的内置清单；没有 routes_cache.csv（先运行一次程序即生成）时只用内置清单。
# 覆盖越全越好：号段表漏掉的航线，其真实号码会混在「对口百位」的候选里白白耗掉核验次数。
# 每个航季（3 月底 / 10 月底换季）后可重跑更新。只读网络；除输出文件外不写任何东西。
#
# 为什么存确切号码、不存号段区间：空号常藏在号段内部的空洞里（实测 ANA 宫崎段 602–618 内的
# 606/607/611/615/616 都是空号），按区间存会把它们一并当成真号扣掉。
#
# 末尾用本次数据校验 airlines.direction_parity（奇偶 = 走向）：准确率低于 98% 说明规则或数据有问题。
# 表里的号码按方向分存（down = 上位端→下位端、up = 反方向），所以改了奇偶规则后不必重抓：
#   python tools/build_flight_blocks.py --revalidate   ← 不联网，按当前规则重算上下位 / 类别 / 地区并重验奇偶。
# 依赖本机 NavData（取机场坐标，用来算「下位端在哪个地区」与等级相同时的上下位）。

import csv
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dispatcher import airlines, flightaware, regions          # noqa: E402
from dispatcher.data import load_airports_from_navigraph       # noqa: E402
from dispatcher.navdata import find_navdata_file               # noqa: E402

# 采样的机场对（双向都抓）：羽田 ↔ 45 个机场，以及大阪 / 名古屋 / 福冈 / 札幌 / 那霸 / 成田之间的主要航线
_HND = ["RJOO", "RJBB", "RJCC", "RJFF", "ROAH", "RJFK", "RJFT", "RJOA", "RJFU", "RJFM", "RJFO", "RJOM", "RJOT",
        "RJOK", "RJOS", "RJOB", "RJOC", "RJDC", "RJFR", "RJNK", "RJNT", "RJEC", "RJCH", "RJCK", "RJCB", "RJCM",
        "RJSA", "RJSK", "RJSM", "RJSC", "RJSY", "ROIG", "ROMY", "RJFS", "RJNW", "RJBE", "RJCW", "RJTH", "RJOH",
        "RJOR", "RJOW", "RJBD", "RJSR", "RJKA", "RJOI"]
PAIRS = ([("RJTT", x) for x in _HND]
         + [("RJOO", x) for x in ["RJCC", "ROAH", "RJFK", "RJSS", "RJSA", "RJSN", "RJFU", "RJFM", "RJOM", "RJCH"]]
         + [("RJBB", x) for x in ["RJCC", "ROAH", "ROIG"]]
         + [("RJGG", x) for x in ["RJCC", "ROAH", "RJFF", "RJFK"]]
         + [("RJFF", "RJCC"), ("RJFF", "ROAH"), ("RJCC", "ROAH"), ("ROAH", "ROIG"), ("ROAH", "ROMY"), ("ROAH", "RJKA")]
         + [("RJAA", x) for x in ["RJCC", "ROAH", "RJFF", "RJBB"]])
MERGE = {"AKX": "ANA"}          # ANA Wings 执飞的航班用的是 ANA 的号（同一号码空间）
SLEEP_S = 0.9
_GIVEUP = 3                     # 连续这么多次网络失败 → 中止（不写文件）

_COMMENT = ("各航司各航线【实测出现过的】航班号（FlightAware findflight 采集）。供模拟呼号按真实编号范式选号："
            "奇偶按走向、锁定对口百位，并精确避开这些已知真号（F30）。由 tools/build_flight_blocks.py 生成，"
            "每个航季可重跑；缺失或损坏时模拟呼号回退为均匀抽号。senior/junior = 上位端/下位端，"
            "senior_class = 上位端城市类别（TYO/OSA/NGO/FUK/SPK/OKA，其它为 null），junior_region = 下位端所在地区；"
            "down = 上位端→下位端实测的号（按规则应为奇数），up = 反方向实测的号（应为偶数）。")


def _sample_pairs():
    """内置清单 ∪ routes_cache.csv 里全部日本境内 AIP 航路的机场对（无向、去重）。"""
    pairs = {tuple(sorted(p)) for p in PAIRS}
    n_builtin = len(pairs)
    try:
        with open(os.path.join(ROOT, "routes_cache.csv"), encoding="utf-8", errors="ignore", newline="") as f:
            rows = csv.reader(f)
            next(rows, None)                                   # 表头 DEP,DEST,…
            for row in rows:
                if len(row) >= 2:
                    a, b = row[0].strip().upper(), row[1].strip().upper()
                    if a != b and len(a) == 4 and len(b) == 4 and a[:2] in ("RJ", "RO") and b[:2] in ("RJ", "RO"):
                        pairs.add(tuple(sorted((a, b))))
    except OSError:
        print("ℹ️ 没找到 routes_cache.csv（先运行一次程序即生成），只采样内置的 %d 对机场。" % n_builtin)
    return sorted(pairs)


def main(out_path):
    dat = find_navdata_file()
    apts = {a.code: a for a in load_airports_from_navigraph(dat, min_runway_ft=0)} if dat else {}
    if not apts:
        print("❌ 需要本机 NavData（用来取机场坐标），未写文件。")
        return 1
    codes = set(airlines._get_data()[0])                       # 只收 airlines.json 里的航司
    pairs = _sample_pairs()
    print("🔎 采样 %d 对机场（双向共 %d 次请求，约 %.0f 分钟）…" % (len(pairs), 2 * len(pairs), 2 * len(pairs) * 1.4 / 60))
    obs = {}                                                   # (航司, 出发, 到达) -> {号码}
    fails = blank = total = 0
    t0 = time.time()
    for dep, arr in pairs:
        for a, b in ((dep, arr), (arr, dep)):
            total += 1
            try:
                res = flightaware.findflight_direct_idents(a, b)
                fails = 0
            except Exception as e:                             # noqa: BLE001
                fails += 1
                print("⚠️ %s→%s 抓取失败（%s）" % (a, b, type(e).__name__))
                if fails >= _GIVEUP:
                    print("❌ 连续 %d 次失败，中止，未写文件。" % _GIVEUP)
                    return 1
                time.sleep(SLEEP_S)
                continue
            if res is None:
                blank += 1
            else:
                for ident in res:
                    m = re.match(r"([A-Z]+)(\d+)$", ident)
                    if m:
                        al = MERGE.get(m.group(1), m.group(1))
                        if al in codes:
                            obs.setdefault((al, a, b), set()).add(int(m.group(2)))
            if total % 100 == 0:
                print("   … %d/%d 次请求，已记下 %d 个航班号" % (total, 2 * len(pairs),
                                                          sum(len(v) for v in obs.values())), flush=True)
            time.sleep(SLEEP_S)
    if blank > total // 2:
        print("❌ 过半请求取不到排班数据（FlightAware 可能改版），未写文件。")
        return 1

    rows = _build_rows(obs, apts)
    _write(rows, out_path, time.strftime("%Y-%m-%d"))
    n_flights = sum(len(r["down"]) + len(r["up"]) for r in rows)
    print("✅ 已写出 %s：%d 条航线、%d 个航班号（%d 次请求，%.0f 秒）。" % (out_path, len(rows), n_flights, total, time.time() - t0))
    _report_parity(obs, apts)
    return 0


def _build_rows(obs, apts):
    """按方向的实测 {(航司, 出发, 到达): {号码}} → 表行：无向航线（上位端在前），号码按方向分存
    down（上位端→下位端，应为奇数）/ up（下位端→上位端，应为偶数）——保留方向，日后改奇偶规则可离线重验。"""
    routes = {}
    for (al, a, b), nums in sorted(obs.items()):
        A, B = apts.get(a), apts.get(b)
        if not A or not B:
            continue
        sj = airlines.senior_junior(al, A, B)
        senior, junior = sj if sj else (A, B)
        r = routes.setdefault((al, senior.code, junior.code), {
            "airline": al, "senior": senior.code, "junior": junior.code,
            "senior_class": airlines._city_class(senior.code),
            "junior_region": regions.region_of(junior.lat_dd, junior.lon_dd), "down": set(), "up": set()})
        r["down" if a == senior.code else "up"] |= nums
    return [dict(r, down=sorted(r["down"]), up=sorted(r["up"]))
            for r in sorted(routes.values(), key=lambda r: (r["airline"], r["senior"], r["junior"]))]


def _write(rows, out_path, as_of):
    """一条航线一行的 JSON，原子写（tmp → os.replace）。"""
    lines = ["{", '  "_comment": %s,' % json.dumps(_COMMENT, ensure_ascii=False),
             '  "as_of": "%s",' % as_of, '  "routes": [']
    for i, r in enumerate(rows):
        lines.append("    " + json.dumps(r, ensure_ascii=False) + ("," if i < len(rows) - 1 else ""))
    lines += ["  ]", "}", ""]
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    os.replace(tmp, out_path)


def _report_parity(obs, apts):
    """用实测方向校验 airlines.direction_parity，打印准确率与「多数号都对不上」的航线方向。"""
    ok = bad = 0
    worst = []
    for (al, a, b), nums in sorted(obs.items()):
        A, B = apts.get(a), apts.get(b)
        p = airlines.direction_parity(al, A, B) if A and B else None
        if p is None:
            continue
        hit = sum(1 for n in nums if n % 2 == p)
        ok, bad = ok + hit, bad + len(nums) - hit
        if len(nums) - hit > hit:
            worst.append("%s %s→%s 预测%s、实测 %s" % (al, a, b, "奇" if p else "偶", sorted(nums)))
    if ok + bad:
        print("🔍 奇偶 = 走向 规则校验：%d/%d = %.1f%%%s" % (ok, ok + bad, 100.0 * ok / (ok + bad),
              "" if ok / (ok + bad) >= 0.98 else "（低于 98%，请检查规则或数据）"))
    for w in worst:
        print("   ✗ " + w)


def revalidate(path):
    """不联网：按【当前】规则重算现有表的上下位、类别、地区并重验奇偶（改了 airlines 的规则后用）。
    需要表里带方向（down/up）；旧格式（只有 numbers）只能重算字段、无法重验。"""
    dat = find_navdata_file()
    apts = {a.code: a for a in load_airports_from_navigraph(dat, min_runway_ft=0)} if dat else {}
    doc = json.load(open(path, encoding="utf-8"))
    obs = {}
    for r in doc.get("routes") or []:
        s, j = r["senior"], r["junior"]
        if "down" in r or "up" in r:
            obs.setdefault((r["airline"], s, j), set()).update(r.get("down") or [])
            obs.setdefault((r["airline"], j, s), set()).update(r.get("up") or [])
        else:                                                   # 旧格式：按奇偶猜方向（只为重算字段）
            nums = r.get("numbers") or []
            obs.setdefault((r["airline"], s, j), set()).update(n for n in nums if n % 2)
            obs.setdefault((r["airline"], j, s), set()).update(n for n in nums if n % 2 == 0)
    has_dir = any("down" in r or "up" in r for r in doc.get("routes") or [])
    rows = _build_rows(obs, apts)
    if not has_dir:                                             # 旧格式：方向是猜的，不能当实测存下来（否则日后重验是循环论证）
        rows = [{k: v for k, v in dict(r, numbers=sorted(set(r["down"]) | set(r["up"]))).items()
                 if k not in ("down", "up")} for r in rows]
    _write(rows, path, doc.get("as_of") or time.strftime("%Y-%m-%d"))
    print("✅ 已按当前规则重算 %s：%d 条航线。" % (path, len(rows)))
    if has_dir:
        _report_parity(obs, apts)
    else:
        print("ℹ️ 旧格式表没有方向信息，无法重验奇偶；重跑一次完整采集即可得到带方向的新表。")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--revalidate":
        sys.exit(revalidate(args[1] if len(args) > 1 else os.path.join(ROOT, "flight_blocks.json")))
    sys.exit(main(args[0] if args else os.path.join(ROOT, "flight_blocks.json")))
