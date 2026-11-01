# ================= SWIM 账号的加密存储（F33）=================
# 账号和密码用 Windows DPAPI 加密后才写入 swim_credentials.dat（运行目录，运行时生成，不随包）。
# 加密绑定当前 Windows 账号：单独拿到这个文件解不开，但同一账号下运行的程序都能调用解密，
# 拿到完整磁盘数据和登录密码也能解开。
#
# 铁律：
#   · 绝不明文落盘：不写临时文件，不写日志；加密不可用时拒绝保存，没有明文后备。
#   · 运行时不缓存：只有登录那一刻 load() 解密到局部变量，用完即丢。
#     （Python 字符串无法主动清零，只能保证不保留引用。）
#   · 异常信息里不带账号和密码。

import ctypes
import json
import os
import sys
from ctypes import wintypes

from .config import get_real_run_path

_FILE = "swim_credentials.dat"
_ENTROPY = b"jpn-flight-dispatcher/swim/v1"     # 不是机密，只防别的程序误解同一份数据


class CredentialError(Exception):
    """保存或读取失败。信息里不含账号和密码。"""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(protect, data):
    if sys.platform != "win32":
        raise CredentialError("当前系统不支持加密存储")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    source, _keep1 = _blob(data)
    entropy, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    ok = fn(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise CredentialError("系统加密服务调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def path():
    return os.path.join(get_real_run_path(), _FILE)


def exists():
    return os.path.exists(path())


def save(user, password):
    """加密后原子写入。失败抛 CredentialError，不留下任何文件。"""
    if not user or not password:
        raise CredentialError("账号和密码都要填写")
    blob = _dpapi(True, json.dumps({"id": user, "password": password}).encode("utf-8"))
    p = path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(blob)                                 # 这里已经是密文
        os.replace(tmp, p)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise CredentialError("无法写入加密文件") from None


def load():
    """解密 → (账号, 密码)；没有文件 → None。解不开（换了电脑或 Windows 账号）抛 CredentialError。
    调用方只能放在局部变量里，用完即丢。"""
    try:
        with open(path(), "rb") as f:
            blob = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        raise CredentialError("无法读取加密文件") from None
    try:
        data = json.loads(_dpapi(False, blob).decode("utf-8"))
        return data["id"], data["password"]
    except (ValueError, KeyError, TypeError):
        raise CredentialError("加密文件已损坏") from None


def clear():
    try:
        os.remove(path())
    except FileNotFoundError:
        pass
    except OSError:
        raise CredentialError("无法删除加密文件") from None
