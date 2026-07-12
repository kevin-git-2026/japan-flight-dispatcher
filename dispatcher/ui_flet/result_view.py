# ================= 结果卡 =================
# viewmodel.result_spans() 产出的 span 列表 → ft.TextSpan。
# 点击动作直接用闭包绑在 TextSpan 上（tk 那边要造动态 tag + tag_bind + _map_tags 簿记，这里全省了）。
# 结果卡在两套 UI 下的【文字逐字一致】——因为文案与语义样式全在 viewmodel 里定死。

import flet as ft

from .theme import panel, span_style


class ResultView:
    def __init__(self, on_open_url, on_open_map):
        self._on_open_url = on_open_url
        self._on_open_map = on_open_map
        self._text = ft.Text(spans=[], selectable=True)
        # 【内部可滚动】：航路多、排班多时结果卡很长，而各人显示器分辨率不同——能滚就不会有内容被吃掉
        self._body = ft.Column([self._text], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0)
        self.control = panel(ft.Column([
            ft.Text("规划结果", size=11, color=ft.Colors.GREY),
            self._body,
        ], spacing=6, expand=True), expand=5, padding=12)

    def render(self, spans):
        out = []
        for sp in spans:
            on_click = None
            act = sp.action
            if act:
                if act[0] in ("url", "simbrief"):
                    url = act[1]
                    on_click = lambda e, u=url: self._on_open_url(u)
                elif act[0] == "map":
                    coords, title = act[1], act[2]
                    on_click = lambda e, c=coords, t=title: self._on_open_map(c, t)
            out.append(ft.TextSpan(sp.text, style=span_style(sp.style), on_click=on_click))
        self._text.spans = out

    def clear(self):
        self._text.spans = []
