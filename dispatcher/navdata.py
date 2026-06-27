# ================= 导航数据定位 / AIRAC 自检 / X-Plane 安装根定位 =================
# 功能 A：导航数据以「程序自带 NavData 文件夹」为主方案，彻底解耦 X-Plane；
# 并在启动时自检 AIRAC 周期是否过期。X-Plane 安装根定位（locate_xp_root）供地景扫描复用。

import os
import re
import datetime

from .config import (
    get_real_run_path, list_drives, XP_COMMON_PATHS,
    load_sim_config, _update_sim_config,
)


# ---- 功能 A：自带 NavData 导航数据 + AIRAC 周期自检 ----

def find_navdata_file():
    """优先读程序根目录自带的 NavData 文件夹里的 earth_aptmeta.dat。
    先看 NavData\\earth_aptmeta.dat，再在 NavData 下浅层（≤2 层）兜底递归。找不到返回 None。"""
    nav_dir = os.path.join(get_real_run_path(), "NavData")
    if not os.path.isdir(nav_dir):
        return None
    direct = os.path.join(nav_dir, "earth_aptmeta.dat")
    if os.path.exists(direct):
        return direct
    try:
        for root, dirs, files in os.walk(nav_dir):
            if root[len(nav_dir):].count(os.sep) >= 2:
                dirs[:] = []
                continue
            if "earth_aptmeta.dat" in files:
                return os.path.join(root, "earth_aptmeta.dat")
    except Exception:
        pass
    return None


# cycle_info.txt 月份缩写 → 月份数字（不依赖系统 locale）
_MONTH_ABBR = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def check_airac_currency(navdata_path):
    """读 NavData\\cycle_info.txt 自检 AIRAC 周期是否过期并提示更新。
    格式：'AIRAC cycle : 2605' / 'Valid (from/to): 14/MAY/2026 - 11/JUN/2026'。
    解析失败/文件缺失静默跳过，不影响主流程。"""
    try:
        info_path = os.path.join(os.path.dirname(navdata_path), "cycle_info.txt")
        if not os.path.exists(info_path):
            return
        with open(info_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        cm = re.search(r"AIRAC cycle\s*:\s*(\S+)", text)
        cycle = cm.group(1) if cm else None
        m = re.search(r"Valid.*?-\s*(\d{1,2})/([A-Za-z]{3})/(\d{4})", text)  # 取有效期结束日期
        if not m:
            return
        mon = _MONTH_ABBR.get(m.group(2).upper())
        if not mon:
            return
        valid_to = datetime.date(int(m.group(3)), mon, int(m.group(1)))
        date_str = valid_to.strftime("%Y-%m-%d")
        if datetime.date.today() <= valid_to:
            print(f"✅ 导航数据 AIRAC {cycle or '?'}，有效期至 {date_str}。")
        else:
            overdue = (datetime.date.today() - valid_to).days
            print(f"⚠️ 导航数据已过期（AIRAC {cycle or '?'}，有效期至 {date_str}，已过 {overdue} 天）。"
                  f"\n   建议前往 Navigraph 下载页 https://navigraph.com/downloads 重新下载"
                  f"「X-Plane 12」的导航数据，用新数据替换程序目录下的 NavData 文件夹内的数据后重启。")
    except Exception:
        pass


# ---- 通用定位 X-Plane 安装根（地景扫描共用）----

def _is_xp_root(root_path):
    """粗判一个目录是否像 X-Plane 安装根（有 Custom Scenery 或 Custom Data 即可）。"""
    if not root_path or not os.path.isdir(root_path):
        return False
    return os.path.isdir(os.path.join(root_path, "Custom Scenery")) or \
           os.path.isdir(os.path.join(root_path, "Custom Data"))


def locate_xp_root():
    """通用定位 X-Plane 安装根：
    1. installed_scenery.json 的 xp_root（验证有效）；
    2. 迁移旧 xp_path_config.txt（存在则读入写进 json）；
    3. 全盘扫描 XP_COMMON_PATHS；命中写回 json。找不到返回 None，不做交互。"""
    saved = load_sim_config().get("xp_root")
    if saved and _is_xp_root(saved):
        return saved
    old_cfg = os.path.join(get_real_run_path(), "xp_path_config.txt")  # 迁移旧配置
    if os.path.exists(old_cfg):
        try:
            with open(old_cfg, "r", encoding="utf-8") as f:
                root = f.read().strip()
            if _is_xp_root(root):
                _update_sim_config(xp_root=root)
                return root
        except Exception:
            pass
    for drive in list_drives():
        for cp in XP_COMMON_PATHS:
            test_root = os.path.join(drive, cp)
            if _is_xp_root(test_root):
                _update_sim_config(xp_root=test_root)
                return test_root
    return None
