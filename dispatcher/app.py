# ================= 交互与执行流程 =================
# 启动一次性初始化（导航数据 + AIRAC + 多源地景 + AIP + Volanta），随后进入无限规划循环。
# 由项目根目录的 flight_dispatcher.py（薄壳入口）调用 main()。

import sys
import random

from .navdata import find_xp_data_files, check_airac_currency
from .data import load_japan_icao_set, load_airports_from_navigraph, load_aip_routes_from_csv
from .scenery import scan_installed_sceneries
from .volanta import (
    prompt_sync_volanta, sync_volanta_via_browser, load_volanta_flown_routes,
    volanta_auto_enabled, enable_volanta_auto, try_fetch_volanta_json_via_session,
)
from .routing import calculate_distance_nm, find_aip_route, get_random_route
from .flightaware import fetch_real_flights_with_filter
from .airlines import pick_sim_airline, init_airline_data


def main():
    # 冻结(exe)运行或 stdout 被重定向到管道/文件时，Windows 默认用本地代码页(中文系统为 GBK)
    # 编码输出，会因无法编码界面里的 emoji(如 ✈️)而崩溃。这里强制 UTF-8 输出，兼容控制台与管道。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("=" * 55)
    print("✈️ 欢迎使用日本航班智能搜索与规划脚本 ✈️")
    print("=" * 55)

    # 🌟 启动智能文件扫描雷达！
    dat_path, _ini_path = find_xp_data_files()
    if dat_path:
        check_airac_currency(dat_path)   # 功能 A：AIRAC 周期自检（过期则提示更新）

    # 真实日本机场 ICAO 白名单（来自导航数据），用于过滤地景扫描的假阳性（如 ROAD/ROCA）
    valid_japan_icaos = load_japan_icao_set(dat_path)

    # 🌍 功能 B：扫描 XP/MSFS 已安装地景（缓存优先，目录未变则秒开）
    print("🌍 正在检测已安装的机场地景（XP / MSFS，首次较慢、之后走缓存）...")
    scenery_map, _scn_cached = scan_installed_sceneries(valid_icaos=valid_japan_icaos)
    if scenery_map is None:
        print("ℹ️ 未检测到 X-Plane / MSFS 地景目录，本次跳过地景标注。")
    else:
        _xp_n = sum(1 for s in scenery_map.values() if "XP" in s)
        _msfs_n = sum(1 for s in scenery_map.values() if "MSFS" in s)
        print(f"🌍 已{'读取缓存' if _scn_cached else '扫描'}到 {len(scenery_map)} 个已装地景机场"
              f"（XP:{_xp_n} / MSFS:{_msfs_n}）。")

    # 加载航司执飞规则（airlines.json，首次运行自动生成，可自行增删改航司）
    init_airline_data()

    aip_data = load_aip_routes_from_csv()

    # 🛩️ F11：读取 Volanta 已飞航线（随机规划软优先未飞航线）
    #   同步偏好持久化在 volanta_data.json 的 preference 字段：
    #   - 用户曾选 Y → 'auto'：以后启动【静默自动同步】、不再询问（未开启前不主动扫浏览器）；
    #   - 未选过 / 选过 N/回车 → 每次启动询问一次（不锁死，随时可改主意）。
    flown_counts, vmeta = {}, {}
    if volanta_auto_enabled():
        # 已开启自动同步：静默用浏览器里的登录会话拉取（json 够新则跳过联网，不重复扫描）
        print("🔑 正在用 Volanta 登录会话自动同步已飞数据...")
        if try_fetch_volanta_json_via_session(skip_if_fresh=3600):
            print("✅ Volanta 已飞数据已是最新。")
        else:
            print("ℹ️ 未能自动刷新（登录可能已过期，可在浏览器重新登录 Volanta），沿用已保存的已飞数据。")
        flown_counts, vmeta = load_volanta_flown_routes()
    elif prompt_sync_volanta():
        # 选 Y：立即同步一次，并记住「以后自动同步」
        if try_fetch_volanta_json_via_session():            # 先试本机已有登录会话（无需开浏览器）
            print("✅ 已通过登录会话获取完整航班数据。")
            flown_counts, vmeta = load_volanta_flown_routes()
        else:
            _synced, flown_counts, vmeta = sync_volanta_via_browser()   # 无有效令牌 → 开浏览器登录
        enable_volanta_auto()
        print("🔖 已记住选择：以后启动将自动同步 Volanta（如需关闭，把 volanta_data.json 里的 preference 改回 ask）。")
    else:
        # N/回车：本次不同步，仍尝试读现有本地数据（手动导出的 json / CSV / 累积库）；下次启动再问
        flown_counts, vmeta = load_volanta_flown_routes()
    if flown_counts:
        _vlatest = vmeta.get("latest")
        print(f"✈️ 已从 Volanta 读取到 {vmeta.get('flights', sum(flown_counts.values()))} 次飞行、"
              f"覆盖 {len(flown_counts)} 条不同有向航线"
              + (f"（缓存更新于 {_vlatest}）" if _vlatest else "")
              + "，随机规划将优先未飞航线。")
    else:
        print("ℹ️ 未读取到 Volanta 数据，本次不启用「优先未飞」。（可在浏览器登录 Volanta 网页，或导出 CSV 放入工作目录）")

    # 预建 AIP 起降索引，供加权枚举时 O(1) 过滤（避免对每对航线线性遍历 aip_data）
    aip_index = {(r[0].strip().upper(), r[1].strip().upper()) for r in aip_data if len(r) >= 2} if aip_data else set()

    while True:
        try:
            print("\n" + "🛫 开 始 新 的 航 班 规 划 🛬".center(49, "-"))
            fixed_departure = input("📡 指定【出发机场】(4位 ICAO，直接回车随机): ").strip().upper()
            fixed_destination = input("📡 指定【目的机场】(4位 ICAO，直接回车随机): ").strip().upper()
            user_airline = input("🎫 指定【执飞航司】(3位 ICAO，直接回车不限): ").strip().upper()

            print("\n🔍 --- 高级筛选器 (直接回车可跳过) ---")
            user_aircraft = input("   ✈️ 期望的【机型代码】(如 737): ").strip()
            user_time_range = input("   ⏰ 期望起飞的【时间区间】(如 08:00-15:30): ").strip()
            print("-" * 55)

            runway_input = input("🛬 【机型起降需要的最短跑道长度，请带上单位（如1800m或5900ft）】(默认 1800m/5900ft，直接回车默认): ").strip().lower()
            min_runway_ft = 5900.0
            if runway_input:
                runway_input = runway_input.replace(" ", "")
                if runway_input.endswith("m"):
                    try: min_runway_ft = float(runway_input[:-1]) * 3.28084
                    except: pass
                elif runway_input.endswith("ft"):
                    try: min_runway_ft = float(runway_input[:-2])
                    except: pass

            min_dist_str = input("📏 【最短航程】(默认 200 NM): ").strip()
            max_dist_str = input("📏 【最长航程】(默认 450 NM): ").strip()
            user_min_dist = float(min_dist_str) if min_dist_str else 200.0
            user_max_dist = float(max_dist_str) if max_dist_str else 450.0
            if user_min_dist > user_max_dist: user_min_dist, user_max_dist = user_max_dist, user_min_dist

            print("-" * 55)
            strict_aip_mode = input("🗺️ 是否严格要求脚本搜索 AIP 规定航路？(Y/N): ").strip().upper() == 'Y'
            print("-" * 55)

            all_airports = load_airports_from_navigraph(dat_path, scenery_map, min_runway_ft)
            if not all_airports:
                print("❌ 未能找到任何符合条件的机场。请检查数据文件。")
                continue

            if strict_aip_mode and not aip_data:
                print("⚠️ 航路数据下载失败，已为您转为自由规划模式。")
                strict_aip_mode = False

            def print_flight_info(dep_obj, arr_obj, route_dist, route_details, flown_count=0):
                dep_warn = dep_obj.scenery_label() + (" [🛡️军用机场]" if dep_obj.is_military else "")
                arr_warn = arr_obj.scenery_label() + (" [🛡️军用机场]" if arr_obj.is_military else "")

                print("\n" + "🛫 航 线 归 划 成 功 🛬".center(51))
                print("*" * 55)
                print(f"  起飞机场 : {dep_obj.code}{dep_warn}")
                print(f"  降落机场 : {arr_obj.code}{arr_warn}")
                print(f"  大圆距离 : {route_dist:.1f} NM")
                if flown_count and flown_count > 0:
                    # F11：抽中的是已飞航线（加权后概率低但未被排除），给出信息性提示
                    print(f"  🔁 Volanta : 这条有向航线你已飞过 {flown_count} 次（已飞过，可考虑换一条）")

                if route_details:
                    print("-" * 55)
                    print("  📜 AIP 航路:")
                    for i, r in enumerate(route_details, 1): print(f"  [{i}] {r}")

                print("-" * 55)
                if not dep_obj.has_scenery or not arr_obj.has_scenery:
                    print("  ⚠️ 地景提醒: 标有 [⚠️无地景] 表示未在 XP/MSFS 地景文件夹中检测到该机场的插件地景")
                if dep_obj.is_military or arr_obj.is_military:
                    print("  🛡️ 军用提醒: 标有 [🛡️军用机场] 意味着该机场为军方使用，可能无民航设施与SID/STAR，请酌情考虑！")
                if not dep_obj.has_scenery or not arr_obj.has_scenery or dep_obj.is_military or arr_obj.is_military:
                    print("-" * 55)

                print(f"🔎 正在拉取现实排班...")
                is_exact, real_flights = fetch_real_flights_with_filter(dep_obj.code, arr_obj.code, user_airline, user_aircraft, user_time_range)
                url = f"https://flightaware.com/live/findflight?origin={dep_obj.code}&destination={arr_obj.code}"

                print("-" * 55)
                if is_exact and real_flights:
                    print(f"  💡 完美匹配！为您检索到以下现实排班 :")
                    for f in real_flights: print(f"     ✈️ {f}")
                elif real_flights:
                    print(f"  💡 仅找到该航线上的其他参考排班 :")
                    for f in real_flights: print(f"     ✈️ {f}")
                else:
                    # 无真实排班时降级：按航线两端所在大区挑一个合理航司（用户指定则优先用户的）
                    sim_airline_code = user_airline or pick_sim_airline(dep_obj.code, arr_obj.code)
                    print(f"  ❌ 未找到排班。已降级生成模拟呼号（⚠️未考虑现实运行情况，可能与现实运行存在出入） : {sim_airline_code}{random.randint(11, 899)}")
                print(f"\n  🔗 查看由flightaware给出的完整排版表: {url}")
                print("*" * 55)

            if fixed_departure and fixed_destination:
                dep_obj = next((a for a in all_airports if a.code == fixed_departure), None)
                arr_obj = next((a for a in all_airports if a.code == fixed_destination), None)
                if not dep_obj or not arr_obj: print("❌ 找不到指定机场。")
                else:
                    route_dist = calculate_distance_nm(dep_obj, arr_obj)
                    route_details = find_aip_route(aip_data, fixed_departure, fixed_destination) if aip_data else None
                    if strict_aip_mode and not route_details: print(f"\n❌ 未查到 AIP 规定航路。")
                    else: print_flight_info(dep_obj, arr_obj, route_dist, route_details, flown_counts.get((fixed_departure, fixed_destination), 0))
            else:
                dep, arr, dist, route, flown_count = get_random_route(all_airports, user_min_dist, user_max_dist, aip_data, strict_aip_mode, fixed_departure, fixed_destination, flown_counts, aip_index)
                print_flight_info(dep, arr, dist, route, flown_count)

        except Exception as e: print(f"\n❌ 发生错误: {e}")

        print("\n" + "=" * 55)
        if input("🔄 回车继续，输入 Q 退出: ").strip().upper() == 'Q': break
