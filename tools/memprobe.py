# ================= 内存监视器（开发工具）：脚本化跑一遍真程序，逐步记录各进程的内存 =================
# 用法（在项目根目录，仅 Windows）：python tools/memprobe.py [--repeats N] [--out 输出.jsonl]
#
# 在本进程里起真程序（源码模式），按固定场景驱动各功能，每一步记录：
#   · 主程序的 Python 进程 与它的 flet.exe（Flutter 客户端）；
#   · 地图子窗口那棵进程树（python --map + 它自己的 flet.exe，见 dispatcher/ui_flet/map_window.py）。
# 指标是【私有工作集】（≈ 任务管理器「内存」列），另记私有提交量；主程序 flet.exe 另记 GPU 共享内存
# （核显没有独立显存，GPU 缓存全落在系统内存里、记在进程头上——这正是程序内地图曾经吃掉 +400 MB 的原因）。
#
# 场景：主界面空闲 → 规划 RJTT→RJFF（9 条 AIP 航路，自动弹 AIP 弹窗）→ 关弹窗 → 打开地图子窗口
#      （DISPATCHER_SELFTEST_MAPTOUR：切遍 9 个标签页 + 飞 14 个视角后自行关窗；记它的峰值与关窗后主程序是否上涨）
#      → 规则编辑器 RJTT 点 12 条规则 → 再规划 N 对机场（默认 6）→ 空闲。
# 隔离：Volanta 自动同步打桩关闭（不读浏览器）；规则编辑器只点不存；运行时缓存照常写进运行目录（同平时源码运行）。
# 会联网（AIP 航路下载、FlightAware、METAR），会弹出主窗口与地图窗口——跑完自动关闭。
#
# 改动 UI / 渲染 / 缓存后重跑，对照上一次的 JSONL：主程序应稳定在约 300 MB（Python ~50 + flet.exe ~250），
# 地图子窗口重度浏览约 650–800 MB、关窗归零。它驱动的是 app 的内部方法（_on_plan_click / _open_map / ops_view…），
# 这些改名时这里要跟着改。

import argparse
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes as W

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MB = 1024 * 1024
OUT = sys.__stdout__                     # 程序会把 sys.stdout 接到日志面板，报告走原始 stdout


# ---------- Win32：进程内存 / 进程树（纯 ctypes）----------
class _PMC(ctypes.Structure):            # PROCESS_MEMORY_COUNTERS_EX2
    _fields_ = [("cb", W.DWORD), ("PageFaultCount", W.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t), ("PrivateWorkingSetSize", ctypes.c_size_t),
                ("SharedCommitUsage", ctypes.c_ulonglong)]


class _PE(ctypes.Structure):             # PROCESSENTRY32W
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", W.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if _k32:
    _k32.OpenProcess.restype = W.HANDLE
    _k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    _k32.CloseHandle.argtypes = [W.HANDLE]
    _k32.K32GetProcessMemoryInfo.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = W.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    _k32.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(_PE)]
    _k32.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(_PE)]


def mem(pid):
    """→ {priv_ws, ws, commit}（MB）；拿不到 → None。"""
    if not pid:
        return None
    h = _k32.OpenProcess(0x1000 | 0x0010, False, pid)        # QUERY_LIMITED_INFORMATION | VM_READ
    if not h:
        return None
    try:
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        if not _k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return None
        return {"priv_ws": round(c.PrivateWorkingSetSize / MB, 1), "ws": round(c.WorkingSetSize / MB, 1),
                "commit": round(c.PrivateUsage / MB, 1)}
    finally:
        _k32.CloseHandle(h)


def processes():
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
    out, e = [], _PE()
    e.dwSize = ctypes.sizeof(_PE)
    ok = _k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile.lower()))
        ok = _k32.Process32NextW(snap, ctypes.byref(e))
    _k32.CloseHandle(snap)
    return out


