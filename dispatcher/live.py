# ================= 实时天气与 ATIS（F33）=================
# 守护线程定时刷新监视机场的 METAR / TAF，并决定 ATIS 来源：
# SWIM 未过期的真实报文 > 生成式 ATIS。一次抓取失败时保留上次成功的结果。
# SWIM 不定时查：只有用户点「更新 ATIS」或在 MCDU 按 SEND 才查一次，和真实世界请求 ATIS 一样；
# 一个请求带上全部 23 场（实测约 0.5 秒）。
#
# 不用 Shell.run_bg：它是线程池，阻塞中的任务会拖住程序退出。这里用真正的守护线程。
# 回调在后台线程上调用，界面更新由 UI 负责 marshal。

import threading
import time

from . import atis, atis_server, config, operations, swim_atis, swim_client, swim_creds, weather
from .data import load_airports_from_navigraph

METAR_SEC = 300                  # METAR 5 分钟刷新一次
TAF_SEC = 1800                   # TAF 30 分钟
_TICK_SEC = 15
_TEMP_KEEP_SEC = 1800            # 只被模拟器请求过的机场，30 分钟后不再刷新
_FIRST_WAIT_SEC = 3.0            # 模拟器请求一个还没数据的机场时最多等这么久
_SWIM_WAIT_SEC = 4.0             # MCDU 按 SEND 时最多等 SWIM 这么久，超时用已有的报文或生成版
_FAIL_TEXT = {"bad_login": "账号或密码错误", "auth": "登录被拒", "no_creds": "没有可用的账号"}


