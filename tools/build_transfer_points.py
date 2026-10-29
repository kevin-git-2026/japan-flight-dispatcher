# ================= 重建 transfer_points.json（VATJPN「空港別移管高度表」→ 进/离场移管点）=================
# 用法（在项目根目录）：
#   python tools/build_transfer_points.py --list              ← 列出发布页里的全部标签页（AIRAC 名 → gid）
#   python tools/build_transfer_points.py                     ← 取 AIRAC 最新的那一页，重建 json + skill 文档
#   python tools/build_transfer_points.py --gid 437830123     ← 指定标签页（也可以直接贴整条 pubhtml 链接）
#   python tools/build_transfer_points.py --gid … --dry-run   ← 只打印变更清单，不写文件
#
# 抓取 / 解析 / STAR 配对都在 dispatcher/vatjpn.py（主程序启动时也用它自动更新 json，见该文件头部）；
# 本脚本是开发者用的命令行外壳，比主程序多做三件事：可指定任意一期、打印逐机场变更清单、重写 skill 文档。
# 与主程序的区别：不给 gid 时取 VATJPN【最新】一期（发版用），而主程序取「≤ 本机导航数据 AIRAC」的最新一期。
#
# 输出：
#   transfer_points.json（运行目录的随包静态数据，router 选进/离场端点、面板「门↔STAR 配对」用；带 `_vatjpn` 来源记录）；
#   .claude/skills/route_planning/reference/transfer_points.md 的头部 AIRAC 标注与「二、各机场」一节。
# 旧文件里带 `_source` 的人工条目（VATJPN 未覆盖的军用场 RJCJ / RJTL / RJTY）原样保留。
#
# ⚠️ STAR 配对按【本机 NavData 的 CIFP】现推——发版前先把本机导航数据更新到与所选那期同期或更新。
# 只读网络（docs.google.com）；除两个输出文件外不写任何东西。

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dispatcher import router, vatjpn                          # noqa: E402
from dispatcher.navdata import airac_cycle, find_navdata_file  # noqa: E402

JSON_OUT = os.path.join(ROOT, "transfer_points.json")
MD_OUT = os.path.join(ROOT, ".claude", "skills", "route_planning", "reference", "transfer_points.md")
_AIRAC = re.compile(r"AIRAC\s*(\d{4})")


# ---------- skill 文档 ----------
def build_md(old_md, out, regions, airports, manual, label):
    """重写头部 AIRAC 标注与「## 二」整节；「## 一」不动；人工条目从旧文档原样摘出，放到其管制区下（表里没有该区 → 末尾一节）。"""
    head, sep, sec2 = old_md.partition("\n## 二")
    if not sep:
        raise SystemExit("❌ transfer_points.md 里找不到「## 二」一节")
    head = _AIRAC.sub(label, head)
    lines = (sep.lstrip("\n") + sec2).split("\n")
    intro, body = [], []
    for i, ln in enumerate(lines):
        if ln.startswith("### "):
            body = lines[i:]
            break
        intro.append(_AIRAC.sub(label, ln))
    blocks, cur = {}, None                                      # 旧文档里人工条目的原文块
    for ln in body:
        m = re.match(r"- \*\*(R[JO][A-Z]{2})\*\*", ln)
        if m:
            cur = m.group(1) if m.group(1) in manual else None
            if cur:
                blocks[cur] = [ln]
        elif cur and ln.startswith("    "):
            blocks[cur].append(ln)
        else:
            cur = None
    by_region = {}
    for icao, ap in airports.items():                          # 表中出现顺序
        if icao in out and icao not in manual:
            by_region.setdefault(ap["region"], []).append(icao)
    orphans = [k for k in manual if out[k].get("region") not in regions]
    text = [head.rstrip("\n"), "", "\n".join(intro).rstrip()]
    for reg in regions:
        icaos = by_region.get(reg, [])
        extra = [k for k in manual if out[k].get("region") == reg and k in blocks]
        if not icaos and not extra:
            continue
        text += ["", "### " + reg]
        for icao in icaos:
            v = out[icao]
            text.append("- **%s**　进场门: %s　‖　离场头: %s" % (icao, " · ".join(v["arr"]) or "—", " · ".join(v.get("dep") or []) or "—"))
            for g in v.get("arr_xfer") or []:                  # 每个到着组一行：到着串 + 其下的移管行（原文）
                rows = "；".join("%s%s %s（%s → %s）" % (r["fix"], " (%s)" % r["cond"] if r.get("cond") else "",
                                                       r["alt"], r["from"], r["to"]) for r in g["rows"])
                text.append("    - 到着 %s：%s" % (" / ".join(g["routes"]), rows or "—"))
        for k in extra:
            text += blocks[k]
    orphan_blocks = [ln for k in orphans if k in blocks for ln in blocks[k]]
    if orphan_blocks:
        text += ["", "### （VATJPN 未覆盖 · 人工推测）"] + orphan_blocks
    return "\n".join(text) + "\n"


