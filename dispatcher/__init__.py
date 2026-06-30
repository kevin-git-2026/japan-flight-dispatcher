# 日本航班智能搜索与规划脚本 —— 功能拆分子包
# 入口仍是项目根目录的 flight_dispatcher.py（薄壳，转调 dispatcher.gui.run_gui）。
# 各模块职责见同目录下文件；纯标准库实现，PyInstaller --onefile --windowed 直接打包入口即可。

__version__ = "1.4.1_alpha2"
