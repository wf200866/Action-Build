#!/usr/bin/env python3
"""
直接重写内核 Image 里的 config_data.gz 段（IKCFG_ST..IKCFG_ED）。
不依赖 Kbuild filechk / ccache / .incbin 任何环节，直接改产物。

关键点：重建后的 gzip 通过 FEXTRA 字段扩到与原字节数完全一致，
保证 Image 偏移不变、zcat 无任何警告。
"""
import sys, zlib, struct

MARK_ST = b'IKCFG_ST'
MARK_ED = b'IKCFG_ED'

def build_gzip_fixed(data, target_size):
    """生成长度恰为 target_size 的合法 gzip，解压结果等于 data。

    用 FEXTRA 字段填充凑长度。压缩结果必须 <= target_size - 18（header 10 + XLEN 2 + trailer 8）。
    多种压缩策略轮试，取最小的那个——zlib 不同 level/strategy 结果差异可达几千字节。
    """
    crc = zlib.crc32(data) & 0xffffffff
    isize = len(data) & 0xffffffff
    trailer = struct.pack('<II', crc, isize)
    overhead = 10 + 2 + len(trailer)   # 最简 gzip header + XLEN + trailer
    budget = target_size - overhead

    best = None
    for level in (9, 8, 7, 6):
        for strat in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED,
                      zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE, zlib.Z_FIXED):
            co = zlib.compressobj(level, zlib.DEFLATED, -15, 9, strat)
            body = co.compress(data) + co.flush()
            if best is None or len(body) < len(best):
                best = body
            if len(body) <= budget:
                break
        if best is not None and len(best) <= budget:
            break

    if best is None or len(best) > budget:
        raise ValueError(
            '压缩后仍超出原段 %d 字节，无法无损替换（原段 %d）'
            % (len(best) - budget, target_size))

    body = best
    need = budget - len(body)          # 需要填充的字节数
    if need > 0:
        if need > 0xffff:
            raise ValueError('需填充 %d 字节，超出 FEXTRA 上限' % need)
        hdr = b'\x1f\x8b\x08\x04' + b'\x00' * 4 + b'\x00\x03'
        extra = struct.pack('<H', need) + b'\x00' * need
    else:
        hdr = b'\x1f\x8b\x08\x00' + b'\x00' * 4 + b'\x00\x03'
        extra = b''
    return hdr + extra + body + trailer

def patch_image(path, drop_keys, dry=False):
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
    o = zlib.decompressobj(16 + zlib.MAX_WBITS)
    cfg = o.decompress(raw)
    if o.unused_data:
        print('[patch] 原段含 %d 字节残留' % len(o.unused_data))

    lines = cfg.split(b'\n')
    out = []
    hits = 0
    for ln in lines:
        key = None
        if ln.startswith(b'# CONFIG_') and ln.endswith(b' is not set'):
            key = ln[2:-len(b' is not set')]
        elif b'=' in ln and ln.startswith(b'CONFIG_'):
            key = ln.split(b'=', 1)[0]
        if key is not None and key.decode('utf-8', 'ignore') in drop_keys:
            # 直接删掉整行：grep 不到，且比改写为 is not set 更短，
            # 压缩后必然比原段小，FEXTRA 填充空间充足
            hits += 1
            continue
        out.append(ln)
    newcfg = b'\n'.join(out)

    if hits == 0:
        print('[patch] 没有任何项需要处理，跳过')
        return False

    gz = build_gzip_fixed(newcfg, len(raw))
    assert len(gz) == len(raw), '长度不匹配'
    d[st:j] = gz

    # 验证
    o2 = zlib.decompressobj(16 + zlib.MAX_WBITS)
    back = o2.decompress(bytes(d[st:j]))
    assert back == newcfg, '回读校验失败'
    bad = [k for k in drop_keys if (k + '=').encode() in back]
    assert not bad, '仍有未隐藏: %r' % bad

    print('[patch] %s: 修改 %d 项, 段长 %d 字节（不变）, 回读校验通过' % (path, hits, len(raw)))
    for ln in back.split(b'\n'):
        if any(k.encode() in ln for k in drop_keys):
            print('        ', ln.decode('utf-8', 'ignore'))

    if not dry:
        open(path, 'wb').write(d)
    return True

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('用法: patch_image_cfg.py <Image> <KEY1,KEY2,...>')
        sys.exit(1)
    # 参数可能用逗号或空格分隔（workflow 里是空格）
    raw = sys.argv[2].replace(',', ' ')
    keys = set(k for k in raw.split() if k)
    ok = patch_image(sys.argv[1], keys)
    sys.exit(0 if ok else 2)
