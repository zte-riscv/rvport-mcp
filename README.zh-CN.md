[English](README.md) | **简体中文**

# rvport-mcp — 本地 RISC-V 文档 MCP 知识库服务

本仓库提供**本地化部署**的 MCP 知识库检索服务（内部服务名 `riscv-doc-rag`）：无需连接远端知识库服务，
知识库数据随仓库分发，开箱即用，可独立作为 stdio MCP server 接入任何
MCP 客户端（Claude Code / Cursor / VS Code Copilot / Claude Desktop 等）。

## 许可

- **代码**（server.py / start.sh / build_kb.sh / tools/）：[Apache-2.0](LICENSE)
- **知识库数据**（docs/ 下的 docstore.json / vectors.npz）：内容来自 RISC-V 各规范文档，
  均遵循 CC BY 4.0（13 个上游仓库的许可逐一核实，见 [spec_repos.yml](spec_repos.yml)）。
  `docs/` 目录为聚合再分发，归属各上游规范作者；许可声明保留在对应 spec 源仓库中，
  数据来源与重建方式见 spec_repos.yml 与「维护者：重建知识库」一节。

## 目录结构

```
rvport-mcp/
├── start.sh              # 自举启动器（全新设备零依赖可跑：缺 mcp SDK 时自动建 venv 安装）
├── server.py             # 本地 stdio MCP server（BM25 + 向量 RRF 混合检索）
├── requirements.txt      # 运行依赖（mcp 必须；fastembed/numpy 可选，缺失降级纯 BM25）
├── build_kb.sh           # 一键流水线入口：拉取 spec -> markdown -> 节点 -> docstore.json
├── spec_repos.yml        # 上游 spec 仓库清单（url/pin，构建时缺失自动拉取）
├── tools/
│   ├── build_kb.py       # 流水线实现（拉取/转换/切分/组装，纯标准库）
│   └── regen_nodes.py    # 从 docstore.json 再生 nodes/*.txt 人读导出（已不纳入 git）
├── doc/                  # spec 源（本地工作目录，不入 git；缺失时按 spec_repos.yml 自动拉取）
└── docs/                 # 知识库数据（git 分发）
    ├── core_isa_manuals_bge_small_zh_v1_5/      # RISC-V ISA 手册（988 节点）
    │   ├── docstore.json                         #   LlamaIndex docstore（节点正文+元数据）
    │   └── vectors.npz                           #   预计算向量（512 维归一化，bge-small-zh-v1.5）
    ├── rvv_vector_extensions_bge_small_zh_v1_5/ # RVV 两卷 + crypto（scalar/vector）+ intrinsic（1168 节点）
    ├── special_instructions_bge_small_zh_v1_5/  # Zba/Zbb/Zbc/Zbs 等（105 节点）
    └── docs_tools_bge_small_zh_v1_5/            # 工具链与优化指南（47 节点）
```

> `nodes/*.txt`（12MB）是 docstore.json 的**逐字节可再生产物**，
> 不纳入 git；需要人读导出时运行 `python3 tools/regen_nodes.py`。

## 提供的 MCP 工具

| 工具 | 知识库 |
| --- | --- |
| `search_core_isa_manuals` | 指令编码、CSR、扩展定义 |
| `search_rvv_vector_extensions` | vsetvli、向量指令、LMUL/SEW、`__riscv_*` intrinsic |
| `search_special_instructions` | Zba/Zbb/Zbc/Zbs 等位操作/字节操作指令 |
| `search_docs_tools` | 编译选项、性能优化实践 |
| `build_vector_index` | 重建向量索引（维护者一次性操作） |
| `list_knowledge_bases` | 列出知识库规模与检索模式（自检用） |

返回结构统一为 `{knowledge_base, query, mode, hit_count, results: [{score, file_path, file_name, header_path, content}]}`，
`file_path` / `header_path` 提供章节证据链；`mode` 字段标示本次检索走 `hybrid` 还是 `bm25`。

## 检索架构

```
查询 ─┬─ BM25（中英混合分词）─────── top10 ─┐
      │                                     ├─ RRF 融合（k=60）→ top_k
      └─ 向量（bge-small-zh-v1.5, ONNX CPU）─ top10 ─┘
```

- **BM25**：指令名/扩展名/CSR 名等精确词命中强（`sh1add`、`vsetvli LMUL`、`Zba`）
- **向量**：中文查询 ↔ 英文文档跨语言语义检索强（"怎么把循环向量化加速"）
- **向量随知识库分发**（`vectors.npz` 预计算），查询端只做一次 512 维 query embed + 矩阵点积，毫秒级
- **模型不随仓库分发**（~95MB）：首次使用时经 hf-mirror 下载到用户缓存目录
  （`~/.cache/fastembed/`），之后完全离线可用
- **优雅降级**：模型不可用（离线/未装 fastembed）→ 自动纯 BM25，服务永远可用
  （`RISCV_EMBED_OFFLINE=1` 可强制）

## 接入方式（Claude Code）

在项目根或用户级 MCP 配置（`.mcp.json` / `~/.claude.json`）注册本服务：

