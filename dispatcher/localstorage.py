# ================= Chromium Local Storage 只读读取器（LevelDB + Snappy，v2.0.1_hotfix3）=================
# Edge / Chrome / Brave 把网页的 localStorage 存成一个 LevelDB（<Profile>/Local Storage/leveldb），
# 全部网站共用这一个库：
#   - 新写入先进【未压缩】的 .log（预写日志）；
#   - 浏览器重启 / 日志写满时，并入 .ldb 表文件（SST），数据块经 **Snappy 压缩**。
# 旧做法（volanta.py 的 `_read_leveldb_text`）把原始字节当文本、用正则扫令牌——令牌一旦被压缩进 .ldb，
# 字节被切碎就再也匹配不到；它还会解码库里【所有网站】长得像 JWT 的串，被别站的数据带崩。
# 这正是 Volanta 同步 2026-07 起失效的两个根因（实测见 revisions.md「v2.0.1 hotfix_3」）。
#
# 本模块按 LevelDB 的公开格式真正解析（doc/log_format.md、doc/table_format.md）：
#   - .log：32 KiB 分块，FULL/FIRST/MIDDLE/LAST 片段重组为 WriteBatch；
#   - .ldb/.sst：48 字节 footer → index 块 → 数据块（块尾压缩字节 0=原样 / 1=Snappy），前缀压缩 + restart 数组；
#   - 合并：所有文件里同一个键取 sequence 最大的那条，删除墓碑生效。
# 并且【只取指定来源（origin）】的键值：其它网站的记录在区间比较时就被过滤掉，值从不解码。
#
# ⚠️ 纯标准库、只读：不加锁、不写任何文件、不复制到临时目录（复制会把明文令牌写到磁盘）。
#    Chromium 以共享读方式打开这些文件，浏览器开着也能读。任何坏块 / 坏文件只跳过那一处，绝不向上抛异常。
# ⚠️ 不解析 MANIFEST：直接合并目录里现有的全部文件。极少数情况下，已被合并删除的旧表文件若尚未从磁盘移除，
#    其中的旧值可能被读到（详见 revisions.md hotfix_3「已知局限」）。

import os
import struct

# ---- LevelDB 格式常量 ----
_LOG_BLOCK = 32768                  # .log 按 32 KiB 分块
_LOG_HEADER = 7                     # 记录头：crc32c(4) + 长度(2，小端) + 类型(1)
_ZERO, _FULL, _FIRST, _MIDDLE, _LAST = 0, 1, 2, 3, 4
_SST_FOOTER = 48                    # 表文件末尾 footer 定长 48 字节
_SST_MAGIC = 0xdb4775248b80fb57
_TYPE_DELETION, _TYPE_VALUE = 0, 1  # 内部键 tag 的低 8 位
_NO_COMPRESSION, _SNAPPY = 0, 1     # 数据块尾的压缩类型字节（其它值如 zstd → 跳过该块）
_TABLE_EXTS = (".ldb", ".sst")      # .sst 是旧版 Chromium 的表文件扩展名

# ---- Chromium localStorage 的键 / 值编码 ----
# 键 = b"_" + 来源 + b"\x00" + 编码字节 + 键名；值 = 编码字节 + 数据。编码字节：1 = Latin-1，0 = UTF-16LE。
_ENC_UTF16, _ENC_LATIN1 = 0, 1


