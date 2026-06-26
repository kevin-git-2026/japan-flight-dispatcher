# ================= 多源地景扫描模块 (功能 B) =================
# 直接扫描 X-Plane 与 MSFS 的地景安装文件夹，提取「已安装地景的机场 ICAO」，
# 合并并标注来源（XP / MSFS）。结果缓存到 installed_scenery.json，目录未变则秒开。
# 纯标准库；任一来源缺失/失败都软降级，不影响主流程。

import os
import re
import json

from .config import list_drives, load_sim_config, _update_sim_config
from .navdata import locate_xp_root

# MSFS UserCfg.opt 候选位置（覆盖 2020/2024 × Steam/MS Store）
_MSFS_USERCFG_CANDIDATES = [
    r"%APPDATA%\Microsoft Flight Simulator 2024\UserCfg.opt",                                   # 2024 Steam
    r"%APPDATA%\Microsoft Flight Simulator\UserCfg.opt",                                        # 2020 Steam
    r"%LOCALAPPDATA%\Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\UserCfg.opt",        # 2024 MS Store
    r"%LOCALAPPDATA%\Packages\Microsoft.FlightSimulator_8wekyb3d8bbwe\LocalCache\UserCfg.opt",  # 2020 MS Store
]
# MSFS 包根的全盘扫描兜底路径（UserCfg.opt 都失败时）
_MSFS_COMMON_PACKAGE_PATHS = [
    r"Microsoft Flight Simulator 2024\Packages",
    r"Microsoft Flight Simulator\Packages",
    r"SteamLibrary\steamapps\common\MicrosoftFlightSimulator2024\Packages",
    r"SteamLibrary\steamapps\common\MicrosoftFlightSimulator\Packages",
]


def _extract_japan_icaos(text):
    """从任意字符串里抓 RJ**/RO** 形式的日本机场 ICAO（大写）。
    用单词边界（前后必须是非字母数字）限定，避免把 'aerocaches' 里的 'roca'、
    单词 'road' 等普通片段误当成 ICAO；调用方仍应用导航数据白名单二次校验。"""
    if not text:
        return set()
    return {s.upper() for s in re.findall(r'(?i)(?<![A-Za-z0-9])R[JO][A-Za-z]{2}(?![A-Za-z0-9])', text)}


def find_msfs_packages_dir():
    """通用定位 MSFS Community 目录（覆盖 2020/2024 × Steam/MS Store）：
    1. installed_scenery.json 的 msfs_packages（验证有 Community）；
    2. 读多候选 UserCfg.opt 的 InstalledPackagesPath；
    3. 全盘扫描常见安装路径。命中写回 json。返回 Community 目录或 None。"""
    def community_of(pkg_root):
        if not pkg_root:
            return None
        comm = os.path.join(pkg_root, "Community")
        return comm if os.path.isdir(comm) else None

    comm = community_of(load_sim_config().get("msfs_packages"))
    if comm:
        return comm
    for cand in _MSFS_USERCFG_CANDIDATES:
        path = os.path.expandvars(cand)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                m = re.search(r'InstalledPackagesPath\s+"([^"]+)"', f.read())
            if m:
                comm = community_of(m.group(1))
                if comm:
                    _update_sim_config(msfs_packages=m.group(1))
                    return comm
        except Exception:
            continue
    for drive in list_drives():
        for cp in _MSFS_COMMON_PACKAGE_PATHS:
            comm = community_of(os.path.join(drive, cp))
            if comm:
                _update_sim_config(msfs_packages=os.path.join(drive, cp))
                return comm
    return None


