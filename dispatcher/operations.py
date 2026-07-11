# ================= 机场运行规则存取（operation.json，F23）=================
# 用户用「⚙️ 编辑机场运行规则」可视化编辑，存运行目录的 operation.json（按机场 ICAO 键）。
# 每机场一组 rules，每条 = {name, cond, dep(runways/sids), arr(runways/stars/iaps)}。
# cond = {time_jst, days, ref_runway, wind_kind, wind_min_kt, ceiling_min_ft, ceiling_cover, visibility_min_m}：
#   · time_jst=JST HHMM-HHMM 串列表；days=生效星期(1=周一…7=周日 ISO，空=每天；深夜运用常按星期几不同)。
#   · 风门槛：相对 ref_runway 的【wind_kind(tailwind 顺风 / headwind 逆风 / crosswind 侧风) 分量 ≥ wind_min_kt 节】触发。
#     真实惯例：顺风超→换向（南風運用）；侧风超→换落跑道例外（都心運用：16L/16R 侧风≥15 就改落 22/23）。
#   · 好天门槛：云底 ≥ ceiling_min_ft 英尺（ceiling_cover=FEW/SCT/BKN/OVC 起算算"云底"，如 LDA 的 few 不计=SCT 起算）
#     且 能见度 ≥ visibility_min_m 米。坏天规则不填、排其后作兜底。各项空/缺 = 不限。
# 上半＝存取 + 规整；下半 evaluate_gates/select_rule ＝规划时的【应用引擎】(v1.6.0)：按 时段+星期+风+天气
# 匹配规则，供 GUI 预选跑道/SID/STAR/IAP。匹配「均等」：不看上下全局优先级，按 条件命中 + 迎风取舍 选。
# 仿 airlines.py / config.py 的运行目录读写：纯标准库、优雅失败、原子写。
import os
import json

from .config import get_real_run_path
from . import weather, procedures, timed

_FILENAME = "operation.json"
_COMMENT = ("机场运行规则：用「⚙️ 编辑机场运行规则」可视化编辑。每机场一组 rules，"
            "规划器（下一版）按当前 JST 时段+风匹配以预选跑道/SID/STAR/IAP。")


def _path():
    """operation.json 路径，锚定运行目录（源码=项目根，frozen=exe 同级，与 airlines.json 一致）。"""
    return os.path.join(get_real_run_path(), _FILENAME)


def load_operations():
    """读取 operation.json → {ICAO: {"rules":[...]}}（含 _comment）。缺失 → {}；损坏 → {} 且告警（不覆盖原文件）。
    不自动创建文件——编辑器保存时才建。"""
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        print("⚠️ 运行规则文件 operation.json 解析失败，本次按空规则处理（不覆盖原文件）。")
        return {}


def _prune(data):
    """规整：_comment 置顶补默认；剔除没有 rules 的机场（保持文件干净）。"""
    out = {"_comment": (data or {}).get("_comment") or _COMMENT}
    for k, v in (data or {}).items():
        if k == "_comment":
            continue
        if isinstance(v, dict) and v.get("rules"):
            out[k.upper()] = {"rules": list(v["rules"])}
    return out


def save_operations(data):
    """原子写 operation.json（temp→os.replace）；剔除空机场；成功返回 True，失败返回 False（不崩）。"""
    path = _path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_prune(data), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"⚠️ 保存 operation.json 失败: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def airport_rules(data, icao):
    """防御式取某机场 rules 列表的浅拷贝（脏值 → []）。"""
    try:
        v = (data or {}).get((icao or "").upper())
        rules = v.get("rules") if isinstance(v, dict) else None
        return list(rules) if isinstance(rules, list) else []
    except Exception:
        return []


def airports(data):
    """有规则的机场 ICAO 列表（排除 _comment 及无 rules 的项），按字母序。"""
    return sorted(k for k, v in (data or {}).items()
                  if k != "_comment" and isinstance(v, dict) and v.get("rules"))


# ================= 应用引擎（v1.6.0）：规划时按 时段+星期+风+天气 匹配规则 =================
# 匹配模型「均等 + 恶天顺位下移」（用户 2026-07-03 定）：不按上下全局优先级，按【条件命中 + 迎风取舍】。
# 恶天用恶天配置＝恶天规则不带天气闸恒过、好天规则天气闸不满足被滤（等价「从好天往下数一条」，不依赖相邻）。


def _in_ring(t, lo, hi):
    """环形闭区间 [lo,hi]（跨午夜 lo>hi 也成立）。"""
    return lo <= t <= hi if lo <= hi else (t >= lo or t <= hi)


