# ================= 机场运行规则存取（operation.json，F23）=================
# 用户用「⚙️ 编辑机场运行规则」可视化编辑，存运行目录的 operation.json（按机场 ICAO 键）。
# 每机场一组 rules，每条 = {name, cond, dep(runways/sids), arr(runways/stars/iaps)}。
# 机场还可带 preferred_runways：同向跑道风况相同时默认选哪条（伊丹的长跑道 32L/14R），不填按字母序。
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
from . import weather, procedures, timed, ntrack

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
    """规整：_comment 置顶补默认；剔除既没有 rules 也没有 preferred_runways 的机场（保持文件干净）。"""
    out = {"_comment": (data or {}).get("_comment") or _COMMENT}
    for k, v in (data or {}).items():
        if k == "_comment":
            continue
        if isinstance(v, dict) and (v.get("rules") or v.get("preferred_runways")):
            out[k.upper()] = {**v, "rules": list(v.get("rules") or [])}
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


def preferred_runways(data, icao):
    """某机场 preferred_runways 的集合（缺失 / 脏值 → 空集）。"""
    v = (data or {}).get((icao or "").upper())
    rwys = v.get("preferred_runways") if isinstance(v, dict) else None
    return {r for r in rwys if isinstance(r, str)} if isinstance(rwys, list) else set()


def airports(data):
    """有规则或优先跑道的机场 ICAO 列表（排除 _comment 及两样都没有的项），按字母序。"""
    return sorted(k for k, v in (data or {}).items()
                  if k != "_comment" and isinstance(v, dict) and (v.get("rules") or v.get("preferred_runways")))


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


def evaluate_gates(cond, ctx, skip_wind=False, skip_weather=False):
    """规则四闸(time/days/wind/weather)「存在即须成立、缺省即过」→ bool。
    ctx = {jst_min, weekday(1-7), wind=(dir,spd,..), sky_layers, vis_m}。天气未知(None) 按过=好天。

    `skip_wind` / `skip_weather`：有【实测运用状况】(ntrack)时用。风闸与天气闸本就是我们用来
    **推断**当前构型的代理指标（「顺风到 10 节就换向」「云底低于 1500ft 就算恶天」）；实测既然直接
    把构型告诉我们了，这两道闸就该由它**取代**，而不是继续拦路——否则会出现「MLIT 说正在跑南風
    都心運用，而我们的门槛还差 0.8 节没跳，于是把南風规则全闸掉」这种荒谬情形（实测过）。
    时段/星期闸门仍然生效：ntrack 不告诉我们 A/B/C 是哪一档，那得靠 EOBT。"""
    cond = cond or {}
    if not _match_time(cond.get("time_jst") or [], ctx.get("jst_min")):
        return False
    days = cond.get("days") or []
    if days and ctx.get("weekday") not in days:
        return False
    if not skip_wind and cond.get("wind_min_kt") is not None:
        comp = _wind_component(cond, ctx.get("wind"))
        if comp is None or comp < cond["wind_min_kt"]:
            return False
    if skip_weather:
        return True
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


def _pick_by_wind(rwys, wind):
    """一组跑道里挑风况最合适的：适航优先、再逆风大、再跑道号。"""
    def key(r):
        hd = procedures.runway_heading_deg(r)
        hw, xw = weather.runway_wind(hd, wind[0], wind[1]) if hd is not None else (0.0, 0.0)
        return (not weather.runway_ok(hw, xw), -hw, procedures._rw_sort_key(r))
    return sorted(rwys, key=key)[0] if rwys else None


def _circling_from(blk, rwys):
    """规则的 IAP 全都不落在规则跑道上 = 目视盘旋（RJOO 南向：飞 32 的进近，盘旋落 14）→ 这些 IAP 所在的跑道（按规则里的顺序）；否则空列表。"""
    iap_rwys = ["RW" + m.group(1) for m in map(procedures._RWNUM_RE.search, blk.get("iaps") or []) if m]
    return [] if set(iap_rwys) & set(rwys) else iap_rwys


