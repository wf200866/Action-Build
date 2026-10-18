#!/bin/bash
# SukiSU Ultra builtin 与一加内核的 API 兼容补丁（自适应 sm8850 / sm7675）
#
# 关键教训：
#   绝不能用 `find /` 找内核头。GitHub runner 自带
#   /usr/src/linux-azure-6.17-headers-*/include/linux/lsm_hooks.h，
#   会误命中并把 lsm_id 分支错误打开，导致 6.1 内核编译失败：
#       hook/lsm_hook.c:3:22: error: variable has incomplete type 'struct lsm_id'
#
#   只允许在被编译的内核源码树内探测。

set -u

echo "=========================================="
echo "[fix] pwd=$(pwd)"
echo "=========================================="

# ---------------------------------------------------------------
# 定位 KernelSU 源码目录
# ---------------------------------------------------------------
KSUDIR=""
for d in "." "./KernelSU" "../KernelSU" "../../KernelSU" "./kernel/KernelSU" "../kernel/KernelSU"; do
  if [ -f "$d/sulog/event.c" ] || [ -f "$d/hook/lsm_hook.c" ]; then
    KSUDIR="$d"
    break
  fi
done

if [ -z "$KSUDIR" ]; then
  echo "[fix] fallback: find event.c under sulog/"
  FOUND=$(find . -maxdepth 6 -name event.c -path '*/sulog/*' 2>/dev/null | head -1)
  [ -n "$FOUND" ] && KSUDIR=$(cd "$(dirname "$FOUND")/.." && pwd)
fi

echo "[fix] KSUDIR='$KSUDIR'"
if [ -z "$KSUDIR" ] || [ ! -d "$KSUDIR" ]; then
  echo "[ERR] KernelSU source not found. Dumping candidates:"
  find . -maxdepth 6 -name 'lsm_hook.c' 2>/dev/null | head -10
  find . -maxdepth 6 -name 'event.c' -path '*sulog*' 2>/dev/null | head -10
  exit 0
fi

cd "$KSUDIR" || exit 0
echo "[fix] cd -> $(pwd)"

# ---------------------------------------------------------------
# 探测内核头：只在内核源码树内找
# ---------------------------------------------------------------
HAS_LSMID=0
LKH=""
for r in ../../common ../../msm-kernel ../common ../msm-kernel ../../../common; do
  if [ -f "$r/include/linux/lsm_hooks.h" ]; then
    LKH="$r/include/linux/lsm_hooks.h"
    break
  fi
done

if [ -z "$LKH" ]; then
  echo "[probe] fallback: bounded find under $(cd ../.. 2>/dev/null && pwd)"
  CAND=$(find "$(cd ../.. 2>/dev/null && pwd)" -maxdepth 4 -type f \
           -path '*/include/linux/lsm_hooks.h' 2>/dev/null | head -3)
  echo "[probe] candidates:"; echo "$CAND"
  [ -n "$CAND" ] && LKH=$(echo "$CAND" | head -1)
fi

echo "[probe] lsm_hooks.h = ${LKH:-<not found>}"

if [ -n "$LKH" ] && [ -f "$LKH" ]; then
  echo "[probe] security_add_hooks 定义:"
  grep -n -A2 'extern void security_add_hooks' "$LKH" 2>/dev/null | head -6
  if grep -q 'const struct lsm_id \*lsmid' "$LKH" 2>/dev/null; then
    HAS_LSMID=1
  fi
fi
echo "[probe] HAS_LSMID=$HAS_LSMID"

if [ "$HAS_LSMID" = "1" ]; then
  if ! grep -q 'KSU_NEED_LSM_ID' Makefile 2>/dev/null; then
    printf '%s\n' 'ccflags-y += -D__KSU_NEED_LSM_ID' >> Makefile
    echo "[fix] Makefile: -D__KSU_NEED_LSM_ID added"
  else
    echo "[fix] Makefile: already present"
  fi
