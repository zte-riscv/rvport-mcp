#!/usr/bin/env bash
# 全新设备自举启动器：确保依赖就绪后拉起本地 stdio MCP server。
#
# 快路径：选中的 Python 已能 import mcp → 直接启动（零安装、零等待）。
# 慢路径：在用户缓存目录建独立 venv（不污染系统 site-packages，也不写
#         只读的 ~/.claude/plugins/cache 插件目录），只装最小依赖 mcp。
#
# 解释器选择：依次探测 python3.12/3.11/3.10/python3，取第一个版本 >= 3.10
# 的（mcp 1.x Requires-Python >= 3.10；RHEL8 系默认 python3 是 3.9，装包会报
# "No matching distribution found"，装了 pip 新版也没用——那是解释器过滤）。
#
# 环境变量：
#   RISCV_MCP_PYTHON  指定解释器（跳过自动探测，需 >= 3.10）
#   RISCV_MCP_VENV    自举 venv 位置（默认 ~/.cache/riscv-doc-rag/venv）
#   RISCV_MCP_FULL=1  自举时连同 fastembed/numpy 一起装（hybrid 模式；
#                     默认不装，检索自动降级纯 BM25，功能完整可用）
#   PIP_INDEX_URL     pip 源（内网镜像可由此覆盖）
#   HF_ENDPOINT       模型下载镜像（默认 https://hf-mirror.com）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

# ---- 解释器选择：>= 3.10（mcp 1.x 的硬性要求）----
MIN_PY="3.10"

py_version() { "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null; }
version_ge() {  # version_ge 实际版本 最低版本
  [ "$(printf '%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}

pick_python() {
  local cand ver
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    ver="$(py_version "$cand")" || continue
    if version_ge "$ver" "$MIN_PY"; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

if [ -n "${RISCV_MCP_PYTHON:-}" ]; then
  PYTHON="$RISCV_MCP_PYTHON"
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[mcp-bootstrap] 找不到 Python 解释器: $PYTHON" >&2
    exit 1
  fi
  ver="$(py_version "$PYTHON")"
  if ! version_ge "$ver" "$MIN_PY"; then
    echo "[mcp-bootstrap] $PYTHON 是 $ver，需要 >= $MIN_PY（mcp 1.x 硬性要求）" >&2
    exit 1
  fi
else
  PYTHON="$(pick_python)" || {
    echo "[mcp-bootstrap] 未找到 >= $MIN_PY 的 Python（RHEL8 系默认 python3 为 3.9）。" >&2
    echo "[mcp-bootstrap] 请安装 python3.10+（dnf/yum install python3.11 或编译安装），或用 RISCV_MCP_PYTHON 指定" >&2
    exit 1
  }
fi

run_with() { exec "$1" "$HERE/server.py" "${@:2}"; }

# ---- 快路径：任一候选解释器已有 mcp SDK，直接启动（零安装零等待）----
# 逐候选探测而非只看选中者：常见"新解释器(3.13)没装 SDK 而旧解释器(3.12)已装"，
# 只探测选中者会白白走 venv 慢路径。
for cand in "$PYTHON" python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c "import mcp.server.fastmcp" >/dev/null 2>&1; then
    PYTHON="$cand"
    break
  fi
done
if "$PYTHON" -c "import mcp.server.fastmcp" >/dev/null 2>&1; then
  run_with "$PYTHON" "$@"
fi

# ---- 慢路径：自举独立 venv ----
BOOT_VENV="${RISCV_MCP_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/riscv-doc-rag/venv}"

try_venv_with() {  # try_venv_with 解释器：全新建 venv 且 pip 可用则返回 0
  local py="$1"
  rm -rf "$BOOT_VENV"
  "$py" -m venv "$BOOT_VENV" 2>/dev/null || return 1
  # venv 建成但 ensurepip 失败（无 pip）也视为不可用，换下一候选
  "$BOOT_VENV/bin/python" -m pip --version >/dev/null 2>&1
}

if [ -x "$BOOT_VENV/bin/python" ]; then
  # venv 已存在：版本不够（旧版 start.sh 用 3.9 建）则重建
  if ! version_ge "$(py_version "$BOOT_VENV/bin/python")" "$MIN_PY"; then
    echo "[mcp-bootstrap] venv 是 $(py_version "$BOOT_VENV/bin/python")，需重建为 >= $MIN_PY" >&2
    rm -rf "$BOOT_VENV"
  fi
fi

if [ ! -x "$BOOT_VENV/bin/python" ]; then
  # 逐候选全新试建（按解析路径去重）：同版本可能存在多个发行
  # （如本机 3.13 ensurepip 损坏而 3.12 正常）
  created=""
  tried_paths=""
  for cand in "$PYTHON" python3.13 python3.12 python3.11 python3.10; do
    bin="$(command -v "$cand" 2>/dev/null)" || continue
    case ":$tried_paths:" in *":$bin:"*) continue ;; esac
    tried_paths=":$bin$tried_paths"
    ver="$(py_version "$bin")" || continue
    version_ge "$ver" "$MIN_PY" || continue
    echo "[mcp-bootstrap] 创建 venv: $BOOT_VENV（$cand, $ver）" >&2
    if try_venv_with "$bin"; then
      created="$cand"
      break
    fi
    echo "[mcp-bootstrap] $cand 建不了可用 venv（ensurepip/pip 异常），换下一个候选" >&2
  done
  if [ -z "$created" ]; then
    echo "[mcp-bootstrap] 所有候选解释器都建不出可用 venv。可手动建好后用 RISCV_MCP_VENV 指向它：" >&2
    echo "[mcp-bootstrap]   python3.12 -m venv ~/.cache/riscv-doc-rag/venv" >&2
    exit 1
  fi
fi
VPY="$BOOT_VENV/bin/python"

if ! "$VPY" -c "import mcp.server.fastmcp" >/dev/null 2>&1; then
  echo "[mcp-bootstrap] 安装最小依赖 mcp（首次约 1-2 分钟；pip 源可用 PIP_INDEX_URL 覆盖）" >&2
  # 钉 mcp<2：2.x 将 FastMCP 改名 MCPServer，server.py 按 v1 API 编写
  if ! "$VPY" -m pip install --quiet "mcp>=1.0.0,<2"; then
    echo "[mcp-bootstrap] mcp 安装失败。常见原因：" >&2
    echo "[mcp-bootstrap]   1) 网络不通 pypi —— 设 PIP_INDEX_URL 指向内网/国内镜像" >&2
    echo "[mcp-bootstrap]   2) pip 过旧不识别新 wheel 标签 —— 升级 venv 内 pip: $VPY -m pip install --upgrade pip" >&2
    exit 1
  fi
  if [ "${RISCV_MCP_FULL:-0}" = "1" ]; then
    "$VPY" -m pip install --quiet -r "$HERE/requirements.txt" || \
      echo "[mcp-bootstrap] fastembed 安装失败，继续纯 BM25 模式" >&2
  fi
fi
run_with "$VPY" "$@"
