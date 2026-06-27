# -*- coding: utf-8 -*-
# 日本航班智能搜索与规划脚本 —— 入口（薄壳）
#
# 程序逻辑已按功能拆分到同目录下的 dispatcher/ 子包，便于维护：
#   model.py       Airport 数据模型
#   config.py      运行路径 / 盘符 / installed_scenery.json 读写
#   navdata.py     自带 NavData 导航数据定位 + AIRAC 自检 + X-Plane 根定位（功能 A）
#   scenery.py     多源地景扫描（XP Custom Scenery + MSFS Community，功能 B）
#   data.py        机场数据 / 真实 ICAO 白名单 / AIP 航路加载
#   volanta.py     Volanta 已飞有向航线读取（F11）
#   flightaware.py 机型匹配 + FlightAware 现实排班爬虫
#   routing.py     大圆距离 / AIP 匹配 / 按已飞次数加权抽线（含「仅两端有地景」过滤）
#   planner.py     build_flight_plan：一次规划的计算（供 GUI 调用）
#   gui.py         tkinter 图形界面（唯一前端）
#
# v1.3.1 起【只保留 GUI】，已移除命令行版（CLI/终端）。
# 打包：pyinstaller --onefile --windowed flight_dispatcher.py

from dispatcher.gui import run_gui

if __name__ == "__main__":
    run_gui()