else
  echo "[fix] Makefile: 6.1 老签名，不加 lsm_id 宏"
  # 保险：若之前误加过，删掉
  if grep -q 'KSU_NEED_LSM_ID' Makefile 2>/dev/null; then
    sed -i '/KSU_NEED_LSM_ID/d' Makefile
    echo "[fix] Makefile: 已移除误加的 lsm_id 宏"
  fi
fi

# ---------------------------------------------------------------
# 修复 1: USER_ARG_NULL（与内核版本无关，任何内核都要修）
# ---------------------------------------------------------------
if [ -f sulog/event.c ]; then
  if grep -q '#define USER_ARG_NULL user_arg_null_ptr()' sulog/event.c; then
    sed -i 's/#define USER_ARG_NULL user_arg_null_ptr()/#define USER_ARG_NULL (*user_arg_null_ptr())/' sulog/event.c
    echo "[PATCH1] OK  USER_ARG_NULL -> (*user_arg_null_ptr())"
  else
    echo "[PATCH1] already patched or absent"
  fi
else
  echo "[PATCH1] FAIL sulog/event.c missing"
fi

# ---------------------------------------------------------------
# 修复 2: security_add_hooks（仅当内核确实要 const struct lsm_id *）
# ---------------------------------------------------------------
if [ -f hook/lsm_hook.c ]; then
  if [ "$HAS_LSMID" != "1" ]; then
    echo "[PATCH2] skip: kernel uses const char *lsm, 保持原样"
    # 若曾被误打过，回滚
    if grep -q '__KSU_LSM_ID_PATCHED' hook/lsm_hook.c; then
      sed -i '/__KSU_LSM_ID_PATCHED/d' hook/lsm_hook.c
      echo "[PATCH2] 已删除误注入的注释行"
    fi
  elif grep -q '__KSU_LSM_ID_PATCHED' hook/lsm_hook.c; then
    echo "[PATCH2] already patched"
  else
    {
      echo '/* __KSU_LSM_ID_PATCHED: kernel uses struct lsm_id { const char *name; u64 id; } */'
      echo '#ifdef __KSU_NEED_LSM_ID'
      echo 'static struct lsm_id ksu_lsmid = {'
      echo '    .name = "ksu",'
      echo '    .id = 500,'
      echo '};'
      echo '#endif'
      cat hook/lsm_hook.c
    } > /tmp/_kh.c
    mv /tmp/_kh.c hook/lsm_hook.c
    echo "[PATCH2a] header block injected"

    awk '
      /security_add_hooks\(ksu_hooks, ARRAY_SIZE\(ksu_hooks\), "ksu"\);/ {
        print "#ifdef __KSU_NEED_LSM_ID"
        print "    security_add_hooks(ksu_hooks, ARRAY_SIZE(ksu_hooks), &ksu_lsmid);"
        print "#else"
        print "    security_add_hooks(ksu_hooks, ARRAY_SIZE(ksu_hooks), \"ksu\");"
        print "#endif"
        next
      }
      { print }
    ' hook/lsm_hook.c > /tmp/_kh2.c && mv /tmp/_kh2.c hook/lsm_hook.c
    echo "[PATCH2b] call site wrapped"
  fi
else
  echo "[PATCH2] FAIL hook/lsm_hook.c missing"
fi

# ---------------------------------------------------------------
# 回显
# ---------------------------------------------------------------
echo "--- verify sulog/event.c ---"
grep -n 'USER_ARG_NULL' sulog/event.c 2>/dev/null | head -3
echo "--- verify hook/lsm_hook.c ---"
grep -n 'ksu_lsmid\|security_add_hooks\|__KSU_LSM_ID_PATCHED' hook/lsm_hook.c 2>/dev/null | head -12
echo "--- verify Makefile tail ---"
tail -4 Makefile 2>/dev/null
echo "=============================="
echo "[fix] done"
echo "=============================="
exit 0