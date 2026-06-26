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
#   routing.py     大圆距离 / AIP 匹配 / 按已飞次数加权抽线
#   app.py         main() 启动初始化 + 交互规划主循环
#
# 本文件仅作为入口保留，使「python flight_dispatcher.py」运行命令
# 与「pyinstaller --onefile flight_dispatcher.py」打包命令保持不变。

from dispatcher.app import main

if __name__ == "__main__":
    main()