def scan_xp_sceneries(custom_scenery_dir):
    """扫 X-Plane Custom Scenery：遍历每个包，读 Earth nav data\\apt.dat 提取机场行 ICAO（仅 RJ/RO）。
    apt.dat 机场行：行码 1/16/17 + 高程 + 2 个废弃字段 + ICAO。返回 set(ICAO)。"""
    icaos = set()
    if not custom_scenery_dir or not os.path.isdir(custom_scenery_dir):
        return icaos
    apt_re = re.compile(r'^\s*(?:1|16|17)\s+\S+\s+\S+\s+\S+\s+([A-Z0-9]{3,4})\b')
    try:
        packs = os.listdir(custom_scenery_dir)
    except Exception:
        return icaos
    for name in packs:
        apt = os.path.join(custom_scenery_dir, name, "Earth nav data", "apt.dat")
        if not os.path.exists(apt):
            continue
        try:
            with open(apt, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = apt_re.match(line)
                    if m and m.group(1).upper()[:2] in ("RJ", "RO"):
                        icaos.add(m.group(1).upper())
        except Exception:
            continue
    return icaos


def _extract_msfs_pack_icaos(pack_dir):
    """MSFS 单个地景包四步级联提 ICAO（逐级降级，命中即止），返回 set(RJ/RO ICAO)：
    1.包文件夹名 2.ContentInfo\\...\\ContentHistory.json(权威) 3.scenery\\*.bgl 文件名 4.放弃。"""
    # 步骤 1：包文件夹名
    found = _extract_japan_icaos(os.path.basename(pack_dir))
    if found:
        return found
    # 步骤 2：ContentInfo\<...>\ContentHistory.json —— items[] 里 type==Airport 的 content 即 ICAO
    content_info = os.path.join(pack_dir, "ContentInfo")
    if os.path.isdir(content_info):
        try:
            for root, dirs, files in os.walk(content_info):
                if "ContentHistory.json" in files:
                    with open(os.path.join(root, "ContentHistory.json"), "r", encoding="utf-8", errors="ignore") as f:
                        data = json.load(f)
                    for it in (data.get("items") or []):
                        if it.get("type") == "Airport":
                            c = str(it.get("content", "")).upper()
                            if c[:2] in ("RJ", "RO"):
                                found.add(c)
            if found:
                return found
        except Exception:
            pass
    # 步骤 3：scenery 子目录下 .bgl 文件名
    scenery_dir = next((os.path.join(pack_dir, n) for n in (os.listdir(pack_dir) if os.path.isdir(pack_dir) else [])
                        if n.lower() == "scenery"), None)
    if scenery_dir and os.path.isdir(scenery_dir):
        try:
            for root, dirs, files in os.walk(scenery_dir):
                for fn in files:
                    if fn.lower().endswith(".bgl"):
                        found |= _extract_japan_icaos(fn)
        except Exception:
            pass
    # 步骤 4：以上都没有 → 空集（不计入，多半是机模/库）
    return found


def scan_msfs_sceneries(community_dir):
    """扫 MSFS Community：每个包先用 manifest.json 的 content_type==SCENERY 预筛，
    再四步级联提 ICAO。返回 set(RJ/RO ICAO)。"""
    icaos = set()
    if not community_dir or not os.path.isdir(community_dir):
        return icaos
    try:
        packs = os.listdir(community_dir)
    except Exception:
        return icaos
    for name in packs:
        pack = os.path.join(community_dir, name)
        if not os.path.isdir(pack):
            continue
        mani = os.path.join(pack, "manifest.json")  # content_type 预筛（缺失则不筛）
        if os.path.exists(mani):
            try:
                with open(mani, "r", encoding="utf-8", errors="ignore") as f:
                    ct = str(json.load(f).get("content_type", "")).upper()
                if ct and ct != "SCENERY":
                    continue
            except Exception:
                pass
        icaos |= _extract_msfs_pack_icaos(pack)
    return icaos


def _scenery_fingerprint(dir_path):
    """对一个地景目录取轻量指纹 {包文件夹名: mtime}（只 listdir+getmtime，不读内容）。"""
    fp = {}
    if not dir_path or not os.path.isdir(dir_path):
        return fp
    try:
        for name in os.listdir(dir_path):
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                try:
                    fp[name] = os.path.getmtime(full)
                except Exception:
                    fp[name] = 0
    except Exception:
        pass
    return fp


def scan_installed_sceneries(force=False, valid_icaos=None):
    """功能 B 主入口：返回 (scenery_map, from_cache)。
    scenery_map: dict{ICAO: set('XP'|'MSFS')}；未检测到任何 sim 地景目录时返回 (None, False)。
    缓存优先：installed_scenery.json 的指纹未变则秒开；变了/force 才全量扫描。
    valid_icaos：导航数据里的真实 RJ/RO 机场白名单；非空时据此过滤掉假阳性
    （如把 'road' 误当 ROAD、'aerocaches' 误当 ROCA），并顺手清理旧缓存里残留的脏数据。"""
    xp_root = locate_xp_root()
    custom_scenery = os.path.join(xp_root, "Custom Scenery") if xp_root else None
    if custom_scenery and not os.path.isdir(custom_scenery):
        custom_scenery = None
    community = find_msfs_packages_dir()
    if not custom_scenery and not community:
        return None, False  # 未检测到任何 sim 地景目录 → 不启用地景标记

    def _whitelist(m):
        # 用导航数据白名单剔除「普通单词被误当 ICAO」的假阳性；白名单为空则不过滤（避免误删）
        if not valid_icaos:
            return m
        return {k: v for k, v in m.items() if k in valid_icaos}

    # 当前指纹（轻量，仅 listdir + getmtime）
    cur_fp = {}
    if custom_scenery:
        cur_fp["XP::" + custom_scenery] = _scenery_fingerprint(custom_scenery)
    if community:
        cur_fp["MSFS::" + community] = _scenery_fingerprint(community)

    cfg = load_sim_config()
    if (not force) and cfg.get("fingerprint") == cur_fp and isinstance(cfg.get("sceneries"), dict):
        cached = {k: set(v) for k, v in cfg["sceneries"].items()}
        cleaned = _whitelist(cached)
        if len(cleaned) != len(cached):
            # 旧缓存里残留的假阳性：回写清理一次（指纹不变，下次仍秒开命中缓存）
            _update_sim_config(sceneries={k: sorted(v) for k, v in cleaned.items()})
        return cleaned, True  # 命中缓存，秒开

    # 失效/无缓存 → 全量扫描
    result = {}
    if custom_scenery:
        for code in scan_xp_sceneries(custom_scenery):
            result.setdefault(code, set()).add("XP")
    if community:
        for code in scan_msfs_sceneries(community):
            result.setdefault(code, set()).add("MSFS")
    result = _whitelist(result)
    _update_sim_config(sceneries={k: sorted(v) for k, v in result.items()}, fingerprint=cur_fp)
    return result, False  # 刚全量扫描