```json
{
  "mcpServers": {
    "riscv-doc-rag": {
      "command": "bash",
      "args": ["/绝对路径/rvport-mcp/start.sh"],
      "env": { "HF_ENDPOINT": "https://hf-mirror.com" }
    }
  }
}
```

注册后 Claude 会话内即可直接调用 `mcp__riscv-doc-rag__search_*` 系列工具。

### 各客户端接入速查

| 场景 | 做法 |
| --- | --- |
| Claude Code 项目级 | 仓库根起 `claude`，把上方 JSON 写进项目 `.mcp.json`，首次会话按提示批准（`claude mcp list` 可查状态：`⏸ Pending approval` → 批准后 `✔ Connected`） |
| Claude Code 用户级（全局、免交互批准） | `claude mcp add riscv-doc-rag -s user -e HF_ENDPOINT=https://hf-mirror.com -- bash <绝对路径>/start.sh` |
| Cursor / Windsurf / VS Code Copilot / Claude Desktop | 在各自 MCP 配置文件（`~/.cursor/mcp.json`、`.vscode/mcp.json`、`claude_desktop_config.json`）的 `mcpServers` 段写上方 JSON；**args 必须写绝对路径** |

手动终端裸跑 `bash start.sh` 时，stdin 是 TTY 会打印就绪横幅并静默等待
JSON-RPC（stdio 协议 stdout 被独占，属正常）；健康自检用
`bash start.sh --selftest "vsetvli" rvv_vector_extensions`。

### 全新设备零依赖运行（start.sh 自举）

- **快路径**：当前 `python3` 已有 mcp SDK → 直接启动，零安装零等待
- **慢路径**：缺 SDK 时在 `~/.cache/riscv-doc-rag/venv` 建独立 venv，安装
  最小依赖 `mcp>=1.0.0,<2`（首次约 1-2 分钟，之后秒启）——不污染系统
  site-packages，也不写只读的插件缓存目录
- 默认自举**不装** fastembed：检索自动降级纯 BM25，功能完整；要 hybrid 模式
  设 `RISCV_MCP_FULL=1`（或事后手动 `pip install -r requirements.txt`）

**解释器要求 Python ≥ 3.10**（mcp 1.x 的 `Requires-Python` 硬性要求）。
自动探测顺序：`RISCV_MCP_PYTHON` 指定的 → `python3.13` → `3.12` → `3.11` →
`3.10` → `python3`，逐个**试建 venv**（版本够但 ensurepip 损坏会自动换下一个
候选），全部失败才报错。注意 RHEL8 系（含 NewStartOS）默认 `python3` 是 3.9，
pip 会报 `No matching distribution found for mcp`——这不是 pip 版本问题，
升级 pip 无用，需要装 `python3.10+`（`dnf install python3.11` 或编译安装）后重跑。

| 环境变量 | 作用 |
| --- | --- |
| `RISCV_MCP_PYTHON` | 指定解释器（默认自动探测：`python3.13` → `3.12` → `3.11` → `3.10` → `python3`） |
| `RISCV_MCP_VENV` | 自举 venv 位置（默认 `~/.cache/riscv-doc-rag/venv`） |
| `RISCV_MCP_FULL=1` | 自举时连同 fastembed/numpy 一起装（hybrid 模式） |
| `PIP_INDEX_URL` | pip 源（内网镜像覆盖） |
| `HF_ENDPOINT` | 模型下载镜像（默认 hf-mirror） |

完全离线的设备：预先把带 fastembed 的 venv 或模型缓存拷贝过去即可跳过联网。

## CLI 查询

不经 MCP 客户端直接走 stdio 查询（脚本化/调试场景），用 `start.sh --selftest`：

```bash
bash start.sh --selftest "vsetvli" rvv_vector_extensions
bash start.sh --selftest "CSR mstatus" core_isa_manuals
```

## 维护者：重建知识库与向量索引

docstore.json 不再是手工产物——[doc/](doc/) 下的 AsciiDoc spec 源可以一键
重建全部知识库（[build_kb.sh](build_kb.sh) 入口 → [tools/build_kb.py](tools/build_kb.py)
实现，Python 纯标准库）：

```bash
bash build_kb.sh                    # 全部 4 库：拉取 spec -> markdown -> 节点 -> docstore.json
bash build_kb.sh core_isa_manuals   # 单库（参数同 server.py 的 KB 短名）
bash build_kb.sh --vectors          # 组装后顺带重建 vectors.npz（需 fastembed）
bash build_kb.sh --markdown-only    # 只转换到 build/<kb>/*.md（调试转换质量）
bash build_kb.sh --skip-convert     # 复用 build/ 下的 markdown，只重跑切分组装
bash build_kb.sh --fetch            # 只拉取缺失的 spec 仓库（spec_repos.yml）
```

