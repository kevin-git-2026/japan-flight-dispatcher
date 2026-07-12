# ================= F23：机场运行规则编辑器 =================
# 为任意 RJ/RO 机场编写「运行规则」（时段 + 星期 + 风门槛 + 好天门槛 → 离场跑道/SID、
# 到达跑道/STAR/IAP），存进 operation.json；规划时由 F24 应用引擎消费。
#
# 整套状态机（载入/切机场/增删改/复制/重排/脏标记/静默提交/保存/多机场隔离）在
# viewmodel.OpsEditorModel，已 headless 单测过。本文件【只画】：
#   ① Model → 控件（规则列表、详情表单、五个多选）；② 控件 → Model（读表单、点选、拖拽）。
#
# ⚠️ 规则顺序【不是】全局优先级：应用引擎按条件命中选规则（见 operations.select_rule）。
#    拖拽调序只用于让「好天 → 恶天」成对相邻（好天在上）。
#
# ⚠️ ReorderableListView 的 on_reorder：Flet 的 Dart 侧【已经】做了 `newIndex -= 1` 归一化
#    （v0.85.3 reorderable_list_view.dart，在 triggerEvent 之前），所以 new_index 就是落位后的
#    最终下标 —— 直接 pop/insert（VM.move_rule）即可，【不要】再自己减 1，否则每次下拖都错一位。
#    但它【不会】替我们重排 controls，所以拖完仍要按 Model 的新顺序重建行。

import flet as ft

from .. import viewmodel as VM
from .theme import panel

_DAYS = "一二三四五六日"
_WKINDS = ["顺风", "逆风", "侧风"]
_COVERS = ["FEW", "SCT", "BKN", "OVC"]
_NO_RWY = "—"                                   # 参照跑道「不限」的哨兵（Dropdown 的空 key 不可靠）
_ROW_H = 40                                     # 一行 Checkbox 的高度（Material 默认，实测）

_HINT_WIND = ("风分量相对该参照跑道算：顺风超→换向（南風運用：相对 34R 顺风≥10）；"
              "侧风超→换落跑道（都心：相对 16L 侧风≥15 改落 22/23）；逆风超→其它例外；留空=默认构型")
_HINT_WX = ("好天=天气至少这么好才用本规则（如 LDA：云底≥1500ft · SCT 起算[few 不计] · 能见度≥6000m）；"
            "恶天规则不填天气门槛、紧排其下作兜底；留空=不限")
_HINT_ORDER = "拖拽调序：让「好天 → 恶天」成对相邻（好天在上）。规则本身是均等匹配，不是全局优先级。"