def _match_time(windows, jst_min):
    """任一 `HHMM-HHMM` JST 窗含 jst_min → True；windows 空 → True（不限时段）。"""
    if not windows:
        return True
    if jst_min is None:
        return False
    for s in windows:
        parts = (s or "").split("-")
        if len(parts) == 2:
            lo, hi = timed.parse_hhmm(parts[0]), timed.parse_hhmm(parts[1])
            if lo is not None and hi is not None and _in_ring(jst_min, lo, hi):
                return True
    return False


def _wind_component(cond, wind):
    """规则风门槛的风分量(kt)：headwind=逆风 / tailwind=顺风 / crosswind=侧风；算不出→None。"""
    ref = cond.get("ref_runway")
    hd = procedures.runway_heading_deg(ref) if ref else None
    if hd is None or not wind:
        return None
    hw, xw = weather.runway_wind(hd, wind[0], wind[1])   # 逆风带符号(正逆/负顺)、侧风绝对值
    kind = cond.get("wind_kind") or "tailwind"
    if kind == "headwind":
        return hw
    if kind == "crosswind":
        return xw
    return -hw                                           # tailwind 顺风


def evaluate_gates(cond, ctx):
    """规则四闸(time/days/wind/weather)「存在即须成立、缺省即过」→ bool。
    ctx = {jst_min, weekday(1-7), wind=(dir,spd,..), sky_layers, vis_m}。天气未知(None) 按过=好天。"""
    cond = cond or {}
    if not _match_time(cond.get("time_jst") or [], ctx.get("jst_min")):
        return False
    days = cond.get("days") or []
    if days and ctx.get("weekday") not in days:
        return False
    if cond.get("wind_min_kt") is not None:
        comp = _wind_component(cond, ctx.get("wind"))
        if comp is None or comp < cond["wind_min_kt"]:
            return False
    cmin = cond.get("ceiling_min_ft")
    if cmin is not None:
        ceil = weather.ceiling_ft(ctx.get("sky_layers") or [], cond.get("ceiling_cover"))
        if ceil is not None and ceil < cmin:      # 云底已知且低于门槛 → 恶天；未知(None)→按过
            return False
    vmin = cond.get("visibility_min_m")
    if vmin is not None:
        vis = ctx.get("vis_m")
        if vis is not None and vis < vmin:
            return False
    return True


def select_rule(rules, side, ctx, rows):
    """为 side('dep'/'arr') 选一条命中规则 → (rule, runway_id, proc_label|None) 或 None。
    rows = procedures.matching_choices 的行 [(rwy,len,labels)]（端点预筛后本侧候选跑道+程序标签）。
    取舍词典序 min：(路线不相容, −时段/星期具体度, −有满足风门槛, 所选跑道侧风, 顺风, −有天气门槛, 列表序)。"""
    rows = rows or []
    row_by_rwy = {r[0]: r for r in rows}
    wind = ctx.get("wind") or (None, 0, None)
    cands = []
    for idx, rule in enumerate(rules or []):
        blk = (rule or {}).get(side) or {}
        rwys = blk.get("runways") or []
        if side == "dep":
            want = set(blk.get("sids") or [])
            if not (rwys or want):
                continue                          # 该 side 空 → 与本侧无关
        else:
            want = set(blk.get("stars") or [])
            if not (rwys or want or blk.get("iaps")):
                continue
        if not evaluate_gates(rule.get("cond") or {}, ctx):
            continue
        cond = rule.get("cond") or {}
        rwy = next((r for r in rwys if r in row_by_rwy), rwys[0] if rwys else None)
        row = row_by_rwy.get(rwy)
        if row is not None:
            labels = row[2]
            inter = [lb for lb in labels if lb.split(".")[0] in want]  # bare 名 ↔ NAME.TRANS 标签对齐
            route_ok = bool(inter) or not want or not labels          # 交集非空 / 规则没指定 SID / 该跑道无程序
        else:
            labels, inter, route_ok = [], [], False                  # 规则跑道不在本航路端点候选 → 路线不相容（方向不对）
        proc_label = inter[0] if inter else None
        hd = procedures.runway_heading_deg(rwy) if rwy else None
        hw, xw = weather.runway_wind(hd, wind[0], wind[1]) if hd is not None else (0.0, 0.0)
        if not weather.runway_ok(hw, xw):
            continue                    # 该规则跑道当前顺/侧风超限 → 真实运行不会用（北風→南風就是为此），跳过→回退按风合规跑道
        specificity = (1 if cond.get("time_jst") else 0) + (1 if cond.get("days") else 0)
        has_wind = cond.get("wind_min_kt") is not None
        has_wx = cond.get("ceiling_min_ft") is not None or cond.get("visibility_min_m") is not None
        key = (not route_ok, -specificity, 0 if has_wind else 1, xw, -hw, 0 if has_wx else 1, idx)
        cands.append((key, rule, rwy, proc_label))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][1:]      # (rule, runway_id, proc_label)
