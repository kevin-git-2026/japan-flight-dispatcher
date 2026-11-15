# ================= SWIM Web API 客户端（F33）=================
# ATIS情報リクエストサービス（S2005）：官方提供 WebAPI，サービス利用審査 無し。
#   登录：POST top.swim.mlit.go.jp/swim/webapi/login {"id","password"} → Cookie MSMSI、MSMAI
#   查询：GET  web.swim.mlit.go.jp/f2atrq/web/FLV402001?location=A,B&dispcnt=1
# 实测一个请求查 23 场约 0.5 秒。服务器 15 秒就断开空闲连接（Keep-Alive: timeout=15），
# 所以隔一会儿再查都要重新握手；要先登录的那次（启动后第一次 / 会话过期后）本机实测 2–4 秒。
#
# 账号和密码只在 _login 里短暂存在于局部变量；登录 Cookie 只放内存，从不落盘，也不打印。
# 账号或密码错误后不再自动重试（避免锁账号），等用户重新保存。
# 不跟随重定向：Cookie 只发给下面这个固定地址。

import http.client
import json
import ssl
import threading
import time
import urllib.parse

from . import swim_creds

LOGIN_URL = "https://top.swim.mlit.go.jp/swim/webapi/login"
ATIS_URL = "https://web.swim.mlit.go.jp/f2atrq/web/FLV402001"
_COOKIES = ("MSMSI", "MSMAI")
_TIMEOUT = 15
_NET_ERRORS = (OSError, http.client.HTTPException)


def _connect(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port, timeout=_TIMEOUT,
                                            context=ssl.create_default_context()), u
    return http.client.HTTPConnection(u.hostname, u.port, timeout=_TIMEOUT), u


class SwimClient:
    def __init__(self, login_url=LOGIN_URL, atis_url=ATIS_URL, load_credentials=swim_creds.load):
        self._login_url, self._atis_url = login_url, atis_url
        self._load_credentials = load_credentials
        self._lock = threading.Lock()
        self._session = None             # Cookie 请求头的值，只在内存
        self._conn = None                # 查询用的长连接，下次查询省去握手
        self.bad_login = False

    @property
    def logged_in(self):
        """有没有登录会话（只说有没有，不涉及 Cookie 内容）。会话可能已在服务器端过期。"""
        return self._session is not None

    def reset(self):
        """账号或密码改了：丢掉会话，允许重新登录。"""
        with self._lock:
            self._session = None
            self.bad_login = False
            self._close()

    def _close(self):
        if self._conn:
            try:
                self._conn.close()
            except _NET_ERRORS:
                pass
        self._conn = None

    def fetch(self, icaos):
        """查一次 → {status, data, login_sec, query_sec}。
        status：ok / no_creds / bad_login / auth（重新登录后仍被拒）/ error。不抛异常。"""
        with self._lock:
            if self.bad_login:
                return {"status": "bad_login"}
            res = {"status": "ok", "login_sec": 0.0}
            try:
                if not self._session:
                    fail = self._login(res)
                    if fail:
                        return dict(res, status=fail)
                code, ctype, body = self._get(icaos, res)
                if self._rejected(code, ctype):           # 会话过期：重新登录一次
                    # 查询连接不关：被拒的响应已读完，连接还能用（服务器关了的话 http.client 会自动重连），
                    # 省一次 TLS 握手（本机实测 0.2–1.1 秒，偶尔 3 秒以上）
                    self._session = None
                    fail = self._login(res)
                    if fail:
                        return dict(res, status=fail)
                    code, ctype, body = self._get(icaos, res)
                    if self._rejected(code, ctype):
                        self._session = None
                        return dict(res, status="auth")
                if code != 200:
                    return dict(res, status="error", detail="HTTP %d" % code)
                res["data"] = json.loads(body.decode("utf-8", "replace"))
                return res
            except swim_creds.CredentialError as e:
                return dict(res, status="no_creds", detail=str(e))
            except ValueError:
                return dict(res, status="error", detail="响应不是 JSON")
            except _NET_ERRORS as e:
                self._close()
                return dict(res, status="error", detail=type(e).__name__)

    @staticmethod
    def _rejected(code, ctype):
        """401/403、重定向或返回 HTML 登录页 = 没有登录状态。"""
        return code in (401, 403) or 300 <= code < 400 or "json" not in (ctype or "")

    def _login(self, res):
        """→ None=成功；否则失败状态。"""
        creds = self._load_credentials()
        if not creds:
            return "no_creds"
        t = time.time()
        conn, u = _connect(self._login_url)
        try:
            body = json.dumps({"id": creds[0], "password": creds[1]})
            creds = None
            conn.request("POST", u.path, body, {"Content-Type": "application/json",
                                                "Accept": "application/json"})
            body = None
            r = conn.getresponse()
            r.read()
            cookies = {}
            for line in r.headers.get_all("Set-Cookie") or []:
                name, _, rest = line.partition("=")
                if name.strip() in _COOKIES:
                    cookies[name.strip()] = rest.split(";", 1)[0]
        finally:
            conn.close()
        res["login_sec"] = time.time() - t
        if r.status >= 500:
            return "error"
        if any(not cookies.get(c) for c in _COOKIES):     # 账号或密码错误
            self.bad_login = True
            return "bad_login"
        self._session = "; ".join("%s=%s" % (c, cookies[c]) for c in _COOKIES)
        return None

    def _get(self, icaos, res):
        u = urllib.parse.urlsplit(self._atis_url)
        target = u.path + "?" + urllib.parse.urlencode({"location": ",".join(icaos), "dispcnt": 1},
                                                       safe=",")
        headers = {"Accept": "application/json", "Cookie": self._session}
        t = time.time()
        for attempt in (1, 2):                            # 长连接可能已被服务器关掉，换新连接再试一次
            if not self._conn:
                self._conn = _connect(self._atis_url)[0]
            try:
                self._conn.request("GET", target, headers=headers)
                r = self._conn.getresponse()
                body = r.read()
                res["query_sec"] = time.time() - t
                return r.status, r.getheader("Content-Type"), body
            except _NET_ERRORS:
                self._close()
                if attempt == 2:
                    raise
