# ================= 数据加载与网络请求模块 =================
# 机场数据（earth_aptmeta.dat）解析 + 真实机场 ICAO 白名单 + AIP 航路 CSV（网络优先/本地缓存）。
# 注：地景检测已改为「多源地景扫描模块」(功能 B / scenery.py)，旧的 load_active_sceneries 已移除。

import os
import io
import csv
import time
import urllib.request

from .model import Airport
from .config import get_real_run_path


def load_airports_from_navigraph(filepath, scenery_map=None, min_runway_ft=5900):
    airport_list = []
    if not filepath or not os.path.exists(filepath):
        print(f"❌ 错误：未找到 earth_aptmeta.dat 导航数据文件: {filepath}")
        print("   请前往 Navigraph 下载页 https://navigraph.com/downloads 下载「X-Plane 12」的导航数据，")
        print("   解压后将其放入程序目录下的 NavData 文件夹（确保 NavData\\earth_aptmeta.dat 存在）再重启。")
        return airport_list

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('I'):
                continue
            try:
                parts = line.split()
                if len(parts) < 7:
                    continue
                code, region, lat_str, lon_str = parts[0], parts[1], parts[2], parts[3]
                type_flag = parts[5]
                runway_str = parts[6]

                if region not in ['RJ', 'RO']: continue
                if int(runway_str) < min_runway_ft: continue

                is_military = (type_flag.upper() == 'M')

                # 地景来源：有 scenery_map 时按其判定(set)；否则 None(未检测，软降级)
                srcs = scenery_map.get(code.upper(), set()) if scenery_map is not None else None

                airport_list.append(Airport(code, lat_str, lon_str, srcs, is_military))
            except Exception:
                continue
    return airport_list


def load_japan_icao_set(filepath):
    """从 earth_aptmeta.dat 读取「全部真实 RJ/RO 机场 ICAO」集合（不按跑道长度过滤）。
    用作地景扫描的白名单：剔除把普通单词误当 ICAO 的假阳性（如 'road'→ROAD、
    'aerocaches'→ROCA）。导航数据缺失/解析失败时返回空集，调用方据此不启用过滤。"""
    icaos = set()
    if not filepath or not os.path.exists(filepath):
        return icaos
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('I'):
                    continue
                parts = line.split()
                if len(parts) >= 7 and parts[1] in ('RJ', 'RO'):
                    icaos.add(parts[0].upper())
    except Exception:
        pass
    return icaos


def load_aip_routes_from_csv(csv_url="https://jp-routes.vercel.app/public/routes.csv"):
    real_dir = get_real_run_path()
    cache_file = os.path.join(real_dir, "routes_cache.csv")
    routes_data = []

    # 🌟 优先级 1：优先联网下载最新数据（只要有网，永远拿最新数据）
    print(f"🌐 正在优先连接网络下载最新 AIP 航路数据...")
    try:
        # 将网络超时缩短到 5 秒，这样万一没网，程序能快速切换到本地，不让用户干等
        req = urllib.request.Request(csv_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            csv_content = response.read().decode('utf-8')

            # 下载成功，同步刷新本地缓存
            try:
                with open(cache_file, 'w', encoding='utf-8', newline='') as f_cache:
                    f_cache.write(csv_content)
            except Exception:
                pass

            f = io.StringIO(csv_content)
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 2:
                    routes_data.append(row)
        print(f"✅ 成功从 https://jp-routes.vercel.app 下载到最新的AIP航路数据！已同步更新本地缓存（共 {len(routes_data)} 条）。")
        return routes_data

    except Exception as e:
        print(f"⚠️ 网络连接失败或超时，正在激活本地保底机制...")

        # 🌟 优先级 2：网络走不通，退入本地加载 CSV 缓存
        if os.path.exists(cache_file):
            file_age_seconds = time.time() - os.path.getmtime(cache_file)

            # 🌟 28天周期判定（28天 = 28 * 24 * 3600 = 2,419,200秒）
            if file_age_seconds < 2419200:
                print("📦 成功加载本地缓存：数据处于 28 天内。")
            else:
                print("⚠️ 提示：本地缓存已超过 28 天，AIP航路缓存可能已过期。因目前处于离线状态，将继续为您加载使用。")

            try:
                with open(cache_file, 'r', encoding='utf-8', errors='ignore') as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if len(row) >= 2:
                            routes_data.append(row)
                return routes_data
            except Exception:
                print("❌ 错误：本地缓存文件已损坏，无法加载。")
        else:
            print("❌ 错误：未找到本地缓存文件，且当前处于离线状态。")

        return []
