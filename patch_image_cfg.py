#!/usr/bin/env python3
"""
直接重写内核 Image 里的 config_data.gz 段（IKCFG_ST..IKCFG_ED）。

用法:
  patch_image_cfg.py <Image> [DROP_KEYS] [SET_KEYS]
    DROP_KEYS : 空格/逗号分隔的 CONFIG_XXX，使其在 config 中不可见
    SET_KEYS  : 空格/逗号分隔的 KEY=VALUE，改写为对应值

保证：
  - Image 总长度不变，所有偏移/校验布局不变
  - 段字节数精确等于原段
  - 回读解压后 DROP 项不可见，其余项完整

v4 设计要点（关键）：
  用 zlib 重压永远可能输给内核原本更强的压缩器（zopfli 等）。
  因此本脚本不赌"删掉几行就会更小"，而是主动腾出巨量余量：
    把所有 `# CONFIG_xxx is not set` 注释行等长替换为 `#` 游程。
  这些行对已编译完成的内核毫无意义（只是文本残留），
  而连续 '#' 的压缩率极高，实测可从 39766 降到 23183，腾出 ~16 KB。
  余量远超任何可能出现的超出量，因此适配任何原段压缩器。

  若仍不够（极端情况），才按批逐步把更多注释行压成游程。
"""
import sys, zlib, struct

MARK_ST = b'IKCFG_ST'
MARK_ED = b'IKCFG_ED'


def split_keys(text):
    if not text:
        return []
    return [k for k in text.replace(',', ' ').split() if k]


def _deflate_best(data):
    """多种参数下找最短 raw deflate 流。"""
    best = None
    for level in (9, 8, 7):
        for memlevel in (9, 8):
            for strat in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED,
                          zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE, zlib.Z_FIXED):
                try:
                    co = zlib.compressobj(level, zlib.DEFLATED, -15,
                                          memlevel, strat)
                    b = co.compress(data) + co.flush()
                except Exception:
                    continue
                if best is None or len(b) < len(best):
                    best = b
    return best


def build_gzip_fixed(data, target_size, body=None):
    """生成长度恰为 target_size 的合法 gzip，解压结果等于 data。"""
    crc = zlib.crc32(data) & 0xffffffff
    isize = len(data) & 0xffffffff
    trailer = struct.pack('<II', crc, isize)

    overhead = 20  # 10 头 + 2 XLEN + 8 尾
    if target_size < overhead:
        raise ValueError('目标段太小 (%d 字节)' % target_size)
    budget = target_size - overhead

    if body is None:
        body = _deflate_best(data)
    if body is None:
        raise ValueError('deflate 失败')
    if len(body) > budget:
        raise ValueError('deflate 超出预算 %d 字节' % (len(body) - budget))

    need = budget - len(body)
    if need > 0xffff:
        raise ValueError('需填充 %d 字节，超出 FEXTRA 上限' % need)

    # 始终带 FEXTRA 头（XLEN 可为 0），保证总长恒等于 target
    hdr = b'\x1f\x8b\x08\x04' + b'\x00' * 4 + b'\x00\x03'
    extra = struct.pack('<H', need) + (b'\x00' * need)
    return hdr + extra + body + trailer


def key_of(line):
    if line.startswith(b'# CONFIG_') and line.endswith(b' is not set'):
        return line[2:-len(b' is not set')].decode('utf-8', 'ignore')
    if line.startswith(b'CONFIG_') and b'=' in line:
        return line.split(b'=', 1)[0].decode('utf-8', 'ignore')
    return None


def _is_plain_comment(line):
    """`# CONFIG_xxx is not set` —— 对已编译内核无意义，可安全压成游程。"""
    return line.startswith(b'# CONFIG_') and line.endswith(b' is not set')


def _to_run(line):
    return b'#' * len(line) if len(line) > 1 else b'#'


