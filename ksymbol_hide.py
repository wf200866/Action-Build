#!/usr/bin/env python3
"""
KernelSU 内核符号去特征化 v3

目标：/proc/kallsyms 中不出现 ksu / kernelsu 字样

关键机制（已实测确认）：
  __initcall_id(fn) = <__KBUILD_MODNAME>__<__COUNTER__>_<__LINE__>_<fn>
  __KBUILD_MODNAME  = kmod_$(modname)
  modname           = multi-used 命中则用它，否则 basetarget
  multi-used        = 由 obj-y 的 multi-search 递归展开得到

  因此光改 kernel/Kbuild 的 obj-$(CONFIG_KSU) 不够 ——
  drivers/Makefile 里 `obj-$(CONFIG_KSU) += kernelsu/` 这个【目录名】
  会先进入 multi-search，把 kernelsu 变成候选 modname。
  必须连目录名一起改（drivers/Makefile + 软链接）。

改动项：
  1. 代码符号（进 kallsyms 的部分）
  2. kernel/Kbuild: kernelsu-objs -> seccore-objs, obj += seccore.o
  3. drivers/Makefile: kernelsu/ -> seccore/
  4. 重建软链接 drivers/seccore -> KernelSU/kernel

不改：bb_bprm_set_creds.ksu_sid（BBG 的，不是 KSU）
"""
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'KernelSU'
if not os.path.isdir(ROOT):
    print('[ksymbol] 目录不存在: %s' % ROOT)
    sys.exit(1)

CODE_MAP = {
    'setup_ksu_cred': 'init_task_cred',
    'is_task_ksu_domain': 'is_task_special',
    'anon_ksu_ioctl': 'anon_dev_ioctl',
    'anon_ksu_release': 'anon_dev_release',
    'anon_ksu_fops': 'anon_dev_fops',
    'ksu_sid': 'special_sid',
    'kernelsu_init_early': 'seccore_init_early',   # 必须在 kernelsu_init 之前
    'kernelsu_init': 'seccore_init',
}

SKIP_DIRS = {'.git', 'manager', 'userspace', 'website', '.github', 'preinit'}
changed = []
total = 0

# ---------- 1. 代码符号 ----------
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in SKIP_DIRS]
    for fn in fns:
        if not fn.endswith(('.c', '.h')):
            continue
        p = os.path.join(dp, fn)
        try:
            with open(p, encoding='utf-8', errors='surrogateescape') as f:
                s = f.read()
        except Exception:
            continue
        orig, cnt = s, 0
        for old, new in CODE_MAP.items():
            s, n = re.subn(r'\b' + re.escape(old) + r'\b', new, s)
            cnt += n
        if s != orig:
            with open(p, 'w', encoding='utf-8', errors='surrogateescape') as f:
                f.write(s)
            changed.append((p, cnt))
            total += cnt

# ---------- 2. kernel/Kbuild ----------
kb = os.path.join(ROOT, 'kernel', 'Kbuild')
if not os.path.isfile(kb):
    kb = os.path.join(ROOT, 'Kbuild')
if os.path.isfile(kb):
    with open(kb, encoding='utf-8', errors='surrogateescape') as f:
        s = f.read()
    orig = s
    s, n1 = re.subn(r'\bkernelsu-objs\b', 'seccore-objs', s)
    s, n2 = re.subn(r'(obj-\$\(CONFIG_KSU\)\s*\+=\s*)kernelsu\.o', r'\1seccore.o', s)
    if s != orig:
        with open(kb, 'w', encoding='utf-8', errors='surrogateescape') as f:
            f.write(s)
        changed.append((kb, n1 + n2))
        total += n1 + n2
        print('[ksymbol] Kbuild: kernelsu-objs x%d, obj -> seccore.o x%d' % (n1, n2))
else:
    print('[ksymbol] !! 未找到 Kbuild')

# ---------- 3+4. drivers/Makefile 与软链接 ----------
dad = None
candidates = [
    # kernel_platform/KernelSU -> kernel_platform/common/drivers
    os.path.abspath(os.path.join(ROOT, '..', 'common', 'drivers', 'Makefile')),
    os.path.abspath(os.path.join(ROOT, '..', '..', 'common', 'drivers', 'Makefile')),
    os.path.abspath(os.path.join(ROOT, '..', '..', 'drivers', 'Makefile')),
    os.path.abspath(os.path.join(ROOT, '..', 'drivers', 'Makefile')),
]
for c in candidates:
    if os.path.isfile(c):
        dad = c
        print('[ksymbol] drivers/Makefile = %s' % c)
        break

if dad:
    with open(dad, encoding='utf-8', errors='surrogateescape') as f:
        s = f.read()
    if 'kernelsu/' in s:
        s2 = re.sub(r'(obj-\$\(CONFIG_KSU\)\s*\+=\s*)kernelsu/', r'\1seccore/', s)
        if s2 != s:
            with open(dad, 'w', encoding='utf-8', errors='surrogateescape') as f:
                f.write(s2)
            print('[ksymbol] drivers/Makefile: obj += seccore/')
        else:
            print('[ksymbol] !! drivers/Makefile 含 kernelsu/ 但正则未命中')
    else:
        print('[ksymbol] drivers/Makefile 无 kernelsu/ 条目')

    ddir = os.path.dirname(dad)
    newlink = os.path.join(ddir, 'seccore')
    kdir = None
    for cand in (os.path.join(ROOT, 'kernel'),
                 os.path.abspath(os.path.join(ROOT, '..', 'KernelSU', 'kernel')),
                 os.path.abspath(os.path.join(ROOT, '..', '..', 'KernelSU', 'kernel'))):
        if os.path.isdir(cand):
            kdir = os.path.abspath(cand)   # 必须绝对路径，否则相对 drivers/ 解析错误
            break
    if kdir:
        if os.path.islink(newlink) or os.path.exists(newlink):
            try:
                os.remove(newlink)
            except Exception:
                pass
        try:
            os.symlink(kdir, newlink)
            print('[ksymbol] symlink %s -> %s' % (newlink, kdir))
        except Exception as e:
            print('[ksymbol] !! symlink 失败: %s' % e)
    else:
        print('[ksymbol] !! 未找到 KernelSU/kernel 目录')
else:
    print('[ksymbol] (未找到 drivers/Makefile，跳过目录改名)')

print('[ksymbol] 修改 %d 个文件, 共 %d 处替换' % (len(changed), total))
for p, c in changed:
    print('    %3d 处  %s' % (c, p))

# ---------- 校验 ----------
left = []
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in SKIP_DIRS]
    for fn in fns:
        if not fn.endswith(('.c', '.h')):
            continue
        p = os.path.join(dp, fn)
        try:
            with open(p, encoding='utf-8', errors='surrogateescape') as f:
                s = f.read()
        except Exception:
            continue
        for old in CODE_MAP:
            if re.search(r'\b' + re.escape(old) + r'\b', s):
                left.append((p, old))

if left:
    print('[ksymbol] !! 残留:')
    for p, o in left:
        print('    %s <- %s' % (o, p))
    sys.exit(1)

print('[ksymbol] 校验通过：目标符号已全部改名')