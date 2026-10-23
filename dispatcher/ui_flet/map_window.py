# ================= 航路地图独立窗口（单独进程）=================
# 为什么不在主窗口里画地图（实测，核显机器）：浏览地图后 flet.exe 被 GPU 渲染缓存（Skia 的资源缓存 + 核显驱动的
# 内存池，核显没有独立显存，全记在进程头上）撑大约 +400 MB，而且关掉地图也不归还——Flutter 引擎不会把它还给系统，
# 预编译的 Flet 客户端也不开放缓存预算；瓦片 / 矢量底图、关动画、调 keep_buffer 都压不住。
# 放进单独的进程：关窗 = 进程退出 = 这部分内存一次还清，主程序常驻不受影响；顺带找回 tk 版「同时开几张地图并排比对」。
# 代价：每个开着的地图窗口多一个 Flutter 引擎（空闲约 270 MB，关窗即还），打开约 1–2 秒。
# （浏览器 Leaflet 也测过：同样的浏览动作 Edge +804 / Chrome +700 MB，关页面后只还回一半左右 → 不选。）
#
# 主程序 → launch(routes, index)：航路写成临时 JSON，带 `--map <json> --parent <pid>` 启动自己，不等待。
# 子进程 → run_map_window(argv)：读入 JSON 即删，画 MapView；看门狗盯着主程序，主程序一退出就关窗，不留孤儿窗口。

import inspect
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time

import flet as ft
import flet_map as fmap

from ..config import get_real_run_path, load_map_settings, save_map_settings
from .map_view import MapView
from .theme import apply_theme

_TMP_DIRNAME = "jp-flight-dispatcher-maps"
_TMP_MAX_AGE_S = 86400                   # 子进程没起来等情况残留的 JSON：一天后顺手清掉

# DISPATCHER_SELFTEST_MAPTOUR 的漫游路线（与 tools/memprobe.py 的测量口径一致）：切遍标签页后依次飞这 14 个视角
_TOUR = [(35.55, 139.78, 8), (35.55, 139.78, 10), (34.7, 135.4, 8), (34.7, 135.4, 10),
         (33.6, 130.45, 9), (31.8, 130.7, 8), (26.2, 127.65, 8), (43.0, 141.4, 8),
         (36.0, 138.0, 6), (35.6, 139.8, 12), (35.6, 139.8, 13), (34.5, 136.0, 7),
         (33.5, 132.0, 8), (36.0, 138.0, 5)]


# ---------- 主程序侧 ----------
def _tmp_dir():
    d = os.path.join(tempfile.gettempdir(), _TMP_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _prune(d):
    cutoff = time.time() - _TMP_MAX_AGE_S
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if name.endswith(".json") and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def launch(routes, index=0):
    """routes=[(coords, title[, kinds])]，coords=[(ident, lat, lon[, 所属段])…]，kinds 见 viewmodel.nav_kinds；
    index=默认选中的那条。启动地图子进程后立即返回。"""
    d = _tmp_dir()
    _prune(d)
    path = os.path.join(d, "route-%s-%s.json" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(4)))
    data = {"index": index,
            "routes": [{"title": r[1] or "", "coords": [list(p) for p in r[0]],
                        "kinds": list(r[2]) if len(r) > 2 and r[2] else None} for r in routes]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    # 冻结态 sys.executable 就是 exe 本身；源码态要带上入口脚本（运行目录 = 项目根，见 config.get_real_run_path）
    cmd = [sys.executable] if getattr(sys, "frozen", False) else \
        [sys.executable, os.path.join(get_real_run_path(), "flight_dispatcher.py")]
    cmd += ["--map", path, "--parent", str(os.getpid())]
    try:
        subprocess.Popen(cmd, cwd=get_real_run_path(), close_fds=True)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


# ---------- 子进程侧 ----------
def _arg(argv, name):
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _close(page):
    """优雅关窗（ft.run 随之返回、进程退出）；关不掉就连同 Flutter 客户端一起结束，保证不留孤儿窗口。
    ⚠️ 只 os._exit 不够：实测 Python 侧被 os._exit 掉后，它起的 flet.exe（窗口）会继续开着。"""
    try:
        page.run_task(page.window.destroy).result(timeout=5)
    except Exception:                                   # noqa: BLE001
        if os.name == "nt":                             # /T = 连同子进程（flet.exe）整棵树
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(os.getpid())], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        os._exit(0)


