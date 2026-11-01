# ================= 实时天气与 ATIS（F33）=================
# 右栏的「实时天气与 ATIS」标签页：监视机场的 ATIS / METAR / TAF，Fenix 和 SWIM 开关。
# 只渲染：内容来自 live.LiveService 的快照，文案在 viewmodel 里。

import flet as ft

from .theme import MONO, panel


class _Section:
    """一个机场：标题行 + ATIS + METAR + TAF。原地改文字，刷新时不打断用户选中文字。"""

    def __init__(self, icao, can_update, on_update, on_remove):
        self.title = ft.Text("", size=13, weight=ft.FontWeight.BOLD, selectable=True, expand=True)
        self.atis = ft.Text("", size=12, font_family=MONO, selectable=True)
        self.metar = ft.Text("", size=11, font_family=MONO, color=ft.Colors.GREY, selectable=True)
        self.taf = ft.Text("", size=11, font_family=MONO, color=ft.Colors.GREY, selectable=True)
        buttons = [ft.TextButton("移除", on_click=lambda e: on_remove(icao))]
        if can_update:                                    # 只有 SWIM 覆盖的机场才能查真实 ATIS
            buttons.insert(0, ft.OutlinedButton("更新 ATIS", on_click=lambda e: on_update(icao)))
        self.control = ft.Column([
            ft.Row([self.title] + buttons),
            self.atis, self.metar, self.taf, ft.Divider(height=8),
        ], spacing=4)

    def show(self, sec):
        self.title.value = sec["title"]
        self.atis.value = sec["atis"]
        self.metar.value = sec["metar"]
        self.taf.value = sec["taf"]
        self.taf.visible = bool(sec["taf"])


class LiveView:
    def __init__(self, can_update, on_add, on_update, on_remove, on_fenix, on_swim,
                 on_save_login, on_clear_login):
        self._can_update = can_update
        self._on_save_login = on_save_login
        self._on_add = on_add
        self._on_update = on_update
        self._on_remove = on_remove
        self._sections = {}
        self.icao = ft.TextField(label="添加机场", width=120, dense=True, text_size=13,
                                 content_padding=10, label_style=ft.TextStyle(size=12),
                                 capitalization=ft.TextCapitalization.CHARACTERS, max_length=4,
                                 counter="", on_submit=lambda e: self._add())
        self.fenix = ft.Switch(label="Fenix ATIS 服务", value=False, label_text_style=ft.TextStyle(size=12),
                               on_change=lambda e: on_fenix(bool(self.fenix.value)))
        self.swim = ft.Switch(label="SWIM 真实 ATIS", value=False, label_text_style=ft.TextStyle(size=12),
                              on_change=lambda e: on_swim(bool(self.swim.value)))
        # 账号密码只用来交给 on_save_login 加密保存，随后立刻清空输入框，也不会再回填
        self.user = ft.TextField(label="SWIM 账号", width=220, dense=True, text_size=13,
                                 content_padding=10, label_style=ft.TextStyle(size=12))
        self.password = ft.TextField(label="SWIM 密码", width=180, dense=True, text_size=13,
                                     content_padding=10, label_style=ft.TextStyle(size=12),
                                     password=True, can_reveal_password=False,
                                     on_submit=lambda e: self._save_login())
        self.login_row = ft.Row([self.user, self.password,
                                 ft.OutlinedButton("加密保存", on_click=lambda e: self._save_login())],
                                spacing=8, wrap=True)
        self.saved_row = ft.Row([ft.Text("SWIM 账号已加密保存在本机", size=12),
                                 ft.TextButton("清除", on_click=lambda e: on_clear_login())],
                                spacing=8, visible=False)
        self.status = ft.Text("", size=11, color=ft.Colors.GREY, selectable=True)
        self.note = ft.Text("", size=11, color=ft.Colors.AMBER_800, visible=False)
        self.empty = ft.Text("还没有监视的机场。规划一次航线，或在上方添加机场。", size=12,
                             color=ft.Colors.GREY)
        self.body = ft.ListView([self.empty], spacing=4, expand=True, auto_scroll=False)
        self.control = panel(ft.Column([
            ft.Row([self.icao, ft.OutlinedButton("添加", on_click=lambda e: self._add()),
                    self.fenix, self.swim], spacing=12, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self.login_row, self.saved_row, self.status, self.note, ft.Divider(height=6), self.body,
        ], spacing=6, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            expand=5, padding=12, visible=False)

    def _add(self):
        icao = (self.icao.value or "").strip().upper()
        if self._on_add(icao):
            self.icao.value = ""

    def _save_login(self):
        user, password = (self.user.value or "").strip(), self.password.value or ""
        self.password.value = ""                          # 不管成败都立刻清掉
        self._on_save_login(user, password)

    def show_login(self, saved, ask_again=False):
        """ask_again：已保存的账号不能用（密码错误或解不开），要能直接重新输入。"""
        self.saved_row.visible = saved
        self.login_row.visible = ask_again or not saved
        if not self.login_row.visible:
            self.user.value = ""

    def set_note(self, text):
        self.note.value = text or ""
        self.note.visible = bool(text)

    def set_airports(self, icaos):
        """监视列表变化时重建；已有机场的控件保留。"""
        self._sections = {i: self._sections.get(i)
                          or _Section(i, i in self._can_update, self._on_update, self._on_remove)
                          for i in icaos}
        self.body.controls = ([s.control for s in self._sections.values()]
                              if self._sections else [self.empty])

    def show_section(self, icao, sec):
        s = self._sections.get(icao)
        if s:
            s.show(sec)
