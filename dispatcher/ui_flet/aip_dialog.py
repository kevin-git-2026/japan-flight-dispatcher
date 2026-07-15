# ================= F21：多 AIP 航路确认弹窗 =================
# 一条航线常有多条官方 AIP 航路（按 EOBT/ETA 时段、机型、巡航高度区分）。规划后自动弹这个窗，
# 仿真实 AIP 航路表罗列，让用户定一条。
#   · 非严格（默认）：纯罗列 航路/时段/高度/机型/用途/距离，手动勾一条。
#   · 严格（表单勾了「严格遵循现实运行规则」）：上方收 EOBT + 机型 + 巡航高度 →
#     实时判定（✓可用 / ✗不符 / ？待定），唯一可用即【自动选定】。
#
# 判定与选取全在 viewmodel.AipTableModel（时间可靠→自动筛；机型/高度是脏自由文本→按用户给的
# 参考值判属，绝不凭脏列硬选）。本文件只画表、收输入。

import flet as ft

from .theme import MONO

_COLS = ["选择", "航路 (Route)", "时段 (Hours)", "高度", "机型", "用途", "距离"]

_ROW_BG = {                                   # 判定的语义底色（同 tk 版）
    "match": ft.Colors.with_opacity(0.14, ft.Colors.GREEN),
    "no": ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
}


class AipDialog:
    """吃 viewmodel.AipTableModel；选定后回调 on_select(idx)。"""

    def __init__(self, page, on_select):
        self.page = page
        self._on_select = on_select
        self.model = None
        self.dlg = None

    def open(self, model, dep_icao, arr_icao):
        self.model = model
        m = model

        self.status = ft.Text(m.status, size=12, color=ft.Colors.GREY)
        self.table = ft.DataTable(
            columns=[ft.DataColumn(ft.Text(c, size=12, weight=ft.FontWeight.BOLD))
                     for c in (_COLS + (["判定"] if m.strict else []))],
            rows=[], column_spacing=18, heading_row_height=36, data_row_max_height=44)

        header = []
        if m.strict:                          # 严格模式才收 EOBT/机型/高度
            self.eobt = ft.TextField(label="EOBT (JST)", width=118, dense=True, text_size=13,
                                     value=m.eobt, content_padding=10,   # 用面板的 EOBT 预填
                                     label_style=ft.TextStyle(size=12),
                                     on_change=lambda e: self._recompute())
            # RadioGroup 的内层 Row 会撑满宽度，把后面的控件挤到下一行 → 给它定宽
            self.cat = ft.RadioGroup(
                value="JET", on_change=lambda e: self._recompute(),
                content=ft.Row([ft.Radio(value="JET", label="JET"),
                                ft.Radio(value="PROP", label="PROP")], spacing=0, width=190))
            self.fl = ft.TextField(label="巡航高度", hint_text="FL340", width=130, dense=True,
                                   text_size=13, content_padding=10,
                                   label_style=ft.TextStyle(size=12),
                                   on_change=lambda e: self._recompute())
            header = [ft.Row([self.eobt, ft.Text("机型", size=12), self.cat, self.fl],
                             spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                      ft.Text("时间可靠 → 自动筛；机型 / 巡航高度是 AIP 里的自由文本，"
                              "按您给的参考值判属。",
                              size=11, color=ft.Colors.GREY)]
            m.set_inputs(eobt=m.eobt)         # 预填的 EOBT 立即参与判定

        self._redraw()
        self.dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认 AIP 航路   %s → %s" % (dep_icao, arr_icao), size=15),
            content=ft.Container(
                width=980,
                content=ft.Column(header + [self.table],
                                  spacing=10, tight=True, scroll=ft.ScrollMode.AUTO)),
            actions=[self.status, ft.FilledButton("确认并关闭", on_click=lambda e: self._close())],
            actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
        self.page.show_dialog(self.dlg)

    # ---------- 内部 ----------
    def _eobt_val(self):
        """严格模式弹窗里填的 EOBT（继承回面板用）；非严格模式无此输入 → None。"""
        return self.eobt.value if (self.model and self.model.strict) else None

    def _close(self):
        # 确认关闭：把弹窗里最终的 EOBT 继承到面板（用户在此填的撤轮挡时刻，面板 EOBT / SimBrief 都该跟上）
        if self._eobt_val() is not None:
            self._on_select(self.model.sel_idx, self._eobt_val())
        self.page.pop_dialog()

    def _pick(self, idx):
        self.model.select(idx)
        self._on_select(idx, self._eobt_val())  # 面板同步换 base_route + 重筛跑道/程序（并继承 EOBT）
        self._redraw()
        self.page.update()

    def _recompute(self):
        auto = self.model.set_inputs(eobt=self.eobt.value, cat=self.cat.value, fl=self.fl.value)
        if auto is not None:
            self._on_select(auto, self._eobt_val())  # 唯一可用 → 自动选定（面板同步更新 + 继承 EOBT）
        self._redraw()
        self.page.update()

    def _redraw(self):
        rows = []
        for idx, cells, verdict in self.model.rows():
            # 航路串定宽 + 省略号：否则它（几十个航点）会把「距离 / 判定」两列顶出弹窗——
            # 而「判定」正是严格模式的核心。完整串挂 tooltip，鼠标悬停可看。
            route = ft.Container(width=340, content=ft.Text(
                cells[1], size=11, font_family=MONO, no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS, tooltip=cells[1]))
            widgets = [ft.Text(cells[0], size=14), route]              # ●/○ 行首单选标记
            widgets += [ft.Text(c, size=12) for c in cells[2:]]
            rows.append(ft.DataRow(
                cells=[ft.DataCell(w) for w in widgets],
                color=_ROW_BG.get(verdict),
                on_select_change=lambda e, i=idx: self._pick(i)))
        self.table.rows = rows
        self.status.value = self.model.status
