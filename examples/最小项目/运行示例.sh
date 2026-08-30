#!/bin/sh

set -eu

SPECSTAMP_BIN=${SPECSTAMP_BIN:-specstamp}
script_dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)

if ! command -v "$SPECSTAMP_BIN" >/dev/null 2>&1; then
    printf '%s\n' "错误：找不到 SpecStamp 命令：$SPECSTAMP_BIN" >&2
    printf '%s\n' "请先安装 SpecStamp，或用 SPECSTAMP_BIN 指定可执行文件。" >&2
    exit 1
fi

if [ -n "${SPECSTAMP_DEMO_DIR:-}" ]; then
    demo_dir=$SPECSTAMP_DEMO_DIR
    mkdir -p "$demo_dir"
else
    demo_dir=$(mktemp -d "${TMPDIR:-/tmp}/specstamp-example.XXXXXX")
fi

if [ -e "$demo_dir/.codex-sdlc" ]; then
    printf '%s\n' "错误：示例目录已经初始化：$demo_dir" >&2
    exit 1
fi

cp "$script_dir/需求资料.md" "$demo_dir/需求资料.md"
cd "$demo_dir"

printf '%s\n' '$ specstamp init-plain'
"$SPECSTAMP_BIN" init-plain

printf '%s\n' '$ specstamp draft create "待办清单"'
"$SPECSTAMP_BIN" draft create "待办清单"

printf '%s\n' '$ specstamp material DRAFT-001 --title "待办清单需求" --type requirement --file 需求资料.md'
"$SPECSTAMP_BIN" material DRAFT-001 \
    --title "待办清单需求" \
    --type requirement \
    --file 需求资料.md

printf '%s\n' '$ specstamp next'
"$SPECSTAMP_BIN" next

printf '\n示例运行完成，结果保留在：%s\n' "$demo_dir"