**spec 源自动拉取**：[spec_repos.yml](spec_repos.yml) 配置 13 个上游仓库的
`url`/`pin`/`dest`。构建时 `doc/<dest>/` 缺失的仓库自动浅克隆（`--depth 1`，
约 500MB vs 完整快照 12GB），pin 必须是**完整 40 位哈希**或分支/标签名——
GitHub 按哈希 fetch 只认完整 SHA，短哈希会被构建器直接拒绝并提示改写。
按哈希钉住的流程：浅克隆默认分支 → `fetch --depth 1 <sha>` → detach。

个别仓库还带转换前准备钩子（`PRE_CONVERT_HOOKS`）：

| 仓库 | 钩子 | 原因 |
| --- | --- | --- |
| riscv-crypto | 补 2 个子模块（bitmanip/sail-riscv，按主仓库钉住提交浅 fetch） | spec 文档 include 其指令定义；其余 5 个子模块（gnu-toolchain 等数 GB）与文档无关 |
| riscv-crypto | 写 `git-commit.adoc` 占位 | Makefile 生成的版本戳 |
| riscv-performance-events | 跑 `gen_event_tables_adocs.py` | 事件表 `adoc_event_tables/` 构建期生成 |
| integer-vector-absolute-difference / riscv-asm-manual | 补 `docs-resources` 子模块 | 卷首 include 的全局配置 |
| riscv-bitmanip | 写 `revision.adoc-snippet` 占位 | Makefile 生成的 revdate/revnumber |

升级 spec 只需改 `pin` 后删旧目录重跑；网络不通时手动把仓库放到 `doc/`
下即可跳过拉取（钩子对已有完整快照同样幂等跳过）。构建后可用
`grep -r "Unresolved directive" build/` 自检是否有漏解析的 include。

流水线三步（[tools/build_kb.py](tools/build_kb.py) 实现，Python 纯标准库）：

1. **转换**：各 spec 仓库入口 `.adoc` 经 `asciidoctor -b html5` 转 HTML，
   再自研 HTML→Markdown 转换器产出 GitHub 风格 Markdown（保留标题层级、
   代码围栏带语言标注、GFM 表格；剥离目录/页脚/样式；正确处理
   `&lt;asm/xxx.h&gt;` 这类实体转义与 sail 注释 `#` 行）
2. **切分**：MarkdownNodeParser 语义——按围栏外标题切节，祖先标题链写入
   `header_path` 元数据；超长节（>1024 词）按行块二次切分且不从代码围栏
   中间断开
3. **组装**：生成与 LlamaIndex 同构的 `docstore.json`
   （`docstore/data` + `docstore/ref_doc_info` + `docstore/metadata`，
   节点含 PREVIOUS/NEXT 链与 SOURCE 关系；server.py 只消费
   id/text/metadata 三字段，hash 为 sha256(text) 稳定值）

源→知识库映射（build_kb.py 的 `KB_SPECS` 可改）：

| 知识库 | spec 源（doc/ 下） |
| --- | --- |
| core_isa_manuals | riscv-isa-manual（合卷 riscv-spec）、riscv-asm-manual、riscv-profiles（RVA23/RVB23） |
| rvv_vector_extensions | integer-vector-absolute-difference（unpriv/priv 两卷）、riscv-crypto（scalar/vector）、riscv-rvv-intrinsic-doc |
| special_instructions | riscv-bitmanip、riscv-bfloat16、riscv-zaamo-zalrsc、riscv-zabha、riscv-zalasr |
| docs_tools | riscv-optimization-guide、riscv-performance-events |

依赖：`asciidoctor`（`gem install --user-install --no-document asciidoctor`
一次安装，或 `dnf install rubygem-asciidoctor`；`ASCIIDOCTOR` 环境变量可
指定路径，`~/bin/` 会被自动探测）+ 网络可达 GitHub（首次拉取 spec；
spec 已在 `doc/` 时离线可跑）。全程约 2-3 分钟（不含 clone）。

> 重建后节点数/内容与历史 docstore.json 会有差异：旧版部分文档（perf-events、
> bitmanip、crypto 等）按 PDF 逐页切分（节点首行为 `## Page N`），新版统一按
> 标题层级切分，证据链（header_path）更准确。重建后记得跑 `--vectors` 与自检。

```bash
# 重建后的完整验证
bash build_kb.sh --vectors
python3 server.py --selftest "vsetvli" rvv_vector_extensions
python3 server.py --selftest "sh1add" special_instructions
git add docs/*/docstore.json docs/*/vectors.npz
```

中间产物 `build/`（HTML/Markdown）与 spec 工作副本 `doc/`（12GB 本地快照，
可由 spec_repos.yml 随时重拉）均已 gitignore，仅 docstore.json / vectors.npz
入库。只重建向量（docstore.json 已变更时）也可以单独跑
`python3 server.py --build-vectors [kb]`（需 fastembed，全部 4 库约
3.5 分钟）。

## 自检

```bash
python3 server.py --selftest "vsetvli" rvv_vector_extensions
python3 server.py --selftest "怎么把循环向量化加速"          # 中文语义（hybrid 模式）
RISCV_EMBED_OFFLINE=1 python3 server.py --selftest "sh1add"  # 强制纯 BM25 降级
```