def _watch_parent(page, ppid):
    """主程序退出 → 关掉本地图窗口。Windows 用进程句柄等待（零轮询），其它平台每 2 秒探一次。"""
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x00100000, 0, ppid)        # SYNCHRONIZE；打不开 = 主程序已经不在了
        if h:
            k32.WaitForSingleObject(h, 0xFFFFFFFF)       # INFINITE：主程序退出才返回
            k32.CloseHandle(h)
    else:
        while True:
            time.sleep(2)
            try:
                os.kill(ppid, 0)
            except OSError:
                break
    _close(page)


def _raise_window(ppid):
    """（仅 Windows）地图窗口一出来就提到最前并激活——前提是用户此刻还在本程序里（前台是主窗口或另一个地图窗口）。
    为什么要自己做：Windows 的前台锁只许「用户正在用的那个进程」激活窗口。用户点的是主程序的窗口（主程序的
    flet.exe），地图窗口却属于子进程起的另一个 flet.exe → 激活被拒，窗口只在任务栏闪、压在主窗口后面。
    Flet 的 focused / to_front 也是由地图那个 flet.exe 去申请，一样被拒。
    做法：本线程挂上前台窗口那个线程的输入状态（AttachThreadInput），借它的权限 SetForegroundWindow，随即解开——
    前台是我们自己的主程序窗口，只挂几毫秒。用户若已切到别的程序，就不抢焦点（窗口照常出现，任务栏闪）。"""
    import ctypes
    from ctypes import wintypes as W

    class PE(ctypes.Structure):                         # PROCESSENTRY32W
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("pid", W.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD),
                    ("cntThreads", W.DWORD), ("ppid", W.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", W.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = W.HANDLE
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(PE)]
    k32.CloseHandle.argtypes = [W.HANDLE]
    u32.GetForegroundWindow.restype = W.HWND
    u32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    u32.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
    for f in (u32.IsWindowVisible, u32.GetWindowTextLengthW, u32.SetForegroundWindow, u32.BringWindowToTop):
        f.argtypes = [W.HWND]

    def parents():                                      # {pid: 父 pid}
        snap, e, out = k32.CreateToolhelp32Snapshot(0x2, 0), PE(), {}
        e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[e.pid] = e.ppid
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
        return out

    def owner(h):                                       # 窗口 → (进程号, 线程号)
        pid = W.DWORD()
        tid = u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return pid.value, tid

    def windows_of(pids):                               # 这些进程的可见、有标题的顶层窗口
        found = []

        def cb(h, _lp):
            if owner(h)[0] in pids and u32.IsWindowVisible(h) and u32.GetWindowTextLengthW(h):
                found.append(h)
            return True
        u32.EnumWindows(ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)(cb), 0)
        return found

    hwnd = None
    for _ in range(100):                                # 等窗口出来（最多 10 秒）：本进程起的 flet.exe 的窗口
        par = parents()
        found = windows_of({p for p, pp in par.items() if pp == os.getpid()})
        if found:
            hwnd = found[0]
            break
        time.sleep(0.1)
    if not hwnd:
        return

    def ours(pid):                                      # 前台窗口的进程往上数几层就是主程序 → 用户还在本程序里
        for _ in range(4):
            if pid == ppid:
                return True
            pid = par.get(pid)
        return False

    # 窗口刚出来时 Flet 还在收尾（实测提上来后 0.1 秒前台又回到主窗口）→ 盯 3 秒：前台若又回到本程序的
    # 其它窗口就再提一次，地图窗口稳稳占住前台 0.3 秒即收手；用户切去别的程序了就不再管
    tid, held, deadline = k32.GetCurrentThreadId(), 0, time.time() + 3
    while time.time() < deadline and held < 3:
        fg = u32.GetForegroundWindow()
        if fg == hwnd:
            held += 1
        else:
            held = 0
            pid, fg_tid = owner(fg) if fg else (0, 0)
            if not fg or not ours(pid):
                return
            if u32.AttachThreadInput(tid, fg_tid, True):
                try:
                    u32.SetForegroundWindow(hwnd)
                    u32.BringWindowToTop(hwnd)
                finally:
                    u32.AttachThreadInput(tid, fg_tid, False)
        time.sleep(0.1)


