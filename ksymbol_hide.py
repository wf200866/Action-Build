#!/usr/bin/env python3
"""
KernelSU 内核符号去特征化（最小改动版）。

只改【真正会出现在 /proc/kallsyms 里】的符号：
    实测暴露清单（非 root 进程可见）:
        setup_ksu_cred              -> init_task_cred
        is_task_ksu_domain          -> is_task_special
        anon_ksu_ioctl              -> anon_dev_ioctl
        anon_ksu_release            -> anon_dev_release
        anon_ksu_fops              -> anon_dev_fops
        bb_bprm_set_creds.ksu_sid   -> (自动跟随 ksu_sid 改名)
        ksu_sid                     -> special_sid

不动任何其他 ksu_* 符号：它们都是 static，不进 kallsyms，
改了只会打破头文件/源文件的一致性，导致编译失败。
"""
import os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'KernelSU'
if not os.path.isdir(ROOT):
    print(f'[ksymbol] 目录不存在: {ROOT}')
    sys.exit(1)

MAP = {
    'setup_ksu_cred':     'init_task_cred',
    'is_task_ksu_domain': 'is_task_special',
    'anon_ksu_ioctl':     'anon_dev_ioctl',
    'anon_ksu_release':   'anon_dev_release',
    'anon_ksu_fops':      'anon_dev_fops',
    'ksu_sid':            'special_sid',
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
        for old, new in MAP.items():
            s, n = re.subn(r'\b' + re.escape(old) + r'\b', new, s)
            cnt += n
        if s != orig:
            open(p, 'w', encoding='utf-8', errors='surrogateescape').write(s)
            changed.append((p, cnt)); total += cnt

print(f'[ksymbol] 修改 {len(changed)} 个文件, 共 {total} 处替换')
for p, c in changed:
    print(f'    {c:3d} 处  {p}')

# 校验
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
        for old in MAP:
            if re.search(r'\b' + re.escape(old) + r'\b', s):
                left.append((p, old))

if left:
    print('[ksymbol] !! 残留:')
    for p, o in left:
        print(f'    {o} <- {p}')
    sys.exit(1)
print('[ksymbol] 校验通过：目标符号已全部改名')
