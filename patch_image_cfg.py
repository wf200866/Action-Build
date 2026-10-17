#!/usr/bin/env python3
"""
直接重写内核 Image 里的 config_data.gz 段（IKCFG_ST..IKCFG_ED）。
不依赖 Kbuild filechk / ccache / .incbin 任何环节，直接改产物。

用法:
  patch_image_cfg.py <Image> [DROP_KEYS] [SET_KEYS]
    DROP_KEYS : 空格或逗号分隔的 CONFIG_XXX，直接删行（grep 不到）
    SET_KEYS  : 空格或逗号分隔的 CONFIG_XXX=y，改写为对应值

要点：重建后的 gzip 用 FEXTRA 字段填充到与原字节数完全一致，
所以 Image 长度、所有偏移量、aboot 校验和布局都不变。
"""
import sys, zlib, struct

MARK_ST = b'IKCFG_ST'
MARK_ED = b'IKCFG_ED'


def split_keys(text):
    if not text:
        return []
    return [k for k in text.replace(',', ' ').split() if k]


def build_gzip_fixed(data, target_size):
    """生成长度恰为 target_size 的合法 gzip，解压结果等于 data。"""
    crc = zlib.crc32(data) & 0xffffffff
    isize = len(data) & 0xffffffff
    trailer = struct.pack('<II', crc, isize)
    overhead = 10 + 2 + len(trailer)
    budget = target_size - overhead

    best = None
    for level in (9, 8, 7, 6):
        for strat in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED,
                      zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE, zlib.Z_FIXED):
            co = zlib.compressobj(level, zlib.DEFLATED, -15, 9, strat)
            body = co.compress(data) + co.flush()
            if best is None or len(body) < len(best):
                best = body
            if len(best) <= budget:
                break
        if best is not None and len(best) <= budget:
            break

    if best is None or len(best) > budget:
        raise ValueError('压缩后超出原段 %d 字节，无法无损替换'
                         % (len(best) - budget))

    need = budget - len(best)
    if need > 0:
        if need > 0xffff:
            raise ValueError('需填充 %d 字节，超出 FEXTRA 上限' % need)
        hdr = b'\x1f\x8b\x08\x04' + b'\x00' * 4 + b'\x00\x03'
        extra = struct.pack('<H', need) + b'\x00' * need
    else:
        hdr = b'\x1f\x8b\x08\x00' + b'\x00' * 4 + b'\x00\x03'
        extra = b''
    return hdr + extra + best + trailer


def key_of(line):
    if line.startswith(b'# CONFIG_') and line.endswith(b' is not set'):
        return line[2:-len(b' is not set')].decode('utf-8', 'ignore')
    if line.startswith(b'CONFIG_') and b'=' in line:
        return line.split(b'=', 1)[0].decode('utf-8', 'ignore')
    return None


def patch_image(path, drop_keys, set_vals, dry=False):
    d = bytearray(open(path, 'rb').read())
    i = d.find(MARK_ST)
    if i < 0:
        print('[patch] 未找到 IKCFG_ST，跳过:', path)
        return False
    st = i + len(MARK_ST)
    j = d.find(MARK_ED, st)
    if j < 0:
        print('[patch] 未找到 IKCFG_ED，跳过:', path)
        return False

    raw = bytes(d[st:j])
    cfg = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
    lines = cfg.split(b'\n')

    drop = set(drop_keys)
    set_vals = dict(set_vals)
    out = []
    dropped = 0
    seted = 0
    for ln in lines:
        k = key_of(ln)
        if k is None:
            out.append(ln)
            continue
        if k in drop:
            dropped += 1
            continue
        if k in set_vals:
            out.append(('%s=%s' % (k, set_vals[k])).encode())
            seted += 1
            continue
        out.append(ln)

    if dropped == 0 and seted == 0:
        print('[patch] 没有任何项需要处理，跳过')
        return False

    newcfg = b'\n'.join(out)
    gz = build_gzip_fixed(newcfg, len(raw))
    d[st:j] = gz

    back = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(bytes(d[st:j]))
    assert back == newcfg, '回读校验失败'
    for k in drop:
        assert (k.encode() + b'=') not in back, '%s 未删除' % k
    for k, v in set_vals.items():
        assert ('%s=%s' % (k, v)).encode() in back, '%s 未设为 %s' % (k, v)

    print('[patch] %s: 删除 %d 项 / 设值 %d 项, 段长 %d 字节（不变）'
          % (path, dropped, seted, len(raw)))

    if not dry:
        open(path, 'wb').write(d)
    return True


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: patch_image_cfg.py <Image> [DROP_KEYS] [SET_KEYS]')
        sys.exit(1)
    drop_keys = split_keys(sys.argv[2] if len(sys.argv) > 2 else '')
    set_keys = []
    for item in split_keys(sys.argv[3] if len(sys.argv) > 3 else ''):
        if '=' in item:
            k, v = item.split('=', 1)
            set_keys.append((k, v))
    ok = patch_image(sys.argv[1], drop_keys, set_keys)
    sys.exit(0 if ok else 2)
