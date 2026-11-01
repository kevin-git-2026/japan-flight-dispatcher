# ================= 本机 ATIS 服务（F33）=================
# 只绑回环地址的小服务，给 Fenix 用（端口 57698）：
#   GET /acars/atis/<ICAO> → 纯文本 ATIS；没有 → 404（MCDU 显示 NO ATIS AVAILABLE）
# 实测 Fenix 走 ::1，所以 IPv4 / IPv6 都要绑。这个端口不是公开接口，Fenix 更新后可能失效。

import errno
import re
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FENIX_PORT = 57698
_ATIS_PATH = re.compile(r"^/acars/atis/([A-Za-z0-9]{4})/?$")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dispatcher"

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class _FenixHandler(_Handler):
    def do_GET(self):
        m = _ATIS_PATH.match(self.path.split("?", 1)[0])
        text = None
        if m:
            try:
                text = self.server.provider(m.group(1).upper())
            except Exception as e:                        # noqa: BLE001
                print("⚠️ ATIS 服务出错:", e)
        if text:
            self._send(200, text.encode("ascii", "ignore"))
        else:
            self._send(404)


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False          # Windows 上开着会让第二个进程也绑上同一端口，检测不到冲突
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):
            return                                        # 对方断开（MCDU 不等了）是常态，不刷日志
        super().handle_error(request, client_address)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)         # 跳过 HTTPServer 里可能很慢的 getfqdn
        self.server_name, self.server_port = self.server_address[:2]


class _Server6(_Server):
    address_family = socket.AF_INET6


class LoopbackService:
    """一个端口的服务（127.0.0.1 + ::1）。attrs 挂到 server 上供 handler 用。"""

    def __init__(self, port, handler, **attrs):
        self.port, self.handler, self.attrs = port, handler, attrs
        self._servers = []

    @property
    def running(self):
        return bool(self._servers)

    def start(self):
        """启动 → None；失败 → 错误文字（端口被占等），已绑上的会释放。"""
        if self._servers:
            return None
        servers = []
        try:
            servers.append(_Server(("127.0.0.1", self.port), self.handler))
            try:
                servers.append(_Server6(("::1", self.port), self.handler))
            except OSError as e:
                if e.errno not in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, 10049, 10047):
                    raise                                 # 本机没有 IPv6 才可以跳过
        except OSError as e:
            for s in servers:
                s.server_close()
            return "端口 %d 无法使用（%s）" % (self.port, e.strerror or e)
        for s in servers:
            for k, v in self.attrs.items():
                setattr(s, k, v)
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2},
                             daemon=True).start()
        self._servers = servers
        return None

    def stop(self):
        servers, self._servers = self._servers, []
        for s in servers:
            s.shutdown()
            s.server_close()


def fenix_service(provider):
    return LoopbackService(FENIX_PORT, _FenixHandler, provider=provider)