def _ui(page, fn, *a):
    """测试漫游用：从后台线程把一次控件改动 marshal 回 Flet 事件循环（同 Shell.post 的道理）。"""
    async def _call():
        r = fn(*a)
        if inspect.isawaitable(r):
            await r
        page.update()
    page.run_task(_call).result(timeout=60)


def _tour(page, mv):
    """DISPATCHER_SELFTEST_MAPTOUR：等 10 秒 → 切遍标签页（每页 3 秒）→ 回第一页飞 14 个视角 → 自行关窗。"""
    time.sleep(10)
    if mv.tabs is not None:
        for i in list(range(1, len(mv.maps))) + [0]:
            _ui(page, setattr, mv.tabs, "selected_index", i)
            time.sleep(3)
    for lat, lon, z in _TOUR:
        _ui(page, mv.maps[0].move_to, fmap.MapLatitudeLongitude(lat, lon), z)
        time.sleep(2.5)
    time.sleep(2)
    _close(page)


def run_map_window(argv):
    """`flight_dispatcher.py --map <json> --parent <pid>` 的入口：只画地图，不跑 init_app、不接管 stdout。"""
    path = _arg(argv, "--map")
    try:
        ppid = int(_arg(argv, "--parent") or 0)
    except ValueError:
        ppid = 0
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    try:
        os.remove(path)
    except OSError:
        pass
    routes = [([tuple(p) for p in r.get("coords") or []], r.get("title") or "", r.get("kinds"))
              for r in data.get("routes") or []]
    index = max(0, min(int(data.get("index") or 0), max(len(routes) - 1, 0)))

    def main(page: ft.Page):
        apply_theme(page)
        head = routes[index][1].split("  ", 1)[0] if routes else ""          # 标题形如 `RJTT→RJFF  航路串`
        page.title = "🗺️ 航路地图" + (" — " + head if head else "") + \
            ("（%d 条航路）" % len(routes) if len(routes) > 1 else "")
        page.window.width = 1240
        page.window.height = 920
        page.window.min_width = 640
        page.window.min_height = 480
        # 底图沿用上次的选择（记在运行目录 map_settings.json；多个地图窗口同时开着时以最后一次切换为准）
        mv = MapView(page, basemap=load_map_settings().get("basemap"),
                     on_basemap=lambda key: save_map_settings(basemap=key))
        view = mv.build(routes, index)
        if view is None:
            page.add(ft.Text("没有可显示的航路坐标。"))
            page.update()
            return
        page.views.clear()
        page.views.append(view)
        page.update()
        off = os.environ.get("DISPATCHER_SELFTEST_MAP_OFF")              # "wp" / "leg" / "both"：验标注开关
        if off:
            mv.set_labels(wp=off not in ("wp", "both"), leg=off not in ("leg", "both"))
        if ppid:
            threading.Thread(target=_watch_parent, args=(page, ppid), daemon=True).start()
            if os.name == "nt":                         # 窗口出来后提到最前（见 _raise_window）
                threading.Thread(target=_raise_window, args=(ppid,), daemon=True).start()
        if os.environ.get("DISPATCHER_SELFTEST_MAPTOUR"):
            threading.Thread(target=_tour, args=(page, mv), daemon=True).start()

    ft.run(main)