def split_tree(root):
    """本进程的子孙 → (主程序 flet.exe 的 pid, 地图子窗口那几棵树的全部 pid)。"""
    procs = processes()
    kids = [(p, exe) for p, pp, exe in procs if pp == root]
    main_flet = next((p for p, exe in kids if exe == "flet.exe"), None)
    # 地图子窗口 = 本进程起的 python（源码态由 map_window.launch 用 sys.executable 启动）；采 GPU 用的 powershell 等不算
    maps, frontier = [], [p for p, exe in kids if exe in ("python.exe", "pythonw.exe", "flight_dispatcher.exe")]
    while frontier:
        maps += frontier
        frontier = [p for p, pp, _exe in procs if pp in frontier]
    return main_flet, maps


def gpu_shared(pid):
    """GPU 共享内存（MB）：WMI 类名不随系统语言本地化。拿不到 → None。"""
    if not pid:
        return None
    ps = ("Get-CimInstance -ClassName Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory | "
          "Where-Object { $_.Name -like 'pid_%d_*' } | Measure-Object -Property SharedUsage -Sum | "
          "Select-Object -ExpandProperty Sum" % pid)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
        return round(float(r.stdout.strip()) / MB, 1)
    except Exception:                                   # noqa: BLE001
        return None


# ---------- 记录 ----------
class Recorder:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.t0 = time.time()
        self.peak = {}
        self.lock = threading.Lock()
        self.main_flet = None
        threading.Thread(target=self._sample, daemon=True).start()

    def _sample(self):
        while True:
            fl, _maps = split_tree(os.getpid())
            self.main_flet = self.main_flet or fl
            py, fm = mem(os.getpid()), mem(self.main_flet)
            with self.lock:
                if py and fm:
                    self.peak["main"] = max(self.peak.get("main", 0), py["priv_ws"] + fm["priv_ws"])
            time.sleep(0.25)

    def mark(self, label, **extra):
        py, fm = mem(os.getpid()), mem(self.main_flet)
        with self.lock:
            peak = self.peak.pop("main", 0)
        rec = {"t": round(time.time() - self.t0, 1), "label": label, "python": py, "flet": fm,
               "main_total": round((py or {}).get("priv_ws", 0) + (fm or {}).get("priv_ws", 0), 1),
               "main_peak_since_last": round(peak, 1), "flet_gpu_shared": gpu_shared(self.main_flet)}
        rec.update(extra)
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        more = "  " + "  ".join("%s=%s" % kv for kv in extra.items()) if extra else ""
        OUT.write("%-40s t=%6.1f | python %6.1f | flet.exe %6.1f | 合计 %6.1f（段内峰值 %6.1f）| GPU共享 %s%s\n" % (
            label, rec["t"], (py or {}).get("priv_ws", 0), (fm or {}).get("priv_ws", 0), rec["main_total"],
            rec["main_peak_since_last"], rec["flet_gpu_shared"], more))
        OUT.flush()


# ---------- 驱动 ----------
APP = {}


def ui(fn, *a):
    """从驱动线程把一次控件操作 marshal 回 Flet 事件循环（同 Shell.post）。"""
    page = APP["app"].page

    async def _call():
        r = fn(*a)
        if asyncio.iscoroutine(r):
            r = await r
        page.update()
        return r
    return page.run_task(_call).result(timeout=90)


def plan(dep, dest):
    app = APP["app"]

    def _set():
        app.form.dep.value = dep
        app.form.dest.value = dest
    ui(_set)
    ui(app._on_plan_click)
    t = time.time()
    while app._busy and time.time() - t < 300:
        time.sleep(0.5)
    time.sleep(3)


def close_dialog():
    try:
        ui(APP["app"].page.pop_dialog)
    except Exception:                                   # noqa: BLE001
        pass


