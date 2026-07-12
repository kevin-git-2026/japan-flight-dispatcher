# ================= 日志区 =================
# stdout/stderr 经 controller.LogSink → Shell.log_emit（缓冲）→ 这里【一批一个 patch】。

import flet as ft

from .theme import MONO, panel

_MAX_BLOCKS = 400                       # 上限，防长跑积累


class LogView:
    def __init__(self):
        self._list = ft.ListView(auto_scroll=True, spacing=0, expand=True, padding=6)
        self.control = panel(ft.Column([
            ft.Text("日志 / 状态", size=11, color=ft.Colors.GREY),
            self._list,
        ], spacing=2, expand=True), padding=8)
        self.control.height = 128

    def append(self, text):
        """只在 Shell.post 内（Flet 事件循环线程）被调用。"""
        if not text:
            return
        self._list.controls.append(
            ft.Text(text.rstrip("\n"), size=11, font_family=MONO, selectable=True))
        if len(self._list.controls) > _MAX_BLOCKS:
            del self._list.controls[:len(self._list.controls) - _MAX_BLOCKS]