def _varint(buf, pos):
    """解一个 varint → (值, 新位置)；越界或超长抛 ValueError。"""
    result = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("bad varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def _snappy_decompress(buf):
    """Snappy 原始格式解压（LevelDB 数据块用的就是它）。任何越界 / 长度不符都抛 ValueError，由调用方跳过该块。"""
    n, pos = _varint(buf, 0)            # 前导 varint = 解压后总长
    out = bytearray()
    end = len(buf)
    while pos < end:
        tag = buf[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:                                   # 字面量
            ln = tag >> 2
            if ln >= 60:                                # 60–63：长度改由后随 1–4 字节（小端）给出
                nb = ln - 59
                if pos + nb > end:
                    raise ValueError("snappy literal length")
                ln = int.from_bytes(buf[pos:pos + nb], "little")
                pos += nb
            ln += 1
            if pos + ln > end:
                raise ValueError("snappy literal")
            out += buf[pos:pos + ln]
            pos += ln
        else:
            if kind == 1:                               # copy-1：长度 4–11，11 位偏移
                if pos + 1 > end:
                    raise ValueError("snappy copy-1")
                ln = ((tag >> 2) & 7) + 4
                off = ((tag >> 5) << 8) | buf[pos]
                pos += 1
            elif kind == 2:                             # copy-2：长度 1–64，16 位偏移
                if pos + 2 > end:
                    raise ValueError("snappy copy-2")
                ln = (tag >> 2) + 1
                off = int.from_bytes(buf[pos:pos + 2], "little")
                pos += 2
            else:                                       # copy-4：长度 1–64，32 位偏移
                if pos + 4 > end:
                    raise ValueError("snappy copy-4")
                ln = (tag >> 2) + 1
                off = int.from_bytes(buf[pos:pos + 4], "little")
                pos += 4
            if off == 0 or off > len(out):
                raise ValueError("snappy copy offset")
            if off >= ln:                               # 不重叠：整段切片
                start = len(out) - off
                out += out[start:start + ln]
            else:
                # 重叠复制（游程，如 "a" 后接 off=1 len=11）：逐字节追加的结果 = 末尾 off 字节的周期延拓，
                # 直接按周期拼出来（与逐字节等价，冒烟里对拍过）；不能用普通切片——可用字节不够 ln 个。
                out += (bytes(out[-off:]) * (ln // off + 1))[:ln]
        if len(out) > n:
            raise ValueError("snappy overrun")
    if len(out) != n:
        raise ValueError("snappy length mismatch")
    return bytes(out)


def _read_block(f, offset, size):
    """读一个表块及其 5 字节块尾（压缩类型 1 + crc 4），按压缩类型解出内容。未知压缩 / 越界抛 ValueError。"""
    f.seek(offset)
    raw = f.read(size + 5)
    if len(raw) != size + 5:
        raise ValueError("short block")
    ctype = raw[size]
    if ctype == _NO_COMPRESSION:
        return raw[:size]
    if ctype == _SNAPPY:
        return _snappy_decompress(raw[:size])
    raise ValueError("unsupported compression %d" % ctype)


def _block_entries(blk):
    """逐条产出表块（数据块 / index 块）里的 (key, value)：键做了前缀压缩，块末是 restart 数组。"""
    if len(blk) < 4:
        raise ValueError("short block body")
    nrest = struct.unpack_from("<I", blk, len(blk) - 4)[0]
    limit = len(blk) - 4 - 4 * nrest
    if limit < 0:
        raise ValueError("bad restart array")
    pos, key = 0, b""
    while pos < limit:
        shared, pos = _varint(blk, pos)
        unshared, pos = _varint(blk, pos)
        vlen, pos = _varint(blk, pos)
        if shared > len(key) or pos + unshared + vlen > limit:
            raise ValueError("bad block entry")
        key = key[:shared] + blk[pos:pos + unshared]
        pos += unshared
        yield key, blk[pos:pos + vlen]
        pos += vlen


def _handle(buf, pos=0):
    """解一个 BlockHandle（varint 偏移 + varint 长度）→ (偏移, 长度, 新位置)。"""
    off, pos = _varint(buf, pos)
    size, pos = _varint(buf, pos)
    return off, size, pos


def _table_records(path, lo, hi):
    """表文件（.ldb/.sst）里用户键落在 [lo, hi) 的记录 → [(用户键, sequence, 类型, 值)]。
    走 footer → index → 首个「分隔键 ≥ lo」的数据块，再【跨块】向后扫到键 ≥ hi 为止——同一个键的多个版本
    可能横跨相邻块。footer / index 坏 → 抛 ValueError（整个文件跳过）；单个数据块坏 → 只跳过那一块。"""
    out = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        fsize = f.tell()
        if fsize < _SST_FOOTER:
            raise ValueError("short table")
        f.seek(fsize - _SST_FOOTER)
        footer = f.read(_SST_FOOTER)
        # magic 写在 footer 最后：正被写入（尚未写完）的表文件在这里就会被挡下
        if len(footer) != _SST_FOOTER or struct.unpack_from("<Q", footer, 40)[0] != _SST_MAGIC:
            raise ValueError("bad table magic")
        _, _, pos = _handle(footer)                     # metaindex（过滤器等，用不到）
        ioff, isize, _ = _handle(footer, pos)
        index = list(_block_entries(_read_block(f, ioff, isize)))
        started = False
        for sep, handle in index:
            # 分隔键 = 内部键（用户键 + 8 字节 tag），满足：本块所有键 ≤ 分隔键 < 下一块首键。
            # 去掉 tag 比用户键即可（被缩短的分隔键也成立：它是某个用户键的前缀时按字节序本就更小）。
            sep_user = sep[:-8]
            if not started:
                if sep_user < lo:                       # 本块所有键都 < lo，整块跳过（不读不解压）
                    continue
                started = True
            boff, bsize, _ = _handle(handle)
            try:
                for ikey, val in _block_entries(_read_block(f, boff, bsize)):
                    if len(ikey) < 8:
                        raise ValueError("bad internal key")
                    ukey = ikey[:-8]
                    if ukey < lo:
                        continue
                    if ukey >= hi:
                        return out
                    tag = struct.unpack_from("<Q", ikey, len(ikey) - 8)[0]
                    out.append((ukey, tag >> 8, tag & 0xFF, val))
            except (ValueError, IndexError, struct.error):
                pass                                    # 坏块 / 未知压缩（如 zstd）：只跳过这一块
            if sep_user >= hi:                          # 下一块的键都 > 分隔键 ≥ hi：到此为止
                break
    return out


def _batch_records(rec, lo, hi, out):
    """解一个 WriteBatch：sequence(8) + 条数(4) + 条目[tag, varint 键长, 键, (varint 值长, 值)]。
    第 i 条的 sequence = 批首 sequence + i。只收用户键落在 [lo, hi) 的条目；
    遇到未知 tag 无法确定条目宽度 → 放弃本批余下部分（不去猜）。"""
    if len(rec) < 12:
        return
    seq = struct.unpack_from("<Q", rec, 0)[0]
    count = struct.unpack_from("<I", rec, 8)[0]
    pos = 12
    try:
        for i in range(count):
            tag = rec[pos]
            pos += 1
            klen, pos = _varint(rec, pos)
            key = rec[pos:pos + klen]
            if len(key) != klen:
                return
            pos += klen
            if tag == _TYPE_VALUE:
                vlen, pos = _varint(rec, pos)
                val = rec[pos:pos + vlen]
                if len(val) != vlen:
                    return
                pos += vlen
            elif tag == _TYPE_DELETION:
                val = None
            else:
                return
            if lo <= key < hi:
                out.append((key, seq + i, tag, val))
    except (IndexError, ValueError):
        return


def _log_records(path, lo, hi):
    """.log（预写日志）里用户键落在 [lo, hi) 的记录 → [(用户键, sequence, 类型, 值)]。
    浏览器可能正在往里追加：尾部残缺的记录 / 没有 LAST 的片段直接丢弃，已解析的照常返回。"""
    with open(path, "rb") as f:
        data = f.read()
    out = []
    n = len(data)
    pos = 0
    frag = None                                         # 正在重组的 FIRST…MIDDLE… 片段
    while pos + _LOG_HEADER <= n:
        left = _LOG_BLOCK - pos % _LOG_BLOCK
        if left < _LOG_HEADER:                          # 块尾不足一个记录头：是零填充，跳到下一块
            pos += left
            continue
        length = data[pos + 4] | (data[pos + 5] << 8)
        rtype = data[pos + 6]
        if _LOG_HEADER + length > left:                 # 记录越过本块边界（与 LevelDB 自己的读取器同判据）
            if pos + left < n:                          # 后面还有块 → 损坏：丢弃本块余下部分，从下一块接着读
                pos += left
                frag = None
                continue
            break                                       # 已是最后一块 → 写到一半（浏览器正在追加）：到此为止
        start = pos + _LOG_HEADER
        if start + length > n:                          # 最后一块被截断（正在写）：到此为止
            break
        if rtype > _LAST or (rtype == _ZERO and length == 0):
            # 类型 0 + 长度 0 = 预分配的零区；未知类型（不校验 crc，多半是错位 / 损坏）。都丢弃本块余下部分。
            pos += left
            frag = None
            continue
        chunk = data[start:start + length]
        pos = start + length
        if rtype == _FULL:
            frag = None
            _batch_records(chunk, lo, hi, out)
        elif rtype == _FIRST:
            frag = bytearray(chunk)
        elif rtype == _MIDDLE:
            if frag is not None:
                frag += chunk
        elif rtype == _LAST:
            if frag is not None:
                frag += chunk
                _batch_records(bytes(frag), lo, hi, out)
            frag = None
        else:                                           # 类型 0 但长度非 0：不认识，丢弃片段
            frag = None
    return out


def origin_range(origin):
    """来源 → 它在库里的键区间 [lo, hi)：lo = b"_" + 来源 + b"\\x00"，hi 为该前缀的上界。
    只匹配第一方存储；存储分区下的第三方键（来源后接 "^0…"）不在此区间内。"""
    lo = b"_" + origin.encode("ascii") + b"\x00"
    return lo, lo[:-1] + b"\x01"


def _decode(b):
    """按 Chromium 的编码字节解出字符串：1 = Latin-1，0 = UTF-16LE；空 / 未知编码 → None。"""
    if not b:
        return None
    if b[0] == _ENC_LATIN1:
        return b[1:].decode("latin-1")
    if b[0] == _ENC_UTF16:
        return b[1:].decode("utf-16-le", "replace")
    return None


def read_origin(db_dir, origin):
    """读取 db_dir（某个浏览器配置的 Local Storage/leveldb）里【指定来源】的 localStorage → {键名: 值}。
    每个键取 sequence 最大的那条记录，删除墓碑生效；其它来源的记录在区间比较时就被过滤，值从不解码。
    返回 {} = 能读，但该来源下没有数据；返回 None = 有数据文件却一个都解析不了（格式变了 / 都读不了）。"""
    lo, hi = origin_range(origin)
    try:
        names = os.listdir(db_dir)
    except OSError:
        return None
    latest = {}
    candidates = parsed = 0
    for name in names:
        low = name.lower()
        if low.endswith(_TABLE_EXTS):
            reader = _table_records
        elif low.endswith(".log"):
            reader = _log_records
        else:
            continue
        candidates += 1
        try:
            recs = reader(os.path.join(db_dir, name), lo, hi)
        except Exception:                               # noqa: BLE001 — 正被写入 / 已被合并删除 / 损坏：跳过该文件
            continue
        parsed += 1
        for ukey, seq, typ, val in recs:
            cur = latest.get(ukey)
            if cur is None or seq > cur[0]:
                latest[ukey] = (seq, typ, val)
    if candidates and not parsed:
        return None
    result = {}
    for ukey, (_seq, typ, val) in latest.items():
        if typ != _TYPE_VALUE:                          # 最新一条是删除墓碑：该键已不存在
            continue
        k, v = _decode(ukey[len(lo):]), _decode(val)
        if k is not None and v is not None:
            result[k] = v
    return result