def circling_pairs(rule, side, rows, proc_label=None):
    """这条规则若是目视盘旋 → {盘旋落地的跑道 B: 飞进近的跑道 A}；否则 {}。
    A、B 默认是同一条物理跑道的两端（32L 盘旋落 14R、32R 盘旋落 14L）；另一端不在规则的进近里时，
    退回能接上所选 STAR 的那条。"""
    blk = (rule or {}).get(side) or {}
    rwys = blk.get("runways") or []
    circ = _circling_from(blk, rwys)
    if not circ:
        return {}
    labels = {r[0]: r[2] for r in rows or []}
    fallback = next((r for r in circ if proc_label in labels.get(r, ())), circ[0])
    return {b: (procedures._reciprocal(b) if procedures._reciprocal(b) in circ else fallback) for b in rwys}


def _iap_runway(ident):
    """进近 ident 落在哪条跑道（I34LY → RW34L、X22-W → RW22）；盘旋进近（VORA）不属任何跑道 → None。"""
    m = procedures._RWNUM_RE.search(ident or "")
    return ("RW" + m.group(1)) if m else None


def _apch_matches(blk, rwy, apch):
    """规则在跑道 rwy 上写的进近，与实测在这条跑道上用的进近**完全吻合**吗（用户要求：具体到程序）。
    apch = ntrack.approaches_by_runway 的结果；盘旋进近（不属任何跑道）算进每条跑道。
      · 实测对这条跑道没给进近 → 跑道对上即可；
      · 规则在这条跑道上没写 IAP（羽田「34R 好天」= HIGHWAY VSL 目视进近）→ 只在实测用的不是 CIFP 程序时吻合；
      · 规则写了 IAP → 实测的 CIFP 进近非空、且全都在规则里（深夜 ILS Y 34L 对不上写着 ILS X / Z 34L 的规则）。"""
    got = apch.get(rwy) or {"idents": set(), "other": []}
    loose = apch.get(None) or {"idents": set(), "other": []}
    m_r = set(got["idents"]) | set(loose["idents"])
    if not m_r and not (got["other"] or loose["other"]):
        return True
    rule_r = {i for i in blk.get("iaps") or [] if _iap_runway(i) in (rwy, None)}
    if not rule_r:
        return not m_r
    return bool(m_r) and m_r <= rule_r


def _measured_ok(blk, side, rwys, inter_r, apch):
    """有实测时一条规则在本侧的吻合跑道（按规则跑道顺序）；对不上 → 空列表。
    离场：规则跑道在实测 DEP 里即可（羽田按单条跑道写规则，05 / 34R 各一条）。
    进场：还要逐条跑道核对具体进近；目视盘旋规则（伊丹南向）核对的是进近所在跑道上的程序。"""
    if side == "dep" or not apch:
        return inter_r
    circ = _circling_from(blk, rwys)
    if circ:
        flown = set().union(*(set(v["idents"]) for v in apch.values()))
        rule_iaps = set(blk.get("iaps") or [])
        return inter_r if (not flown or flown <= rule_iaps) else []
    return [r for r in inter_r if _apch_matches(blk, r, apch)]


