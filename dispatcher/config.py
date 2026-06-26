# ================= 运行路径 / 盘符 / installed_scenery.json 配置 =================
# 本模块是其它模块的基础：提供运行根目录锚点、盘符枚举，以及
# installed_scenery.json（统一存放「模拟器目录记忆 + 地景缓存」）的读写。

import os
import sys
import json


def get_real_run_path():
    """获取程序运行时的真实根目录（NavData / 各缓存文件的锚点）。
    冻结(exe)模式取 exe 所在目录；源码模式下本文件位于 <项目根>/dispatcher/config.py，
    上溯两级到项目根，确保拆包后同级文件的定位与单文件时完全一致。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# X-Plane 常见安装路径（全盘扫描时各盘符下逐一拼接）
XP_COMMON_PATHS = [
    r"Program Files (x86)\Steam\steamapps\common\X-Plane 12",
    r"Program Files (x86)\Steam\steamapps\common\X-Plane 11",
    r"SteamLibrary\steamapps\common\X-Plane 12",
    r"SteamLibrary\steamapps\common\X-Plane 11",
    r"Steam\steamapps\common\X-Plane 12",
    r"Steam\steamapps\common\X-Plane 11",
    r"X-Plane 12",
    r"X-Plane 11",
]


def list_drives():
    """返回当前系统所有可用盘符根（Windows）或 '/'（类 Unix）。"""
    if sys.platform == 'win32':
        import string
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return ['/']


# ---- installed_scenery.json：统一存放「模拟器目录记忆 + 地景缓存」 ----

def _sim_config_path():
    """installed_scenery.json 路径，锚定到运行目录。"""
    return os.path.join(get_real_run_path(), "installed_scenery.json")


def load_sim_config():
    """读取 installed_scenery.json（xp_root / msfs_packages / sceneries / fingerprint）。
    文件缺失/损坏返回空 dict，绝不崩。"""
    try:
        with open(_sim_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_sim_config(cfg):
    """把配置/缓存写回 installed_scenery.json，容错。"""
    try:
        with open(_sim_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _update_sim_config(**kv):
    """读出 → 更新指定字段 → 写回（保留其它字段）。"""
    cfg = load_sim_config()
    cfg.update(kv)
    save_sim_config(cfg)
    return cfg
