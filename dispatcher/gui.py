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
import copy
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
from .planner import build_flight_plan, parse_runway_ft, parse_dist, simbrief_url
from .aircraft import aircraft_choices
from .router import generate_route, route_geometry, route_length_nm
from . import procedures, weather, timed, operations

try:                                                 # 地图可视化（第三方库；缺失则地图按钮不显示，不影响其它功能）
    import tkintermapview
    from PIL import Image, ImageDraw, ImageTk
    _HAS_MAP = True
except Exception:
    _HAS_MAP = False


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
        self._last_simbrief_url = None
        self._proc_sb_url = None      # F20：按所选 SID/STAR 重建的 SimBrief 链接
        self._proc_sb_base = None
        self._proc_base_route = ""

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
        self.result.tag_configure("sblink", foreground="#1a6fdb", underline=True)
        self.result.tag_bind("sblink", "<Button-1>", self._open_simbrief)
        self.result.tag_bind("sblink", "<Enter>", lambda e: self.result.config(cursor="hand2"))
        self.result.tag_bind("sblink", "<Leave>", lambda e: self.result.config(cursor=""))

        # 结果区下方：跑道 + SID/STAR 选择面板（F20，规划后按航路端点预筛 + 天气辅助）
        self._build_proc_panel(rightf)

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
        ttk.Label(form, text="执飞航司 ICAO（空=不限）").grid(row=r, column=0, sticky="w")
        self.var_airline = tk.StringVar()
        e_air = ttk.Entry(form, textvariable=self.var_airline, width=12); e_air.grid(row=r, column=1, sticky="w", pady=2); r += 1

        ttk.Separator(form, orient="horizontal").grid(row=r, column=0, columnspan=2, sticky="ew", pady=6); r += 1
        ttk.Label(form, text="高级筛选（可留空）", foreground="#888").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1

        ttk.Label(form, text="机型（可搜索/下拉）").grid(row=r, column=0, sticky="w")
        self.var_aircraft = tk.StringVar()
        self._ac_rows = aircraft_choices()                          # [(显示串, id, 搜索blob), ...]
        self._ac_labels = [lbl for lbl, _id, _blob in self._ac_rows]
        self._ac_label_to_id = {lbl: _id for lbl, _id, _blob in self._ac_rows}
        self.cb_aircraft = ttk.Combobox(form, textvariable=self.var_aircraft, width=22,
                                        values=self._ac_labels)
        self.cb_aircraft.grid(row=r, column=1, sticky="w", pady=2); r += 1
        self.cb_aircraft.bind("<KeyRelease>", self._on_aircraft_type)   # 输入即过滤候选
        ttk.Label(form, text="时间区间（08:00-15:30）").grid(row=r, column=0, sticky="w")
        self.var_time = tk.StringVar()
        e_tm = ttk.Entry(form, textvariable=self.var_time, width=12); e_tm.grid(row=r, column=1, sticky="w", pady=2); r += 1
        ttk.Label(form, text="最短跑道长度（1800m/5900ft）").grid(row=r, column=0, sticky="w")
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

        # F21：第二重严格度——多条 AIP 航路时按 EOBT/机型/高度自动定唯一（勾选）；否则弹窗列出供手动选
        self.var_strict_ops = tk.BooleanVar(value=False)
        chk_ops = ttk.Checkbutton(form, text="严格遵循现实运行规则（按 EOBT/机型/高度定航路）",
                                  variable=self.var_strict_ops)
        chk_ops.grid(row=r, column=0, columnspan=2, sticky="w", pady=2); r += 1

        # 问题1：用户所用模拟器——地景判定/标注/「仅地景」筛选都按此（单次飞行只用一款，故 XP/MSFS 二选一）
        ttk.Label(form, text="本次飞行使用的模拟器").grid(row=r, column=0, sticky="w")
        sf = ttk.Frame(form); sf.grid(row=r, column=1, sticky="w", pady=2)
        self.var_sim = tk.StringVar(value="XP")
        for _txt, _val in (("X-Plane", "XP"), ("MSFS", "MSFS")):
            ttk.Radiobutton(sf, text=_txt, value=_val, variable=self.var_sim).pack(side="left", padx=(0, 6))
        r += 1

        self.var_scenery_only = tk.BooleanVar(value=False)
        self.chk_scenery = ttk.Checkbutton(form, text="仅在两端都有地景的机场间规划",
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
        self.btn_ops = ttk.Button(form, text="⚙️ 编辑机场运行规则", command=self._open_ops_editor)   # F23
        self.btn_ops.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(4, 0)); r += 1

        # 受 _refresh_controls 统一启停的表单控件
        self._form_widgets = [e_dep, e_dest, e_air, self.cb_aircraft, e_tm, e_rw, e_dmin, e_dmax, chk_strict, chk_ops, self.chk_auto]
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
        try:                                                # F23：运行规则编辑器（就绪即可，不受 _busy 规划影响）
            self.btn_ops.configure(state=("normal" if (self._ready and self.dat_path) else "disabled"))
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
            print(f"📁 已读取程序自带的导航数据：{os.path.relpath(dat, get_real_run_path())}")
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
                if try_fetch_volanta_json_via_session(skip_if_fresh=3600, diag=True):
                    print("✅ Volanta 已飞数据已是最新。")
                else:
                    print("ℹ️ 未能自动刷新，沿用已保存的数据。（如需更新，可点「同步 Volanta」）")
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
        self._set_status(f"✅ 初始化完成 · 导航数据已读取 · {scen_txt} · AIP {len(aip)}")
        if scenery_map is None:
            self.var_scenery_only.set(False)
            self.lbl_scenery_hint.configure(text="未检测到地景目录，无法按地景筛选")
        else:
            self.lbl_scenery_hint.configure(text="")
        self._apply_volanta(flown, vmeta)
        self._refresh()

    def _on_navdata_missing(self):
        self._set_status("❌ 未找到导航数据")
        print("❌ 未找到导航数据。请前往 https://navigraph.com/downloads 下载「X-Plane 12」导航数据，"
              "放入程序目录的 NavData 文件夹后重启。")
        messagebox.showwarning(
            "缺少导航数据",
            "未找到导航数据。\n\n请前往 https://navigraph.com/downloads 下载\n"
            "「X-Plane 12」导航数据，解压放入程序目录的 NavData 文件夹\n"
            "后重启本程序。")

    # ---------- 规划 ----------
    def _on_plan_click(self):
        if self._busy or self._vsyncing or not self._ready:
            return
        fields = {
            "dep": self.var_dep.get().strip().upper(),
            "dest": self.var_dest.get().strip().upper(),
            "airline": self.var_airline.get().strip().upper(),
            "aircraft": self._resolve_aircraft(),
            "time": self.var_time.get().strip(),
            "runway": self.var_runway.get(),
            "dmin": self.var_dmin.get(),
            "dmax": self.var_dmax.get(),
            "strict": bool(self.var_strict.get()),
            "strict_ops": bool(self.var_strict_ops.get()),
            "scenery_only": bool(self.var_scenery_only.get()) and self.scenery_map is not None,
            "sim": self.var_sim.get(),
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
            active = {f["sim"]} if f.get("sim") in ("XP", "MSFS") else None    # 问题1：所用模拟器(单选)

            if f["dep"] and f["dest"]:
                dep_obj = next((a for a in all_airports if a.code == f["dep"]), None)
                arr_obj = next((a for a in all_airports if a.code == f["dest"]), None)
                if not dep_obj or not arr_obj:
                    raise RuntimeError("找不到指定机场（检查 ICAO 是否正确、跑道是否够长）。")
                dist = calculate_distance_nm(dep_obj, arr_obj)
                route = find_aip_route(self.aip_data, f["dep"], f["dest"]) if self.aip_data else None
                if strict and not route:
                    raise RuntimeError("未查到该航线的 AIP 航路。")
                flown_count = self.flown_counts.get((f["dep"], f["dest"]), 0)
            else:
                def _route_len(d_obj, a_obj):
                    """候选航线的真实航路长(NM)：优先官方 AIP(取最短变体)，否则本地生成航路；都没有→None。"""
                    rows = ([r for r in self.aip_data
                             if len(r) > 5 and r[0].strip().upper() == d_obj.code
                             and r[1].strip().upper() == a_obj.code] if self.aip_data else [])
                    best_len = None
                    for r in rows:
                        rs = r[5].strip()
                        if not rs:
                            continue
                        try:
                            pts = route_geometry(d_obj, a_obj, rs, self.dat_path)
                            L = route_length_nm(pts) if pts else None
                        except Exception:
                            L = None
                        if L and (best_len is None or L < best_len):
                            best_len = L
                    if best_len is not None:
                        return best_len
                    if strict:                                  # 严格模式只认 AIP，不走生成
                        return None
                    try:
                        g = generate_route(d_obj, a_obj, dat_path=self.dat_path,
                                           aip_data=self.aip_data, airports=all_airports)
                        return g["dist_nm"] if g else None
                    except Exception:
                        return None
                dep_obj, arr_obj, dist, route, flown_count = get_random_route(
                    all_airports, dmin, dmax, self.aip_data, strict, f["dep"], f["dest"],
                    self.flown_counts, self.aip_index, require_both_scenery=f["scenery_only"],
                    active_sims=active, route_len_fn=_route_len)

            # F15：无 AIP 航路且非严格模式 → 用本地导航数据 A* 生成一条参考航路（两分支统一在此处理）
            generated = generated_warn = generated_dist = gr = None
            if route is None and not strict:
                try:
                    gr = generate_route(dep_obj, arr_obj, dat_path=self.dat_path,
                                        aip_data=self.aip_data, airports=all_airports)
                    if gr:
                        generated = gr["route_str"]
                        generated_dist = gr["dist_nm"]
                        generated_warn = gr["warn"] if gr.get("suspect") else None
                        print("🧭 无 AIP 航路，已用本地导航数据生成参考航路。")
                    else:
                        print("ℹ️ 本地导航数据未能连通该航线，跳过航路生成。")
                except Exception as e:
                    print(f"⚠️ 航路生成失败（已忽略）: {e}")   # 绝不让生成中断规划

            # 各 AIP 航路：长度（与 find_aip_route 同序）+ 地图航点（每条一份，可分别打开窗口）+ 航点(供 F21 端点预筛复用)
            matched = aip_dists = aip_maps = aip_pts = None
            gen_map = None
            if route:
                matched = [r for r in self.aip_data
                           if len(r) > 5 and r[0].strip().upper() == dep_obj.code and r[1].strip().upper() == arr_obj.code]
                aip_dists, aip_maps, aip_pts = [], [], []
                for r in matched:
                    rs = r[5].strip()
                    try:
                        pts = route_geometry(dep_obj, arr_obj, rs, self.dat_path) if rs else None
                    except Exception:
                        pts = None
                    aip_pts.append(pts)
                    aip_dists.append(route_length_nm(pts) if pts else None)
                    aip_maps.append((pts, "%s→%s  %s" % (dep_obj.code, arr_obj.code, rs)) if pts else None)
            elif generated and gr and gr.get("coords"):
                gen_map = (gr["coords"], "%s→%s  %s" % (dep_obj.code, arr_obj.code, generated))

            print("🔎 正在拉取现实排班...")
            plan = build_flight_plan(dep_obj, arr_obj, dist, route,
                                     f["airline"], f["aircraft"], f["time"], flown_count,
                                     generated_route=generated, generated_route_warn=generated_warn,
                                     generated_route_dist=generated_dist, aip_route_dists=aip_dists,
                                     aip_maps=aip_maps, gen_map=gen_map)
            plan.active_sims = active                       # 问题1：渲染按所用模拟器标注地景
            proc = self._compute_proc(dep_obj, arr_obj, generated,   # F20/F21：逐 AIP 候选预筛跑道/SID/STAR + 抓天气
                                      matched=matched, aip_dists=aip_dists, aip_pts=aip_pts,
                                      strict_ops=f.get("strict_ops"))
            self._post(self._render_plan, plan, proc)
        except Exception as e:
            print(f"❌ 发生错误: {e}")
            self._post(self._show_error, str(e))
        finally:
            self._post(self._finish_plan)

    def _compute_proc(self, dep_obj, arr_obj, generated, matched=None,
                      aip_dists=None, aip_pts=None, strict_ops=False):
        """F20/F21（后台线程）：为每条 AIP 航路（或生成航路）预算端点预筛的跑道/SID·STAR，并抓 dep/arr 的 METAR+TAF。
        matched=该航线全部 AIP 原始行（>1 条→用户在弹窗按 EOBT/机型/高度选或定唯一）；aip_pts/aip_dists 与之同序
        （复用 _plan_worker 已算几何、免重算）。任一步失败都不影响主规划（返回空/None，UI 优雅降级）。"""
        def _prefilter(base_route, pts):
            """一条航路串 → (dep_rows, dep_matched, arr_rows, arr_matched)。pts 有则复用其航点、否则现算。"""
            route_fixes = []
            try:
                if pts is None and base_route:
                    pts = route_geometry(dep_obj, arr_obj, base_route, self.dat_path)
                if pts:
                    route_fixes = [p[0] for p in pts[1:-1]]  # 去首尾机场，留 enroute（首=离场点、末=进场点）
            except Exception:
                route_fixes = []
            try:
                dr, dm = procedures.matching_choices(dep_obj.code, self.dat_path, route_fixes, "dep")
            except Exception:
                dr, dm = [], False
            try:
                ar, am = procedures.matching_choices(arr_obj.code, self.dat_path, list(reversed(route_fixes)), "arr")
            except Exception:
                ar, am = [], False
            return dr, dm, ar, am

        candidates = []
        if matched:                                          # AIP 分支：逐条候选（含时段/高度/机型 + 端点预筛）
            for i, anno in enumerate(timed.annotate_routes(matched)):
                pts = aip_pts[i] if (aip_pts and i < len(aip_pts)) else None
                dist = aip_dists[i] if (aip_dists and i < len(aip_dists)) else None
                dr, dm, ar, am = _prefilter(anno["route"], pts)
                candidates.append({**anno, "dist": dist, "pts": pts,   # pts 供全段航路预览(F21 续)复用 enroute 几何
                                   "dep_rows": dr, "dep_matched": dm, "arr_rows": ar, "arr_matched": am})
        elif generated:                                      # 生成航路：单候选（无时段/机型/高度）
            try:
                gpts = route_geometry(dep_obj, arr_obj, generated, self.dat_path)
            except Exception:
                gpts = None
            dr, dm, ar, am = _prefilter(generated, gpts)
            candidates.append({"route": generated, "restr": "", "alt": "", "aircraft": "", "dist": None, "pts": gpts,
                               "dep_rows": dr, "dep_matched": dm, "arr_rows": ar, "arr_matched": am})

        print("🌦️ 正在获取机场天气（METAR / 网格回退）…")
        return {
            "aip_candidates": candidates, "selected": 0, "strict_ops": bool(strict_ops),
            "dep_wx": weather.resolve_airport_wx(dep_obj.code, dep_obj.lat_dd, dep_obj.lon_dd),
            "arr_wx": weather.resolve_airport_wx(arr_obj.code, arr_obj.lat_dd, arr_obj.lon_dd),
        }

    def _finish_plan(self):
        self._busy = False
        self._refresh()

    # ---------- 结果渲染 ----------
    def _clear_result(self):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.configure(state="disabled")
        self._last_url = None
        self._last_simbrief_url = None
        self._reset_proc()                              # F20：隐藏上一轮的程序面板

    def _show_error(self, msg):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("end", f"❌ 发生错误：{msg}\n")
        self.result.configure(state="disabled")

    def _render_plan(self, plan, proc=None):
        dep, arr = plan.dep, plan.arr
        active = getattr(plan, "active_sims", None)         # 问题1：按所用模拟器标注地景
        R = self.result
        R.configure(state="normal")
        R.delete("1.0", "end")
        for _t in getattr(self, "_map_tags", []):        # 清理上一轮的动态地图链接 tag
            R.tag_delete(_t)
        self._map_tags = []

        def ins(text, *tags):
            R.insert("end", text, tags)

        def _map_link(mp):
            """mp=(coords,title)：为该条航路插入一个独立的「在地图查看」链接（点击各自弹窗，可同时开多个）。"""
            if not (_HAS_MAP and mp and mp[0]):
                return
            coords, title = mp
            tag = "maproute_%d" % len(self._map_tags)
            R.tag_configure(tag, foreground="#137333", underline=True)
            R.tag_bind(tag, "<Button-1>", lambda e, c=coords, t=title: self._open_map(c, t))
            R.tag_bind(tag, "<Enter>", lambda e: R.config(cursor="hand2"))
            R.tag_bind(tag, "<Leave>", lambda e: R.config(cursor=""))
            self._map_tags.append(tag)
            ins("       🗺️ 在地图查看本航路\n", tag)

        def ins_airport(role, ap):
            ins(f"  {role} : ", "label")
            ins(ap.code, "code")
            lbl = ap.scenery_label(active)                 # 按所用模拟器: " [地景:XP]" / " [⚠️无XP地景]" / ""
            if lbl:
                ins(lbl, "scn_yes" if ap.has_scenery_for(active) else "scn_no")
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

        def _dev(route_len):
            """航路长度 + 相对大圆偏差的展示串；缺数据返回 None。"""
            if not route_len or not plan.dist_nm:
                return None
            pct = (route_len - plan.dist_nm) / plan.dist_nm * 100.0
            return "航路长 %.0f NM（较大圆 %+.1f%%）" % (route_len, pct)

        if plan.aip_routes:
            ins("\n  📜 AIP 航路\n", "section")
            _dists = plan.aip_route_dists or []
            _maps = plan.aip_maps or []
            for i, rr in enumerate(plan.aip_routes, 1):
                cols = [x.strip() for x in rr.split(",")]         # rr=逗号拼接行 → [DEP,DEST,时段,高度,机型,航路,备注]
                route_s = cols[5] if len(cols) > 5 else rr
                restr = cols[2] if len(cols) > 2 else ""
                alt = cols[3] if len(cols) > 3 else ""
                ac = cols[4] if len(cols) > 4 else ""
                ins(f"  [{i}] ", "muted"); ins(f"{route_s}\n", "aip")
                cond = " · ".join(x for x in [("时段 " + restr) if restr else "",
                                              ("高度 " + alt) if alt else "",
                                              ("机型 " + ac) if ac else ""] if x)
                if cond:
                    ins("       条件：" + cond + "\n", "muted")
                _dd = _dev(_dists[i - 1] if i - 1 < len(_dists) else None)
                if _dd:
                    ins("       └ " + _dd + "\n", "muted")
                _map_link(_maps[i - 1] if i - 1 < len(_maps) else None)

        if plan.generated_route:
            ins("\n  🧭 生成航路（本地导航数据，非官方 AIP）\n", "section")
            ins("  " + plan.generated_route + "\n", "aip")
            _gd = _dev(plan.generated_route_dist)
            if _gd:
                ins("  " + _gd + "\n", "muted")
            _map_link(plan.gen_map)
            ins("  ⚠️ 起讫点取自 SID/STAR 衔接点、中间为 A* 连出的 enroute 航路；仅供参考，未含具体 SID/STAR 程序段。\n", "warn")
            if plan.generated_route_warn:
                ins("  ⚠️ " + plan.generated_route_warn + "——存在大角度转弯，可能非最优/有问题，请自行检查斟酌。\n", "warn")

        if (not dep.has_scenery_for(active)) or (not arr.has_scenery_for(active)):
            ins("  ⚠️ 地景提醒: [⚠️无…地景] = 未在所选模拟器的地景文件夹中检测到该机场插件地景\n", "warn")
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

        if plan.simbrief_url:
            ins("\n  🛩️ SimBrief 一键签派 : ", "label")
            ins("点击生成并查看simbrief计划（需登录）\n", "sblink")

        R.configure(state="disabled")
        self._last_url = plan.url
        self._last_simbrief_url = plan.simbrief_url
        self._populate_proc(plan, proc)                 # F20：填充跑道 / SID·STAR 面板（含天气）

    def _open_link(self, _event=None):
        if self._last_url:
            try:
                webbrowser.open(self._last_url)
            except Exception:
                pass

    def _open_simbrief(self, _event=None):
        if self._last_simbrief_url:
            try:
                webbrowser.open(self._last_simbrief_url)
            except Exception:
                pass

    # ---------- 跑道 / SID·STAR 选择面板（F20）----------
    def _build_proc_panel(self, parent):
        """结果区下方的「跑道 + SID/STAR」面板：规划后按航路端点预筛、天气辅助选跑道。初始隐藏。"""
        fr = ttk.LabelFrame(parent, text="跑道 / SID·STAR（按航路端点预筛 · 天气辅助选跑道）", padding=6)
        fr.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        for c in (1, 3):
            fr.columnconfigure(c, weight=1)
        self._proc_frame = fr
        self.var_dep_wx = tk.StringVar(); self.var_arr_wx = tk.StringVar()
        self.var_proc_sel = tk.StringVar(); self.var_proc_hint = tk.StringVar()
        self.var_dep_rwy = tk.StringVar(); self.var_dep_sid = tk.StringVar()
        self.var_arr_rwy = tk.StringVar(); self.var_arr_star = tk.StringVar()
        self.var_aip = tk.StringVar()
        self.var_eobt = tk.StringVar(); self.var_apply_ops = tk.BooleanVar(value=True)
        self.var_ops_dep = tk.StringVar(); self.var_ops_arr = tk.StringVar(); self.var_eobt_z = tk.StringVar()

        # v1.6.0：EOBT(JST) + 按运行规则预选 —— EOBT 用于匹配 operation.json 时段(离场按 EOBT、到达按 ETA)并回填 SimBrief 撤轮挡
        ctrl = ttk.Frame(fr)
        ctrl.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        ttk.Label(ctrl, text="EOBT(JST)").grid(row=0, column=0, sticky="w")
        self.ent_eobt = ttk.Entry(ctrl, textvariable=self.var_eobt, width=7)
        self.ent_eobt.grid(row=0, column=1, sticky="w", padx=(2, 2))
        ttk.Label(ctrl, textvariable=self.var_eobt_z, foreground="#9aa0a6").grid(row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Checkbutton(ctrl, text="按机场运行规则预选跑道/程序（operation.json）", variable=self.var_apply_ops,
                        command=self._on_apply_ops_toggle).grid(row=0, column=3, sticky="w")
        self.var_eobt.trace_add("write", lambda *_a: self._on_eobt_changed())

        # F21：AIP 航路选择行（仅该航线有多条 AIP 时显示）——下拉即时切换 +「确认航路」开弹窗按 EOBT/机型/高度选/定
        self._aip_row = ttk.Frame(fr)
        self._aip_row.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        self._aip_row.columnconfigure(1, weight=1)
        ttk.Label(self._aip_row, text="AIP 航路").grid(row=0, column=0, sticky="w")
        self.cb_aip = ttk.Combobox(self._aip_row, textvariable=self.var_aip, state="readonly")
        self.cb_aip.grid(row=0, column=1, sticky="ew", padx=(2, 8))
        self.cb_aip.bind("<<ComboboxSelected>>", self._on_aip_combo)
        self.btn_aip = ttk.Button(self._aip_row, text="确认航路 (EOBT/机型/高度)…", command=self._open_aip_popup)
        self.btn_aip.grid(row=0, column=2, sticky="e")
        self._aip_row.grid_remove()

        ttk.Label(fr, textvariable=self.var_dep_wx, foreground="#5f6368", wraplength=560, justify="left"
                  ).grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Label(fr, text="出发跑道").grid(row=3, column=0, sticky="w")
        self.cb_dep_rwy = ttk.Combobox(fr, textvariable=self.var_dep_rwy, width=24, state="readonly")
        self.cb_dep_rwy.grid(row=3, column=1, sticky="ew", padx=(2, 8))
        ttk.Label(fr, text="SID").grid(row=3, column=2, sticky="w")
        self.cb_dep_sid = ttk.Combobox(fr, textvariable=self.var_dep_sid, width=18)
        self.cb_dep_sid.grid(row=3, column=3, sticky="ew", padx=2)
        ttk.Label(fr, textvariable=self.var_ops_dep, foreground="#137333", wraplength=560, justify="left"
                  ).grid(row=4, column=0, columnspan=4, sticky="w")

        ttk.Label(fr, textvariable=self.var_arr_wx, foreground="#5f6368", wraplength=560, justify="left"
                  ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(fr, text="到达跑道").grid(row=6, column=0, sticky="w")
        self.cb_arr_rwy = ttk.Combobox(fr, textvariable=self.var_arr_rwy, width=24, state="readonly")
        self.cb_arr_rwy.grid(row=6, column=1, sticky="ew", padx=(2, 8))
        ttk.Label(fr, text="STAR").grid(row=6, column=2, sticky="w")
        self.cb_arr_star = ttk.Combobox(fr, textvariable=self.var_arr_star, width=18)
        self.cb_arr_star.grid(row=6, column=3, sticky="ew", padx=2)
        ttk.Label(fr, textvariable=self.var_ops_arr, foreground="#137333", wraplength=560, justify="left"
                  ).grid(row=7, column=0, columnspan=4, sticky="w")

        ttk.Label(fr, textvariable=self.var_proc_hint, foreground="#9aa0a6", wraplength=560, justify="left"
                  ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(fr, textvariable=self.var_proc_sel, foreground="#202124", wraplength=560, justify="left"
                  ).grid(row=9, column=0, columnspan=4, sticky="w")
        if _HAS_MAP:                                          # F21 续：预览全段航路(SID+enroute+STAR) → 复用 _open_map
            self.lbl_proc_preview = ttk.Label(fr, text="🗺️ 预览完整航路（SID + enroute + STAR）",
                                              foreground="#137333", cursor="hand2")
            self.lbl_proc_preview.grid(row=10, column=0, columnspan=4, sticky="w", pady=(2, 0))
            self.lbl_proc_preview.bind("<Button-1>", self._preview_full_route)
        self.lbl_proc_sb = ttk.Label(fr, text="🛩️ 按所选程序派遣 SimBrief（需登录）",
                                     foreground="#1a6fdb", cursor="hand2")
        self.lbl_proc_sb.grid(row=11, column=0, columnspan=4, sticky="w", pady=(2, 0))
        self.lbl_proc_sb.bind("<Button-1>", self._open_proc_simbrief)

        self.cb_dep_rwy.bind("<<ComboboxSelected>>", lambda e: self._on_rwy_selected("dep"))
        self.cb_arr_rwy.bind("<<ComboboxSelected>>", lambda e: self._on_rwy_selected("arr"))
        self.cb_dep_sid.bind("<<ComboboxSelected>>", lambda e: self._on_proc_changed())
        self.cb_arr_star.bind("<<ComboboxSelected>>", lambda e: self._on_proc_changed())
        self.cb_dep_sid.bind("<KeyRelease>", lambda e: self._on_proc_filter("dep", e))
        self.cb_arr_star.bind("<KeyRelease>", lambda e: self._on_proc_filter("arr", e))
        fr.grid_remove()                                # 规划后才显示

    def _reset_proc(self):
        """清空并隐藏程序面板（新规划/出错时）。"""
        if hasattr(self, "_proc_frame"):
            self._proc_frame.grid_remove()
        if hasattr(self, "_aip_row"):
            self._aip_row.grid_remove()
        self._proc_sb_url = None
        self._aip_candidates = []
        self._proc_sel_idx = 0
        self._proc_ready = False
        if hasattr(self, "var_ops_dep"):
            self.var_ops_dep.set(""); self.var_ops_arr.set("")

    def _wind_desc(self, wind, rwy_id):
        """(逆/顺风文本+侧风+适航, ok, headwind)；wind=(dir,spd,gust) 或 None / 静风 / VRB → ('', True, 0)。"""
        if not wind or not isinstance(wind[0], (int, float)) or not wind[1]:
            return "", True, 0.0
        hw, cw = weather.runway_wind(procedures.runway_heading_deg(rwy_id), wind[0], wind[1])
        ok = weather.runway_ok(hw, cw)
        wd = ("逆风%.0f节" % hw) if hw >= 0 else ("顺风%.0f节" % abs(hw))   # 顺/逆已表方向，分量取绝对值 + 单位「节」
        return "%s 侧风%.0f节 %s" % (wd, cw, "✓" if ok else "⚠️超限"), ok, hw

    def _wx_text(self, prefix, icao, wx):
        """该机场的天气块（F22）：实测 METAR 与网格合成 METAR 两分支【同一套渲染】（风摘要 + 报文原文），
        仅标题标注不同——网格分支明确标「Open-Meteo·<model> 模型合成·非实测」。wx=resolve_airport_wx 结果。"""
        raw = wx.get("metar_raw") if wx else None
        if not raw:
            head = "%s %s  ·  天气获取失败（断网或暂无观测）" % (prefix, icao)
            taf = wx.get("taf_raw") if wx else None
            return head + ("\n    %s" % taf if taf else "")
        wd, ws, gust = weather.parse_wind(raw)
        if not ws:
            wind_s = "静风"
        elif isinstance(wd, int):
            wind_s = "风 %03d°/%d节%s" % (wd, ws, ("阵%d" % gust if gust else ""))
        else:
            wind_s = "风 不定/%d节" % ws
        age = wx.get("metar_age_sec")
        stale = age is not None and age > weather._METAR_STALE_SEC
        if wx.get("source") == "grid":
            model = (wx.get("model") or "model").upper().replace("_", "-")
            toks = raw.split()
            zt = toks[1] if len(toks) > 1 and toks[1].endswith("Z") else ""
            head = "%s %s  🌐 %s  《Open-Meteo·%s 模型合成 METAR·非实测%s》\n    %s" % (
                prefix, icao, wind_s, model, (" · " + zt if zt else ""), raw)
            if stale:
                head += "\n    ⚠️ 实测 METAR 已 %.0f 小时前，改用网格合成" % (age / 3600.0)
        else:
            head = "%s %s  %s%s\n    %s" % (
                prefix, icao, wind_s, ("  ⚠️可能过期(%.0fh)" % (age / 3600.0) if stale else ""), raw)
        taf = wx.get("taf_raw")
        if taf:
            head += "\n    %s" % taf
        return head

    def _fill_rwy(self, side, rows, wind):
        """填某侧跑道下拉（显示长度 + 风分量 + 适航；合规优先、再逆风、再跑道号排序），并预选首个跑道、级联其程序。"""
        items = []
        for rwy_id, length_ft, labels in rows:
            short = rwy_id.replace("RW", "")
            parts = [short] + (["%.0fm" % (length_ft * 0.3048)] if length_ft else [])   # 东亚习惯：显示层用米（数据底层为英尺）
            wdesc, ok, hw = self._wind_desc(wind, rwy_id)
            if wdesc:
                parts.append(wdesc)
            items.append({"disp": " · ".join(parts), "rwy": rwy_id, "labels": labels, "ok": ok, "hw": hw})
        items.sort(key=lambda it: (not it["ok"], -it["hw"], procedures._rw_sort_key(it["rwy"])))
        combo = self.cb_dep_rwy if side == "dep" else self.cb_arr_rwy
        combo["values"] = [it["disp"] for it in items]
        setattr(self, "_%s_rwy_map" % side, {it["disp"]: it for it in items})
        if items:
            combo.set(items[0]["disp"])
            self._fill_proc(side, items[0])
        else:
            combo.set("（无可选程序）")
            self._fill_proc(side, None)

    def _fill_proc(self, side, item):
        """按所选跑道填 SID/STAR 下拉（预选首个）。item=None → 清空。"""
        combo = self.cb_dep_sid if side == "dep" else self.cb_arr_star
        labels = (item or {}).get("labels", []) if item else []
        setattr(self, "_%s_proc_all" % side, labels)
        combo["values"] = labels
        combo.set(labels[0] if labels else "")

    def _on_rwy_selected(self, side):
        combo = self.cb_dep_rwy if side == "dep" else self.cb_arr_rwy
        item = getattr(self, "_%s_rwy_map" % side, {}).get(combo.get())
        self._fill_proc(side, item)
        self._on_proc_changed()

    def _on_proc_filter(self, side, event):
        """SID/STAR 下拉可搜索：输入即过滤候选（回车确认更新）。"""
        if event is not None and event.keysym in ("Up", "Down", "Return", "Escape", "Left", "Right"):
            if event.keysym == "Return":
                self._on_proc_changed()
            return
        combo = self.cb_dep_sid if side == "dep" else self.cb_arr_star
        typed = combo.get().strip().lower()
        allv = getattr(self, "_%s_proc_all" % side, [])
        combo["values"] = [v for v in allv if typed in v.lower()] or allv

    def _on_proc_changed(self, *_a):
        """SID/STAR/跑道变化 → 更新摘要 + 用所选程序重建 SimBrief 链接（route = SID + enroute + STAR）。"""
        dmap = getattr(self, "_dep_rwy_map", {}); amap = getattr(self, "_arr_rwy_map", {})
        ditem = dmap.get(self.cb_dep_rwy.get()); aitem = amap.get(self.cb_arr_rwy.get())
        dep_rwy = ditem["rwy"].replace("RW", "") if ditem else "—"
        arr_rwy = aitem["rwy"].replace("RW", "") if aitem else "—"
        sid = self.var_dep_sid.get().strip(); star = self.var_arr_star.get().strip()
        self.var_proc_sel.set("已选：%s 跑道 %s / %s    →    %s 跑道 %s / %s"
                              % (self._proc_dep_icao, dep_rwy, sid or "—",
                                 self._proc_arr_icao, arr_rwy, star or "—"))
        route = " ".join(x for x in [sid.split(".")[0], self._proc_base_route, star.split(".")[0]] if x)
        eobt_utc = self._eobt_utc_min()                 # SimBrief 用 UTC(Zulu)：JST−9h
        self.var_eobt_z.set(("→ SimBrief %02d%02dZ" % (eobt_utc // 60, eobt_utc % 60)) if eobt_utc is not None else "")
        self._proc_sb_url = simbrief_url(self._proc_sb_base, route, eobt_utc) if self._proc_sb_base else None

    def _eobt_jst_min(self):
        """EOBT 输入(JST HHMM) → 当日分钟；空/非法 → None。"""
        return timed.parse_hhmm(self.var_eobt.get()) if getattr(self, "var_eobt", None) else None

    def _eobt_utc_min(self):
        """EOBT(JST) → 当日 UTC 分钟（供 SimBrief deph/depm）；无 → None。"""
        j = self._eobt_jst_min()
        return None if j is None else (j - 540) % 1440

    def _on_eobt_changed(self):
        """EOBT 改动：开关开则按新时段重选（经 _on_aip_route_selected→_apply_ops_rules），否则仅刷新 SimBrief。"""
        if not getattr(self, "_proc_ready", False):
            return
        if self.var_apply_ops.get():
            self._on_aip_route_selected(getattr(self, "_proc_sel_idx", 0))
        else:
            self._on_proc_changed()

    def _on_apply_ops_toggle(self):
        """勾/取消「按运行规则预选」：重跑当前候选（开→应用规则、关→回按风预选）。"""
        if not getattr(self, "_proc_ready", False):
            return
        self._on_aip_route_selected(getattr(self, "_proc_sel_idx", 0))

    def _apply_ops_rules(self):
        """v1.6.0：按 operation.json 运行规则预选跑道/SID·STAR（覆盖 _fill_rwy 的按风默认），并展示命中规则。
        离场机场按 EOBT、到达机场按 ETA(=EOBT+航程) 匹配时段；关闭开关或无规则 → 保持按风预选、优雅降级。"""
        self.var_ops_dep.set(""); self.var_ops_arr.set("")
        if not self.var_apply_ops.get() or not getattr(self, "_ops_data", None):
            return
        cands = getattr(self, "_aip_candidates", [])
        if not cands:
            return
        c = cands[getattr(self, "_proc_sel_idx", 0)]
        eobt_jst = self._eobt_jst_min()
        now_min, now_wd = weather.now_jst()
        dep_jst = eobt_jst if eobt_jst is not None else now_min
        try:
            dist = c.get("dist") or getattr(self, "_proc_dist", None) or 0
            _eu, eta_utc = timed.plan_times_utc(dep_jst, dist)
            eta_jst = (eta_utc + 540) % 1440
        except Exception:
            eta_jst = dep_jst
        arr_wd = now_wd if eta_jst >= dep_jst else (now_wd % 7 + 1)   # 到达跨午夜 → 星期 +1
        dep_layers, dep_vis = getattr(self, "_dep_sky", ([], None))
        arr_layers, arr_vis = getattr(self, "_arr_sky", ([], None))
        self._apply_ops_side("dep", self._proc_dep_icao, c.get("dep_rows", []),
                             {"jst_min": dep_jst, "weekday": now_wd, "wind": getattr(self, "_dep_wind", None),
                              "sky_layers": dep_layers, "vis_m": dep_vis}, self.var_ops_dep, "SID")
        self._apply_ops_side("arr", self._proc_arr_icao, c.get("arr_rows", []),
                             {"jst_min": eta_jst, "weekday": arr_wd, "wind": getattr(self, "_arr_wind", None),
                              "sky_layers": arr_layers, "vis_m": arr_vis}, self.var_ops_arr, "STAR")

    def _apply_ops_side(self, side, icao, rows, ctx, label_var, proc_name):
        """单侧应用：命中规则 → 设跑道下拉 + 级联程序 + 设 SID/STAR + 展示；无规则不动、无命中给提示。"""
        rules = operations.airport_rules(getattr(self, "_ops_data", {}), icao)
        if not rules:
            return
        try:
            sel = operations.select_rule(rules, side, ctx, rows)
        except Exception as e:
            print("⚠️ 运行规则匹配失败:", e)
            return
        if not sel:
            label_var.set("🎯 %s 运行规则：当前风/时段/天气无【可用】匹配规则（超限跑道已排除，保持按风合规跑道）" % icao)
            return
        rule, rwy, proc_label = sel
        proccombo = self.cb_dep_sid if side == "dep" else self.cb_arr_star
        rmap = getattr(self, "_%s_rwy_map" % side, {})
        disp = next((d for d, it in rmap.items() if it["rwy"] == rwy), None)
        if disp:
            (self.cb_dep_rwy if side == "dep" else self.cb_arr_rwy).set(disp)
            self._fill_proc(side, rmap[disp])               # 级联该跑道路线相符的 SID/STAR（预选首个）
        if proc_label:
            proccombo.set(proc_label)                       # 规则程序命中航路端点→用它；否则保留 _fill_proc 的路线相符首个
        iap = ""
        if side == "arr":
            iaps = (rule.get("arr") or {}).get("iaps") or []
            if iaps:
                iap = "  IAP %s" % iaps[0]
        label_var.set("🎯 %s 运行规则：%s → RW%s / %s %s%s%s"
                      % (icao, rule.get("name", ""), (rwy or "").replace("RW", ""),
                         proc_name, proccombo.get() or "—", iap,
                         "" if disp else "（规则跑道不在本航路端点候选，仅标注）"))

    def _open_proc_simbrief(self, _event=None):
        url = self._proc_sb_url or self._last_simbrief_url
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    def _preview_full_route(self, _event=None):
        """把当前选定的 SID + enroute + STAR 全段画到地图（复用 F18 的 _open_map）。"""
        cands = getattr(self, "_aip_candidates", [])
        if not (_HAS_MAP and cands):
            return
        c = cands[getattr(self, "_proc_sel_idx", 0)]
        ditem = getattr(self, "_dep_rwy_map", {}).get(self.cb_dep_rwy.get())
        aitem = getattr(self, "_arr_rwy_map", {}).get(self.cb_arr_rwy.get())
        dep_rwy = ditem["rwy"] if ditem else None
        arr_rwy = aitem["rwy"] if aitem else None
        sid, star = self.var_dep_sid.get().strip(), self.var_arr_star.get().strip()
        try:
            coords = procedures.full_route_coords(
                self._proc_dep_icao, sid, dep_rwy, self._proc_arr_icao, star, arr_rwy,
                c.get("pts"), self.dat_path)
        except Exception as e:
            print(f"⚠️ 生成全段航路失败: {e}")
            return
        if len(coords) < 2:
            print("ℹ️ 全段航路坐标不足，无法预览（SID/STAR 航点未能解析）。")
            return
        title = "%s→%s  %s" % (self._proc_dep_icao, self._proc_arr_icao, c.get("route", ""))
        self._open_map(coords, title)

    def _populate_proc(self, plan, proc):
        """规划后填充程序面板（Tk 线程，_render_plan 调）。proc=各 AIP/生成候选的端点预筛 + 天气（F21）。"""
        if not proc or not proc.get("aip_candidates"):
            self._reset_proc()
            return
        self._proc_ready = False                            # 抑制填充期 EOBT trace 递归（末尾置 True）
        self._proc_dep_icao, self._proc_arr_icao = plan.dep.code, plan.arr.code
        self._proc_sb_base = getattr(plan, "sb_base", None)
        self._aip_candidates = proc["aip_candidates"]
        self._proc_strict_ops = bool(proc.get("strict_ops"))
        self._proc_dist = getattr(plan, "dist_nm", None)
        dep_wx, arr_wx = proc.get("dep_wx"), proc.get("arr_wx")
        self._dep_wind = dep_wx.get("wind") if dep_wx else None
        self._arr_wind = arr_wx.get("wind") if arr_wx else None
        self._dep_sky = weather.parse_sky(dep_wx.get("metar_raw") if dep_wx else None)   # (layers, vis_m) 供好天门槛
        self._arr_sky = weather.parse_sky(arr_wx.get("metar_raw") if arr_wx else None)
        self._ops_data = operations.load_operations()       # v1.6.0：规划时应用运行规则
        self.var_dep_wx.set(self._wx_text("🛫 出发", plan.dep.code, dep_wx))
        self.var_arr_wx.set(self._wx_text("🛬 到达", plan.arr.code, arr_wx))
        nm, _wd = weather.now_jst()                         # EOBT 默认＝当前 JST（可改）
        self.var_eobt.set("%02d%02d" % (nm // 60, nm % 60))

        cands = self._aip_candidates
        if len(cands) > 1:                                  # 多条 AIP → 显示下拉 + 确认按钮，规划后自动弹窗
            self.cb_aip["values"] = [self._aip_label(i, c) for i, c in enumerate(cands)]
            self._aip_row.grid()
        else:
            self.cb_aip["values"] = []
            self._aip_row.grid_remove()

        self._proc_frame.grid()
        self._on_aip_route_selected(proc.get("selected", 0))
        self._proc_ready = True
        if len(cands) > 1:
            self.root.after(0, self._open_aip_popup)        # 规划检索到多条 AIP → 自动弹窗（严格自动定唯一 / 非严格备注选）

    def _aip_label(self, i, c):
        """AIP 航路下拉紧凑标签：[序号] 时段 · 距离 · 航路首…尾。"""
        restr = c.get("restr") or "全时段"
        dist = ("%.0fNM · " % c["dist"]) if c.get("dist") else ""
        toks = (c.get("route") or "").split()
        short = " ".join(toks) if len(toks) <= 4 else "%s…%s" % (" ".join(toks[:2]), toks[-1])
        return "[%d] %s · %s%s" % (i + 1, restr, dist, short)

    def _on_aip_combo(self, _e=None):
        idx = self.cb_aip.current()
        if idx is not None and idx >= 0:
            self._on_aip_route_selected(idx)

    def _on_aip_route_selected(self, idx):
        """选定第 idx 条 AIP/生成候选 → 换 base_route、按其预筛填跑道/SID·STAR、更新提示与 SimBrief。"""
        cands = getattr(self, "_aip_candidates", [])
        if not cands:
            return
        idx = max(0, min(int(idx), len(cands) - 1))
        self._proc_sel_idx = idx
        c = cands[idx]
        self._proc_base_route = c.get("route", "")
        if self.cb_aip["values"]:
            self.cb_aip.current(idx)
        self._fill_rwy("dep", c.get("dep_rows", []), getattr(self, "_dep_wind", None))
        self._fill_rwy("arr", c.get("arr_rows", []), getattr(self, "_arr_wind", None))
        self._apply_ops_rules()                             # v1.6.0：运行规则命中则覆盖按风预选（在 _on_proc_changed 之前）
        notes = []
        dep_rows, arr_rows = c.get("dep_rows"), c.get("arr_rows")
        has_proc = lambda rows: any(r[2] for r in (rows or []))   # 行内 label 非空 = 该跑道挂有 SID/STAR
        if not dep_rows:
            notes.append("出发无跑道数据")
        elif not has_proc(dep_rows):
            notes.append("出发无可用 SID（可选跑道，雷达引导离场）")
        elif not c.get("dep_matched"):
            notes.append("出发端点未直接匹配 SID（已列全部）")
        if not arr_rows:
            notes.append("到达无跑道数据")
        elif not has_proc(arr_rows):
            notes.append("到达无 STAR（可选跑道，仪表进近 IAP）")
        elif not c.get("arr_matched"):
            notes.append("到达端点未直接匹配 STAR（已列全部）")
        self.var_proc_hint.set("ℹ️ " + "；".join(notes) if notes else "")
        self._on_proc_changed()

    def _open_aip_popup(self, _e=None):
        """F21 确认航路弹窗：仿真实 AIP 航路表 + 行首选择框（单选，点行即选中并实时更新面板）。
        非严格：纯罗列 时段/用途/机型/高度/距离/航路，手动勾选一条。
        严格：上方收 EOBT/机型/高度 → 实时判定(✓可用/✗不符/？待定)，唯一可用即自动选定。"""
        cands = getattr(self, "_aip_candidates", [])
        if len(cands) <= 1:
            return
        strict = getattr(self, "_proc_strict_ops", False)
        win = tk.Toplevel(self.root)
        win.title("确认 AIP 航路   %s → %s" % (self._proc_dep_icao, self._proc_arr_icao))
        win.transient(self.root)
        win.columnconfigure(0, weight=1); win.rowconfigure(1, weight=1)
        status = tk.StringVar()
        eobt_v = tk.StringVar(); cat_v = tk.StringVar(value="JET"); fl_v = tk.StringVar()

        cols = [("sel", "选择", 44), ("route", "航路 (Route)", 330), ("hours", "时段 (Hours)", 145),
                ("alt", "高度", 64), ("ac", "机型", 56), ("use", "用途", 150), ("dist", "距离", 66)]
        if strict:
            cols.append(("verdict", "判定", 64))
        tv = ttk.Treeview(win, columns=[c[0] for c in cols], show="headings",
                          height=min(len(cands), 12), selectmode="browse")
        for key, txt, w in cols:
            tv.heading(key, text=txt)
            tv.column(key, width=w, anchor="w", stretch=(key == "route"))
        tv.tag_configure("match", background="#e6f4ea")
        tv.tag_configure("no", foreground="#9aa0a6")

        def _draw(verdicts=None):
            tv.delete(*tv.get_children())
            sel = getattr(self, "_proc_sel_idx", 0)
            for i, c in enumerate(cands):
                v = verdicts[i] if verdicts else None
                dist = ("%.0f NM" % c["dist"]) if c.get("dist") else "-"
                row = ["●" if i == sel else "○", c.get("route", ""), c.get("restr") or "-",
                       c.get("alt") or "-", c.get("aircraft") or "-",
                       timed.describe_restriction(c.get("restr", "")), dist]
                if strict:
                    row.append({"match": "✓可用", "no": "✗不符", "unknown": "？待定"}.get(v, "-"))
                tv.insert("", "end", iid=str(i), values=row, tags=((v,) if v in ("match", "no") else ()))

        def _recompute(*_a):
            if not strict:
                _draw(); return
            eobt = timed.parse_hhmm(eobt_v.get())
            fl = timed.parse_fl(fl_v.get())
            eobt_utc = eta_utc = None
            if eobt is not None:
                eobt_utc, eta_utc = timed.plan_times_utc(eobt, cands[getattr(self, "_proc_sel_idx", 0)].get("dist"))
            verds = timed.filter_candidates(cands, eobt_utc, eta_utc, cat_v.get(), fl)
            uniq = timed.resolve_unique(verds)
            if uniq is not None and eobt is not None and fl is not None:
                self._on_aip_route_selected(uniq)           # 唯一可用 → 自动选定（面板同步更新）
                status.set("✓ 唯一匹配：第 %d 条已自动选定，可「确认并关闭」" % (uniq + 1))
            elif eobt is not None or fl is not None:
                status.set("当前无法唯一确定，请补齐 EOBT/机型/高度或手动勾选")
            else:
                status.set("填入 EOBT/机型/高度自动定唯一航路，或直接手动勾选")
            _draw(verds)

        def _pick(_e=None):
            f = tv.focus()
            if f:
                self._on_aip_route_selected(int(f))
                _recompute() if strict else _draw()

        if strict:
            inp = ttk.Frame(win); inp.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
            ttk.Label(inp, text="EOBT(JST)").pack(side="left")
            e1 = ttk.Entry(inp, textvariable=eobt_v, width=7); e1.pack(side="left", padx=(2, 10))
            ttk.Label(inp, text="机型").pack(side="left")
            for cat in ("JET", "PROP"):
                ttk.Radiobutton(inp, text=cat, value=cat, variable=cat_v, command=_recompute).pack(side="left")
            ttk.Label(inp, text="巡航高度").pack(side="left", padx=(10, 0))
            e2 = ttk.Entry(inp, textvariable=fl_v, width=9); e2.pack(side="left", padx=2)
            e1.bind("<KeyRelease>", _recompute); e2.bind("<KeyRelease>", _recompute)

        tv.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        tv.bind("<<TreeviewSelect>>", _pick)
        bar = ttk.Frame(win); bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        ttk.Label(bar, textvariable=status, foreground="#5f6368", wraplength=560).pack(side="left")
        ttk.Button(bar, text="确认并关闭", command=win.destroy).pack(side="right")

        win._f21 = {"eobt": eobt_v, "cat": cat_v, "fl": fl_v, "recompute": _recompute}   # 测试钩子
        _recompute()
        try:                                                # 预选当前生效的那条
            cur = str(getattr(self, "_proc_sel_idx", 0))
            tv.selection_set(cur); tv.focus(cur); tv.see(cur)
        except Exception:
            pass

    def _open_map(self, coords, title=""):
        """弹出独立地图窗口，把指定航路画在真实地图上（tkintermapview，可拖拽 / 缩放）。"""
        if not (_HAS_MAP and coords):
            return
        try:
            majors = set((title or "").split())                      # 换路点 ident（标字）；其余为加密出的中间点
            win = tk.Toplevel(self.root)
            win.title("航路地图 · " + (title or ""))
            win.geometry("1000x680")
            mapw = tkintermapview.TkinterMapView(win, corner_radius=0)
            mapw.pack(fill="both", expand=True)
            mapw.set_tile_server("https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")

            def _dot(d, fill, outline="#ffffff"):
                img = Image.new("RGBA", (d + 2, d + 2), (0, 0, 0, 0))
                ImageDraw.Draw(img).ellipse([1, 1, d, d], fill=fill, outline=outline)
                return ImageTk.PhotoImage(img)
            icon_ap, icon_wp, icon_mid = _dot(12, "#e53935"), _dot(8, "#1a6fdb"), _dot(4, "#6aa8e0")
            win._icons = [icon_ap, icon_wp, icon_mid]                # 挂到窗口保活，防 GC

            pts = [(la, lo) for _id, la, lo in coords]
            if len(pts) >= 2:
                mapw.set_path(pts, color="#1a6fdb", width=2)
            for i, (ident, la, lo) in enumerate(coords):
                if i == 0 or i == len(coords) - 1:                   # 起降机场：红点 + 字
                    mapw.set_marker(la, lo, text=ident, icon=icon_ap, text_color="#b00020", font=("Segoe UI", 9, "bold"))
                elif ident in majors:                               # 换路点：蓝点 + 字
                    mapw.set_marker(la, lo, text=ident, icon=icon_wp, text_color="#0b3d91", font=("Segoe UI", 7, "bold"))
                else:                                               # 中间过渡点：小点 + 小号淡字（放大可看清）
                    mapw.set_marker(la, lo, text=ident, icon=icon_mid,
                                    text_color="#5a7fa0", font=("Segoe UI", 6))
            if len(pts) >= 2:
                lats, lons = [p[0] for p in pts], [p[1] for p in pts]
                mapw.fit_bounding_box((max(lats) + 0.6, min(lons) - 0.6), (min(lats) - 0.6, max(lons) + 0.6))
            elif pts:
                mapw.set_position(pts[0][0], pts[0][1]); mapw.set_zoom(7)
        except Exception as e:
            print(f"⚠️ 打开地图失败: {e}")

    def _on_aircraft_type(self, event=None):
        """机型可搜索下拉：按输入内容过滤候选（匹配 icao / 名字 / 厂商别名）。"""
        if event is not None and event.keysym in ("Up", "Down", "Return", "Escape", "Left", "Right"):
            return
        typed = self.var_aircraft.get().strip().lower()
        if not typed:
            self.cb_aircraft["values"] = self._ac_labels
            return
        filt = [lbl for lbl, _id, blob in self._ac_rows if typed in blob]
        self.cb_aircraft["values"] = filt or self._ac_labels

    def _resolve_aircraft(self):
        """机型框取值：下拉选中的显示串 → 其 SimBrief id；手输则原样返回（供 FlightAware 匹配 + planner 再规范化）。"""
        v = self.var_aircraft.get().strip()
        return self._ac_label_to_id.get(v, v) if v else ""

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

    # Volanta 同步轮询窗口：令牌在 /map 登录后即生成，但 Chromium 把它从内存写到磁盘（我们读的 leveldb）
    # 有 ~30 秒~1 分钟延迟、空闲时甚至更久——这正是旧的 180s 偶尔超时的根因。放宽到 300s，并用弹窗引导。
    _VOLANTA_POLL_CAP = 300

    def _volanta_worker(self):
        try:
            # 1) 快路径：本机已有有效令牌（14 天内同步过）→ 无需浏览器
            if try_fetch_volanta_json_via_session(diag=True):
                self._post(self._volanta_synced)
                return
            # 2) 打开 /map 让用户登录。令牌登录后即生成，但要等它从内存写到磁盘才读得到。
            self._post(self._set_vstatus, "Volanta：已打开浏览器，请在地图页登录…")
            _open_volanta_in_browser()
            self._post(self._volanta_popup_wait)              # 醒目弹窗①：正在等待令牌写入
            waited = 0
            popup2_done = False
            while waited < self._VOLANTA_POLL_CAP and not self._cancel_evt.is_set():
                self._cancel_evt.wait(3)
                waited += 3
                if self._cancel_evt.is_set():
                    break
                if try_fetch_volanta_json_via_session():      # 轮询不开 diag，避免每 3s 刷屏（状态栏+弹窗已反馈）
                    self._post(self._volanta_synced)
                    return
                self._post(self._set_vstatus,
                           f"Volanta：登录后请稍候，正在等待令牌写入磁盘…（{waited}/{self._VOLANTA_POLL_CAP}s）")
                if (not popup2_done) and waited >= 60:        # 约 1 分钟仍无 → 弹窗②升级引导
                    popup2_done = True
                    self._post(self._volanta_popup_flights)
            if self._cancel_evt.is_set():
                self._post(self._set_vstatus, "Volanta：已取消同步。")
            else:
                self._post(self._set_vstatus, "Volanta：等待超时，未更新。")
        except Exception as e:
            self._post(self._set_vstatus, f"Volanta：同步出错（{e}）")
        finally:
            self._post(self._finish_volanta)

    def _volanta_popup_wait(self):
        """醒目弹窗①：告知令牌登录后约 30s~1min 才写盘、程序会自动获取（状态栏易被忽略，故用弹窗）。"""
        try:
            messagebox.showinfo(
                "Volanta 同步",
                "已打开 Volanta 登录页。请点「确定」后在浏览器中登录。\n\n"
                "登录后，令牌需要约 30 秒~1 分钟才会写入磁盘，程序会自动获取，请耐心等待。\n"
                "想更快：在 Volanta 页面上滚动或点几下即可。")
        except Exception:
            pass

    def _volanta_popup_flights(self):
        """醒目弹窗②：约 1 分钟仍未获取到时升级引导——去航班页刷新 + 滚动，催令牌尽快写盘。"""
        try:
            messagebox.showinfo(
                "Volanta 同步 · 仍在等待",
                "还没获取到登录令牌。请在浏览器打开 Volanta 的「航班 / Flights」页，\n"
                "刷新该页并向下滚动飞行记录列表——这会促使令牌尽快写入磁盘。\n\n"
                "程序仍在后台自动获取，关闭本提示不影响。")
        except Exception:
            pass

    def _volanta_synced(self):
        flown, vmeta = load_volanta_flown_routes()
        self.flown_counts = flown
        self._apply_volanta(flown, vmeta)
        print(f"✅ Volanta 同步完成：{len(flown)} 条有向航线。")
        try:                                                  # 成功也给个醒目反馈（同步多在用户切到浏览器时完成）
            messagebox.showinfo("Volanta 同步", f"✅ 同步完成：已读取 {len(flown)} 条有向航线。")
        except Exception:
            pass

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

    # ========== F23：机场运行规则编辑器（operation.json；增删改查在内存工作副本，仅「保存」落盘）==========
    def _open_ops_editor(self, _e=None):
        """可视化编辑各机场运行规则（时段+风 → 离/进场跑道 + SID/STAR/IAP），存运行目录 operation.json。"""
        if getattr(self, "_ops_win", None) is not None:
            try:
                self._ops_win.lift(); return
            except Exception:
                self._ops_win = None
        if not self.dat_path:
            messagebox.showwarning("运行规则", "导航数据未就绪，暂时无法编辑（需 CIFP 程序数据）。")
            return
        self._ops_all = operations.load_operations()
        self._ops_icao = None
        self._ops_rules = []
        self._ops_sel = None
        self._ops_dirty = False
        self._ops_form_dirty = False
        self._ops_loading = False
        self._ops_drag = None

        win = tk.Toplevel(self.root)
        self._ops_win = win
        win.title("⚙️ 机场运行规则编辑器")
        win.geometry("%dx%d" % (int(1100 * self.scale), int(700 * self.scale)))
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._ops_on_close)
        win.columnconfigure(0, weight=1); win.rowconfigure(1, weight=1)

        # 顶部：机场选择（查·机场级）
        top = ttk.Frame(win, padding=(10, 8)); top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="机场 ICAO").pack(side="left")
        self._ops_var_icao = tk.StringVar()
        self._ops_cb_airport = ttk.Combobox(top, textvariable=self._ops_var_icao, width=10)
        self._ops_cb_airport.pack(side="left", padx=(4, 6))
        self._ops_cb_airport.bind("<<ComboboxSelected>>",
                                  lambda e: self._ops_load_airport(self._ops_var_icao.get()))
        ttk.Button(top, text="载入", command=lambda: self._ops_load_airport(self._ops_var_icao.get())).pack(side="left")
        self._ops_var_existing = tk.StringVar()
        ttk.Label(top, textvariable=self._ops_var_existing, foreground="#5f6368", wraplength=640).pack(side="left", padx=10)

        # 中部：左规则表 + 右详情
        body = ttk.Frame(win, padding=(10, 0)); body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1); body.rowconfigure(0, weight=1)

        left = ttk.Frame(body); left.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        cols = [("name", "名称", 116), ("time", "时段", 84), ("days", "星期", 54), ("wind", "风", 76),
                ("wx", "天气", 90), ("dep", "离场", 58), ("arr", "进场", 58)]
        tv = ttk.Treeview(left, columns=[c[0] for c in cols], show="headings", height=16, selectmode="browse")
        for k, t, w in cols:
            tv.heading(k, text=t); tv.column(k, width=int(w * self.scale), anchor="w")
        tv.grid(row=0, column=0, sticky="ns")
        tv.bind("<<TreeviewSelect>>", self._ops_on_tree_select)
        tv.bind("<ButtonPress-1>", self._ops_drag_start, add="+")     # 拖拽排序（保持"好天→恶天"成对相邻；匹配为均等+恶天顺位下移，非全局优先级）
        tv.bind("<B1-Motion>", self._ops_drag_motion, add="+")
        tv.bind("<ButtonRelease-1>", self._ops_drag_drop, add="+")
        self._ops_tree = tv
        lb = ttk.Frame(left); lb.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(lb, text="＋ 新增", command=self._ops_add_rule).pack(side="left")
        ttk.Button(lb, text="📋 复制", command=self._ops_dup_rule).pack(side="left", padx=6)
        ttk.Button(lb, text="－ 删除", command=self._ops_delete_rule).pack(side="left")
        ttk.Label(left, text="（拖拽规则可调优先级顺序：上→下先匹配）", foreground="#9aa0a6").grid(
            row=2, column=0, sticky="w")

        det = ttk.LabelFrame(body, text="规则详情", padding=8); det.grid(row=0, column=1, sticky="nsew")
        det.columnconfigure(0, weight=1); det.rowconfigure(1, weight=1)
        # 条件区
        cf = ttk.Frame(det); cf.grid(row=0, column=0, sticky="ew", pady=(0, 6)); cf.columnconfigure(1, weight=1)
        ttk.Label(cf, text="名称").grid(row=0, column=0, sticky="w")
        self._ops_var_name = tk.StringVar()
        e_name = ttk.Entry(cf, textvariable=self._ops_var_name); e_name.grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        self._ops_e_name = e_name
        ttk.Label(cf, text="时段(JST)").grid(row=1, column=0, sticky="w")
        self._ops_var_time = tk.StringVar()
        ttk.Entry(cf, textvariable=self._ops_var_time).grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        ttk.Label(cf, text="如 1500-1900，逗号分隔多段；空=全天不限", foreground="#9aa0a6").grid(
            row=2, column=1, sticky="w", padx=4)
        ttk.Label(cf, text="星期").grid(row=3, column=0, sticky="w")
        self._ops_var_days = [tk.BooleanVar() for _ in range(7)]
        dfr = ttk.Frame(cf); dfr.grid(row=3, column=1, sticky="w", padx=4)
        for _i, _lab in enumerate("一二三四五六日"):
            ttk.Checkbutton(dfr, text=_lab, variable=self._ops_var_days[_i],
                            command=lambda: setattr(self, "_ops_form_dirty", True)).pack(side="left")
        ttk.Label(dfr, text="（全不勾=每天；深夜运用常按星期几不同）", foreground="#9aa0a6").pack(side="left", padx=(6, 0))
        ttk.Label(cf, text="换向门槛").grid(row=4, column=0, sticky="w")
        self._ops_var_refrwy = tk.StringVar(); self._ops_var_wkind = tk.StringVar(value="顺风"); self._ops_var_tw = tk.StringVar()
        wf = ttk.Frame(cf); wf.grid(row=4, column=1, sticky="w", padx=4)
        ttk.Label(wf, text="相对跑道").pack(side="left")
        self._ops_cb_refrwy = ttk.Combobox(wf, textvariable=self._ops_var_refrwy, width=6, state="readonly")
        self._ops_cb_refrwy.pack(side="left", padx=(2, 6))
        self._ops_cb_wkind = ttk.Combobox(wf, textvariable=self._ops_var_wkind, width=5, state="readonly",
                                           values=["顺风", "逆风", "侧风"])
        self._ops_cb_wkind.pack(side="left")
        ttk.Label(wf, text="≥").pack(side="left", padx=(4, 0))
        ttk.Entry(wf, textvariable=self._ops_var_tw, width=4).pack(side="left", padx=2)
        ttk.Label(wf, text="节 时触发").pack(side="left")
        ttk.Label(cf, text="风分量相对该参照跑道算：顺风超→换向（南風運用：相对 34R 顺风≥10）；侧风超→换落跑道（都心：相对 16L 侧风≥15 改落 22/23）；逆风超→其它例外；留空=默认构型",
                  foreground="#9aa0a6", wraplength=460).grid(row=5, column=1, sticky="w", padx=4)
        ttk.Label(cf, text="好天门槛").grid(row=6, column=0, sticky="w")
        self._ops_var_ceil = tk.StringVar(); self._ops_var_ceilcov = tk.StringVar(value="SCT"); self._ops_var_vis = tk.StringVar()
        wxf = ttk.Frame(cf); wxf.grid(row=6, column=1, sticky="w", padx=4)
        ttk.Label(wxf, text="云底 ≥").pack(side="left")
        ttk.Entry(wxf, textvariable=self._ops_var_ceil, width=6).pack(side="left", padx=2)
        ttk.Label(wxf, text="ft ·").pack(side="left")
        self._ops_cb_ceilcov = ttk.Combobox(wxf, textvariable=self._ops_var_ceilcov, width=5, state="readonly",
                                             values=["FEW", "SCT", "BKN", "OVC"])
        self._ops_cb_ceilcov.pack(side="left", padx=(2, 0))
        ttk.Label(wxf, text="及以上算云底 · 能见度 ≥").pack(side="left", padx=(2, 0))
        ttk.Entry(wxf, textvariable=self._ops_var_vis, width=6).pack(side="left", padx=2)
        ttk.Label(wxf, text="m").pack(side="left")
        ttk.Label(cf, text="好天=天气至少这么好才用本规则（如 LDA：云底≥1500ft·SCT 起算[few 不计]·能见度≥6000m）；坏天规则不填、排其后作兜底；留空=不限",
                  foreground="#9aa0a6", wraplength=460).grid(row=7, column=1, sticky="w", padx=4)
        # 跑道/程序多选区
        lists = ttk.Frame(det); lists.grid(row=1, column=0, sticky="nsew")
        lists.columnconfigure(0, weight=1); lists.columnconfigure(1, weight=1); lists.rowconfigure(0, weight=1)
        depf = ttk.LabelFrame(lists, text="离场", padding=4); depf.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ttk.Label(depf, text="跑道").pack(anchor="w")
        self._ops_lb_dep_rwy = self._ops_make_lb(depf, 5)
        ttk.Label(depf, text="SID（可搜索 · 点击切换）").pack(anchor="w")
        self._ops_lb_dep_sid = self._ops_make_lb(depf, 7, filterable=True)
        arrf = ttk.LabelFrame(lists, text="到达", padding=4); arrf.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        ttk.Label(arrf, text="跑道").pack(anchor="w")
        self._ops_lb_arr_rwy = self._ops_make_lb(arrf, 3)
        ttk.Label(arrf, text="STAR（可搜索）").pack(anchor="w")
        self._ops_lb_arr_star = self._ops_make_lb(arrf, 4, filterable=True)
        ttk.Label(arrf, text="IAP 仪表进近（可搜索）").pack(anchor="w")
        self._ops_lb_arr_iap = self._ops_make_lb(arrf, 5, filterable=True)
        ttk.Button(det, text="✓ 应用到所选规则", command=self._ops_apply_rule).grid(row=2, column=0, sticky="e", pady=(6, 0))

        # 表单任一变化 → 标脏（供切换/保存时提交）
        for v in (self._ops_var_name, self._ops_var_time, self._ops_var_refrwy, self._ops_var_wkind,
                  self._ops_var_tw, self._ops_var_ceil, self._ops_var_ceilcov, self._ops_var_vis):
            v.trace_add("write", lambda *_a: setattr(self, "_ops_form_dirty", True))
        # Listbox 点选的标脏在 _ops_lb_on_select 里做（搜索框改变不算编辑，不标脏）

        # 底部：保存 / 关闭
        bar = ttk.Frame(win, padding=(10, 8)); bar.grid(row=2, column=0, sticky="ew")
        ttk.Button(bar, text="💾 保存到 operation.json", command=self._ops_save).pack(side="left")
        ttk.Button(bar, text="关闭", command=self._ops_on_close).pack(side="right")

        self._ops_refresh_existing()
        win._ops = {"load": self._ops_load_airport, "add": self._ops_add_rule,
                    "apply": self._ops_apply_rule, "delete": self._ops_delete_rule,
                    "dup": self._ops_dup_rule, "move": self._ops_move_rule,
                    "save": self._ops_save, "to_rule": self._ops_form_to_rule,
                    "to_form": self._ops_rule_to_form, "select": self._ops_select_rule}

    def _ops_make_lb(self, parent, height, filterable=False):
        """多选 Listbox（`selectmode='multiple'` 点击即切换、免 ctrl）+ 可选模糊搜索框。
        用【值集合 `_sel`】模型（与显示解耦）——搜索/过滤时隐藏的已选项也不会丢。`exportselection=False` 让多个 Listbox 同时保留选择。"""
        holder = ttk.Frame(parent); holder.pack(fill="both", expand=True, pady=(0, 4))
        lbx = tk.Listbox(holder, selectmode="multiple", exportselection=False, height=height,
                         font=("Consolas", 9), activestyle="none")
        lbx._all, lbx._sel, lbx._shown, lbx._fvar = [], set(), [], None
        if filterable:
            lbx._fvar = tk.StringVar()
            ttk.Entry(holder, textvariable=lbx._fvar).pack(fill="x")   # 搜索框在列表上方
            lbx._fvar.trace_add("write", lambda *_a, _lb=lbx: self._ops_lb_render(_lb))
        lbx.pack(fill="both", expand=True)
        lbx.bind("<<ListboxSelect>>", lambda e, _lb=lbx: self._ops_lb_on_select(_lb))
        return lbx

    def _ops_lb_render(self, lb):
        """按搜索框重画（模糊子串匹配显示或存值），并从 `_sel` 恢复勾选高亮。程序化选择不触发 on_select。"""
        filt = lb._fvar.get().strip().lower() if lb._fvar is not None else ""
        lb.delete(0, "end")
        shown = []
        for disp, val in lb._all:
            if not filt or filt in disp.lower() or filt in val.lower():
                lb.insert("end", disp); shown.append(val)
        lb._shown = shown
        for i, val in enumerate(shown):
            if val in lb._sel:
                lb.selection_set(i)

    def _ops_lb_on_select(self, lb):
        """用户点选变化 → 同步 `_sel`：只改当前显示项，隐藏的已选项保留。"""
        shown = lb._shown
        now = {shown[i] for i in lb.curselection() if i < len(shown)}
        lb._sel = (lb._sel - set(shown)) | now
        self._ops_form_dirty = True

    def _ops_fill_lb(self, lb, pairs):
        """pairs=[(显示, 存值)…] → 重置候选与选择、清搜索、重画。"""
        lb._all = list(pairs)
        lb._sel = set()
        if lb._fvar is not None:
            lb._fvar.set("")
        self._ops_lb_render(lb)

    def _ops_read_lb(self, lb):
        """已选存值列表（保持候选原顺序）。"""
        return [val for _d, val in lb._all if val in lb._sel]

    def _ops_select_lb(self, lb, values):
        """按存值集合设选择、清搜索、重画（换规则时避免旧过滤藏住新项）。"""
        lb._sel = set(values or [])
        if lb._fvar is not None:
            lb._fvar.set("")
        self._ops_lb_render(lb)

    def _ops_commit_current(self):
        """把当前机场工作副本写回全量 dict（切机场/保存前调，隔离多机场）。"""
        if self._ops_icao:
            self._ops_all[self._ops_icao] = {"rules": list(self._ops_rules)}

    def _ops_refresh_existing(self):
        aps = operations.airports(self._ops_all)
        self._ops_cb_airport["values"] = aps
        self._ops_var_existing.set(("已有规则：" + " ".join(aps)) if aps else "（暂无机场规则；键入 ICAO + 载入即可新建）")

    def _ops_load_airport(self, icao):
        """查·机场级：提交当前机场 → 读该机场 CIFP 候选填四类 Listbox + 载入其规则。"""
        icao = (icao or "").strip().upper()
        if not icao:
            return
        self._ops_commit_current()
        self._ops_icao = icao
        self._ops_var_icao.set(icao)
        try:
            rwys = sorted(procedures._parse_runways(icao, self.dat_path), key=procedures._rw_sort_key)
        except Exception:
            rwys = []
        try:
            procs = procedures.enumerate_procedures(icao, self.dat_path)
            sids = sorted(procs.get("SID", {}).keys())
            stars = sorted(procs.get("STAR", {}).keys())
        except Exception:
            sids = stars = []
        try:
            iaps = procedures.enumerate_approaches(icao, self.dat_path)
        except Exception:
            iaps = []
        self._ops_fill_lb(self._ops_lb_dep_rwy, [(r.replace("RW", ""), r) for r in rwys])
        self._ops_fill_lb(self._ops_lb_dep_sid, [(s, s) for s in sids])
        self._ops_fill_lb(self._ops_lb_arr_rwy, [(r.replace("RW", ""), r) for r in rwys])
        self._ops_fill_lb(self._ops_lb_arr_star, [(s, s) for s in stars])
        self._ops_fill_lb(self._ops_lb_arr_iap, [(a["name"], a["ident"]) for a in iaps])
        self._ops_cb_refrwy["values"] = [""] + [r.replace("RW", "") for r in rwys]   # 换向门槛的参照跑道
        if not (rwys or sids or stars or iaps):
            print(f"ℹ️ {icao} 无 CIFP 程序数据，候选为空（仍可建规则、但选不出跑道/程序）。")
        self._ops_rules = operations.airport_rules(self._ops_all, icao)
        self._ops_sel = None
        self._ops_clear_form()
        self._ops_refresh_tree()

    def _ops_row_values(self, rule):
        cond = rule.get("cond", {}) or {}
        tm = ",".join(cond.get("time_jst") or []) or "全天"
        days = cond.get("days") or []
        dtxt = "".join("一二三四五六日"[d - 1] for d in sorted(days) if 1 <= d <= 7) if days else "每天"
        ref = (cond.get("ref_runway") or "").replace("RW", "")
        wmin = cond.get("wind_min_kt")
        klabel = {"headwind": "逆风", "crosswind": "侧风"}.get(cond.get("wind_kind"), "顺风")
        wind = ("%s≥%s@%s" % (klabel, wmin, ref)) if (wmin is not None and ref) else "默认"
        wxp = []
        if cond.get("ceiling_min_ft") is not None:
            wxp.append("云≥%s" % cond["ceiling_min_ft"])
        if cond.get("visibility_min_m") is not None:
            wxp.append("能≥%s" % cond["visibility_min_m"])
        wx = "·".join(wxp) or "-"
        dep = ",".join(r.replace("RW", "") for r in (rule.get("dep", {}).get("runways") or [])) or "-"
        arr = ",".join(r.replace("RW", "") for r in (rule.get("arr", {}).get("runways") or [])) or "-"
        return [rule.get("name") or "(未命名)", tm, dtxt, wind, wx, dep, arr]

    def _ops_refresh_tree(self):
        """查·规则级：按工作副本重绘 Treeview（程序化选择时置 _ops_loading 避免回调递归）。"""
        tv = self._ops_tree
        self._ops_loading = True
        try:
            tv.delete(*tv.get_children())
            for i, rule in enumerate(self._ops_rules):
                tv.insert("", "end", iid=str(i), values=self._ops_row_values(rule))
            if self._ops_sel is not None and 0 <= self._ops_sel < len(self._ops_rules):
                tv.selection_set(str(self._ops_sel)); tv.focus(str(self._ops_sel))
        finally:
            self._ops_loading = False

    def _ops_on_tree_select(self, _e=None):
        if self._ops_loading:
            return
        f = self._ops_tree.focus()
        if f == "":
            return
        new = int(f)
        if self._ops_sel is not None and self._ops_sel != new and self._ops_form_dirty \
                and 0 <= self._ops_sel < len(self._ops_rules):     # 切换前静默提交旧规则的未应用改动
            rule = self._ops_form_to_rule(warn=False)
            if rule is not None:
                self._ops_rules[self._ops_sel] = rule
                self._ops_tree.item(str(self._ops_sel), values=self._ops_row_values(rule))
                self._ops_dirty = True
        self._ops_select_rule(new)

    def _ops_select_rule(self, idx):
        """选中并回填第 idx 条（供 Treeview 回调与测试钩子共用）。"""
        if not (0 <= idx < len(self._ops_rules)):
            return
        self._ops_sel = idx
        self._ops_rule_to_form(self._ops_rules[idx])
        self._ops_form_dirty = False

    def _ops_add_rule(self):
        """增：追加空白规则、选中、聚焦名称。"""
        if not self._ops_icao:
            messagebox.showinfo("运行规则", "请先在上方键入机场 ICAO 并「载入」。")
            return
        blank = {"name": "新规则",
                 "cond": {"time_jst": [], "days": [], "ref_runway": None, "wind_kind": "tailwind", "wind_min_kt": None,
                          "ceiling_min_ft": None, "ceiling_cover": None, "visibility_min_m": None},
                 "dep": {"runways": [], "sids": []}, "arr": {"runways": [], "stars": [], "iaps": []}}
        self._ops_rules.append(blank)
        self._ops_sel = len(self._ops_rules) - 1
        self._ops_dirty = True
        self._ops_refresh_tree()
        self._ops_rule_to_form(blank)
        self._ops_form_dirty = False
        try:
            self._ops_e_name.focus_set(); self._ops_e_name.selection_range(0, "end")
        except Exception:
            pass

    def _ops_dup_rule(self):
        """复用：把所选规则整条深拷贝一份（含全部条件/程序），改名"…副本"、插在其后，供微调——
        免得相同的跑道/SID/STAR/IAP 每条都重输。"""
        if self._ops_sel is None:
            messagebox.showinfo("运行规则", "请先选中一条规则再复制。")
            return
        if self._ops_form_dirty:                              # 先提交当前表单编辑，复制所见
            r = self._ops_form_to_rule(warn=True)
            if r is None:
                return
            self._ops_rules[self._ops_sel] = r
        dup = copy.deepcopy(self._ops_rules[self._ops_sel])
        dup["name"] = (dup.get("name") or "规则") + " 副本"
        self._ops_rules.insert(self._ops_sel + 1, dup)
        self._ops_sel += 1
        self._ops_dirty = True
        self._ops_refresh_tree()
        self._ops_rule_to_form(dup)
        self._ops_form_dirty = False
        try:
            self._ops_e_name.focus_set(); self._ops_e_name.selection_range(0, "end")
        except Exception:
            pass

    def _ops_move_rule(self, from_idx, to_idx):
        """把第 from_idx 条规则移到 to_idx（拖拽排序 / 测试共用）。规则顺序 = 优先级（上→下先匹配）。"""
        n = len(self._ops_rules)
        if not (0 <= from_idx < n):
            return
        to_idx = max(0, min(int(to_idx), n - 1))
        if to_idx == from_idx:
            return
        self._ops_rules.insert(to_idx, self._ops_rules.pop(from_idx))
        self._ops_sel = to_idx
        self._ops_dirty = True
        self._ops_refresh_tree()

    def _ops_drag_start(self, event):
        self._ops_drag = self._ops_tree.identify_row(event.y) or None

    def _ops_drag_motion(self, event):
        drag = self._ops_drag
        if not drag:
            return
        tv = self._ops_tree
        tgt = tv.identify_row(event.y)
        if tgt and tgt != drag:
            tv.move(drag, "", tv.index(tgt))                  # 实时视觉移动（iid 不变）

    def _ops_drag_drop(self, event):
        drag = self._ops_drag
        self._ops_drag = None
        if not drag:
            return
        try:                                                  # iid=原下标 str；拖后其视觉位置即目标序
            to_idx = list(self._ops_tree.get_children("")).index(drag)
        except ValueError:
            return
        self._ops_move_rule(int(drag), to_idx)                # 据此重排列表并刷新（iid 复位）

    def _ops_apply_rule(self, _e=None):
        """改：把表单收进所选规则并刷新该行。"""
        if self._ops_sel is None:
            messagebox.showinfo("运行规则", "请先在左侧选中一条规则（或「＋ 新增规则」）。")
            return
        rule = self._ops_form_to_rule(warn=True)
        if rule is None:
            return
        self._ops_rules[self._ops_sel] = rule
        self._ops_tree.item(str(self._ops_sel), values=self._ops_row_values(rule))
        self._ops_dirty = True
        self._ops_form_dirty = False

    def _ops_delete_rule(self):
        """删：确认后移除所选规则。"""
        if self._ops_sel is None:
            return
        if not messagebox.askyesno("删除规则", "删除所选运行规则？"):
            return
        del self._ops_rules[self._ops_sel]
        self._ops_sel = None
        self._ops_dirty = True
        self._ops_clear_form()
        self._ops_refresh_tree()

    def _ops_parse_int(self, s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None

    def _ops_form_to_rule(self, warn=True):
        """收表单 → 规则 dict；时段/风向不合法 → 返回 None（warn=True 时弹提示）。"""
        name = self._ops_var_name.get().strip() or "新规则"
        times = []
        for seg in self._ops_var_time.get().replace("，", ",").split(","):
            seg = seg.strip().replace("：", ":")
            if not seg:
                continue
            p = seg.split("-")
            if len(p) != 2 or timed.parse_hhmm(p[0]) is None or timed.parse_hhmm(p[1]) is None:
                if warn:
                    messagebox.showwarning("时段格式", "时段应为 HHMM-HHMM（如 1500-1900），多段用逗号。\n无法解析：%s" % seg)
                return None
            times.append("%s-%s" % (p[0].strip(), p[1].strip()))
        days = [i + 1 for i, v in enumerate(self._ops_var_days) if v.get()]   # 1=周一…7=周日(ISO)
        if len(days) == 7:
            days = []                                        # 全选 = 每天，规整为空
        ref = self._ops_var_refrwy.get().strip().upper()
        ref_rwy = ("RW" + ref) if ref else None
        wkind = {"逆风": "headwind", "侧风": "crosswind"}.get(self._ops_var_wkind.get().strip(), "tailwind")
        wmin = self._ops_parse_int(self._ops_var_tw.get())
        if wmin is not None and ref_rwy is None:
            if warn:
                messagebox.showwarning("换向门槛", "填了风分量节数就得选一条参照跑道（顺/逆/侧风分量相对它算）。")
            return None
        ceil = self._ops_parse_int(self._ops_var_ceil.get())
        vis = self._ops_parse_int(self._ops_var_vis.get())
        ceilcov = (self._ops_var_ceilcov.get().strip().upper() or "SCT") if ceil is not None else None
        return {"name": name,
                "cond": {"time_jst": times, "days": days, "ref_runway": ref_rwy, "wind_kind": wkind, "wind_min_kt": wmin,
                         "ceiling_min_ft": ceil, "ceiling_cover": ceilcov, "visibility_min_m": vis},
                "dep": {"runways": self._ops_read_lb(self._ops_lb_dep_rwy),
                        "sids": self._ops_read_lb(self._ops_lb_dep_sid)},
                "arr": {"runways": self._ops_read_lb(self._ops_lb_arr_rwy),
                        "stars": self._ops_read_lb(self._ops_lb_arr_star),
                        "iaps": self._ops_read_lb(self._ops_lb_arr_iap)}}

    def _ops_rule_to_form(self, rule):
        """回填表单。"""
        cond = rule.get("cond", {}) or {}
        self._ops_var_name.set(rule.get("name") or "")
        self._ops_var_time.set(",".join(cond.get("time_jst") or []))
        _dset = set(cond.get("days") or [])
        for _i, _v in enumerate(self._ops_var_days):
            _v.set((_i + 1) in _dset)
        self._ops_var_refrwy.set((cond.get("ref_runway") or "").replace("RW", ""))
        self._ops_var_wkind.set({"headwind": "逆风", "crosswind": "侧风"}.get(cond.get("wind_kind"), "顺风"))
        self._ops_var_tw.set("" if cond.get("wind_min_kt") is None else str(cond.get("wind_min_kt")))
        self._ops_var_ceil.set("" if cond.get("ceiling_min_ft") is None else str(cond.get("ceiling_min_ft")))
        self._ops_var_ceilcov.set(cond.get("ceiling_cover") or "SCT")
        self._ops_var_vis.set("" if cond.get("visibility_min_m") is None else str(cond.get("visibility_min_m")))
        dep, arr = rule.get("dep", {}) or {}, rule.get("arr", {}) or {}
        self._ops_select_lb(self._ops_lb_dep_rwy, dep.get("runways"))
        self._ops_select_lb(self._ops_lb_dep_sid, dep.get("sids"))
        self._ops_select_lb(self._ops_lb_arr_rwy, arr.get("runways"))
        self._ops_select_lb(self._ops_lb_arr_star, arr.get("stars"))
        self._ops_select_lb(self._ops_lb_arr_iap, arr.get("iaps"))
        self._ops_form_dirty = False

    def _ops_clear_form(self):
        for v in (self._ops_var_name, self._ops_var_time, self._ops_var_refrwy, self._ops_var_tw,
                  self._ops_var_ceil, self._ops_var_vis):
            v.set("")
        for v in self._ops_var_days:
            v.set(False)
        self._ops_var_wkind.set("顺风")
        self._ops_var_ceilcov.set("SCT")
        for lbx in (self._ops_lb_dep_rwy, self._ops_lb_dep_sid, self._ops_lb_arr_rwy,
                    self._ops_lb_arr_star, self._ops_lb_arr_iap):
            self._ops_select_lb(lbx, [])
        self._ops_form_dirty = False

    def _ops_save(self):
        """持久化：提交当前表单 + 当前机场 → 整体原子写 operation.json（剔除空机场）。"""
        if self._ops_sel is not None and self._ops_form_dirty:
            rule = self._ops_form_to_rule(warn=True)
            if rule is None:
                return
            self._ops_rules[self._ops_sel] = rule
            self._ops_tree.item(str(self._ops_sel), values=self._ops_row_values(rule))
            self._ops_form_dirty = False
        self._ops_commit_current()
        if operations.save_operations(self._ops_all):
            self._ops_all = operations._prune(self._ops_all)     # 内存同步剔除空机场
            self._ops_dirty = False
            self._ops_refresh_existing()
            n = len(operations.airports(self._ops_all))
            print(f"💾 运行规则已保存到 operation.json（{n} 个机场）。")
            messagebox.showinfo("运行规则", f"已保存到 operation.json（{n} 个机场）。")

    def _ops_on_close(self):
        if self._ops_dirty or self._ops_form_dirty:
            ans = messagebox.askyesnocancel("未保存", "有未保存的规则改动，保存后关闭？\n（是=保存并关闭 / 否=直接关闭 / 取消=返回）")
            if ans is None:
                return
            if ans:
                self._ops_save()
        try:
            self._ops_win.destroy()
        except Exception:
            pass
        self._ops_win = None


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