def _rule_candidates(rules, side, ctx, rows, config=None, prefer=()):
    """select_rule / active_rules 共用：过闸、吻合实测、风况合规的规则 → [(key, rule, rwy, proc_label)]。
    key 的第一位是「路线不相容」，其余是与路线无关的取舍（见 select_rule）。"""
    rows = rows or []
    row_by_rwy = {r[0]: r for r in rows}
    with_proc = {r[0] for r in rows if r[2]}      # rows 里还有只带 IAP 的落地跑道（标签空）
    wind = ctx.get("wind") or (None, 0, None)
    used = set((config or {}).get("runways") or ())
    used_iaps = set((config or {}).get("iaps") or ())
    apch = (config or {}).get("apch") or {}
    cands = []
    for idx, rule in enumerate(rules or []):
        blk = (rule or {}).get(side) or {}
        rwys = sorted(blk.get("runways") or [], key=lambda r: r not in prefer)
        if side == "dep":
            want = set(blk.get("sids") or [])
            if not (rwys or want):
                continue                          # 该 side 空 → 与本侧无关
        else:
            want = set(blk.get("stars") or [])
            if not (rwys or want or blk.get("iaps")):
                continue
        inter_r = [r for r in rwys if r in used] if used else []
        if used:
            inter_r = _measured_ok(blk, side, rwys, inter_r, apch)
            if not inter_r:
                continue                          # 跑道或进近对不上实测构型 → 不是当前在跑的这条规则
        if not evaluate_gates(rule.get("cond") or {}, ctx,
                              skip_wind=bool(used), skip_weather=bool(used)):
            continue
        cond = rule.get("cond") or {}
        if used:                                  # 实测在用 ∩ 本航路端点候选 → 再按风挑一条
            rwy = _pick_by_wind([r for r in inter_r if r in with_proc]
                                or [r for r in inter_r if r in row_by_rwy] or inter_r, wind)
        else:                                     # 盘旋：首选进近那条跑道的另一端；否则能衔接程序的优先，只带 IAP 的其次
            ends = [procedures._reciprocal(a) for a in _circling_from(blk, rwys)]
            ends.sort(key=lambda r: r not in prefer)
            rwy = next((r for r in ends if r in rwys and r in row_by_rwy), None) or next(
                (r for r in rwys if r in with_proc),
                next((r for r in rwys if r in row_by_rwy), rwys[0] if rwys else None))
        row = row_by_rwy.get(rwy)
        if row is not None:
            labels = row[2]
            inter = [lb for lb in labels if lb.split(".")[0] in want]  # bare 名 ↔ NAME.TRANS 标签对齐
            if not inter and side == "arr":           # 盘旋落地：STAR 跟进近走，服务的是进近那条跑道
                circ = _circling_from(blk, rwys)
                inter = [lb for r in rows if r[0] in circ for lb in r[2] if lb.split(".")[0] in want]
            # 交集非空 / 规则没指定程序 / 本侧根本没有程序。规则指定了 STAR、这条跑道却只带 IAP → 不相容
            route_ok = bool(inter) or not want or not with_proc
        else:
            labels, inter, route_ok = [], [], False                  # 规则跑道不在本航路端点候选 → 路线不相容（方向不对）
        proc_label = inter[0] if inter else None
        hd = procedures.runway_heading_deg(rwy) if rwy else None
        hw, xw = weather.runway_wind(hd, wind[0], wind[1]) if hd is not None else (0.0, 0.0)
        if not used and not weather.runway_ok(hw, xw):
            continue                    # 该规则跑道当前顺/侧风超限 → 真实运行不会用（北風→南風就是为此），跳过→回退按风合规跑道
        iap_hit = len(set(blk.get("iaps") or []) & used_iaps) if (side == "arr" and used_iaps) else 0
        specificity = (1 if cond.get("time_jst") else 0) + (1 if cond.get("days") else 0)
        has_wind = cond.get("wind_min_kt") is not None
        has_wx = cond.get("ceiling_min_ft") is not None or cond.get("visibility_min_m") is not None
        key = (not route_ok, -specificity, -iap_hit,
               0 if has_wind else 1, xw, -hw, 0 if has_wx else 1, idx)
        cands.append((key, rule, rwy, proc_label))
    return cands


def select_rule(rules, side, ctx, rows, config=None, prefer=()):
    """为 side('dep'/'arr') 选一条命中规则 → (rule, runway_id, proc_label|None) 或 None。
    rows = procedures.matching_choices 的行 [(rwy,len,labels)]（端点预筛后本侧候选跑道+程序标签）。
    取舍词典序 min：(路线不相容, −时段/星期具体度, −实测进近命中, −有满足风门槛, 所选跑道侧风, 顺风, −有天气门槛, 列表序)。
    `prefer` = 该机场的 preferred_runways：一条规则列了多条跑道时优先取它们，与规则里的先后无关。

    **`config` = 实测运用状况**（SWIM 真实 ATIS / 羽田 ntrack）：`measured_config` 的结果
    `{"runways": {在用跑道}, "iaps": {在用进近 ident}, "apch": {跑道: 在用进近}}`。
    给了它，就从「按风/天气**推断**构型」切换成「按实测构型**认**规则」：
      · 跑道对不上实测在用集合的规则，直接排除（它不是当前在跑的那条）；
      · 进场还要**逐条跑道对上具体进近**（`_apch_matches`）：深夜 34R 维护、34L 用 ILS Y 时，
        写着 ILS X / ILS Z 34L 的规则不算吻合；对不上的规则也直接排除；
      · **风闸与天气闸交给实测取代**（见 evaluate_gates 的 skip_* 说明）——它们本就是构型的代理指标；
      · **不再做顺/侧风超限剔除**：实测在用的跑道就是现实，我们的风模型说它勉强也没用；
      · 时段/星期闸门仍生效：实测反映的是此刻，航班按 EOBT / ETA 出发（用户决定）。
    一条都对不上 → None，由调用方以实测为主。"""
    cands = _rule_candidates(rules, side, ctx, rows, config, prefer)
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][1:]      # (rule, runway_id, proc_label)


