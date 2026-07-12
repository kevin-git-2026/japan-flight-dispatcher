# -*- coding: utf-8 -*-
# 日本航班智能搜索与规划脚本 —— 入口（薄壳）
#
# 程序逻辑按功能拆分到同目录下的 dispatcher/ 子包：
#   model.py       Airport 数据模型
#   config.py      运行路径 / 盘符 / installed_scenery.json 读写
#   navdata.py     自带 NavData 导航数据定位 + AIRAC 自检 + X-Plane 根定位（功能 A）
#   scenery.py     多源地景扫描（XP Custom Scenery + MSFS Community，功能 B）
#   data.py        机场数据 / 真实 ICAO 白名单 / AIP 航路加载
#   volanta.py     Volanta 已飞有向航线读取（F11）
#   flightaware.py 机型匹配 + FlightAware 现实排班爬虫
#   routing.py     大圆距离 / AIP 匹配 / 按已飞次数加权抽线（含「仅两端有地景」过滤）
#   planner.py     build_flight_plan：一次规划的计算
#   router.py      无 AIP 航路时本地 A* 航路生成（解析 NavData 的 airway/fix/nav，F15）
#   procedures.py  CIFP 跑道 / SID / STAR / IAP 枚举 + 航路端点预筛（F20/F23）
#   weather.py     METAR/TAF + 网格天气回退 + 跑道风分量（F20/F22）
#   operations.py  operation.json 机场运行规则：存取 + 应用引擎（F23/F24）
#   timed.py       分时段 AIP 航路选择（F21）
#   controller.py  编排：应用状态 / 后台任务 / 日志（零 GUI 依赖）
#   viewmodel.py   各界面的纯数据 Model（零 GUI 依赖，可 headless 单测）
#   ui_flet/       Flet 前端（Python 写、Flutter 渲染）——v2.0.0 起唯一的前端
#
# 三层架构：17 个逻辑模块（零 GUI）→ controller + viewmodel（零 GUI，可 headless 单测）→ ui_flet（只剩渲染）。
#
# 打包：flet pack flight_dispatcher.py -n flight_dispatcher -y \
#         --pyinstaller-build-args="--exclude-module=numpy" ...=scipy ...=tkinter ...=PIL

from dispatcher.ui_flet import run_flet

if __name__ == "__main__":
    run_flet()
