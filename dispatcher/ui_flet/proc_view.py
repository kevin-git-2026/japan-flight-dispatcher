# ================= 进离场面板（F20 / F21 / F24）=================
# 结果卡下方：AIP 航路选择 + 天气 + 跑道 + SID/STAR + EOBT + 「🎯 运行规则」预选。
#
# 整套状态机在 viewmodel.ProcPanelModel（跑道排序、风分量、天气块、运行规则匹配、
# SimBrief route 拼装…全在那儿，且已 headless 单测过）。本文件只做三件事：
#   ① 把 model 的 items/labels 画成下拉；② 把用户点选喂回 model.select_*；③ 把 model 的
#   ops_label/hint/summary 画成文字。
#
# ⚠️ 重入守卫 `_syncing`：Flet 的 on_change 对【程序化赋值】同样会触发（同 tk 的 trace），
#    不挡住就会「model→控件→on_change→model」无限套娃。

import flet as ft

from .theme import MONO, panel


class ProcView:
    def __init__(self, on_open_url, on_preview_route, on_open_aip_dialog):
        self.model = None
        self._syncing = False
        self._on_open_url = on_open_url
        self._on_preview = on_preview_route
        self._on_aip_dialog = on_open_aip_dialog

        # ---- EOBT + 运行规则开关 ----
        self.eobt = ft.TextField(label="EOBT (JST)", width=120, dense=True, text_size=13,
                                 content_padding=10, label_style=ft.TextStyle(size=12),
                                 on_change=lambda e: self._on_eobt())
        self.eobt_z = ft.Text("", size=11, color=ft.Colors.GREY)     # SimBrief 用的是 UTC(Zulu)
        self.apply_ops = ft.Checkbox(  # 标签保持短——Checkbox 标签不换行，长文案会把整行宽度顶开
            label="按机场运行规则预选", value=True, label_style=ft.TextStyle(size=12),
            tooltip="按 operation.json 的机场运行规则（时段+星期+风+天气）预选跑道与 SID/STAR/IAP；可随时手动改选",
            on_change=lambda e: self._on_apply_ops())

        # ---- AIP 航路（仅多条时显示）----
        self.aip = ft.Dropdown(label="AIP 航路", dense=True, text_size=12, expand=True,
                               label_style=ft.TextStyle(size=12), options=[],
                               on_select=lambda e: self._on_aip_select())
        self.btn_aip = ft.OutlinedButton("确认航路 (EOBT/机型/高度)…",
                                         on_click=lambda e: self._on_aip_dialog())
        self.aip_row = ft.Row([self.aip, self.btn_aip], spacing=8, visible=False)

        # ---- 两侧：天气 / 跑道 / 程序 / 运行规则标注 ----
        self.dep_wx = self._wx_text()
        self.arr_wx = self._wx_text()
        self.dep_rwy, self.dep_proc = self._rwy_row("dep")
        self.arr_rwy, self.arr_proc = self._rwy_row("arr")
        # 实测运用状况（国土交通省 ntrack，仅羽田）——预选的首选依据，故用更醒目的靛蓝
        self.dep_nt = ft.Text("", size=12, color=ft.Colors.INDIGO_400, selectable=True)
        self.arr_nt = ft.Text("", size=12, color=ft.Colors.INDIGO_400, selectable=True)
        self.dep_ops = ft.Text("", size=12, color=ft.Colors.GREEN_600, selectable=True)
        self.arr_ops = ft.Text("", size=12, color=ft.Colors.GREEN_600, selectable=True)

        self.hint = ft.Text("", size=11, color=ft.Colors.GREY)
        self.summary = ft.Text("", size=12, selectable=True)
        self.lnk_preview = ft.Text(
            spans=[ft.TextSpan("🗺️ 预览完整航路（SID + enroute + STAR）",
                               style=ft.TextStyle(size=12, color=ft.Colors.GREEN_600,
                                                  decoration=ft.TextDecoration.UNDERLINE),
                               on_click=lambda e: self._on_preview())])
        self.lnk_sb = ft.Text(
            spans=[ft.TextSpan("🛩️ 按所选程序派遣 SimBrief（需登录）",
                               style=ft.TextStyle(size=12, color=ft.Colors.BLUE_700,
                                                  decoration=ft.TextDecoration.UNDERLINE),
                               on_click=lambda e: self._open_sb())])

        # 三段式：头部（EOBT / 运行规则开关 / AIP 航路）与底栏（摘要 + 两个操作链接）【固定】，
        # 中间的出发/到达两块【内部可滚动】。
        # 为什么中间要能滚：各人显示器分辨率不同，靠「缩字号 + 调高度比例」硬塞进窗口的做法换台机器就崩；
        # 能滚就永远不会有内容被吃掉，字号也不必迁就。
        # 为什么头尾不进滚动区：它们是常驻控件与动作入口，不该被内容顶出视野；
        # 且 Material 的浮动标签会浮到控件上沿【之外】，放进滚动区会被视口顶边裁掉。
        # 用 ListView 而不是 Column(scroll=AUTO)：后者在内容更新后会把滚动位置跳到底部
        # （实测规划完直接停在「到达」那侧，出发块被顶出视野）；ListView 老实停在顶部。
        body = ft.ListView([
            self.dep_wx,
            self.dep_nt,
            ft.Row([self.dep_rwy, self.dep_proc], spacing=8),
            self.dep_ops,
            ft.Divider(height=8),
            self.arr_wx,
            self.arr_nt,
            ft.Row([self.arr_rwy, self.arr_proc], spacing=8),
            self.arr_ops,
            self.hint,
        ], spacing=6, expand=True, auto_scroll=False)

        self.control = panel(visible=False, expand=5,
            padding=ft.Padding.only(left=12, right=12, top=14, bottom=10),
            content=ft.Column([
                ft.Text("跑道 / SID·STAR（按航路端点预筛 · 天气辅助选跑道）",
                        size=11, color=ft.Colors.GREY),
                # wrap=True：窗口窄时换行，而不是把整个面板的宽度顶出右栏（Row 默认不换行）
                ft.Row([self.eobt, self.eobt_z, self.apply_ops], spacing=8, wrap=True,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                self.aip_row,
                ft.Divider(height=8),
                body,
                ft.Divider(height=6),
                self.summary,
                ft.Row([self.lnk_preview, self.lnk_sb], spacing=20, wrap=True),
            ], spacing=6, expand=True))

    # ---------- 控件工厂 ----------
    def _wx_text(self):
        # METAR 原文用等宽 → 天气块该用等宽的地方才用。
        # 字号不必为了「塞进窗口」而缩小：面板内部可滚动，任何分辨率都不会吃掉内容。
        return ft.Text("", size=11, font_family=MONO, color=ft.Colors.GREY, selectable=True)

    def _rwy_row(self, side):
        rwy = ft.Dropdown(label="出发跑道" if side == "dep" else "到达跑道",
                          dense=True, text_size=12, expand=3, options=[],
                          label_style=ft.TextStyle(size=12),
                          on_select=lambda e, s=side: self._on_rwy(s))
        # SID/STAR 可搜索（editable+enable_filter 是 Flet 原生的，不用像 tk 那样手写 KeyRelease 过滤）
        proc = ft.Dropdown(label="SID" if side == "dep" else "STAR",
                           dense=True, text_size=12, expand=2, options=[],
                           editable=True, enable_filter=True,
                           label_style=ft.TextStyle(size=12),
                           on_select=lambda e, s=side: self._on_proc(s))
        return rwy, proc

    def _open_sb(self):
        if self.model:
            self._on_open_url(self.model.simbrief_url())

    # ---------- 事件（读控件 → 喂 model → 回写控件）----------
    def _on_eobt(self):
        if self._syncing or not self.model:
            return
        self.model.set_eobt(self.eobt.value)
        # 开关开着时 set_eobt 会按新时段重选跑道/程序 → 得全量回写
        self.sync() if self.model.apply_ops else self.sync_derived()

    def _on_apply_ops(self):
        if self._syncing or not self.model:
            return
        self.model.set_apply_ops(bool(self.apply_ops.value))
        self.sync()

    def _on_aip_select(self):
        if self._syncing or not self.model or self.aip.value is None:
            return
        self.select_candidate(int(self.aip.value))

    def _on_rwy(self, side):
        if self._syncing or not self.model:
            return
        combo = self.dep_rwy if side == "dep" else self.arr_rwy
        self.model.select_runway(side, combo.value)          # option key = rwy_id（"RW34L"）
        self.sync()

    def _on_proc(self, side):
        if self._syncing or not self.model:
            return
        combo = self.dep_proc if side == "dep" else self.arr_proc
        self.model.select_proc(side, combo.value or "")
        self.sync_derived()

    # ---------- 对外 ----------
    def select_candidate(self, idx, eobt=None):
        """选定第 idx 条 AIP 候选（AIP 下拉 / F21 弹窗共用）。
        eobt 非空＝把 F21 弹窗里填的 EOBT【继承到面板】（先设 EOBT 再选，运行规则预选才按新时段算）。"""
        if not self.model:
            return
        if eobt is not None:
            self.eobt.value = eobt
            self.model.set_eobt(eobt)
        self.model.select_candidate(idx)
        self.sync()

    def show(self, model):
        self.model = model
        self.control.visible = bool(model and model.visible)
        if not self.control.visible:
            return
        self._syncing = True
        try:
            self.eobt.value = model.eobt
            self.apply_ops.value = model.apply_ops
        finally:
            self._syncing = False
        self.sync()

    def hide(self):
        self.model = None
        self.control.visible = False

    # ---------- Model → 控件 ----------
    def sync(self):
        """全量回写（跑道/程序候选也重建）。"""
        m = self.model
        if not m:
            return
        self._syncing = True
        try:
            self.aip_row.visible = m.show_aip_row
            if m.show_aip_row:
                # option key 用【下标】而非显示串 —— 干掉 tk 那套「显示串→item」字典 hack
                self.aip.options = [ft.DropdownOption(key=str(i), text=lbl)
                                    for i, lbl in enumerate(m.aip_labels())]
                self.aip.value = str(m.sel_idx)

            for side, wx, nt, cb_rwy, cb_proc, ops in (
                    ("dep", self.dep_wx, self.dep_nt, self.dep_rwy, self.dep_proc, self.dep_ops),
                    ("arr", self.arr_wx, self.arr_nt, self.arr_rwy, self.arr_proc, self.arr_ops)):
                wx.value = m.wx_text(side)
                nt.value = m.nt_label[side]
                nt.visible = bool(nt.value)
                items = m.items[side]
                cb_rwy.options = [ft.DropdownOption(key=it["rwy"], text=it["disp"]) for it in items]
                cb_rwy.value = m.sel_rwy[side]
                cb_rwy.hint_text = None if items else "（无可选程序）"
                labels = m.proc_labels[side]
                cb_proc.options = [ft.DropdownOption(key=p, text=p) for p in labels]
                cb_proc.value = m.sel_proc[side] or None
                # ⚠️ 可搜索下拉（editable=True）的 `text` 是【独立于 value 的字段】——它存的是「输入框里显示的文字」。
                #    控件是 __init__ 建一次、跨多次规划复用的，只写 value 不写 text，上一次规划的 SID/STAR 名会一直
                #    挂在框里：实测规划到 RJFE（该场【根本没有 STAR】）时，STAR 框里赫然显示着上一条航线的 REMENW，
                #    而 model 里 sel_proc 是空的（摘要行显示「/ —」）——纯粹的显示残留。故必须一并回写。
                cb_proc.text = m.sel_proc[side] or None
                cb_proc.hint_text = None if labels else ("（该机场无 SID）" if side == "dep"
                                                         else "（该机场无 STAR）")
                ops.value = m.ops_label[side]
                ops.visible = bool(m.ops_label[side])
            self.hint.value = m.hint_text()
            self.hint.visible = bool(self.hint.value)
        finally:
            self._syncing = False
        self.sync_derived()

    def sync_derived(self):
        """只刷派生（摘要 / Zulu 提示）—— 不动下拉候选，免踩掉正在输入的搜索过滤。"""
        m = self.model
        if not m:
            return
        self._syncing = True
        try:
            self.summary.value = m.summary_text()
            self.eobt_z.value = m.eobt_zulu_text()
        finally:
            self._syncing = False