def _reciprocal_any(a, b):
    """两组跑道里有没有方向相反的（01 对 19、05 对 23、16 对 34）：那是另一种运行方向。"""
    for x in a:
        for y in b:
            hx, hy = procedures.runway_heading_deg(x), procedures.runway_heading_deg(y)
            if hx is not None and hy is not None and abs((hx - hy + 180) % 360 - 180) >= 150:
                return True
    return False


def active_rules(rules, ctx, rows, config=None, prefer=()):
    """此刻【同时生效】的一组规则 → {"dep": [(rule, runway_id)…], "arr": […]}（各按取舍键排序）——生成航路要在其中按航向选。
    rows / config 都是 {"dep": …, "arr": …}（config 可为 None）。

    不能只挑一条：羽田离场 05（西行）与 34R（北、东行）是两条同时生效的规则，进场 34L / 34R 也是，
    要等航路定了才知道用哪条（面板就是这样：先看路线相容）。生成航路之前还没有航路，所以：
      · 离场、进场两侧的候选放在一起排序，排第一的定下运行方向；与已选入的规则（**两侧都算**）跑道方向相反的
        → 另一种运行方向，不要。只看本侧不够：南风时离场 05 与 16L 只差 110°，要和进场的 23 比才看得出是北风构型；
      · 本侧与已选入的规则共用跑道的 → 被它取代（好天 / 恶天对子、深夜规则对全天规则），不要；
      · 交叉或平行的跑道（05 与 34R、34L 与 34R）→ 一并生效；
      · 但排第一的若是【同时写了离场和进场】的规则（羽田深夜「离场 05，进场 34L」、RJCC / 伊丹的规则），
        它本身就是整座机场的完整构型——只写一侧的规则（羽田白天按单条跑道写的那些）此时都不生效，
        否则深夜还会混进白天全天有效的「34R 进场」。
    取舍键与 select_rule 相同，只是去掉「路线不相容」（还没有航路）。"""
    allc = []
    for side in ("dep", "arr"):
        for key, rule, rwy, _lb in _rule_candidates(rules, side, ctx, (rows or {}).get(side),
                                                     (config or {}).get(side), prefer):
            allc.append((key[1:], side, rule, rwy))
    allc.sort(key=lambda c: c[0])
    out = {"dep": [], "arr": []}
    taken = {"dep": set(), "arr": set()}
    whole = None
    for _key, side, rule, rwy in allc:
        rwys = set(((rule or {}).get(side) or {}).get("runways") or ())
        other = "arr" if side == "dep" else "dep"
        if whole is None:
            ok = True                       # 排第一的：定下运行方向
        elif any(r is rule for r, _ in out[other]):
            # 同一条规则的另一侧已选入：照它写的来（羽田深夜「离场 16L，进场 34R」两侧本就方向相反）
            ok = not (rwys & taken[side])
        elif whole or _complete(rule):
            # 完整构型与别的规则互为替代方案（深夜的好天 / 恶天两套、白天按跑道拆写的规则 vs 深夜构型）
            ok = False
        else:
            ok = not (rwys & taken[side] or _reciprocal_any(rwys, taken["dep"] | taken["arr"]))
        if not ok:
            continue
        if whole is None:
            whole = _complete(rule)
        out[side].append((rule, rwy))
        taken[side] |= rwys
    return out


