# ================= 图形界面（tkinter，v1.3.0）=================
# GUI 是一个【薄表现层】：只负责窗体/线程/渲染，所有业务逻辑复用 dispatcher/ 的现有函数，
# 计算统一走 planner.build_flight_plan。→ 将来若换 GUI 框架，只需重写本文件。
#
# 关键设计：
#  - 线程：Tk mainloop 在主线程；所有阻塞/联网工作（初始化、规划+FlightAware、Volanta 轮询）
#    放 daemon 线程，UI 更新一律经 root.after() 回主线程（tkinter 非线程安全）。
#  - stdout 重定向：PyInstaller --windowed 下 sys.stdout/stderr 为 None，而复用函数大量 print()。
#    这里把 stdout/stderr 接到「日志框」，现有 print 自动成为 GUI 状态日志，业务逻辑零改动。
# 纯标准库（tkinter / threading / webbrowser 均标准库）。

import os
import sys
import threading
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from . import __version__
from .config import get_real_run_path
from .navdata import find_navdata_file, check_airac_currency
from .data import load_japan_icao_set, load_airports_from_navigraph, load_aip_routes_from_csv
from .scenery import scan_installed_sceneries
from .airlines import init_airline_data
from .volanta import (
    load_volanta_flown_routes, volanta_auto_enabled, set_volanta_auto,
    try_fetch_volanta_json_via_session, _open_volanta_in_browser,
)
from .routing import calculate_distance_nm, find_aip_route, get_random_route
from .planner import build_flight_plan, parse_runway_ft, parse_dist


class _TkTextWriter:
    """file-like：把 print() 输出汇入 GUI 日志框。后台线程的写入也经 after() 回主线程改控件。"""
    def __init__(self, gui):
        self.gui = gui

    def write(self, s):
        if s:
            try:
                self.gui.root.after(0, self.gui._append_log, s)
            except Exception:
                pass
        return len(s) if s else 0

    def flush(self):
        pass