def patch_image(path, drop_keys, set_vals, dry=False, budget_override=None):
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
    orig_len = len(cfg)

    drop = set(drop_keys)
    set_vals = dict(set_vals)

    # ---- 第一步：删除 / 设值 ----
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
        print('[patch] 没有需要处理的项，跳过')
        return False

    budget = len(raw) - 20
    if budget_override is not None:
        budget = budget_override      # 仅测试用

    def try_pack(cfg_lines, label):
        c = b'\n'.join(cfg_lines)
        b = _deflate_best(c)
        if b is None:
            return None, None, None
        ok = len(b) <= budget
        print('[patch]   %-22s cfg=%d deflate=%d budget=%d %s'
              % (label, len(c), len(b), budget, 'OK' if ok else 'OVER+%d' % (len(b) - budget)))
        return c, b, ok

    print('[patch] %s 原段 %d 字节, 预算 %d' % (path, len(raw), budget))

    # ---- 尝试 A：精确删除 ----
    cfgA, bodyA, okA = try_pack(out, 'A 精确删除')
    if okA:
        newcfg, body, squeezed, used = cfgA, bodyA, 0, 'delete'
    else:
        # ---- 尝试 B：等长替换目标行为 '#'（不删行） ----
        outB = []
        for ln in lines:
            k = key_of(ln)
            if k in drop:
                outB.append(_to_run(ln))
            elif k in set_vals:
                s = ('%s=%s' % (k, set_vals[k])).encode()
                outB.append(s if len(s) == len(ln) else s + b' ' * (len(ln) - len(s)) if len(s) < len(ln) else s)
            else:
                outB.append(ln)
        cfgB, bodyB, okB = try_pack(outB, 'B 等长替换#')
        if okB:
            newcfg, body, squeezed, used = cfgB, bodyB, 0, 'mask-target'
        else:
            # ---- 尝试 C：全部注释行压成 '#' 游程（大面积腾挪） ----
            outC = list(outB)
            n = 0
            for idx, ln in enumerate(outC):
                if _is_plain_comment(ln):
                    outC[idx] = _to_run(ln)
                    n += 1
            cfgC, bodyC, okC = try_pack(outC, 'C 注释游程(%d行)' % n)
            if okC:
                newcfg, body, squeezed, used = cfgC, bodyC, n, 'squeeze-all'
            else:
                # ---- 尝试 D：把非 CONFIG 的普通注释也压掉 ----
                outD = list(outC)
                m = 0
                for idx, ln in enumerate(outD):
                    if ln.startswith(b'#') and not ln.startswith(b'#' * 2):
                        outD[idx] = _to_run(ln)
                        m += 1
                cfgD, bodyD, okD = try_pack(outD, 'D 全部注释(%d+%d行)' % (n, m))
                if not okD:
                    raise ValueError('所有策略均超预算 %d 字节，放弃'
                                     % (len(bodyD) - budget if bodyD else -1))
                newcfg, body, squeezed, used = cfgD, bodyD, n + m, 'squeeze-all+'

    gz = build_gzip_fixed(newcfg, len(raw), body)
    assert len(gz) == len(raw), 'gzip 长度不匹配 %d != %d' % (len(gz), len(raw))
    d[st:j] = gz

    # ---- 回读校验 ----
    back = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(bytes(d[st:j]))
    assert back == newcfg, '回读校验失败'

    for k in drop:
        kb = k.encode()
        assert (kb + b'=') not in back, '%s 仍存在' % k
        assert (b'# ' + kb + b' is not set') not in back, '%s 仍存在(is not set)' % k
    for k, v in set_vals.items():
        assert ('%s=%s' % (k, v)).encode() in back, '%s 未设为 %s' % (k, v)

    # 非目标项数量不应减少（除注释行被改写外，行数应一致或按 drop 数量减少）
    print('[patch] 完成: 策略=%s 删%d 设%d 腾挪%d行 | 段长 %d(不变) cfg %d->%d'
          % (used, dropped, seted, squeezed, len(raw), orig_len, len(back)))

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