def _complete(rule):
    """同时写了离场和进场跑道的规则 = 一整套机场构型。"""
    return bool(((rule or {}).get("dep") or {}).get("runways")) and bool(((rule or {}).get("arr") or {}).get("runways"))


# ================= 生成航路按运行规则 / 实测定端点（v2.1.0 hotfix）=================
# 共用的上下文、实测构型、端点——面板（viewmodel）与生成航路（controller）用同一份，免得两边各写一份走样。

# EOBT 默认＝此刻 + 30 分钟：启动模拟器 + 做航前一般要半小时左右，直接拿「此刻」当撤轮挡时刻不现实（用户反馈）。
EOBT_PREP_MIN = 30


def default_eobt_min():
    """默认 EOBT（当日 JST 分钟）＝此刻 + EOBT_PREP_MIN；过了午夜就落到次日凌晨（取模）。"""
    return (weather.now_jst()[0] + EOBT_PREP_MIN) % 1440


def eobt_weekday(eobt_min, now_min, now_wd):
    """EOBT 只有钟点、不带日期 → 它是星期几（运行规则的星期闸门要用）。
    默认按「今天」；但若它刚过午夜、且就在此刻之后 EOBT_PREP_MIN 分钟内——默认 EOBT 在 23:30 后跨午夜时正是如此——
    那是明天，星期 +1。其余（含用户手填的更早钟点）照旧按今天。"""
    if eobt_min < now_min and eobt_min + 1440 - now_min <= EOBT_PREP_MIN:
        return now_wd % 7 + 1
    return now_wd


def rule_contexts(eobt_jst, dist_nm, dep_wind, dep_sky, arr_wind, arr_sky, eta_jst=None):
    """规则匹配上下文 → (dep_ctx, arr_ctx)：离场按 EOBT、到达按 ETA。
    eta_jst = 调用方用 eta.EtaModel 算好的 ETA（F36：机型剖面 + 高空风 + 该航路的几何）；
    不给 → 旧算法（EOBT + 滑出 + dist_nm ÷ 巡航速度）。sky = weather.parse_sky 的 (layers, vis_m)。到达跨午夜时星期 +1。"""
    now_min, now_wd = weather.now_jst()
    dep_wd = eobt_weekday(eobt_jst, now_min, now_wd)              # 默认 EOBT 跨午夜 → 是明天
    if eta_jst is None:
        try:
            _eu, eta_utc = timed.plan_times_utc(eobt_jst, dist_nm or 0)
            eta_jst = (eta_utc + 540) % 1440
        except Exception:                                         # noqa: BLE001
            eta_jst = eobt_jst
    arr_wd = dep_wd if eta_jst >= eobt_jst else (dep_wd % 7 + 1)  # 到达跨午夜 → 星期 +1
    dep_sky, arr_sky = dep_sky or ([], None), arr_sky or ([], None)
    return ({"jst_min": eobt_jst, "weekday": dep_wd, "wind": dep_wind,
             "sky_layers": dep_sky[0], "vis_m": dep_sky[1]},
            {"jst_min": eta_jst, "weekday": arr_wd, "wind": arr_wind,
             "sky_layers": arr_sky[0], "vis_m": arr_sky[1]})


def measured_config(nt_cfg, side, iap_list):
    """实测运用状况（SWIM 报文 / ntrack，形如 {approaches, ldg, dep}）→ 规则引擎的 config；无实测 → None。
    离场只看 DEP 跑道；进场看 LDG 跑道 + 按跑道分组的具体进近（iap_list = procedures.enumerate_approaches）。"""
    if not nt_cfg:
        return None
    rwys = set(nt_cfg.get("dep" if side == "dep" else "ldg") or ())
    if not rwys:
        return None
    if side == "dep":
        return {"runways": rwys, "iaps": set(), "apch": {}}
    apps = nt_cfg.get("approaches") or []
    return {"runways": rwys, "iaps": set(ntrack.match_iaps(apps, iap_list)),
            "apch": ntrack.approaches_by_runway(apps, iap_list)}