class OpsView:
    """运行规则编辑器（pushed ft.View —— Flet 无多窗口，故不是 Toplevel）。"""

    def __init__(self, page, on_close):
        self.page = page
        self._on_close = on_close
        self.model = None
        self._syncing = False
        self.view = None

    # ---------- 构建 ----------
    def build(self, dat_path):
        self.model = VM.OpsEditorModel(dat_path)
        self._syncing = False

        # —— 顶部：机场选择（可从已有规则的机场里选，也可自由键入新 ICAO）——
        self.cb_icao = ft.Dropdown(label="机场 ICAO", width=150, editable=True, enable_filter=True,
                                   dense=True, text_size=13, options=[],
                                   label_style=ft.TextStyle(size=12),
                                   on_select=lambda e: self._load_airport())
        self.lbl_existing = ft.Text("", size=12, color=ft.Colors.GREY, expand=True)
        top = ft.Row([self.cb_icao,
                      ft.OutlinedButton("载入", on_click=lambda e: self._load_airport()),
                      self.lbl_existing],
                     spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        # —— 左：规则列表（可拖拽调序）——
        self.rlv = ft.ReorderableListView(controls=[], expand=True, spacing=2,
                                          on_reorder=self._on_reorder)
        left = panel(width=360, padding=10, content=ft.Column([
            ft.Text("规则", size=12, weight=ft.FontWeight.BOLD),
            self.rlv,
            # 用 Material 图标而不是全角「＋ / －」：那两个字符在任何 UI 字体下都渲染得很难看
            ft.Row([ft.OutlinedButton("新增", icon=ft.Icons.ADD,
                                      on_click=lambda e: self._add_rule()),
                    ft.OutlinedButton("复制", icon=ft.Icons.CONTENT_COPY,
                                      on_click=lambda e: self._dup_rule()),
                    ft.OutlinedButton("删除", icon=ft.Icons.DELETE_OUTLINE,
                                      on_click=lambda e: self._ask_delete())],
                   spacing=6, wrap=True),
            ft.Text(_HINT_ORDER, size=11, color=ft.Colors.GREY, no_wrap=False),
        ], spacing=8, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH))

        # —— 右：规则详情（条件 + 跑道/程序多选）——
        right = panel(expand=True, padding=ft.Padding.only(left=12, right=12, top=12, bottom=10),
                      content=ft.Column([
                          ft.Text("规则详情", size=12, weight=ft.FontWeight.BOLD),
                          # 表单主体内部滚动：多选列表很长（IAP 可达 30+ 条），任何分辨率都别指望塞得下。
                          # 「✓ 应用」按钮留在滚动区【外】——它是动作入口，不该被内容顶出视野。
                          ft.ListView(self._detail_controls(), expand=True, spacing=8,
                                      auto_scroll=False),
                          ft.Row([ft.FilledButton("✓ 应用到所选规则",
                                                  on_click=lambda e: self._apply_rule())],
                                 alignment=ft.MainAxisAlignment.END),
                      ], spacing=8, expand=True,
                          horizontal_alignment=ft.CrossAxisAlignment.STRETCH))

        self.view = ft.View(
            route="/ops", padding=10,
            bgcolor=ft.Colors.SURFACE,                            # 显式不透明，别指望 View 的默认底色
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,   # 默认 START → 子控件只取内容宽
            appbar=ft.AppBar(
                leading=ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回",
                                      on_click=lambda e: self.ask_close()),
                title=ft.Text("⚙️ 机场运行规则编辑器"),
                actions=[ft.FilledButton("💾 保存到 operation.json",
                                         on_click=lambda e: self._save()),
                         ft.Container(width=10)]),
            controls=[ft.Column([
                top,
                ft.Row([left, right], expand=True, spacing=10,
                       vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            ], expand=True, spacing=10)])

        self._refresh_existing()
        self._render_rules()
        return self.view

    def _detail_controls(self):
        """详情表单的控件（条件区 + 离场/到达多选区）。"""
        self.f_name = ft.TextField(label="名称", dense=True, text_size=13, content_padding=10,
                                   label_style=ft.TextStyle(size=12),
                                   on_change=lambda e: self._mark_dirty())
        self.f_time = ft.TextField(label="时段 (JST)", hint_text="1500-1900，逗号分隔多段；空=全天不限",
                                   dense=True, text_size=13, content_padding=10,
                                   label_style=ft.TextStyle(size=12),
                                   on_change=lambda e: self._mark_dirty())
        # width 必给：不定宽的 Checkbox 放进 wrap 的 Row 里会各占一整行（实测七个星期竖成一列，
        # 白吃 400+px 高度，把下面的跑道/程序多选区顶出视野）。定宽后才横排成一行。
        self.f_days = [ft.Checkbox(label=d, value=False, width=64,
                                   label_style=ft.TextStyle(size=12),
                                   on_change=lambda e: self._mark_dirty()) for d in _DAYS]

        self.f_ref = ft.Dropdown(label="参照跑道", width=118, dense=True, text_size=13,
                                 label_style=ft.TextStyle(size=12), value=_NO_RWY,
                                 options=[ft.DropdownOption(key=_NO_RWY, text="（不限）")],
                                 on_select=lambda e: self._mark_dirty())
        self.f_wkind = ft.Dropdown(label="风向", width=104, dense=True, text_size=13, value="顺风",
                                   label_style=ft.TextStyle(size=12),
                                   options=[ft.DropdownOption(key=k, text=k) for k in _WKINDS],
                                   on_select=lambda e: self._mark_dirty())
        self.f_wmin = ft.TextField(label="≥ 节", width=88, dense=True, text_size=13, content_padding=10,
                                   label_style=ft.TextStyle(size=12),
                                   on_change=lambda e: self._mark_dirty())

        self.f_ceil = ft.TextField(label="云底 ≥ ft", width=112, dense=True, text_size=13,
                                   content_padding=10, label_style=ft.TextStyle(size=12),
                                   on_change=lambda e: self._mark_dirty())
        self.f_cover = ft.Dropdown(label="起算云量", width=118, dense=True, text_size=13, value="SCT",
                                   label_style=ft.TextStyle(size=12),
                                   options=[ft.DropdownOption(key=c, text=c) for c in _COVERS],
                                   on_select=lambda e: self._mark_dirty())
        self.f_vis = ft.TextField(label="能见度 ≥ m", width=124, dense=True, text_size=13,
                                  content_padding=10, label_style=ft.TextStyle(size=12),
                                  on_change=lambda e: self._mark_dirty())

        # 高度按整行数给（一行 Checkbox ≈ _ROW_H），否则列表底部永远切着半个勾选框，像渲染坏了
        self._ms = {}
        dep_block = ft.Column([
            ft.Text("离场", size=12, weight=ft.FontWeight.BOLD),
            self._ms_block("dep_rwy", "跑道", rows=4),
            self._ms_block("dep_sid", "SID（可搜索）", rows=5, filterable=True),
        ], spacing=6, expand=True)
        arr_block = ft.Column([
            ft.Text("到达", size=12, weight=ft.FontWeight.BOLD),
            self._ms_block("arr_rwy", "跑道", rows=4),
            self._ms_block("arr_star", "STAR（可搜索）", rows=4, filterable=True),
            self._ms_block("arr_iap", "IAP 仪表进近（可搜索）", rows=5, filterable=True),
        ], spacing=6, expand=True)

        return [
            # Material 的浮动标签会浮到控件上沿【之外】，紧贴滚动视口顶边会被裁掉 → 垫一行
            ft.Container(height=2),
            self.f_name,
            self.f_time,
            ft.Row([ft.Text("星期", size=12, width=64)] + self.f_days +
                   [ft.Text("（全不勾=每天；深夜运用常按星期几不同）", size=11, color=ft.Colors.GREY)],
                   spacing=0, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([ft.Text("换向门槛", size=12, width=64), self.f_ref, self.f_wkind, self.f_wmin],
                   spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Text(_HINT_WIND, size=11, color=ft.Colors.GREY, no_wrap=False),
            ft.Row([ft.Text("好天门槛", size=12, width=64), self.f_ceil, self.f_cover, self.f_vis],
                   spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Text(_HINT_WX, size=11, color=ft.Colors.GREY, no_wrap=False),
            ft.Divider(height=8),
            ft.Row([dep_block, arr_block], spacing=12,
                   vertical_alignment=ft.CrossAxisAlignment.START),
        ]

    # ---------- 五个多选（渲染 VM.MultiSelectModel）----------
    def _ms_block(self, key, title, rows, filterable=False):
        """多选列表：Checkbox 本就是「点击即切换」的诚实 UI（tk 版得靠 selectmode=multiple 模拟）。
        过滤只改可见项，已选但被过滤隐藏的项【不丢】—— 这条语义在 MultiSelectModel 里，白送。"""
        lv = ft.ListView(controls=[], height=rows * _ROW_H + 4, spacing=0, auto_scroll=False)
        fld = None
        if filterable:
            fld = ft.TextField(hint_text="搜索…", dense=True, text_size=12, content_padding=8,
                               hint_style=ft.TextStyle(size=12),
                               on_change=lambda e, k=key: self._ms_filter(k))
        self._ms[key] = {"lv": lv, "filter": fld}
        kids = [ft.Text(title, size=11, color=ft.Colors.GREY)]
        if fld is not None:
            kids.append(fld)
        kids.append(ft.Container(
            content=lv, border_radius=8, padding=ft.Padding.symmetric(horizontal=4, vertical=2),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.18, ft.Colors.ON_SURFACE))))
        return ft.Column(kids, spacing=4)

    def _ms_render(self, key):
        ms = self.model.ms[key]
        self._ms[key]["lv"].controls = [
            ft.Checkbox(label=disp, value=sel, label_style=ft.TextStyle(size=12),
                        on_change=lambda e, k=key, v=val: self._ms_toggle(k, v))
            for disp, val, sel in ms.shown()]

    def _ms_render_all(self):
        self._syncing = True
        try:
            for key in self._ms:
                fld = self._ms[key]["filter"]
                if fld is not None:
                    fld.value = self.model.ms[key].filter_text      # 换规则时 Model 清了搜索
                self._ms_render(key)
        finally:
            self._syncing = False

    def _ms_filter(self, key):
        if self._syncing:
            return
        self.model.ms[key].set_filter(self._ms[key]["filter"].value or "")
        self._ms_render(key)                                        # 过滤不是编辑 → 不标脏
        self.page.update()

    def _ms_toggle(self, key, value):
        if self._syncing:
            return
        self.model.ms[key].toggle(value)
        self._mark_dirty()

    # ---------- 表单 ↔ Model ----------
    def _mark_dirty(self):
        if self.model and not self._syncing:
            self.model.form_dirty = True

    def _read_form(self):
        ref = (self.f_ref.value or "").strip()
        return VM.FormData(
            name=self.f_name.value or "", time_text=self.f_time.value or "",
            days=[bool(c.value) for c in self.f_days],
            ref_rwy="" if ref == _NO_RWY else ref,
            wind_kind=self.f_wkind.value or "顺风", wind_min=self.f_wmin.value or "",
            ceiling=self.f_ceil.value or "", ceiling_cover=self.f_cover.value or "SCT",
            visibility=self.f_vis.value or "")

    def _write_form(self, fd):
        if fd is None:
            return
        self._syncing = True
        try:
            self.f_name.value = fd.name
            self.f_time.value = fd.time_text
            for i, c in enumerate(self.f_days):
                c.value = bool(fd.days[i])
            self.f_ref.value = fd.ref_rwy or _NO_RWY
            self.f_wkind.value = fd.wind_kind or "顺风"
            self.f_wmin.value = fd.wind_min
            self.f_ceil.value = fd.ceiling
            self.f_cover.value = fd.ceiling_cover or "SCT"
            self.f_vis.value = fd.visibility
        finally:
            self._syncing = False
        self._ms_render_all()
        self.model.form_dirty = False

    # ---------- 机场 ----------
    def _refresh_existing(self):
        self.cb_icao.options = [ft.DropdownOption(key=a, text=a)
                                for a in self.model.existing_airports()]
        self.lbl_existing.value = self.model.existing_text()

    def _load_airport(self):
        icao = (self.cb_icao.text or self.cb_icao.value or "").strip().upper()
        if not icao:
            return
        has_cifp = self.model.load_airport(icao)
        self._syncing = True
        try:
            self.cb_icao.value = self.model.icao if any(
                o.key == self.model.icao for o in self.cb_icao.options) else None
            self.cb_icao.text = self.model.icao
            self.f_ref.options = [ft.DropdownOption(key=(r or _NO_RWY),
                                                    text=(r or "（不限）"))
                                  for r in self.model.runway_choices] or [
                ft.DropdownOption(key=_NO_RWY, text="（不限）")]
        finally:
            self._syncing = False
        self._write_form(self.model.clear_form())
        self._render_rules()
        if not has_cifp:
            self._info("运行规则", "%s 没有 CIFP 程序数据，跑道 / SID / STAR / IAP 候选为空。\n"
                                  "仍可为它建规则，只是选不出具体跑道与程序。" % icao)
        self.page.update()

    # ---------- 规则列表 ----------
    def _render_rules(self):
        rows = self.model.rows()
        sel = self.model.sel
        self.rlv.controls = [self._rule_row(i, v, i == sel) for i, v in enumerate(rows)]

    def _rule_row(self, idx, vals, selected):
        name, tm, days, wind, wx, dep, arr = vals
        sub = "%s · %s · 风 %s · 天气 %s · 离 %s / 进 %s" % (tm, days, wind, wx, dep, arr)
        return ft.Container(
            content=ft.Column([
                ft.Text(name, size=13, weight=ft.FontWeight.BOLD, no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(sub, size=11, color=ft.Colors.GREY, no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS, tooltip=sub),
            ], spacing=1),
            # 右边留白给默认拖拽手柄（它盖在每项的尾边上），否则文字会被压在手柄底下
            padding=ft.Padding.only(left=10, right=36, top=6, bottom=6),
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.14, ft.Colors.INDIGO) if selected else None,
            on_click=lambda e, i=idx: self._on_row_click(i))

    def _on_row_click(self, idx):
        if self._syncing or not self.model:
            return
        cur = self.model.sel
        if cur is not None and cur != idx:
            self.model.commit_form(self._read_form())      # 静默提交（校验失败就放弃，不打扰用户）
        fd = self.model.select_rule(idx)
        self._write_form(fd)
        self._render_rules()
        self.page.update()

    def _on_reorder(self, e):
        # Flet 已归一化 new_index（见文件头注），直接 pop/insert；但它不重排 controls → 按 Model 重建行
        if self.model.move_rule(e.old_index, e.new_index) is not None:
            self._render_rules()
            self.page.update()

    # ---------- 增 / 改 / 删 / 复制 ----------
    def _add_rule(self):
        try:
            fd = self.model.add_rule()
        except VM.ValidationError as ex:
            self._info("运行规则", str(ex))
            return
        self._write_form(fd)
        self._render_rules()
        self.page.update()

    def _dup_rule(self):
        try:
            fd = self.model.dup_rule(self._read_form())
        except VM.ValidationError as ex:
            self._info("运行规则", str(ex))
            return
        self._write_form(fd)
        self._render_rules()
        self.page.update()

    def _apply_rule(self):
        try:
            self.model.apply_rule(self._read_form())
        except VM.ValidationError as ex:
            self._info("规则不合法", str(ex))
            return
        self._render_rules()
        self.page.update()

    def _delete_rule(self):
        self._write_form(self.model.delete_rule())
        self._render_rules()
        self.page.update()

    # ---------- 保存 / 关闭 ----------
    def _save(self):
        try:
            n = self.model.save(self._read_form())
        except VM.ValidationError as ex:
            self._info("规则不合法", str(ex))
            return
        if n is None:
            self._info("保存失败", "写入 operation.json 失败（目录只读或磁盘满？）。")
            return
        self._refresh_existing()
        self._render_rules()
        self.page.update()
        self._info("已保存", "运行规则已保存到 operation.json（%d 个机场）。" % n)

    def _ask_delete(self):
        if self.model.sel is None:
            self._info("运行规则", "请先在左侧选中一条规则。")
            return
        name = (self.model.rules[self.model.sel].get("name") or "该规则")
        self._confirm("删除规则", "确定删除「%s」？" % name,
                      [("删除", self._delete_rule), ("取消", None)])

    def ask_close(self):
        """未保存守卫（返回键 / 系统返回手势都走这里）。
        tk 用阻塞式 askyesnocancel 拿返回值；Flet 的对话框不阻塞 → 改成回调式三选一。"""
        if not (self.model and self.model.has_unsaved):
            self._on_close()
            return
        self._confirm("未保存的改动", "有未保存的规则改动。",
                      [("保存并关闭", self._save_and_close),
                       ("直接关闭", self._on_close),
                       ("取消", None)])

    def _save_and_close(self):
        try:
            n = self.model.save(self._read_form())
        except VM.ValidationError as ex:
            self._info("规则不合法", str(ex))          # 存不下就别关，免得改动白丢
            return
        if n is None:
            self._info("保存失败", "写入 operation.json 失败，未关闭。")
            return
        self._on_close()

    # ---------- 对话框 ----------
    def _info(self, title, message):
        dlg = ft.AlertDialog(title=ft.Text(title, size=15), content=ft.Text(message))
        dlg.actions = [ft.TextButton("确定", on_click=lambda e: self.page.pop_dialog())]
        self.page.show_dialog(dlg)

    def _confirm(self, title, message, choices):
        """choices=[(按钮文字, 回调|None)]；点任一按钮都先关弹窗，再跑回调。"""
        def _mk(cb):
            def _h(_e):
                self.page.pop_dialog()
                if cb:
                    cb()
            return _h
        dlg = ft.AlertDialog(modal=True, title=ft.Text(title, size=15), content=ft.Text(message))
        dlg.actions = [ft.TextButton(t, on_click=_mk(cb)) for t, cb in choices]
        self.page.show_dialog(dlg)
