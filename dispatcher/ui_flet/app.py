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
from .. import swim_atis
from .. import viewmodel as VM
from ..live import LiveService
from .aip_dialog import AipDialog
from .form_view import FormView
from . import map_window
from .live_view import LiveView
from .log_view import LogView
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
        self.proc = ProcView(on_regen=self._regen_route, on_open_url=self._open_url,
                             on_preview_route=self._preview_full_route,
                             on_open_aip_dialog=self._open_aip_dialog)
        self.log = LogView()
        self.shell.bind_log(self.log)
        self.ops_view = OpsView(self.page, on_close=self._pop_view)
        self.aip_dialog = AipDialog(self.page, on_select=self.proc.select_candidate)
        self._plan_maps = []
        self._plan = None                                # 当前结果卡对应的 FlightPlan（改 EOBT 重新生成航路时要更新它）
        self._regen_seq = 0                              # 重新生成的请求序号：EOBT 逐字输入时只认最后一次
        self.live_svc = None
        self.live = LiveView(can_update=swim_atis.SWIM_AIRPORTS, on_add=self._live_add,
                             on_update=self._live_update, on_remove=self._live_remove,
                             on_fenix=self._live_fenix, on_swim=self._live_swim,
                             on_save_login=self._live_save_login, on_clear_login=self._live_clear_login)
        self.tab_result = ft.TextButton("规划结果", on_click=lambda e: self._show_tab("result"))
        self.tab_live = ft.TextButton("实时天气与 ATIS", on_click=lambda e: self._show_tab("live"))
        self._show_tab("result")

        # 右栏：结果卡 / 实时标签页 + 进离场面板（规划后才显示，贴在结果卡下方，同 tk 版结构）。
        # · 两者各占一份高度、【各自内部滚动】——外层不能设 scroll，否则 expand 子控件高度无界。
        # · horizontal_alignment=STRETCH 必须给：Column 默认 START，子控件会取【自身内容宽度】
        #   而不是拉满，于是宽内容（如面板那排下拉）会把卡片顶出右栏、横向溢出窗口。
        right = ft.Column([ft.Row([self.tab_result, self.tab_live], spacing=4),
                           self.result.control, self.live.control, self.proc.control],
                          expand=True, spacing=8,
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

    def _show_tab(self, name):
        on = ft.ButtonStyle(bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.PRIMARY))
        self.tab_result.style = on if name == "result" else None
        self.tab_live.style = on if name == "live" else None
        self.result.control.visible = name == "result"
        self.live.control.visible = name == "live"

    # ---------- 实时天气与 ATIS ----------
    def _start_live(self):
        """启动实时服务（F33）。失败不影响规划功能。"""
        try:
            self.live_svc = LiveService(self.state.dat_path,
                                        lambda icao: self.shell.post(self._on_live_change, icao))
            errors = self.live_svc.start()
        except Exception as e:                          # noqa: BLE001
            print(f"⚠️ 实时天气启动失败: {e}")
            self.live_svc = None
            return
        self.live.fenix.value = self.live_svc.fenix_on
        self.live.swim.value = self.live_svc.swim_on
        self.live.set_note("；".join(errors))
        self.live.set_airports(self.live_svc.watch)
        self._live_refresh_all()

    def _live_refresh_all(self):
        querying = self.live_svc.querying()
        for icao in self.live_svc.watch:
            self.live.show_section(icao, VM.live_section(
                self.live_svc.snapshot(icao) or {"icao": icao},
                querying and icao in swim_atis.SWIM_AIRPORTS))
        self._live_status()

    def _live_status(self):
        status = swim_atis.store.status()
        self.live.status.value = VM.live_status_text(
            self.live_svc.fenix_on, self.live_svc.swim_on, self.live_svc.has_credentials(), status)
        self.live.show_login(self.live_svc.has_credentials(), status in ("bad_login", "no_creds"))

    def _on_live_change(self, icao):
        """后台线程经 Shell.post 回到这里。icao=None → SWIM 状态或查询结果变了，全部刷新。"""
        if not self.live_svc:
            return
        self._live_refresh_all()
        snap = self.live_svc.snapshot(icao) if icao else None
        if snap and self.proc.model and self.proc.model.update_wx(icao, snap.get("wx")):
            self.proc.sync_wx()

    def _live_update(self, _icao):
        """点「更新 ATIS」：查一次 SWIM（一个请求带全部 23 场）。结果回来时走 _on_live_change。"""
        if not self.live_svc:
            return
        if not self.live_svc.can_query_swim():
            self.live.set_note(VM.live_blocked_text(self.live_svc.swim_on, self.live_svc.has_credentials()))
            return
        self.live.set_note("")
        self.live_svc.start_query()
        self._live_refresh_all()

    def _live_save_login(self, user, password):
        if self.live_svc:
            self.live.set_note(self.live_svc.save_credentials(user, password))
            self._live_status()

    def _live_clear_login(self):
        if self.live_svc:
            self.live.set_note(self.live_svc.clear_credentials())
            self._live_status()

    def _live_add(self, icao):
        if not self.live_svc or not self.live_svc.known(icao):
            self.live.set_note("%s 不是导航数据里的日本机场" % icao if icao else "")
            return False
        self.live.set_note("")
        self.live_svc.add(icao)
        self.live.set_airports(self.live_svc.watch)
        self._live_refresh_all()
        return True

    def _live_remove(self, icao):
        if self.live_svc:
            self.live_svc.remove(icao)
            self.live.set_airports(self.live_svc.watch)

    def _live_fenix(self, on):
        if self.live_svc:
            self.live.set_note(self.live_svc.set_fenix(on))
            self.live.fenix.value = self.live_svc.fenix_on
            self._live_status()

    def _live_swim(self, on):
        if self.live_svc:
            self.live_svc.set_swim(on)
            self._live_status()

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
        if st.navdata_notice:                            # 本机导航数据比 VATJPN 最新移管表旧
            self._dialog(C.NAVDATA_STALE_TITLE, st.navdata_notice)
        self._start_live()
        self._selftest()

    def _selftest(self):
        """真客户端自测钩子（DISPATCHER_SELFTEST="RJTT>RJOO"）：初始化完就自动跑一次规划。
        用来抓「控件构造得出、但 Flutter 端渲染抛错」这类 headless 冒烟看不见的问题。"""
        if os.environ.get("DISPATCHER_SELFTEST_LIVE"):           # 打开实时标签页
            self._show_tab("live")
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
            # 规划也算一次用户发起的 SWIM 查询（30 分钟内查过就不查）：实测是选运行规则、定航路端点的首选依据
            refresh = self.live_svc.ensure_fresh if self.live_svc else None
            plan, proc = C.plan(self.state, f, refresh_measured=refresh)
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
        self._plan = plan
        self._regen_seq += 1                             # 新的一次规划：作废还没回来的重新生成
        self.result.render(VM.result_spans(plan, has_map=True))
        self._plan_maps = VM.plan_maps(plan)             # 本次规划的全部航路（地图标签页用）
        self._populate_proc(plan, proc)
        self._show_tab("result")
        if self.live_svc and self.live_svc.add(plan.dep.code, plan.arr.code):
            self.live.set_airports(self.live_svc.watch)
            self._live_refresh_all()
        if os.environ.get("DISPATCHER_SELFTEST_MAP") and self._plan_maps:
            # 地图子进程继承环境变量：DISPATCHER_SELFTEST_MAP_OFF（验标注开关）/ _MAPTOUR（自动漫游后关窗）在那边生效
            print("🧪 SELFTEST: 自动打开地图（%d 条航路）" % len(self._plan_maps))
            self._open_map(*self._plan_maps[0])

    # ---------- 生成航路按运行规则重新生成（改 EOBT / 运行规则开关）----------
    def _regen_route(self, req):
        """面板改了 EOBT / 开关：生效的运行规则变了 → 后台重新生成航路（kind=gen）；
        AIP 的适用性变了 → 后台重新改端（kind=aip）。worker 只拿快照，不碰 plan 与面板 model。"""
        m, plan = self.proc.model, self._plan
        if not m or not plan or not req:
            return
        self._regen_seq += 1
        seq = self._regen_seq
        aip = {"rows": m.aip_rows, "orig": m.aip_orig, "strict": m.aip_strict, "gc": m.aip_gc,
               "ops": m.ops_data, "eta": m.eta_model, "measured_default": m.eobt_is_default()}
        # 实测全部交过去 + 「EOBT 是否还是默认」：改过 EOBT → 实测只在报文覆盖的时段内算数（离场按 EOBT、到达按 ETA，用户定），
        # 由 controller 按每条航路自己的 ETA 判断
        self.shell.run_bg(self._regen_worker, seq, plan, req,
                          m.dep_wx, m.arr_wx, dict(m.ntrack), aip)

    def _regen_worker(self, seq, plan, req, dep_wx, arr_wx, nt, aip):
        try:
            if req.get("kind") == "aip":
                res = C.regenerate_aip(self.state, plan.dep, plan.arr, aip["rows"], aip["orig"], req["eobt"],
                                       dep_wx, arr_wx, nt, req["apply_ops"], aip["strict"], aip["gc"],
                                       ops_data=aip["ops"], em=aip["eta"],
                                       measured_default=aip["measured_default"])
            else:
                res = C.regenerate_generated(self.state, plan.dep, plan.arr, req["eobt"],
                                             dep_wx, arr_wx, nt, req["apply_ops"], em=aip["eta"],
                                             measured_default=aip["measured_default"])
        except Exception as e:                          # noqa: BLE001
            print(f"⚠️ 重新计算航路失败（已保留原航路）: {e}")
            return
        if res:
            self.shell.post(self._apply_regen, seq, plan, req, res)

    def _apply_regen(self, seq, plan, req, res):
        if seq != self._regen_seq or plan is not self._plan or not self.proc.model:
            return                                       # 过期（又改了 EOBT / 又规划了一次）
        if res.get("kind") == "aip":
            self._apply_aip_regen(plan, req, res)
            return
        gr = res["gr"]
        old = plan.generated_route
        plan.generated_route = gr["route_str"]
        plan.generated_route_dist = gr["dist_nm"]
        plan.generated_route_warn = gr.get("warn") or None
        plan.generated_route_ops = gr.get("ops")
        plan.gen_map, plan.gen_xfer = res["gen_map"], res["gen_xfer"]
        plan.gen_eta = res.get("eta")
        plan.simbrief_url = C.simbrief_url(plan.sb_base, plan.generated_route)
        self.result.render(VM.result_spans(plan, has_map=True))
        self._plan_maps = VM.plan_maps(plan)
        self.proc.replace_generated(res["candidate"])
        if plan.generated_route != old:
            print("🔁 EOBT %s%s：生效的运行规则变了，已重新生成航路 → %s"
                  % (req["eobt"] or "（默认）", "" if req["apply_ops"] else "（已关闭按规则预选）",
                     plan.generated_route))

    def _apply_aip_regen(self, plan, req, res):
        """AIP 改端重算回来（UI 线程）：换结果卡里的改端版、地图、SimBrief 链接与面板候选。"""
        alts = res["alts"]
        old = [(v or {}).get("route") for v in (plan.aip_alts or [])]
        new = [(v or {}).get("route") for v in (alts.get("rows") or [])]
        if not any(old) and not any(new) and not plan.aip_alts and not alts.get("rows") \
                and plan.aip_alt_note == alts.get("note"):
            self.proc.model.aip_key = res["key"]         # 前后都不改端：只记下新判据
            return
        plan.aip_alts, plan.aip_alt_note = alts.get("rows"), alts.get("note")
        plan.aip_etas = res.get("aip_etas") or plan.aip_etas
        plan.simbrief_url = C.simbrief_url(plan.sb_base, res["sb_route"])
        self.result.render(VM.result_spans(plan, has_map=True))
        self._plan_maps = VM.plan_maps(plan)
        self.proc.replace_candidates(res["candidates"], res["key"])
        self.aip_dialog.refresh(res["candidates"])
        tag = "%s%s" % (req["eobt"] or "（默认）", "" if req["apply_ops"] else "（已关闭按规则预选）")
        if any(new):
            print("🔁 EOBT %s：AIP 在此时段 / 运行下不适用，已按运行规则改端 → %s"
                  % (tag, " ｜ ".join(r for r in new if r)))
        elif any(old):
            print("🔁 EOBT %s：AIP 已适用，恢复官方 AIP 航路" % tag)

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
        tm = VM.AipTableModel(m.candidates, m.sel_idx, m.strict_ops, eta_model=m.eta_model)
        tm.eobt = m.eobt                                 # 用面板的 EOBT（默认＝当前 JST + 30 分钟）预填，省得重敲
        self.aip_dialog.open(tm, m.dep_icao, m.arr_icao)

    # ---------- 地图 ----------
    def _open_map(self, coords, title=""):
        """点某条航路的 🗺️ → 在【独立的地图窗口（单独进程）】里打开：本次规划的所有航路做成标签页，
        默认选中点的那条。为什么开新进程而不在主窗口里画：见 map_window.py 文件头（地图的 GPU 缓存关窗才还）。"""
        routes = list(getattr(self, "_plan_maps", None) or [])
        idx = next((i for i, (c, t) in enumerate(routes) if c is coords), None)
        if idx is None:                                  # 临时航路（如进离场面板的全段预览）→ 单独一页
            routes = [(coords, title)]
            idx = 0
        try:
            # 子进程不建航路图（要几秒），哪些点是 VOR / NDB 在这边查好带过去，地图据此画符号
            map_window.launch([(c, t, VM.nav_kinds(c)) for c, t in routes], idx)
            print("🗺️ 正在打开航路地图窗口（%d 条航路）…" % len(routes))
        except Exception as e:                          # noqa: BLE001
            print(f"⚠️ 打开地图窗口失败: {e}")

    # ---------- 视图栈（Flet 无多窗口 → 规则编辑器是 pushed View）----------
    def _pop_view(self):
        if len(self.page.views) > 1:
            self.page.views.pop()
            self.page.update()
            if self.live_svc:                            # 规则编辑器可能改了 operation.json
                self.live_svc.reload_operations()

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
        # 不能在这里同步停：会阻塞事件循环，窗口进程趁机退出 → 连接被重置 →
        # asyncio 关连接时抛异常，Flet 的 wait_closed 永远等不到结束，程序退不出去。
        if self.live_svc:
            threading.Thread(target=self.live_svc.stop, daemon=True).start()
        try:
            sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
        except Exception:
            pass


def run_flet():
    """Flet 入口：由 flight_dispatcher.py 调用。"""
    ft.run(DispatcherApp)
