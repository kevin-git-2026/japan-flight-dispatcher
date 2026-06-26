# ================= 导航数据定位 / AIRAC 自检 / X-Plane 安装根定位 =================
# 功能 A：导航数据以「程序自带 NavData 文件夹」为主方案，彻底解耦 X-Plane；
# 并在启动时自检 AIRAC 周期是否过期。X-Plane 根定位既作导航数据兜底，也供地景扫描复用。

import os
import sys
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


# ---- 通用定位 X-Plane 安装根（导航数据兜底 + 地景扫描共用）----

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


def find_xp_data_files():
    """定位导航数据 earth_aptmeta.dat（功能 A 后主方案=程序自带 NavData）+ 可选 scenery_packs.ini。
    优先级：0.程序自带 NavData -> 1.exe 同级目录 -> 2.记忆/全盘扫描 XP -> 3.手动输入。
    地景检测已改为功能 B 多源扫描，故 ini 一律「可选」（缺失则地景走功能 B / 软降级）。"""
    real_dir = get_real_run_path()
    local_dat = os.path.join(real_dir, "earth_aptmeta.dat")
    local_ini = os.path.join(real_dir, "scenery_packs.ini")
    opt_ini = local_ini if os.path.exists(local_ini) else None

    # 🌟 优先级 0：程序根目录自带的 NavData 文件夹（彻底解耦 XP，对只飞 MSFS 的用户也适用）
    nav = find_navdata_file()
    if nav:
        print(f"📁 已读取程序自带的 NavData 导航数据：{os.path.relpath(nav, real_dir)}")
        return nav, opt_ini

    # 🌟 优先级 1：如果用户主动把 earth_aptmeta.dat 放在 exe 同级目录，直接使用（ini 可选）
    if os.path.exists(local_dat):
        print("📁 优先检测到本目录内的 earth_aptmeta.dat，将直接读取。")
        return local_dat, opt_ini

    def check_xp_root(root_path):
        if not os.path.exists(root_path): return None, None
        ini = os.path.join(root_path, "Custom Scenery", "scenery_packs.ini")
        # 兼容两种导航数据常见的存放层级
        dat1 = os.path.join(root_path, "Custom Data", "earth_aptmeta.dat")
        dat2 = os.path.join(root_path, "Custom Data", "earth_nav_data", "earth_aptmeta.dat")
        dat = dat1 if os.path.exists(dat1) else (dat2 if os.path.exists(dat2) else "")
        if os.path.exists(ini) and os.path.exists(dat):
            return dat, ini
        return None, None

    config_path = os.path.join(real_dir, "xp_path_config.txt")

    # 🌟 优先级 2：检查上次成功运行后保存的路径配置
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            saved_root = f.read().strip()
            d, i = check_xp_root(saved_root)
            if d and i:
                print(f"📁 成功读取已保存的 X-Plane 目录:\n   {saved_root}")
                return d, i

    # 🌟 优先级 3：暴力美学！遍历所有盘符扫描常见安装路径
    print("🔍 未检测到本地文件或配置文件，正在全盘智能扫描 X-Plane 11/12 安装目录...")
    drives = []
    if sys.platform == 'win32':
        import string
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    else:
        drives = ['/']

    common_paths = [
        r"Program Files (x86)\Steam\steamapps\common\X-Plane 12",
        r"Program Files (x86)\Steam\steamapps\common\X-Plane 11",
        r"SteamLibrary\steamapps\common\X-Plane 12",
        r"SteamLibrary\steamapps\common\X-Plane 11",
        r"Steam\steamapps\common\X-Plane 12",
        r"Steam\steamapps\common\X-Plane 11",
        r"X-Plane 12",
        r"X-Plane 11"
    ]

    for drive in drives:
        for cp in common_paths:
            test_root = os.path.join(drive, cp)
            d, i = check_xp_root(test_root)
            if d and i:
                print(f"✅ 自动扫描大成功！找到 X-Plane 目录:\n   {test_root}")
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(test_root)
                return d, i

    # 🌟 优先级 4：所有方法失效，请求用户自己喂饭，并永远记住！
    print("\n⚠️ 无法自动定位 X-Plane 根目录 (可能安装在了非常规路径)。")
    while True:
        user_root = input("📂 请直接粘贴您的 X-Plane 11/12 根目录路径 (右键粘贴，回车确认): ").strip().strip('"').strip("'")
        if not user_root:
            print("❌ 未提供路径，程序将继续使用缺失状态运行。")
            return local_dat, local_ini

        d, i = check_xp_root(user_root)
        if d and i:
            print("✅ 验证成功！已记住该路径，下次打开无需再次输入。")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(user_root)
            return d, i
        else:
            print("❌ 错误: 在该目录下找不到 Custom Scenery 或 Custom Data 数据，请检查路径是否正确！")