class DispatcherGUI:
    def __init__(self, root, scale=1.0):
        self.root = root
        self.scale = scale or 1.0
        root.title(f"✈️ 日本航班智能搜索与规划  v{__version__}")
        # 窗口几何按 DPI 缩放比放大（字体由 tk scaling 放大，几何按比例跟上，避免高分屏下窗口偏小）
        root.geometry(f"{int(980 * self.scale)}x{int(680 * self.scale)}")
        root.minsize(int(860 * self.scale), int(600 * self.scale))

        # 运行状态
        self._ready = False          # 初始化是否完成
        self._busy = False           # 初始化/规划进行中（锁表单）
        self._vsyncing = False       # Volanta 同步进行中
        self._cancel_evt = threading.Event()
        self.dat_path = None
        self.scenery_map = None
        self.aip_data = []
        self.aip_index = set()
        self.flown_counts = {}
        self._last_url = None

        self._build_widgets()

        # 先装好 stdout 重定向，再跑任何复用函数（--windowed 下 stdout=None，否则 print 会崩）
        self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _TkTextWriter(self)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_status("正在初始化…")
        print(f"✈️ 日本航班智能搜索与规划 v{__version__} — 正在初始化…")
        # 窗口先画出来，再启动后台初始化
        root.after(50, lambda: self._run_bg(self._init_worker))

    # ---------- 控件构建 ----------
    def _build_widgets(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # 顶部：标题 + 状态
        top = ttk.Frame(root, padding=(10, 8))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="✈️ 日本航班智能搜索与规划", font=("Segoe UI", 13, "bold")).pack(side="left")
        self.var_status = tk.StringVar(value="启动中…")
        ttk.Label(top, textvariable=self.var_status, foreground="#555").pack(side="left", padx=12)

        # 中部：左=输入表单，右=结果
        main = ttk.Frame(root, padding=(10, 0))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        form = ttk.LabelFrame(main, text="规划输入", padding=10)
        form.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        self._build_form(form)

        rightf = ttk.LabelFrame(main, text="规划结果", padding=6)
        rightf.grid(row=0, column=1, sticky="nsew")
        rightf.rowconfigure(0, weight=1)
        rightf.columnconfigure(0, weight=1)
        self.result = scrolledtext.ScrolledText(rightf, wrap="word", state="disabled",
                                                font=("Consolas", 10), height=18)
        self.result.grid(row=0, column=0, sticky="nsew")
        # 结果卡配色标签：Tk 无法彩色 emoji，但给文字 tag 上前景色会连带把单色 emoji 染色 → 语义配色
        R = self.result
        R.tag_configure("h1", font=("Microsoft YaHei", 14, "bold"), foreground="#137333", spacing1=4, spacing3=4)
        R.tag_configure("sep", foreground="#c8ccd0")
        R.tag_configure("label", foreground="#5f6368")
        R.tag_configure("code", font=("Consolas", 13, "bold"), foreground="#0b3d91")
        R.tag_configure("dist", font=("Consolas", 10, "bold"), foreground="#202124")
        R.tag_configure("scn_yes", foreground="#137333")                          # 有地景=绿
        R.tag_configure("scn_no", foreground="#c5221f")                           # 无地景=红
        R.tag_configure("mil", font=("Consolas", 10, "bold"), foreground="#c5221f")   # 军用=红
        R.tag_configure("flown", foreground="#b06000")                            # 已飞过=琥珀
        R.tag_configure("section", font=("Microsoft YaHei", 10, "bold"), foreground="#202124", spacing1=6)
        R.tag_configure("aip", foreground="#3c4043")
        R.tag_configure("muted", foreground="#9aa0a6")
        R.tag_configure("warn", foreground="#b06000")                             # 提醒=琥珀
        R.tag_configure("success", font=("Microsoft YaHei", 11, "bold"), foreground="#137333", spacing1=4)
        R.tag_configure("partial", font=("Microsoft YaHei", 11, "bold"), foreground="#b06000", spacing1=4)
        R.tag_configure("nomatch", font=("Microsoft YaHei", 11, "bold"), foreground="#5f6368", spacing1=4)
        R.tag_configure("flight", font=("Consolas", 10), foreground="#202124")
        R.tag_configure("callsign", font=("Consolas", 11, "bold"), foreground="#0b3d91")
        self.result.tag_configure("link", foreground="#1a6fdb", underline=True)
        self.result.tag_bind("link", "<Button-1>", self._open_link)
        self.result.tag_bind("link", "<Enter>", lambda e: self.result.config(cursor="hand2"))
        self.result.tag_bind("link", "<Leave>", lambda e: self.result.config(cursor=""))

        # 底部：日志
        logf = ttk.LabelFrame(root, text="日志 / 状态", padding=4)
        logf.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 8))
        logf.columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(logf, wrap="word", state="disabled",
                                             font=("Consolas", 9), height=8)
        self.log.grid(row=0, column=0, sticky="ew")

    def _build_form(self, form):
        r = 0
        # 出发 / 目的
        ttk.Label(form, text="出发 ICAO（空=随机）").grid(row=r, column=0, sticky="w")
        self.var_dep = tk.StringVar()
        e_dep = ttk.Entry(form, textvariable=self.var_dep, width=12); e_dep.grid(row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Label(form, text="目的 ICAO（空=随机）").grid(row=r, column=0, sticky="w")
        self.var_dest = tk.StringVar()
        e_dest = ttk.Entry(form, textvariable=self.var_dest, width=12); e_dest.grid(row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Label(form, text="执飞航司（空=不限）").grid(row=r, column=0, sticky="w")
        self.var_airline = tk.StringVar()
        e_air = ttk.Entry(form, textvariable=self.var_airline, width=12); e_air.grid(row=r, column=1, sticky="w", pady=2); r += 1

        ttk.Separator(form, orient="horizontal").grid(row=r, column=0, columnspan=2, sticky="ew", pady=6); r += 1
        ttk.Label(form, text="高级筛选（可留空）", foreground="#888").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1

        ttk.Label(form, text="机型代码（如 737）").grid(row=r, column=0, sticky="w")
        self.var_aircraft = tk.StringVar()
        e_ac = ttk.Entry(form, textvariable=self.var_aircraft, width=12); e_ac.grid(row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Label(form, text="时间区间（08:00-15:30）").grid(row=r, column=0, sticky="w")
        self.var_time = tk.StringVar()
        e_tm = ttk.Entry(form, textvariable=self.var_time, width=12); e_tm.grid(row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Label(form, text="最短跑道（1800m/5900ft）").grid(row=r, column=0, sticky="w")
        self.var_runway = tk.StringVar()
        e_rw = ttk.Entry(form, textvariable=self.var_runway, width=12); e_rw.grid(row=r, column=1, sticky="w", pady=2); r += 1

        ttk.Label(form, text="航程 NM（最短 / 最长）").grid(row=r, column=0, sticky="w")
        df = ttk.Frame(form); df.grid(row=r, column=1, sticky="w", pady=2)
        self.var_dmin = tk.StringVar(); self.var_dmax = tk.StringVar()
        e_dmin = ttk.Entry(df, textvariable=self.var_dmin, width=6); e_dmin.pack(side="left")
        ttk.Label(df, text="—").pack(side="left", padx=2)
        e_dmax = ttk.Entry(df, textvariable=self.var_dmax, width=6); e_dmax.pack(side="left"); r += 1

        self.var_strict = tk.BooleanVar(value=False)
        chk_strict = ttk.Checkbutton(form, text="严格要求 AIP 规定航路", variable=self.var_strict)
        chk_strict.grid(row=r, column=0, columnspan=2, sticky="w", pady=2); r += 1

        self.var_scenery_only = tk.BooleanVar(value=False)
        self.chk_scenery = ttk.Checkbutton(form, text="仅在两端都有地景的机场间随机规划",
                                           variable=self.var_scenery_only)
        self.chk_scenery.grid(row=r, column=0, columnspan=2, sticky="w", pady=2); r += 1
        self.lbl_scenery_hint = ttk.Label(form, text="", foreground="#b06000", wraplength=240)
        self.lbl_scenery_hint.grid(row=r, column=0, columnspan=2, sticky="w"); r += 1

        ttk.Separator(form, orient="horizontal").grid(row=r, column=0, columnspan=2, sticky="ew", pady=6); r += 1
        # Volanta
        vf = ttk.Frame(form); vf.grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        self.btn_volanta = ttk.Button(vf, text="同步 Volanta", command=self._on_volanta_click)
        self.btn_volanta.pack(side="left")
        self.var_auto = tk.BooleanVar(value=False)
        self.chk_auto = ttk.Checkbutton(vf, text="自动同步", variable=self.var_auto, command=self._on_auto_toggle)
        self.chk_auto.pack(side="left", padx=8)
        self.var_vstatus = tk.StringVar(value="Volanta：—")
        ttk.Label(form, textvariable=self.var_vstatus, foreground="#555", wraplength=240).grid(
            row=r, column=0, columnspan=2, sticky="w"); r += 1

        ttk.Separator(form, orient="horizontal").grid(row=r, column=0, columnspan=2, sticky="ew", pady=6); r += 1
        self.btn_plan = ttk.Button(form, text="🛫 规划航线", command=self._on_plan_click)
        self.btn_plan.grid(row=r, column=0, columnspan=2, sticky="ew", ipady=4); r += 1

        # 受 _refresh_controls 统一启停的表单控件
        self._form_widgets = [e_dep, e_dest, e_air, e_ac, e_tm, e_rw, e_dmin, e_dmax, chk_strict, self.chk_auto]
        self._set_controls_state(False)  # 初始化完成前禁用

    # ---------- 线程 / 状态辅助 ----------
    def _run_bg(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def _post(self, fn, *args):
        try:
            self.root.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _append_log(self, s):
        try:
            self.log.configure(state="normal")
            self.log.insert("end", s)
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass

    def _set_status(self, text):
        self.var_status.set(text)

    def _set_vstatus(self, text):
        self.var_vstatus.set(text)

    def _set_controls_state(self, enabled):
        """根据 _ready/_busy/_vsyncing 统一启停表单 + 按钮。"""
        locked = self._busy or self._vsyncing
        form_on = enabled and self._ready and not locked
        for w in self._form_widgets:
            try:
                w.configure(state=("normal" if form_on else "disabled"))
            except Exception:
                pass
        try:
            self.btn_plan.configure(state=("normal" if form_on else "disabled"))
        except Exception:
            pass
        # 地景复选框：还需检测到地景目录（scenery_map 非 None）才可用
        scen_on = form_on and self.scenery_map is not None
        try:
            self.chk_scenery.configure(state=("normal" if scen_on else "disabled"))
        except Exception:
            pass
        # Volanta 按钮：同步中→「取消同步」；否则就绪且非忙→可点
        if self._vsyncing:
            self.btn_volanta.configure(text="取消同步", state="normal")
        else:
            self.btn_volanta.configure(text="同步 Volanta",
                                       state=("normal" if (self._ready and not self._busy) else "disabled"))

    def _refresh(self):
        self._set_controls_state(True)

    # ---------- 初始化 worker ----------
    def _init_worker(self):
        try:
            dat = find_navdata_file()
            if not dat:
                self._post(self._on_navdata_missing)
                return
            print(f"📁 已读取程序自带的 NavData 导航数据：{os.path.relpath(dat, get_real_run_path())}")
            check_airac_currency(dat)

            valid = load_japan_icao_set(dat)
            print("🌍 正在检测已安装的机场地景（XP / MSFS，首次较慢、之后走缓存）...")
            scenery_map, cached = scan_installed_sceneries(valid_icaos=valid)
            if scenery_map is None:
                print("ℹ️ 未检测到 X-Plane / MSFS 地景目录，本次跳过地景标注。")
            else:
                xp = sum(1 for s in scenery_map.values() if "XP" in s)
                ms = sum(1 for s in scenery_map.values() if "MSFS" in s)
                print(f"🌍 已{'读取缓存' if cached else '扫描'}到 {len(scenery_map)} 个已装地景机场（XP:{xp} / MSFS:{ms}）。")

            init_airline_data()
            aip = load_aip_routes_from_csv()
            aip_index = {(row[0].strip().upper(), row[1].strip().upper())
                         for row in aip if len(row) >= 2} if aip else set()

            auto = volanta_auto_enabled()
            if auto:
                print("🔑 正在用 Volanta 登录会话自动同步已飞数据...")
                if try_fetch_volanta_json_via_session(skip_if_fresh=3600):
                    print("✅ Volanta 已飞数据已是最新。")
                else:
                    print("ℹ️ 未能自动刷新（登录可能已过期，可点「同步 Volanta」重新登录），沿用已保存的数据。")
            flown, vmeta = load_volanta_flown_routes()

            self._post(self._on_init_done, dat, scenery_map, aip, aip_index, flown, vmeta, auto)
        except Exception as e:
            print(f"❌ 初始化出错: {e}")
            self._post(self._set_status, f"❌ 初始化出错: {e}")

    def _on_init_done(self, dat, scenery_map, aip, aip_index, flown, vmeta, auto):
        self.dat_path = dat
        self.scenery_map = scenery_map
        self.aip_data = aip
        self.aip_index = aip_index
        self.flown_counts = flown
        self._ready = True
        self.var_auto.set(bool(auto))

        scen_txt = "未检测到地景" if scenery_map is None else f"地景 {len(scenery_map)}"
        self._set_status(f"✅ 初始化完成 · NavData 已读取 · {scen_txt} · AIP {len(aip)}")
        if scenery_map is None:
            self.var_scenery_only.set(False)
            self.lbl_scenery_hint.configure(text="未检测到地景目录，无法按地景筛选")
        else:
            self.lbl_scenery_hint.configure(text="")
        self._apply_volanta(flown, vmeta)
        self._refresh()

    def _on_navdata_missing(self):
        self._set_status("❌ 未找到 NavData 导航数据")
        print("❌ 未找到导航数据。请前往 https://navigraph.com/downloads 下载「X-Plane 12」导航数据，"
              "放入程序目录的 NavData 文件夹（确保 NavData\\earth_aptmeta.dat 存在）后重启。")
        messagebox.showwarning(
            "缺少导航数据",
            "未找到 NavData 导航数据。\n\n请前往 https://navigraph.com/downloads 下载\n"
            "「X-Plane 12」导航数据，解压放入程序目录的 NavData 文件夹\n"
            "（确保 NavData\\earth_aptmeta.dat 存在）后重启本程序。")

    # ---------- 规划 ----------
    def _on_plan_click(self):
        if self._busy or self._vsyncing or not self._ready:
            return
        fields = {
            "dep": self.var_dep.get().strip().upper(),
            "dest": self.var_dest.get().strip().upper(),
            "airline": self.var_airline.get().strip().upper(),
            "aircraft": self.var_aircraft.get().strip(),
            "time": self.var_time.get().strip(),
            "runway": self.var_runway.get(),
            "dmin": self.var_dmin.get(),
            "dmax": self.var_dmax.get(),
            "strict": bool(self.var_strict.get()),
            "scenery_only": bool(self.var_scenery_only.get()) and self.scenery_map is not None,
        }
        self._busy = True
        self._refresh()
        self._clear_result()
        print("\n" + "🛫 开 始 新 的 航 班 规 划 🛬".center(49, "-"))
        self._run_bg(self._plan_worker, fields)

    def _plan_worker(self, f):
        try:
            min_rwy = parse_runway_ft(f["runway"])
            dmin, dmax = parse_dist(f["dmin"], f["dmax"])
            all_airports = load_airports_from_navigraph(self.dat_path, self.scenery_map, min_rwy)
            if not all_airports:
                raise RuntimeError("未能找到任何符合条件的机场。请检查跑道长度或数据文件。")
            strict = f["strict"]
            if strict and not self.aip_data:
                print("⚠️ 航路数据下载失败，已为您转为自由规划模式。")
                strict = False

            if f["dep"] and f["dest"]:
                dep_obj = next((a for a in all_airports if a.code == f["dep"]), None)
                arr_obj = next((a for a in all_airports if a.code == f["dest"]), None)
                if not dep_obj or not arr_obj:
                    raise RuntimeError("找不到指定机场（检查 ICAO 是否正确、跑道是否够长）。")
                dist = calculate_distance_nm(dep_obj, arr_obj)
                route = find_aip_route(self.aip_data, f["dep"], f["dest"]) if self.aip_data else None
                if strict and not route:
                    raise RuntimeError("未查到该航线的 AIP 规定航路。")
                flown_count = self.flown_counts.get((f["dep"], f["dest"]), 0)
            else:
                dep_obj, arr_obj, dist, route, flown_count = get_random_route(
                    all_airports, dmin, dmax, self.aip_data, strict, f["dep"], f["dest"],
                    self.flown_counts, self.aip_index, require_both_scenery=f["scenery_only"])

            print("🔎 正在拉取现实排班...")
            plan = build_flight_plan(dep_obj, arr_obj, dist, route,
                                     f["airline"], f["aircraft"], f["time"], flown_count)
            self._post(self._render_plan, plan)
        except Exception as e:
            print(f"❌ 发生错误: {e}")
            self._post(self._show_error, str(e))
        finally:
            self._post(self._finish_plan)

    def _finish_plan(self):
        self._busy = False
        self._refresh()

    # ---------- 结果渲染 ----------
    def _clear_result(self):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.configure(state="disabled")
        self._last_url = None

    def _show_error(self, msg):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("end", f"❌ 发生错误：{msg}\n")
        self.result.configure(state="disabled")

    def _render_plan(self, plan):
        dep, arr = plan.dep, plan.arr
        R = self.result
        R.configure(state="normal")
        R.delete("1.0", "end")

        def ins(text, *tags):
            R.insert("end", text, tags)

        def ins_airport(role, ap):
            ins(f"  {role} : ", "label")
            ins(ap.code, "code")
            lbl = ap.scenery_label()                       # " [地景:XP+MSFS]" / " [⚠️无地景]" / ""
            if lbl:
                ins(lbl, "scn_yes" if ap.has_scenery else "scn_no")
            if ap.is_military:
                ins(" [🛡️军用机场]", "mil")
            ins("\n")

        ins("  🛫  航 线 规 划 成 功\n", "h1")
        ins("  " + "─" * 44 + "\n", "sep")
        ins_airport("起飞机场", dep)
        ins_airport("降落机场", arr)
        ins("  大圆距离 : ", "label"); ins(f"{plan.dist_nm:.1f} NM\n", "dist")
        if plan.flown_count and plan.flown_count > 0:
            ins(f"  🔁 Volanta : 这条有向航线你已飞过 {plan.flown_count} 次（可考虑换一条）\n", "flown")

        if plan.aip_routes:
            ins("\n  📜 AIP 航路\n", "section")
            for i, rr in enumerate(plan.aip_routes, 1):
                ins(f"  [{i}] ", "muted"); ins(f"{rr}\n", "aip")

        if (not dep.has_scenery) or (not arr.has_scenery):
            ins("  ⚠️ 地景提醒: [⚠️无地景] = 未在 XP/MSFS 地景文件夹中检测到该机场插件地景\n", "warn")
        if dep.is_military or arr.is_military:
            ins("  🛡️ 军用提醒: 军用机场可能无民航设施与 SID/STAR，请酌情考虑！\n", "warn")

        ins("\n")
        if plan.is_exact and plan.real_flights:
            ins("  ✅ 完美匹配！为您检索到以下现实排班 :\n", "success")
            for fl in plan.real_flights:
                ins("     ✈ ", "muted"); ins(f"{fl}\n", "flight")
        elif plan.real_flights:
            ins("  ℹ️ 仅找到该航线上的其他参考排班 :\n", "partial")
            for fl in plan.real_flights:
                ins("     ✈ ", "muted"); ins(f"{fl}\n", "flight")
        else:
            ins("  ❌ 未找到排班，已降级生成模拟呼号", "nomatch")
            ins("（⚠️未必符合现实运行）: ", "muted"); ins(f"{plan.sim_callsign}\n", "callsign")

        ins("\n  🔗 查看 FlightAware 完整排班表:\n", "label")
        ins("  ", "label"); ins(plan.url + "\n", "link")

        R.configure(state="disabled")
        self._last_url = plan.url

    def _open_link(self, _event=None):
        if self._last_url:
            try:
                webbrowser.open(self._last_url)
            except Exception:
                pass

    # ---------- Volanta ----------
    def _on_auto_toggle(self):
        try:
            set_volanta_auto(bool(self.var_auto.get()))
            print(f"🔖 Volanta 自动同步偏好已设为：{'auto（以后启动自动同步）' if self.var_auto.get() else 'ask（每次手动）'}")
        except Exception as e:
            print(f"⚠️ 写入 Volanta 偏好失败: {e}")

    def _on_volanta_click(self):
        if self._vsyncing:
            self._cancel_evt.set()          # 同步中 → 取消
            self._set_vstatus("Volanta：正在取消…")
            return
        if self._busy or not self._ready:
            return
        self._cancel_evt = threading.Event()
        self._vsyncing = True
        self._refresh()
        self._set_vstatus("Volanta：正在同步…")
        self._run_bg(self._volanta_worker)

    def _volanta_worker(self):
        try:
            # 1) 先试本机已有登录会话（无需开浏览器）
            if try_fetch_volanta_json_via_session():
                self._post(self._volanta_synced)
                return
            # 2) 打开浏览器登录，后台每 3s 轮询，最长 180s，可取消
            self._post(self._set_vstatus, "Volanta：已打开浏览器，请在 Volanta 地图页登录…")
            _open_volanta_in_browser()
            waited = 0
            while waited < 180 and not self._cancel_evt.is_set():
                self._cancel_evt.wait(3)
                waited += 3
                if self._cancel_evt.is_set():
                    break
                if try_fetch_volanta_json_via_session():
                    self._post(self._volanta_synced)
                    return
                self._post(self._set_vstatus, f"Volanta：等待登录/获取中…（{waited}/180s）")
            if self._cancel_evt.is_set():
                self._post(self._set_vstatus, "Volanta：已取消同步。")
            else:
                self._post(self._set_vstatus, "Volanta：等待超时，未更新。")
        except Exception as e:
            self._post(self._set_vstatus, f"Volanta：同步出错（{e}）")
        finally:
            self._post(self._finish_volanta)

    def _volanta_synced(self):
        flown, vmeta = load_volanta_flown_routes()
        self.flown_counts = flown
        self._apply_volanta(flown, vmeta)
        print(f"✅ Volanta 同步完成：{len(flown)} 条有向航线。")

    def _apply_volanta(self, flown, vmeta):
        self.flown_counts = flown or {}
        if self.flown_counts:
            n_flights = vmeta.get("flights", sum(self.flown_counts.values()))
            latest = vmeta.get("latest")
            txt = f"Volanta：已读取 {n_flights} 次飞行 / {len(self.flown_counts)} 条航线"
            if latest:
                txt += f"（更新于 {latest}）"
            self._set_vstatus(txt)
        else:
            self._set_vstatus("Volanta：未读取到数据（可点「同步 Volanta」）")

    def _finish_volanta(self):
        self._vsyncing = False
        self._refresh()

    # ---------- 关闭 ----------
    def _on_close(self):
        self._cancel_evt.set()
        try:
            sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
        except Exception:
            pass
        self.root.destroy()


def _enable_hidpi():
    """Windows 高分屏：声明进程 DPI 感知（否则会被系统位图拉伸而发虚，像「低分辨率」），
    并返回系统缩放比（96 DPI=1.0、150%=1.5…）。必须在创建第一个窗口（tk.Tk()）之前调用。
    非 Windows / 任何调用失败都返回 1.0，不影响运行。"""
    if sys.platform != "win32":
        return 1.0
    scale = 1.0
    try:
        import ctypes
        # DPI 感知：Per-Monitor v2 → Per-Monitor → System，逐级兜底
        # （若已被 manifest 设过，调用会失败，忽略即可——感知本就已生效）
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)                        # PER_MONITOR
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()                             # SYSTEM（老系统兜底）
        # 系统 DPI → 缩放比
        try:
            scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        scale = 1.0
    return scale if scale and scale > 0 else 1.0


def run_gui():
    """GUI 入口：由 flight_dispatcher.py 调用。"""
    scale = _enable_hidpi()                       # 必须在 tk.Tk() 之前声明 DPI 感知
    root = tk.Tk()
    if abs(scale - 1.0) > 0.01:
        try:
            # tk scaling = 像素/点；设为 DPI/72 让点字号随 DPI 放大（控件按字号长大）
            root.tk.call("tk", "scaling", scale * 96.0 / 72.0)
        except Exception:
            pass
    DispatcherGUI(root, scale)
    root.mainloop()
