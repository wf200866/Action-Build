#!/usr/bin/env python3
"""
KernelSU 内核符号去特征化 v4

目标：/proc/kallsyms 中不出现 ksu / kernelsu / KSU 专属词汇

== 关键机制（实测确认）==
  __initcall_id(fn) = <__KBUILD_MODNAME>__<__COUNTER__>_<__LINE__>_<fn>
  __KBUILD_MODNAME  = kmod_$(modname)
  modname           = multi-used 命中则用它，否则 basetarget
  multi-used        = 由 obj-y 的 multi-search 递归展开

  因此光改 kernel/Kbuild 不够：drivers/Makefile 里
  `obj-$(CONFIG_KSU) += kernelsu/` 这个【目录名】会先进入
  multi-search，把 kernelsu 变成候选 modname。
  必须连目录名一起改（drivers/Makefile + 软链接）。

== v4 新增 ==
  第二批符号：这些是 static 但被 CFI / __ADDRESSABLE 提升进 kallsyms 的。
  实测设备上暴露的：
    kernel_umount_feature_get/set$hash(+.cfi_jt)
    kernel_umount_handler
    sulog_feature_get/set$hash(+.cfi_jt)
    do_get_sulog_fd$hash(+.cfi_jt)
    sulog_handler
    sulog_queue.llvm.NNN
    do_get_app_profile$hash(+.cfi_jt)
    do_set_app_profile$hash(+.cfi_jt)
    throne_tracker_mutex
    allowlist_mutex
    is_init_second_stage_not_executed
    is_first_zygote

== 明确不改 ==
  bb_bprm_set_creds.ksu_sid / cached_su_sid   属于 BBG(LKM_BBG)，不是 KSU
  btf_allowlist_d_path                         内核 BPF 的 btf_ids，不是 KSU
  allowlist_names                              紧邻 initial_sid_to_string，
                                               属 SELinux 而非 KSU（行号相隔 7 万）
  .name = "kernel_umount" / "sulog"            是 feature_id 注册字符串，
                                               改了会破坏用户态协议
  me.weishu.kernelsu                           管理器签名校验用的包名，绝不能改
"""
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'KernelSU'
if not os.path.isdir(ROOT):
    print('[ksymbol] 目录不存在: %s' % ROOT)
    sys.exit(1)

# 顺序敏感：长名在前，避免前缀误替换
CODE_MAP = {
    # --- 第一批（v2 起）---
    'kernelsu_init_early': 'seccore_init_early',
    'kernelsu_init': 'seccore_init',
    'setup_ksu_cred': 'init_task_cred',
    'is_task_ksu_domain': 'is_task_special',
    'anon_ksu_ioctl': 'anon_dev_ioctl',
    'anon_ksu_release': 'anon_dev_release',
    'anon_ksu_fops': 'anon_dev_fops',
    'ksu_sid': 'special_sid',

    # --- 第二批（v4）：feature handler ---
    'kernel_umount_feature_get': 'kcore_feature_get_a',
    'kernel_umount_feature_set': 'kcore_feature_set_a',
    'kernel_umount_handler': 'kcore_handler_a',
    'sulog_feature_get': 'kcore_feature_get_b',
    'sulog_feature_set': 'kcore_feature_set_b',
    'sulog_handler': 'kcore_handler_b',
    'sulog_queue': 'kcore_queue_b',
    'do_get_sulog_fd': 'kcore_get_fd_b',

    # --- 第二批：supercall dispatch ---
    'do_get_app_profile': 'kcore_get_prof',
    'do_set_app_profile': 'kcore_set_prof',

    # --- 第二批：静态变量 ---
    'throne_tracker_mutex': 'mgr_track_mutex',
    'allowlist_mutex': 'perm_mutex',
    # ksu10.patch 把函数内 static 提升成了全局 static_key，
    # 名字变成 is_* 前缀（构建机上的实际形态）；
    # 未打补丁的上游源码则是 first_zygote / init_second_stage_executed。
    # 两个形态都要覆盖。
    'is_init_second_stage_not_executed': 'stage_done_b',
    'is_first_zygote': 'stage_flag_a',
    'init_second_stage_executed': 'stage_done_b',
    'first_zygote': 'stage_flag_a',
}

# 注意：'manager' 不能跳过 —— kernel/manager/throne_tracker.c 里有
# throne_tracker_mutex，它会进 kallsyms。早期版本误跳过导致漏改。
# 顶层 ROOT 下的 manager/（管理器 UI，Kotlin）用路径判断排除，见下方 in_kernel_code。
SKIP_DIRS = {'.git', 'userspace', 'website', '.github', 'preinit'}


def is_manager_ui(path):
    """ROOT/manager 是 KernelSU 管理器（Kotlin/Java），不改；
    但 ROOT/kernel/manager 是内核代码，必须改。"""
    rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
    return rel == 'manager' or rel.startswith('manager/')


changed = []
total = 0

# ---------- 1. 代码符号 ----------
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns
              if d not in SKIP_DIRS and not is_manager_ui(os.path.join(dp, d))]
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
    dns[:] = [d for d in dns
              if d not in SKIP_DIRS and not is_manager_ui(os.path.join(dp, d))]
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