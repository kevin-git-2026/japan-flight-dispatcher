# 日本航班智能搜索与规划脚本 —— 功能拆分子包
# 入口是项目根目录的 flight_dispatcher.py（薄壳，转调 dispatcher.ui_flet.run_flet）。
# 各模块职责见同目录下文件。逻辑层纯标准库；前端是 Flet（Python 写、Flutter 渲染），用 flet pack 打包。

__version__ = "2.0.0_alpha1"