def map_tour(rec):
    """开地图子窗口（自动漫游后自关）→ 采它那棵进程树的峰值，直到全部退出。"""
    app = APP["app"]
    os.environ["DISPATCHER_SELFTEST_MAPTOUR"] = "1"         # 子进程继承环境变量
    try:
        t0 = time.time()
        ui(app._open_map, *app._plan_maps[0])
        seen, peak, t_up = set(), 0.0, None
        while time.time() - t0 < 240:
            _fl, maps = split_tree(os.getpid())
            seen |= set(maps)
            tot = sum((mem(p) or {}).get("priv_ws", 0) for p in maps)
            peak = max(peak, tot)
            if t_up is None and tot > 150:
                t_up = time.time() - t0
            if seen and not maps:
                break
            time.sleep(0.5)
        return {"map_window_peak": round(peak, 1), "map_window_up_s": round(t_up or -1, 1),
                "map_window_closed_after_s": round(time.time() - t0, 1)}
    finally:
        os.environ.pop("DISPATCHER_SELFTEST_MAPTOUR", None)


def scenario(rec, repeats):
    app = APP["app"]
    try:
        rec.mark("00 初始化完成")
        time.sleep(6)
        rec.mark("01 主界面空闲")
        plan("RJTT", "RJFF")
        rec.mark("02 规划 RJTT→RJFF + AIP 弹窗")
        close_dialog()
        time.sleep(2)
        rec.mark("03 关掉 AIP 弹窗")
        info = map_tour(rec)
        time.sleep(5)
        rec.mark("04 地图子窗口漫游并关闭后", **info)
        ui(app._on_ops_editor)

        def _load():
            app.ops_view.cb_icao.text = "RJTT"
            app.ops_view._load_airport()
        ui(_load)
        time.sleep(2)
        for i in range(12):
            try:
                ui(app.ops_view._on_row_click, i)
            except Exception:                           # noqa: BLE001
                pass
            time.sleep(0.6)
        time.sleep(3)
        rec.mark("05 规则编辑器 RJTT 点 12 条规则")
        ui(app._pop_view)
        time.sleep(5)
        rec.mark("06 关掉规则编辑器")
        pairs = [("RJCC", "RJTT"), ("RJBB", "ROAH"), ("RJGG", "RJCC"), ("RJOA", "RJTH"),
                 ("RJFK", "RJTT"), ("RJAA", "RJOO")]
        for k in range(repeats):
            d, a = pairs[k % len(pairs)]
            plan(d, a)
            close_dialog()
            rec.mark("%02d 再规划 %s→%s" % (7 + k, d, a))
        time.sleep(10)
        rec.mark("%02d 空闲 10 秒" % (7 + repeats))
    except Exception as e:                              # noqa: BLE001
        OUT.write("❌ 场景中断: %r\n" % (e,))
    finally:
        try:
            ui(app.page.window.destroy)
        except Exception:                               # noqa: BLE001
            os._exit(0)


def main():
    if os.name != "nt":
        sys.exit("memprobe 只支持 Windows（进程内存走 Win32 API）。")
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=6, help="末段再规划几对机场（默认 6）")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "memprobe-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S")))
    args = ap.parse_args()

    import flet as ft
    from dispatcher import controller as C
    from dispatcher.ui_flet import app as A
    C.volanta_auto_enabled = lambda: False                  # 隔离：不做启动自动同步（不读浏览器的本地存储）

    rec = Recorder(args.out)
    orig_init, orig_done = A.DispatcherApp.__init__, A.DispatcherApp._on_init_done

    def _init(self, page):
        APP["app"] = self
        orig_init(self, page)

    def _done(self, st):
        orig_done(self, st)
        threading.Thread(target=scenario, args=(rec, args.repeats), daemon=True).start()
    A.DispatcherApp.__init__ = _init
    A.DispatcherApp._on_init_done = _done
    OUT.write("📏 内存监视器：私有工作集（MB，≈任务管理器「内存」列）；结果另存 %s\n" % args.out)
    ft.run(A.DispatcherApp)
    OUT.write("✅ 完成：%s\n" % args.out)


if __name__ == "__main__":
    main()