def airport_active_rules(ops_data, icao, ctx, nt_cfg, dat_path=None):
    """一个机场此刻生效的规则（两侧）→ (active, configs, rows)。nt_cfg = 该机场的实测运用状况（SWIM / ntrack）或 None。
    面板（生成航路的候选）和生成航路都用它，保证两边认的是同一组规则。"""
    rows = {s: procedures.matching_choices(icao, dat_path, [], s)[0] for s in ("dep", "arr")}
    iaps = procedures.enumerate_approaches(icao, dat_path) if nt_cfg else []
    configs = {s: measured_config(nt_cfg, s, iaps) for s in ("dep", "arr")}
    rules = airport_rules(ops_data, icao)
    active = (active_rules(rules, ctx, rows, configs, preferred_runways(ops_data, icao))
              if rules else {"dep": [], "arr": []})
    return active, configs, rows


def route_endpoints(ops_data, icao, side, ctx, nt_cfg, dat_path=None):
    """生成航路的端点限定 → {source, rules, runways, unmatched, procs, fixes} 或 None（不限定）。
    nt_cfg = 该机场的实测运用状况（SWIM 报文 / ntrack）或 None。优先级（用户定）：
      1. 有实测：用实测匹配（跑道 + 具体进近）此刻生效的规则 → 这些规则的程序；
      2. 实测在用、却没有规则对上的跑道 → 以实测为主：这条跑道上的全部程序（全都对不上时 source='measured'）；
      3. 无实测：按 EOBT / ETA、风、天气此刻生效的规则（source='rule'）；
      4. 都没有 → None。
    规则没写本侧程序时，用它选中跑道上的全部程序。fixes = 这些程序能与航路衔接的点（procedures.connect_idents）。"""
    kind = "SID" if side == "dep" else "STAR"
    act, configs, rows_by = airport_active_rules(ops_data, icao, ctx, nt_cfg, dat_path)
    active, config = act[side], configs[side]
    labels_on = {r[0]: [lb.split(".")[0] for lb in r[2]] for r in rows_by[side]}
    fixes, procs, rwys, unmatched = set(), [], [], []

    def add(names, rwy):
        nonlocal fixes
        fixes |= procedures.connect_idents(icao, kind, names, rwy if side == "dep" else None, dat_path)
        procs.extend(n for n in names if n not in procs)
        if rwy and rwy not in rwys:
            rwys.append(rwy)

    covered = set()
    for rule, rwy in active:
        blk = rule.get(side) or {}
        add(list(blk.get("sids" if side == "dep" else "stars") or []) or labels_on.get(rwy, []), rwy)
        covered |= set(blk.get("runways") or ())
    if config:
        # 实测在用、却没有任何规则对上的跑道 → 这条跑道以实测为主（实测离场 05/34R、只有 34R 对上规则时，05 也照样能用）
        for rwy in sorted(set(config.get("runways") or ()) - covered):
            unmatched.append(rwy)
            add(labels_on.get(rwy, []), rwy)
    if not fixes:
        return None
    source = ("rule" if not unmatched else "rule+measured") if active else "measured"
    return {"source": source, "measured": bool(config), "rules": [r.get("name", "") for r, _ in active],
            "runways": rwys, "unmatched": unmatched, "procs": procs, "fixes": fixes}


# ================= AIP 适用性：时段 + 运行规则（AIP 改端的触发判据）=================
# 官方 AIP 不总是全天适用：RJCC→RJTT 的两条都写着 ETA 06:00–22:59 JST，深夜到羽田要走 `… DAIGO Y108 MESSE`；
# RJCC→ROAH 的 AIP 不限时段，却从 DALBI 起飞，而新千岁深夜离场只走 CHE / KURIS。
# 没有一条 AIP 行「既在时段内、又能接上此刻生效规则 / 实测的程序」→ 按规则改端（router.splice_route_ends）。

AIP_SHORT_NM = 250      # 大圆不超过它、又有时段适用行的短程照旧用 AIP（短程多靠雷达引导，规则程序常接不上，与 F34 同口径）


def _ends_hit(e, pts):
    """这一端的点能不能接上规则 / 实测的程序 → None（这一端不限定）/ True / False。航路串该端没有点 → True。"""
    if not e:
        return None
    if not pts:
        return True
    return any(p in e["fixes"] for p in pts)