class LiveService:
    def __init__(self, dat_path, on_change):
        self.dat_path = dat_path
        self._on_change = on_change
        self._lock = threading.Lock()
        self._snap = {}                  # 机场 → {icao, wx, atis, source, fetched_at, letter}
        self._temp = {}                  # 只被模拟器请求过的机场 → 时间
        self._fetched = {}               # 机场 → 上次抓取时间
        self._ready = {}                 # 机场 → 首次数据到达的 Event
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ops = operations.load_operations()
        self._airports = {a.code: (a.lat_dd, a.lon_dd)
                          for a in load_airports_from_navigraph(dat_path, None, 0)}
        settings = config.load_live_settings()
        self.watch = [i for i in settings.get("watch", []) if i in self._airports]
        self.fenix_on = bool(settings.get("fenix"))
        self.swim_on = bool(settings.get("swim"))
        self.fenix = atis_server.fenix_service(self.text_for_sim)
        self.client = swim_client.SwimClient()
        self._query_lock = threading.Lock()
        self._query_thread = None

    # ---------- 生命周期 ----------
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="live-weather").start()
        err = self.fenix.start() if self.fenix_on else None
        self.fenix_on = self.fenix.running
        return [err] if err else []

    def stop(self):
        self._stop.set()
        self._wake.set()
        self.fenix.stop()

    def set_fenix(self, on):
        """开关 Fenix 服务 → 失败时的错误文字。"""
        err = self.fenix.start() if on else None
        if not on:
            self.fenix.stop()
        self.fenix_on = self.fenix.running
        self._save()
        return err

    def set_swim(self, on):
        self.swim_on = bool(on)
        self._save()

    def _save(self):
        config.save_live_settings(watch=list(self.watch), fenix=self.fenix_on, swim=self.swim_on)

    # ---------- 监视机场 ----------
    def known(self, icao):
        return icao in self._airports

    def add(self, *icaos):
        added = False
        for icao in icaos:
            if icao in self._airports and icao not in self.watch:
                self.watch.append(icao)
                added = True
        if added:
            self._watch_changed()
        return added

    def remove(self, icao):
        if icao in self.watch:
            self.watch.remove(icao)
            self._watch_changed()

    def _watch_changed(self):
        self._save()
        self._wake.set()

    def snapshot(self, icao):
        with self._lock:
            s = self._snap.get(icao)
            return dict(s) if s else None

    def reload_operations(self):
        """规则编辑器保存后调（UI 线程）。"""
        self._ops = operations.load_operations()
        self._wake.set()

    # ---------- SWIM 账号 ----------
    def has_credentials(self):
        return swim_creds.exists()

    def save_credentials(self, user, password):
        """加密保存账号和密码 → 失败时的错误文字。这里不留任何引用。"""
        try:
            swim_creds.save(user, password)
        except swim_creds.CredentialError as e:
            return str(e)
        self.client.reset()
        swim_atis.store.set_status("none")
        return None

    def clear_credentials(self):
        try:
            swim_creds.clear()
        except swim_creds.CredentialError as e:
            return str(e)
        self.client.reset()
        swim_atis.store.set_status("none")
        return None

    # ---------- SWIM 查询：用户发起一次，查一次 ----------
    def can_query_swim(self):
        return self.swim_on and self.has_credentials() and not self.client.bad_login

    def querying(self):
        t = self._query_thread
        return bool(t and t.is_alive())

    def start_query(self):
        """发起一次查询（已有查询在进行就并入它）→ 查询线程；不能查 → None。不阻塞。"""
        if not self.can_query_swim():
            return None
        with self._query_lock:
            if not self.querying():
                self._query_thread = threading.Thread(target=self._run_query, daemon=True,
                                                      name="swim-query")
                self._query_thread.start()
        self._notify(None)
        return self._query_thread

    def _run_query(self):
        t = time.time()
        res = self.client.fetch(swim_atis.SWIM_AIRPORTS)
        status = res.get("status")
        if status == "ok":
            done = swim_atis.store.put_response(res.get("data"))
            print("⏱️ SWIM 查询 %d 场共 %.1f 秒（登录 %.1f 秒 · 查询 %.1f 秒）" % (
                len(done), time.time() - t, res.get("login_sec", 0), res.get("query_sec", 0)))
            for icao in done:
                if icao in self._snap or icao in self.watch:
                    self._rebuild(icao)
        else:
            swim_atis.store.set_status(status)
            print("⚠️ SWIM 查询失败：%s" % (res.get("detail") or _FAIL_TEXT.get(status, status)))
        self._query_thread = None
        self._notify(None)

    # ---------- 给模拟器 ----------
    def text_for_sim(self, icao):
        """HTTP 线程调：MCDU 按 SEND = 请求最新 ATIS。RJ/RO 以外或暂时没有 → None。不会长时间阻塞。"""
        if icao not in self._airports:
            return None
        finished = True
        thread = self.start_query() if icao in swim_atis.SWIM_AIRPORTS else None
        if thread:
            thread.join(_SWIM_WAIT_SEC)
            finished = not thread.is_alive()
        rep = swim_atis.store.report(icao)
        if rep:
            if not finished:
                print("ℹ️ MCDU 请求 %s：SWIM %.0f 秒内没有回应，先返回已有报文" % (icao, _SWIM_WAIT_SEC))
            return rep["raw"]
        if thread:
            why = ("SWIM %.0f 秒内没有回应" % _SWIM_WAIT_SEC if not finished
                   else "SWIM 没有这个机场的报文或查询失败（%s）" % swim_atis.store.status())
            print("ℹ️ MCDU 请求 %s：%s，返回生成版" % (icao, why))
        with self._lock:
            snap = self._snap.get(icao)
            if icao not in self.watch:
                self._temp[icao] = time.time()
            ready = self._ready.setdefault(icao, threading.Event())
        if snap and snap.get("source") == "swim":         # 上次的真实报文过期了 → 改用生成版
            self._rebuild(icao)
            snap = self.snapshot(icao)
        if not snap or not snap.get("atis"):
            self._wake.set()
            ready.wait(_FIRST_WAIT_SEC)
            with self._lock:
                snap = self._snap.get(icao)
        return snap.get("atis") if snap else None

    # ---------- 后台 ----------
    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._tick()
            except Exception as e:                        # noqa: BLE001
                print("⚠️ 实时天气刷新出错:", e)
            self._wake.wait(_TICK_SEC)

    def _tick(self):
        now = time.time()
        with self._lock:
            self._temp = {k: t for k, t in self._temp.items() if now - t < _TEMP_KEEP_SEC}
            icaos = list(self.watch) + [k for k in self._temp if k not in self.watch]
        for icao in icaos:
            if self._stop.is_set():
                return
            if now - self._fetched.get(icao, 0) >= METAR_SEC:
                self._fetched[icao] = now
                self._refresh(icao)

    def _refresh(self, icao):
        lat, lon = self._airports[icao]
        wx = weather.resolve_airport_wx(icao, lat, lon, metar_max_age=METAR_SEC - 10,
                                        taf_max_age=TAF_SEC)
        with self._lock:
            old = self._snap.get(icao)
        if wx.get("source") is None and old and old.get("wx"):
            wx = dict(old["wx"], taf_raw=wx.get("taf_raw") or old["wx"].get("taf_raw"))
            fetched_at = old.get("fetched_at")
        else:
            fetched_at = time.time()
        with self._lock:
            self._snap[icao] = {"icao": icao, "wx": wx, "fetched_at": fetched_at,
                                "atis": (old or {}).get("atis"), "source": (old or {}).get("source")}
        self._rebuild(icao)
        self._notify(icao)

    def _rebuild(self, icao):
        """定 ATIS 来源并生成文本。"""
        rep = swim_atis.store.report(icao)
        with self._lock:
            snap = self._snap.get(icao)
        report_z = received = None
        if rep:
            text, source = rep["raw"], "swim"
            report_z, received = rep["time"].strftime("%d%H%MZ"), rep.get("received_at")
        elif snap and snap.get("wx"):
            lat, lon = self._airports[icao]
            try:
                text = atis.generate(icao, snap["wx"], lat, lon, self.dat_path, self._ops)
            except Exception as e:                        # noqa: BLE001
                print("⚠️ 生成 %s ATIS 失败: %s" % (icao, e))
                text = None
            source = "generated" if text else None
        else:
            text, source = None, None
        with self._lock:
            cur = self._snap.setdefault(icao, {"icao": icao, "wx": None, "fetched_at": None})
            cur["atis"], cur["source"] = text, source
            cur["report_z"], cur["received_at"] = report_z, received
            ready = self._ready.get(icao)
        if ready and text:
            ready.set()

    def _notify(self, icao):
        try:
            self._on_change(icao)
        except Exception:                                 # noqa: BLE001
            pass
