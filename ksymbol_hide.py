#!/usr/bin/env python3
"""
KernelSU 内核符号去特征化 v2

目标：/proc/kallsyms 中不出现 ksu / kernelsu 字样

本版新增（基于实机验证结果）：
  1. kernelsu.o -> seccore.o   模块名，影响 __initstub__kmod_XXX
  2. kernelsu_init -> seccore_init   函数名
  3. kernelsu_init_early -> seccore_init_early
  4. 同步 Kbuild 里的 kernelsu-objs 和 obj-$(CONFIG_KSU)
  5. 同步内联汇编里的 "b kernelsu_init"

保留不改：
  - bb_bprm_set_creds.ksu_sid  属于 BBG 模块，不是 KSU 的
  - 其他 static 符号不进 kallsyms，改了只增加编译风险
"""
import os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'KernelSU'
if not os.path.isdir(ROOT):
    print(f'[ksymbol] 目录不存在: {ROOT}')
    sys.exit(1)

# 只改真正进 kallsyms 的符号。cacheable，不动其他。
CODE_MAP = {
    'setup_ksu_cred':     'init_task_cred',
    'is_task_ksu_domain': 'is_task_special',
    'anon_ksu_ioctl':     'anon_dev_ioctl',
    'anon_ksu_release':   'anon_dev_release',
    'anon_ksu_fops':      'anon_dev_fops',
    'ksu_sid':            'special_sid',
    'kernelsu_init_early':'seccore_init_early',   # 必须在前
    'kernelsu_init':      'seccore_init',
}

SKIP_DIRS = {'.git', 'manager', 'userspace', 'website', '.github', 'preinit'}
changed = []
total = 0

for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in SKIP_DIRS]
    for fn in fns:
        if not fn.endswith(('.c', '.h')):
            continue
        p = os.path.join(dp, fn)
        try:
            s = open(p, encoding='utf-8', errors='surrogateescape').read()
        except Exception:
            continue
        orig, cnt = s, 0
        for old, new in CODE_MAP.items():
            s, n = re.subn(r'\b' + re.escape(old) + r'\b', new, s)
            cnt += n
        if s != orig:
            open(p, 'w', encoding='utf-8', errors='surrogateescape').write(s)
            changed.append((p, cnt)); total += cnt

# --- Kbuild：模块名（位于 ROOT/kernel/Kbuild，不是 ROOT/Kbuild）---
kb = os.path.join(ROOT, 'kernel', 'Kbuild')
if not os.path.isfile(kb):
    kb = os.path.join(ROOT, 'Kbuild')
if os.path.isfile(kb):
    s = open(kb, encoding='utf-8', errors='surrogateescape').read()
    orig = s
    s, n1 = re.subn(r'\bkernelsu-objs\b', 'seccore-objs', s)
    s, n2 = re.subn(r'^(obj-\$\(CONFIG_KSU\) \+= )kernelsu\.o', r'\1seccore.o', s, flags=re.M)
    if s != orig:
        open(kb, 'w', encoding='utf-8', errors='surrogateescape').write(s)
        changed.append((kb, n1 + n2)); total += n1 + n2
        print('[ksymbol] Kbuild: kernelsu-objs x%d, obj->seccore.o x%d' % (n1, n2))

print(f'[ksymbol] 修改 {len(changed)} 个文件, 共 {total} 处替换')
for p, c in changed:
    print(f'    {c:3d} 处  {p}')

# --- 校验 ---
left = []
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in SKIP_DIRS]
    for fn in fns:
        if not fn.endswith(('.c', '.h')):
            continue
        p = os.path.join(dp, fn)
        try:
            s = open(p, encoding='utf-8', errors='surrogateescape').read()
        except Exception:
            continue
        for old in CODE_MAP:
            if re.search(r'\b' + re.escape(old) + r'\b', s):
                left.append((p, old))

if left:
    print('[ksymbol] !! 残留:')
    for p, o in left:
        print(f'    {o} <- {p}')
    sys.exit(1)

print('[ksymbol] 校验通过：目标符号已全部改名')