# ---------- 变更清单 ----------
def report(old, new):
    keys = [k for k in dict.fromkeys(list(old) + list(new)) if not k.startswith("_")]
    changed = 0
    for k in keys:
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        changed += 1
        if a is None or b is None:
            print("  %s  %s" % (k, "新增" if a is None else "⚠️ 删除（新表里没有了）"))
            continue
        lines = []
        for f in ("region", "arr", "dep"):
            x, y = a.get(f) or [], b.get(f) or []
            if x != y:
                if f == "region":
                    lines.append("管制区 %s → %s" % (x, y))
                else:
                    add, rm = [g for g in y if g not in x], [g for g in x if g not in y]
                    lines.append("%s%s%s%s" % ("进场门" if f == "arr" else "离场头", (" +" + ",".join(add)) if add else "",
                                               (" −" + ",".join(rm)) if rm else "", "" if add or rm else " 顺序变了"))
        for g in dict.fromkeys(list((a.get("arr_gates") or {})) + list((b.get("arr_gates") or {}))):
            x, y = (a.get("arr_gates") or {}).get(g), (b.get("arr_gates") or {}).get(g)
            if x != y and x and y:
                xs = [(r["via_fix"], r["via_awy"], r["cond"], r["dct"]) for r in x]
                ys = [(r["via_fix"], r["via_awy"], r["cond"], r["dct"]) for r in y]
                st_x = sorted({s for r in x for s in r["star"]})
                st_y = sorted({s for r in y for s in r["star"]})
                if xs != ys:
                    lines.append("门 %s 径路 %s → %s" % (g, xs, ys))
                if st_x != st_y:
                    lines.append("门 %s 配 STAR %s → %s" % (g, st_x or "—", st_y or "—"))
        if (a.get("arr_dct") or {}) != (b.get("arr_dct") or {}):
            lines.append("arr_dct %s → %s" % (a.get("arr_dct") or {}, b.get("arr_dct") or {}))
        for f, nm in (("arr_xfer", "到着移管"), ("dep_xfer", "出発移管")):
            x, y = a.get(f) or [], b.get(f) or []
            if x != y:
                lines.append("%s %d 组 / %d 行 → %d 组 / %d 行" % (nm, len(x), sum(len(g["rows"]) for g in x),
                                                              len(y), sum(len(g["rows"]) for g in y)))
        print("  %s  %s" % (k, "；".join(lines) or "（仅顺序 / 格式）"))
    print("  —— 共 %d 个机场有变化" % changed if changed else "  （与原文件完全一致）")


def network_check(out, dat):
    """进场门不在航路网上 → router 用不上它（除非 arr_dct 能重锚到网上的点，如 RJFF HABOH→FUGEN）。
    离场头不在网上很常见（多是 SID 里的终端区点），只计数。"""
    g = router.get_graph(dat)
    if not g.adj:
        return
    off, offd = [], 0
    for icao, v in out.items():
        if icao.startswith("_") or v.get("_source"):
            continue
        off += ["%s %s" % (icao, x) for x in v.get("arr", []) if (g.by_ident.get(x) or (None,)) not in g.inset]
        offd += sum(1 for x in v.get("dep", []) if (g.by_ident.get(x) or (None,)) not in g.outset)
    print("ℹ️ 不在航路网上的进场门：%s；离场头 %d 个（多是 SID 里的终端区点，正常）" % ("、".join(off) or "无", offd))


