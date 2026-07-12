# ================= Flet 应用外壳 =================
# 组装各视图 + 把 controller 的后台任务接到 Shell 的 marshal 点上。
# 这里【不做任何业务判断】：算什么、怎么算，全在 controller / viewmodel。

import os
import sys
import threading
import webbrowser

import flet as ft

from .. import __version__
from .. import controller as C
from .. import viewmodel as VM
from .aip_dialog import AipDialog
from .form_view import FormView
from .log_view import LogView
from .map_view import MapView
from .ops_view import OpsView
from .proc_view import ProcView
from .result_view import ResultView
from .shell import Shell
from .theme import apply_theme


class DispatcherApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.shell = Shell(page)

        # 运行状态
        self._ready = False
        self._busy = False
        self._vsyncing = False
        self._cancel_evt = threading.Event()
        self.state = C.AppState()
        self.ac = VM.AircraftModel()

        apply_theme(page)
        page.title = f"✈️ 日本航班智能搜索与规划  v{__version__}"
        page.window.width = 1240
        page.window.height = 920
        page.window.min_width = 940
        page.window.min_height = 680
        page.padding = 12
        page.on_close = self._on_close

        self._build()

        # 先装好 stdout 重定向，再跑任何复用函数（--noconsole 下 stdout=None，否则 print 会崩）
        self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = C.LogSink(self.shell.log_emit)

        self._set_status("正在初始化…")
        print(f"✈️ 日本航班智能搜索与规划 v{__version__} — 正在初始化…")
        self.shell.run_bg(self._init_worker)

    # ---------- 布局 ----------
    def _build(self):
        self.status = ft.Text("启动中…", size=12, color=ft.Colors.GREY)
        header = ft.Row([
            ft.Text("✈️ 日本航班智能搜索与规划", size=18, weight=ft.FontWeight.BOLD),
            self.status,
        ], spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self.form = FormView(self.ac, on_plan=self._on_plan_click,
                             on_ops_editor=self._on_ops_editor,
                             on_volanta=self._on_volanta_click,
                             on_auto_toggle=self._on_auto_toggle)
        self.result = ResultView(on_open_url=self._open_url, on_open_map=self._open_map)
        self.proc = ProcView(on_open_url=self._open_url,
                             on_preview_route=self._preview_full_route,
                             on_open_aip_dialog=self._open_aip_dialog)
        self.log = LogView()
        self.shell.bind_log(self.log)
        self.map_view = MapView(self.page, on_close=self._pop_view)
        self.ops_view = OpsView(self.page, on_close=self._pop_view)
        self.aip_dialog = AipDialog(self.page, on_select=self.proc.select_candidate)
        self._plan_maps = []

        # 右栏：结果卡 + 进离场面板（规划后才显示，贴在结果卡下方，同 tk 版结构）。
        # · 两者各占一份高度、【各自内部滚动】——外层不能设 scroll，否则 expand 子控件高度无界。
        # · horizontal_alignment=STRETCH 必须给：Column 默认 START，子控件会取【自身内容宽度】
        #   而不是拉满，于是宽内容（如面板那排下拉）会把卡片顶出右栏、横向溢出窗口。
        right = ft.Column([self.result.control, self.proc.control], expand=True, spacing=8,
                          horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self.page.add(
            ft.Column([
                header,
                ft.Row([self.form.control, right],
                       expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH, spacing=10),
                self.log.control,
            ], expand=True, spacing=10))
        self.page.on_view_pop = self._on_view_pop               # 系统返回键 / 手势
        self._refresh()

    # ---------- 状态 / 通用 ----------
    def _set_status(self, text):
        self.status.value = text

    def _refresh(self):
        self.form.apply_enabled(VM.enabled_controls(
            self._ready, self._busy, self._vsyncing,
            bool(self.state.dat_path), self.state.scenery_map is not None))

    def _open_url(self, url):
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    def _dialog(self, title, message):
        """信息弹窗（Flet 的对话框不阻塞，只作告知用）。"""
        dlg = ft.AlertDialog(title=ft.Text(title), content=ft.Text(message))
        dlg.actions = [ft.TextButton("确定", on_click=lambda e: self.page.pop_dialog())]
        self.page.show_dialog(dlg)

    # ---------- 初始化 ----------
    def _init_worker(self):
        try:
            st = C.init_app()
        except C.NavDataMissing:
            self.shell.post(self._on_navdata_missing)
            return
        except Exception as e:                          # noqa: BLE001
            print(f"❌ 初始化出错: {e}")
            self.shell.post(self._set_status, f"❌ 初始化出错: {e}")
            return
        self.shell.post(self._on_init_done, st)

    def _on_init_done(self, st):
        self.state = st
        self._ready = True
        self.form.chk_auto.value = st.volanta_auto
        self._set_status(VM.init_status_text(st.scenery_map, st.aip_data))
        if st.scenery_map is None:
            self.form.scenery_only.value = False
            self.form.scenery_hint.value = "未检测到地景目录，无法按地景筛选"
        else:
            self.form.scenery_hint.value = ""
        self.form.vstatus.value = VM.volanta_status_text(st.flown_counts, st.volanta_meta)
        self._refresh()
        self._selftest()

    def _selftest(self):
        """真客户端自测钩子（DISPATCHER_SELFTEST="RJTT>RJOO"）：初始化完就自动跑一次规划。
        用来抓「控件构造得出、但 Flutter 端渲染抛错」这类 headless 冒烟看不见的问题。"""
        ops = os.environ.get("DISPATCHER_SELFTEST_OPS")          # =ICAO：直接开运行规则编辑器
        if ops:
            print("🧪 SELFTEST: 打开运行规则编辑器 → %s" % ops)
            self._on_ops_editor()
            self.ops_view.cb_icao.text = ops.strip().upper()
            self.ops_view._load_airport()
            if self.ops_view.model.rules:
                self.ops_view._on_row_click(0)
            return

        spec = os.environ.get("DISPATCHER_SELFTEST")
        if not spec or ">" not in spec:
            return
        dep, dest = spec.split(">", 1)
        self.form.dep.value = dep.strip().upper()
        self.form.dest.value = dest.strip().upper()
        self.form.strict_ops.value = bool(os.environ.get("DISPATCHER_SELFTEST_STRICT"))
        print("🧪 SELFTEST: 自动规划 %s → %s%s" % (
            self.form.dep.value, self.form.dest.value,
            "（严格遵循现实运行规则）" if self.form.strict_ops.value else ""))
        self.page.update()
        self._on_plan_click()

    def _on_navdata_missing(self):
        self._set_status("❌ 未找到导航数据")
        print(C.NAVDATA_LOG)
        self._dialog("缺少导航数据", C.NAVDATA_HELP)

    # ---------- 规划 ----------
    def _on_plan_click(self):
        if self._busy or self._vsyncing or not self._ready:
            return
        fields = self.form.values(self.state.scenery_map is not None)
        self._busy = True
        self._refresh()
        self.result.clear()
        self.proc.hide()                                 # 隐藏上一轮的面板
        print("\n" + "🛫 开 始 新 的 航 班 规 划 🛬".center(49, "-"))
        self.shell.run_bg(self._plan_worker, fields)

    def _plan_worker(self, f):
        try:
            plan, proc = C.plan(self.state, f)
            self.shell.post(self._render_plan, plan, proc)
        except Exception as e:                          # noqa: BLE001
            print(f"❌ 发生错误: {e}")
            self.shell.post(self.result.render, VM.error_spans(str(e)))
        finally:
            self.shell.post(self._finish_plan)

    def _finish_plan(self):
        self._busy = False
        self._refresh()

    def _render_plan(self, plan, proc=None):
        self.result.render(VM.result_spans(plan, has_map=True))
        self._plan_maps = VM.plan_maps(plan)             # 本次规划的全部航路（地图标签页用）
        self._populate_proc(plan, proc)
        if os.environ.get("DISPATCHER_SELFTEST_MAP") and self._plan_maps:
            print("🧪 SELFTEST: 自动打开地图（%d 条航路）" % len(self._plan_maps))
            self._open_map(*self._plan_maps[0])
            off = os.environ.get("DISPATCHER_SELFTEST_MAP_OFF")   # "wp" / "leg" / "both"：验标注开关
            if off:
                print("🧪 SELFTEST: 关闭标注 → %s" % off)
                self.map_view.set_labels(wp=off not in ("wp", "both"),
                                         leg=off not in ("leg", "both"))

    # ---------- 进离场面板（F20/F21/F24）----------
    def _populate_proc(self, plan, proc):
        if not proc or not proc.get("aip_candidates"):
            self.proc.hide()
            return
        self.proc.show(VM.ProcPanelModel(plan, proc, self.state.dat_path))
        if self.proc.model.show_aip_row:                  # 多条 AIP → 自动弹窗让用户确认一条
            self._open_aip_dialog()

    def _preview_full_route(self):
        """把当前选定的 SID + enroute + STAR 全段画到地图。"""
        if not self.proc.model:
            return
        coords, title = self.proc.model.preview_coords()
        if not coords:
            print(title)                                  # 失败原因（viewmodel 已组好提示串）
            return
        self._open_map(coords, title)

    def _open_aip_dialog(self):
        """F21：多条 AIP 航路 → 弹窗定一条（规划后自动弹；也可点面板的「确认航路…」重开）。"""
        m = self.proc.model
        if not m or not m.show_aip_row:
            return
        tm = VM.AipTableModel(m.candidates, m.sel_idx, m.strict_ops)
        tm.eobt = m.eobt                                 # 用面板的 EOBT（默认当前 JST）预填，省得重敲
        self.aip_dialog.open(tm, m.dep_icao, m.arr_icao)

    # ---------- 地图 ----------
    def _open_map(self, coords, title=""):
        """点某条航路的 🗺️ → 推入地图视图。Flet 无多窗口，故把本次规划的【所有】航路做成标签页，
        默认选中点的那条 —— 用切标签页代替 tk 版的「并排开多个窗口」。"""
        routes = list(getattr(self, "_plan_maps", None) or [])
        idx = next((i for i, (c, t) in enumerate(routes) if c is coords), None)
        if idx is None:                                  # 临时航路（如进离场面板的全段预览）→ 单独一页
            routes = [(coords, title)]
            idx = 0
        view = self.map_view.build(routes, idx)
        if view is None:
            return
        self.page.views.append(view)
        self.page.update()

    # ---------- 视图栈（Flet 无多窗口 → 地图 / 规则编辑器都是 pushed View）----------
    def _pop_view(self):
        if len(self.page.views) > 1:
            self.page.views.pop()
            self.page.update()

    def _on_view_pop(self, _e=None):
        """系统返回键 / 手势。栈顶若是规则编辑器，得先走它的未保存守卫，不能直接弹掉。"""
        top = self.page.views[-1] if len(self.page.views) > 1 else None
        if top is not None and top is getattr(self.ops_view, "view", None):
            self.ops_view.ask_close()
            return
        self._pop_view()

    # ---------- 运行规则编辑器（F23）----------
    def _on_ops_editor(self):
        if not self.state.dat_path:
            self._dialog("运行规则", "导航数据未就绪，暂时无法编辑（需 CIFP 程序数据）。")
            return
        self.page.views.append(self.ops_view.build(self.state.dat_path))
        self.page.update()

    # ---------- Volanta ----------
    def _on_auto_toggle(self, on):
        try:
            print(C.set_auto_sync(on))
        except Exception as e:                          # noqa: BLE001
            print(f"⚠️ 写入 Volanta 偏好失败: {e}")

    def _on_volanta_click(self):
        if self._vsyncing:
            self._cancel_evt.set()                      # 同步中 → 取消
            self.form.vstatus.value = "Volanta：正在取消…"
            self.page.update()
            return
        if self._busy or not self._ready:
            return
        self._cancel_evt = threading.Event()
        self._vsyncing = True
        self._refresh()
        self.form.vstatus.value = "Volanta：正在同步…"
        self.page.update()
        self.shell.run_bg(self._volanta_worker)

    def _volanta_worker(self):
        res = C.volanta_sync(self._cancel_evt,
                             lambda t: self.shell.post(self._set_vstatus, t),
                             lambda kind: self.shell.post(self._volanta_popup, kind))
        if res == "synced":
            self.shell.post(self._volanta_synced)
        elif res == "cancelled":
            self.shell.post(self._set_vstatus, "Volanta：已取消同步。")
        elif res == "timeout":
            self.shell.post(self._set_vstatus, "Volanta：等待超时，未更新。")
        else:
            self.shell.post(self._set_vstatus, "Volanta：同步出错（%s）" % res[6:])
        self.shell.post(self._finish_volanta)

    def _set_vstatus(self, text):
        self.form.vstatus.value = text

    def _volanta_popup(self, kind):
        self._dialog("Volanta 同步" if kind == "wait" else "Volanta 同步 · 仍在等待",
                     C.POPUP_WAIT if kind == "wait" else C.POPUP_SLOW)

    def _volanta_synced(self):
        flown, vmeta = C.reload_volanta(self.state)
        self._set_vstatus(VM.volanta_status_text(flown, vmeta))
        print(f"✅ Volanta 同步完成：{len(flown)} 条有向航线。")
        self._dialog("Volanta 同步", f"✅ 同步完成：已读取 {len(flown)} 条有向航线。")

    def _finish_volanta(self):
        self._vsyncing = False
        self._refresh()

    # ---------- 关闭 ----------
    def _on_close(self, _e=None):
        self._cancel_evt.set()
        try:
            sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
        except Exception:
            pass


def run_flet():
    """Flet 入口：由 flight_dispatcher.py 调用。"""
    ft.run(DispatcherApp)
