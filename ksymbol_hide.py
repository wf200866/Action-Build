#!/usr/bin/env python3
"""
KernelSU 内核符号去特征化。
在 kernel_workspace/kernel_platform 下执行：
    python3 ksymbol_hide.py ./KernelSU

效果：把 kallsyms 里暴露的 KSU 符号改成中性名。
"""
import os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'KernelSU'
if not os.path.isdir(ROOT):
    print(f'[!] 目录不存在: {ROOT}')
    sys.exit(1)

# 函数/变量名映射：旧 -> 新（全部中性，不含 ksu/su/root 字样之外的敏感词）
MAP = {
    'setup_ksu_cred':     'init_task_cred',
    'is_task_ksu_domain': 'is_task_special',
    'is_ksu_domain':      'is_special_domain',
    'anon_ksu_ioctl':     'anon_dev_ioctl',
    'anon_ksu_release':   'anon_dev_release',
    'anon_ksu_fops':      'anon_dev_fops',
    'ksu_sid':            'special_sid',
    'ksu_cred':           'special_cred',
}

SKIP_DIRS = {'.git', 'manager', 'userspace', 'website', '.github'}

changed = []
total_repl = 0
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fn in filenames:
        if not fn.endswith(('.c', '.h')):
            continue
        p = os.path.join(dirpath, fn)
        try:
            s = open(p, encoding='utf-8', errors='surrogateescape').read()
        except Exception:
            continue
        orig = s
        cnt = 0
        for old, new in MAP.items():
            s, n = re.subn(r'\b' + re.escape(old) + r'\b', new, s)
            cnt += n
        if s != orig:
            open(p, 'w', encoding='utf-8', errors='surrogateescape').write(s)
            changed.append((p, cnt))
            total_repl += cnt

print(f'[ksymbol] 修改 {len(changed)} 个文件, 共 {total_repl} 处替换')
for p, c in changed:
    print(f'    {c:3d} 处  {p}')

# 校验：确认源码里不再有旧名
left = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fn in filenames:
        if not fn.endswith(('.c', '.h')):
            continue
        p = os.path.join(dirpath, fn)
        try:
            s = open(p, encoding='utf-8', errors='surrogateescape').read()
        except Exception:
            continue
        for old in MAP:
            if re.search(r'\b' + re.escape(old) + r'\b', s):
                left.append((p, old))

if left:
    print('[!] 残留:')
    for p, o in left:
        print(f'    {o}  <- {p}')
    sys.exit(1)
print('[ksymbol] 校验通过：源码中已无旧符号名')
