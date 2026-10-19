#!/usr/bin/env bash
#
# verify_umbrello.sh -- 用真实的 Umbrello 无界面地加载一个 XMI，
#                       把每张图导出成 PNG，用来验证「能不能打开、中文乱不乱」。
#
# 用法:
#   ./verify_umbrello.sh 图书管理系统.xmi [输出目录]
#
# 说明:
#   Umbrello 是 GUI 程序，这里用 QT_QPA_PLATFORM=offscreen 跑无界面模式，
#   再用 --export png 触发「加载 + 渲染」，因此能真实反映打开效果：
#     * 进程退出码为 0 且生成了 PNG  -> 文件被成功解析
#     * 生成的 PNG 文件名是正确的中文 -> 编码正确（乱码时会变成替换字符）
#     * 直接看 PNG 内容             -> 图和文字是否正确
#
#   本机如果没有装 Umbrello，脚本会提示两种安装方式后退出 2。

set -uo pipefail

XMI="${1:-}"
OUTDIR="${2:-}"

if [[ -z "$XMI" ]]; then
    echo "用法: $0 <file.xmi> [输出目录]" >&2
    exit 2
fi
if [[ ! -f "$XMI" ]]; then
    echo "找不到文件: $XMI" >&2
    exit 2
fi

XMI_ABS="$(readlink -f "$XMI")"
XMI_DIR="$(dirname "$XMI_ABS")"
XMI_BASE="$(basename "$XMI_ABS")"
OUTDIR="${OUTDIR:-$(mktemp -d /tmp/umbrello-verify-XXXXXX)}"
mkdir -p "$OUTDIR"
OUTDIR="$(readlink -f "$OUTDIR")"

# ---------------------------------------------------------------------------
# 找到可用的 umbrello 可执行程序（原生安装 / flatpak 都支持）
# ---------------------------------------------------------------------------
RUNNER=()
UML_BIN=""

for cand in umbrello6 umbrello; do
    if command -v "$cand" >/dev/null 2>&1; then
        RUNNER=("$cand"); UML_BIN="$cand"; break
    fi
done

if [[ -z "$UML_BIN" ]] && command -v flatpak >/dev/null 2>&1; then
    if flatpak info org.kde.umbrello >/dev/null 2>&1; then
        # flatpak 沙箱默认看不到家目录，这里按需把输入和输出目录暴露进去
        RUNNER=(flatpak run \
                --filesystem="$XMI_DIR" \
                --filesystem="$OUTDIR" \
                --command=umbrello6 org.kde.umbrello)
        UML_BIN="flatpak:org.kde.umbrello"
    fi
fi

if [[ -z "$UML_BIN" ]]; then
    cat >&2 <<'EOF'
没有找到 Umbrello。安装方式二选一：

  Arch / CachyOS : sudo pacman -S umbrello
  Debian / Ubuntu: sudo apt install umbrello
  免 root（flatpak）: flatpak --user install flathub org.kde.umbrello

装好后再运行本脚本。
EOF
    exit 2
fi

echo "== 用 $UML_BIN 加载并导出 =="
echo "   输入: $XMI_ABS"
echo "   输出: $OUTDIR"

LOG="$OUTDIR/umbrello.log"
QT_QPA_PLATFORM=offscreen "${RUNNER[@]}" \
    --export png --directory "$OUTDIR" "$XMI_ABS" >"$LOG" 2>&1
rc=$?

echo "== 退出码: $rc =="

# 解析失败时 Umbrello 会弹「Load Error」并写出这个标记
if grep -q "There was a problem loading" "$LOG"; then
    echo "!! Umbrello 报告加载失败：" >&2
    grep -A3 "There was a problem loading" "$LOG" >&2
    exit 1
fi

shopt -s nullglob
PNGS=("$OUTDIR"/*.png)
if (( ${#PNGS[@]} == 0 )); then
    echo "!! 没有导出任何 PNG，说明文档没有被正确加载" >&2
    tail -30 "$LOG" >&2
    exit 1
fi

echo
echo "== 导出的图 =="
printf '  %s\n' "${PNGS[@]##*/}"

# 乱码检测：解码失败时 Qt 会写入 U+FFFD (EF BF BD)，文件名里就会出现它
bad=0
for p in "${PNGS[@]}"; do
    if grep -qP '\xef\xbf\xbd' <<<"$p"; then
        echo "!! 文件名里出现替换字符 U+FFFD，编码很可能有问题: $p" >&2
        bad=1
    fi
done

if (( bad )); then
    exit 1
fi

echo
echo "OK：文档被成功加载，导出了 ${#PNGS[@]} 张图，文件名没有出现替换字符。"
echo "请打开上面的 PNG 目视确认中文是否正常。"
exit 0
