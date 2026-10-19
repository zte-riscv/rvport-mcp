#!/usr/bin/env bash
# 一键流水线：doc/ 下的 AsciiDoc spec -> markdown -> 节点切分 -> docstore.json
#
# 用法：
#   bash build_kb.sh                  # 全部 4 个知识库（拉取+转换+切分+组装）
#   bash build_kb.sh core_isa_manuals # 单个知识库（server.py 的 KB 短名）
#   bash build_kb.sh --vectors        # 组装后顺带重建 vectors.npz（需 fastembed，约 3.5 分钟）
#   bash build_kb.sh --markdown-only  # 只做 spec -> build/<kb>/*.md
#   bash build_kb.sh --skip-convert   # 复用 build/ 下的 markdown，只重跑切分组装
#   bash build_kb.sh --fetch          # 只拉取缺失的 spec 仓库（spec_repos.yml 配置）
#
# spec 源拉取：doc/<repo>/ 缺失时按 spec_repos.yml 的 url/pin 自动浅克隆
# 并 checkout 到钉住提交；已存在的目录（含手工放置的快照）一律不动。
#
# 环境变量：
#   ASCIIDOCTOR   指定 asciidoctor 可执行文件（默认 PATH 或 ~/bin/asciidoctor）
#   RISCV_KB_ROOT docstore.json 输出根（默认 ./docs，同 server.py）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/bin:$PATH"   # gem --user-install 的 asciidoctor 落在这里

# ---- 解释器：本脚本纯标准库，>= 3.8 即可，无 mcp 依赖 ----
PY="${RISCV_MCP_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "[build_kb] 找不到 $PY" >&2; exit 1; }

# ---- 依赖自检：git 与 asciidoctor（转换/拉取步骤需要；--skip-convert 时不需要）----
NEED_ADOC=1
for a in "$@"; do
  case "$a" in --skip-convert) NEED_ADOC=0 ;; esac
done
if [ "$NEED_ADOC" = "1" ]; then
  if ! command -v asciidoctor >/dev/null 2>&1; then
    echo "[build_kb] 未找到 asciidoctor。安装（一次即可，约 1 分钟）：" >&2
    echo "[build_kb]   gem install --user-install --no-document asciidoctor" >&2
    echo "[build_kb] gem 不可用时也可用 dnf/yum install rubygem-asciidoctor，或设 ASCIIDOCTOR 指向可执行文件" >&2
    exit 1
  fi
fi

exec "$PY" "$HERE/tools/build_kb.py" "$@"