def aip_fit(rows, eobt_jst, etas, ed, eas):
    """每条 AIP 行此刻是否适用 → [{time_ok, dep_hit, arr_hit}]。
    rows = AIP 原始行 [DEP, DEST, 时段, 高度, 机型, 航路, …]；ed = 离场端 route_endpoints 的结果或 None。
    F36：每行用【自己的】ETA（etas，JST 当日分钟，按该行原 AIP 的几何算）和自己的到达端限定（eas，按该 ETA 选的规则）。
    用的是原 AIP 的几何、不是改端版的：判据只随 EOBT 变，不会在「AIP / 改端」之间来回翻。"""
    eobt_utc = (int(eobt_jst) - 540) % 1440
    out = []
    for r, eta_jst, ea in zip(rows, etas, eas):
        lead, trail = procedures.lead_trail_fixes(r[5] if len(r) > 5 else "")
        out.append({"time_ok": timed.route_matches_time(r[2] if len(r) > 2 else "", eobt_utc, (eta_jst - 540) % 1440),
                    "dep_hit": _ends_hit(ed, lead), "arr_hit": _ends_hit(ea, trail)})
    return out


def aip_key(fits, ed, eas):
    """面板据此判断改 EOBT / 规则开关后要不要重算改端：两端的限定（到达端每行一个）+ 各行是否在时段内。"""
    f = lambda e: (frozenset(e["fixes"]), e["source"]) if e else None
    return (f(ed), tuple(f(e) for e in eas), tuple(x["time_ok"] for x in fits))


def aip_splice_ends(fits, ed, eas, gc_nm):
    """要不要改端 → None（照旧用 AIP）或每行要改的端 [{"dep","arr"} 的子集 …]（空集 = 这一行改不了，只是时段不符）。
    只有实测、没对上任何规则的一端（source='measured'）不参与触发：那时衔接点是实测跑道上的全部程序，
    零散的实测构型（只报一条跑道）会让白天也误触发；已经触发时，这一端照常按实测衔接点改。"""
    ok = lambda hit, e: hit is not False or (e or {}).get("source") == "measured"
    if any(x["time_ok"] and ok(x["dep_hit"], ed) and ok(x["arr_hit"], ea) for x, ea in zip(fits, eas)):
        return None
    if gc_nm <= AIP_SHORT_NM and any(x["time_ok"] for x in fits):
        return None
    return [{s for s, hit in (("dep", x["dep_hit"]), ("arr", x["arr_hit"])) if hit is False} for x in fits]


# ================= 实测的覆盖时段（用户定）=================
# 实测（SWIM 报文 / 羽田 ntrack）只代表报文那一刻。程序默认按「现在」规划，实测照用；
# 用户手动改了 EOBT → 按改后的时刻规划，这个时刻不在报文覆盖的时段里就以运行规则为准
# （白天的报文不能拿去定深夜 23:00 的航路：会按白天的 ILS Z 落 GODIN，而深夜规则要走 MESSE）。

def measured_covers(cfg, jst_min, weekday):
    """计划时刻（JST 当日分钟 + ISO 星期）是否落在这份实测覆盖的时段内：观测时刻起 valid_min 分钟。
    实测没带观测时刻（旧数据 / 测试桩）→ True（照旧使用）。"""
    obs = (cfg or {}).get("obs_jst")
    if not obs or jst_min is None:
        return True
    week = 7 * 1440
    d = (((weekday or 1) - 1) * 1440 + jst_min - ((obs[1] - 1) * 1440 + obs[0])) % week
    return d <= (cfg.get("valid_min") or 90)


def measured_at(cfg, ctx, planned_default=True):
    """这一端的实测此刻算不算数 → cfg 或 None。ctx = rule_contexts 给这一端的上下文（离场按 EOBT、到达按 ETA，用户定）。
    planned_default = EOBT 还是规划时的默认值（按「现在」规划）→ 实测照用；用户改了 EOBT → 只在报文覆盖的时段内算数。"""
    if not cfg:
        return None
    if planned_default:
        return cfg
    return cfg if measured_covers(cfg, ctx.get("jst_min"), ctx.get("weekday")) else None
