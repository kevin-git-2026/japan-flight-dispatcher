# ================= Shell：唯一允许碰 page 的模块 =================
# Flet 的 page.update() / control.update() **不是线程安全的**（patch_control 直接 send_message，
# 无锁、无 call_soon_threadsafe）。所以跨线程 marshal 是【强制】的，且必须收敛到一个地方。
#
# 铁律（评审强制）：
#   · page.update() / control.update() 只许出现在 Shell.post 里或 Flet 事件处理器里；
#   · 绝不许出现在 run_bg 的目标函数里（那是后台线程）。
#
# 对照 tkinter：run_bg ≈ threading.Thread(daemon=True)；post ≈ root.after(0, …)。

import threading


class Shell:
    def __init__(self, page):
        self.page = page
        self._log_view = None
        self._log_buf = []
        self._log_lock = threading.Lock()
        self._log_scheduled = False
        self._tls = threading.local()          # 防重入：flush 期间的 print 不再排队

    # ---------- 线程 ----------
    def run_bg(self, fn, *a):
        """后台线程跑阻塞/联网工作。目标函数【不得】碰任何控件。"""
        self.page.run_thread(fn, *a)

    def post(self, fn, *a):
        """THE marshal point：任何线程想改控件，都得经这里回到 Flet 的事件循环线程。
        （page.run_task 内部走 asyncio.run_coroutine_threadsafe，任意线程安全。）"""
        async def _call():
            try:
                fn(*a)
            finally:
                self.page.update()             # 一次 patch，且在 loop 线程
        try:
            self.page.run_task(_call)
        except Exception:
            pass

    # ---------- stdout 桥（controller.LogSink 的 emit）----------
    def bind_log(self, log_view):
        self._log_view = log_view

    def log_emit(self, s):
        """print() → 缓冲 + 只调度一次 flush → 【一批一个 patch】（每条 print 一个 patch 太贵）。"""
        if getattr(self._tls, "in_flush", False):
            return                             # 重入守卫：flet 自身 logging 写 stderr，而 stderr 已被我们接管
        with self._log_lock:
            self._log_buf.append(s)
            if self._log_scheduled:
                return
            self._log_scheduled = True
        self.post(self._flush_log)

    def _flush_log(self):
        with self._log_lock:
            buf, self._log_buf = self._log_buf, []
            self._log_scheduled = False
        if not buf or self._log_view is None:
            return
        self._tls.in_flush = True
        try:
            self._log_view.append("".join(buf))
        finally:
            self._tls.in_flush = False