def _show(path):
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:                                         # 不同盘符
        return path


def main():
    ap = argparse.ArgumentParser(description="从 VATJPN 发布的移管表重建 transfer_points.json 与 skill 文档")
    ap.add_argument("--list", action="store_true", help="列出全部标签页（AIRAC 名 → gid）")
    ap.add_argument("--gid", help="标签页 gid，或整条 pubhtml 链接（链接里的发布 ID 也一并采用）；不给则取 AIRAC 最新的一页")
    ap.add_argument("--pub", help="发布 ID（2PACX-…）；默认沿用 json 里 _vatjpn 记录的，再没有就用内置的")
    ap.add_argument("--out", default=JSON_OUT)
    ap.add_argument("--md-out", default=MD_OUT)
    ap.add_argument("--dry-run", action="store_true", help="只打印变更清单，不写文件")
    a = ap.parse_args()

    old = vatjpn.load(a.out)
    rec = old.get("_vatjpn") if isinstance(old.get("_vatjpn"), dict) else {}
    m = re.search(r"/d/e/(2PACX-[\w-]+)", a.gid or "")
    pub = a.pub or (m.group(1) if m else None) or rec.get("pub") or vatjpn.PUB_ID
    tabs = vatjpn.list_tabs(pub)
    newest = vatjpn.pick_tab(tabs, None)[1]
    if a.list:
        for name, gid in tabs:
            print("  %-14s gid=%s%s" % (name, gid, "   ← 最新" if newest and gid == newest[1] else ""))
        return
    if a.gid:
        m = re.search(r"gid=(\d+)", a.gid)
        gid = m.group(1) if m else a.gid.strip()
    elif newest:
        gid = newest[1]
    else:
        raise SystemExit("❌ 发布页里没有标签页，请用 --gid 指定")
    name = next((n for n, g in tabs if g == gid), None)
    label = vatjpn.tab_label(name, gid)
    print("📥 读取「%s」（gid=%s）%s" % (name or "未列出的标签页", gid,
                                     "" if vatjpn.tab_airac(name) is not None else " ⚠️ 名称里没有 AIRAC 号"))

    dat = find_navdata_file()
    if not dat:
        raise SystemExit("❌ 找不到 NavData（STAR 配对要用本机 CIFP 现推）")
    cycle = airac_cycle(dat)
    tab_a = vatjpn.tab_airac(name)
    if tab_a is not None and cycle and cycle.isdigit() and int(cycle) < tab_a:
        print("⚠️ 本机导航数据是 AIRAC %s，比这一期旧：STAR 会按旧 CIFP 配对。发版前建议先更新 NavData。" % cycle)
    page = vatjpn._get(vatjpn.sheet_url(pub, gid), 30)
    source = {"pub": pub, "gid": gid, "tab": name, "navdata": cycle}
    out, regions, airports, skipped, manual, warn = vatjpn.build(page, old, dat, label, source)

    n = len([k for k in out if not k.startswith("_")])
    print("✈️ %d 个机场（表中 %d、人工保留 %d：%s）；表里是空行、不录：%s"
          % (n, n - len(manual), len(manual), ",".join(manual) or "无", ",".join(skipped) or "无"))
    for w in warn:
        print("  ⚠️ " + w)
    print("📋 与原文件相比：")
    if rec != out.get("_vatjpn"):
        print("  来源记录 _vatjpn：%s → %s" % (rec or "无", out.get("_vatjpn")))
    report(old, out)
    network_check(out, dat)

    md = build_md(open(a.md_out, encoding="utf-8").read(), out, regions, airports, manual, label) if os.path.exists(a.md_out) else None
    if a.dry_run:
        print("（--dry-run：未写文件）")
        return
    vatjpn.write_text(a.out, vatjpn.dumps(out))
    print("✅ 已写出 %s" % _show(a.out))
    if md:
        vatjpn.write_text(a.md_out, md)
        print("✅ 已写出 %s" % _show(a.md_out))


if __name__ == "__main__":
    main()
