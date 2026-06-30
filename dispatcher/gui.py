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
from .aircraft import aircraft_choices
from .router import generate_route, route_geometry, route_length_nm

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

        # 受 _refresh_controls 统一启停的表单控件
        self._form_widgets = [e_dep, e_dest, e_air, self.cb_aircraft, e_tm, e_rw, e_dmin, e_dmax, chk_strict, self.chk_auto]
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

            # 各 AIP 航路：长度（与 find_aip_route 同序）+ 地图航点（每条一份，可分别打开窗口）
            aip_dists = aip_maps = None
            gen_map = None
            if route:
                matched = [r for r in self.aip_data
                           if len(r) > 5 and r[0].strip().upper() == dep_obj.code and r[1].strip().upper() == arr_obj.code]
                aip_dists, aip_maps = [], []
                for r in matched:
                    rs = r[5].strip()
                    try:
                        pts = route_geometry(dep_obj, arr_obj, rs, self.dat_path) if rs else None
                    except Exception:
                        pts = None
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
        self._last_simbrief_url = None

    def _show_error(self, msg):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("end", f"❌ 发生错误：{msg}\n")
        self.result.configure(state="disabled")

    def _render_plan(self, plan):
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
                ins(f"  [{i}] ", "muted"); ins(f"{rr}\n", "aip")
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
