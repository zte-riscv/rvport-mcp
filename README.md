# rvport-mcp — Offline RISC-V Documentation MCP Server

**English** | [简体中文](README.zh-CN.md)

rvport-mcp is a self-contained [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that puts the RISC-V specification corpus at an AI coding assistant's fingertips (internal service name: `riscv-doc-rag`). Knowledge bases are prebuilt and shipped inside the repository, so the server works out of the box — no external services, no database, no index-building on the user side. It plugs into any MCP client: Claude Code, Cursor, VS Code Copilot, Claude Desktop, and more.

## License

- **Code** (server.py / start.sh / build_kb.sh / tools/): [Apache-2.0](LICENSE)
- **Knowledge-base data** (docs/ — docstore.json / vectors.npz): content comes from upstream RISC-V specifications, all licensed **CC BY 4.0** (verified per repository; see [spec_repos.yml](spec_repos.yml)). The `docs/` directory is an aggregated redistribution attributed to the upstream spec authors; license notices live in the source spec repositories. See spec_repos.yml and the *Maintainers: rebuilding the knowledge bases* section for data sources and the rebuild procedure.

## Repository Layout

```
rvport-mcp/
├── start.sh              # Bootstrap launcher (runs on a fresh machine with zero deps)
├── server.py             # Local stdio MCP server (BM25 + vector RRF hybrid retrieval)
├── requirements.txt      # Runtime deps (mcp required; fastembed/numpy optional -> BM25 fallback)
├── build_kb.sh           # One-command pipeline entry: fetch specs -> markdown -> nodes -> docstore.json
├── spec_repos.yml        # Upstream spec repo manifest (url/pin, auto-fetched when missing)
├── tools/
│   ├── build_kb.py       # Pipeline implementation (fetch/convert/split/assemble; stdlib only)
│   └── regen_nodes.py    # Regenerate human-readable nodes/*.txt from docstore.json (not in git)
├── doc/                  # Spec sources (local working dir, git-ignored; auto-fetched per spec_repos.yml)
└── docs/                 # Knowledge-base data (distributed via git)
    ├── core_isa_manuals_bge_small_zh_v1_5/      # RISC-V ISA manuals (988 nodes)
    │   ├── docstore.json                         #   LlamaIndex docstore (node text + metadata)
    │   └── vectors.npz                           #   Precomputed vectors (512-dim normalized, bge-small-zh-v1.5)
    ├── rvv_vector_extensions_bge_small_zh_v1_5/ # RVV volumes + crypto (scalar/vector) + intrinsics (1,168 nodes)
    ├── special_instructions_bge_small_zh_v1_5/  # Zba/Zbb/Zbc/Zbs etc. (105 nodes)
    └── docs_tools_bge_small_zh_v1_5/            # Toolchain & optimization guides (47 nodes)
```

> `nodes/*.txt` (12MB) is a byte-for-byte reproducible product of docstore.json and is not committed; run `python3 tools/regen_nodes.py` to produce the human-readable export.

## MCP Tools

| Tool | Knowledge base |
| --- | --- |
| `search_core_isa_manuals` | Instruction encodings, CSRs, extension definitions |
| `search_rvv_vector_extensions` | vsetvli, vector instructions, LMUL/SEW, `__riscv_*` intrinsics |
| `search_special_instructions` | Bit/byte manipulation instructions (Zba/Zbb/Zbc/Zbs, ...) |
| `search_docs_tools` | Compiler options, performance-optimization practices |
| `build_vector_index` | Rebuild vector indexes (one-off maintainer operation) |
| `list_knowledge_bases` | List KB sizes and retrieval modes (self-check) |

Every tool returns `{knowledge_base, query, mode, hit_count, results: [{score, file_path, file_name, header_path, content}]}`; `file_path` / `header_path` provide section-level provenance, and `mode` reports whether the query ran in `hybrid` or `bm25` mode.

## Retrieval Architecture

```
query ─┬─ BM25 (mixed CJK/ASCII tokenization) ────── top10 ─┐
       │                                                     ├─ RRF fusion (k=60) -> top_k
       └─ dense vectors (bge-small-zh-v1.5, ONNX CPU) ─ top10 ┘
```

- **BM25**: strong exact-term hits on instruction/extension/CSR names (`sh1add`, `vsetvli LMUL`, `Zba`)
- **Vectors**: strong cross-lingual semantic recall (Chinese query ↔ English docs, e.g. "怎么把循环向量化加速")
- **Vectors ship with the KB** (`vectors.npz`, precomputed): the query side does a single 512-dim query embedding + matrix dot product — milliseconds
- **The model is not distributed** (~95MB): downloaded to the user cache (`~/.cache/fastembed/`) on first use via an HF mirror, fully offline afterwards
- **Graceful degradation**: model unavailable (offline / fastembed missing) → automatic pure-BM25 fallback; the service never goes down (`RISCV_EMBED_OFFLINE=1` forces it)

## Getting Started (Claude Code)

Register the server in a project-level or user-level MCP config (`.mcp.json` / `~/.claude.json`):

```json
{
  "mcpServers": {
    "riscv-doc-rag": {
      "command": "bash",
      "args": ["/absolute/path/to/rvport-mcp/start.sh"],
      "env": { "HF_ENDPOINT": "https://hf-mirror.com" }
    }
  }
}
```

Once registered, the `mcp__riscv-doc-rag__search_*` tools are available directly inside Claude sessions.

### Client quick reference

| Client | How |
| --- | --- |
| Claude Code (project) | Start `claude` at the repo root, put the JSON above into `.mcp.json`, approve on first session (`claude mcp list` shows `⏸ Pending approval` → `✔ Connected` after approval) |
| Claude Code (user scope, no per-project approval) | `claude mcp add riscv-doc-rag -s user -e HF_ENDPOINT=https://hf-mirror.com -- bash /absolute/path/to/start.sh` |
| Cursor / Windsurf / VS Code Copilot / Claude Desktop | Add the JSON above to the `mcpServers` section of the client's MCP config (`~/.cursor/mcp.json`, `.vscode/mcp.json`, `claude_desktop_config.json`); **args must be absolute paths** |

Running `bash start.sh` manually in a terminal: with a TTY on stdin it prints a ready banner and then silently waits for JSON-RPC (stdio protocol owns stdout — this is normal). Health check:

```bash
bash start.sh --selftest "vsetvli" rvv_vector_extensions
```

### Zero-dependency bootstrap (start.sh)

- **Fast path**: the selected `python3` already has the mcp SDK → starts directly, zero install, zero wait
- **Slow path**: SDK missing → builds an isolated venv at `~/.cache/riscv-doc-rag/venv` and installs the minimal dependency `mcp>=1.0.0,<2` (~1-2 min the first time, instant afterwards) — no pollution of system site-packages
- By default the bootstrap does **not** install fastembed: retrieval degrades to pure BM25, which is fully functional. Set `RISCV_MCP_FULL=1` for hybrid mode (or `pip install -r requirements.txt` afterwards)

**Python ≥ 3.10 required** (hard requirement of mcp 1.x's `Requires-Python`). Probe order: `RISCV_MCP_PYTHON` → `python3.13` → `3.12` → `3.11` → `3.10` → `python3`; each candidate is **venv-tested** (a new-enough interpreter with a broken ensurepip falls through to the next one), and only exhausting all candidates is an error. Note that on RHEL8-family distros the default `python3` is 3.9, and pip will report `No matching distribution found for mcp` — that is interpreter filtering, not a pip problem; install `python3.10+` (`dnf install python3.11` or build from source) and rerun.

| Env var | Purpose |
| --- | --- |
| `RISCV_MCP_PYTHON` | Pin the interpreter (default: auto-probe `python3.13` → `3.12` → `3.11` → `3.10` → `python3`) |
| `RISCV_MCP_VENV` | Bootstrap venv location (default `~/.cache/riscv-doc-rag/venv`) |
| `RISCV_MCP_FULL=1` | Also install fastembed/numpy during bootstrap (hybrid mode) |
| `PIP_INDEX_URL` | pip index (override for internal mirrors) |
| `HF_ENDPOINT` | Model-download mirror (default: hf-mirror) |

Fully offline machines: pre-copy a fastembed-equipped venv or the model cache to skip network access entirely.

## CLI Queries

To query over stdio without an MCP client (scripting / debugging), use `start.sh --selftest`:

```bash
bash start.sh --selftest "vsetvli" rvv_vector_extensions
bash start.sh --selftest "CSR mstatus" core_isa_manuals
```

## Maintainers: Rebuilding the Knowledge Bases

The docstore.json files are generated artifacts — the AsciiDoc spec sources under [doc/](doc/) rebuild every knowledge base with one command ([build_kb.sh](build_kb.sh) entry point → [tools/build_kb.py](tools/build_kb.py), stdlib-only Python):

```bash
bash build_kb.sh                    # all 4 KBs: fetch specs -> markdown -> nodes -> docstore.json
bash build_kb.sh core_isa_manuals   # a single KB (same short names as server.py)
bash build_kb.sh --vectors          # also rebuild vectors.npz afterwards (needs fastembed)
bash build_kb.sh --markdown-only    # convert to build/<kb>/*.md only (debug conversion quality)
bash build_kb.sh --skip-convert     # reuse markdown under build/, re-run split + assemble only
bash build_kb.sh --fetch            # only fetch missing spec repos (spec_repos.yml)
```

**Automatic spec fetching**: [spec_repos.yml](spec_repos.yml) configures `url`/`pin`/`dest` for 13 upstream repositories. Repos missing from `doc/<dest>/` are shallow-cloned (`--depth 1`, ~500MB vs 12GB full snapshots) at build time. The `pin` must be a **full 40-char hash** or a branch/tag name — GitHub hash-fetches require the full SHA, and short hashes are rejected by the builder with a hint to rewrite them. Pinned-hash flow: shallow-clone the default branch → `fetch --depth 1 <sha>` → detach.

A few repositories need pre-conversion hooks (`PRE_CONVERT_HOOKS`):

| Repo | Hook | Why |
| --- | --- | --- |
| riscv-crypto | fetch 2 submodules (bitmanip / sail-riscv, shallow-fetched at the main repo's pinned commits) | spec text includes their instruction definitions; the other 5 submodules (gnu-toolchain etc., several GB) are irrelevant to docs |
| riscv-crypto | write placeholder `git-commit.adoc` | version stamp generated by the Makefile |
| riscv-performance-events | run `gen_event_tables_adocs.py` | event tables under `adoc_event_tables/` are generated at build time |
| integer-vector-absolute-difference / riscv-asm-manual | fetch the `docs-resources` submodule | global config included by the volume preamble |
| riscv-bitmanip | write placeholder `revision.adoc-snippet` | revdate/revnumber generated by the Makefile |

Upgrading a spec means changing its `pin`, deleting the old directory, and rerunning. Without network access, place the repositories under `doc/` manually to skip fetching (the hooks also skip idiomatically for existing full snapshots). After building, `grep -r "Unresolved directive" build/` checks for unresolved includes.

The pipeline's three stages (implemented in [tools/build_kb.py](tools/build_kb.py), stdlib only):

1. **Convert**: each spec repo's entry `.adoc` goes through `asciidoctor -b html5`, then a custom HTML→Markdown converter produces GitHub-flavored Markdown (preserving heading levels, language-tagged code fences, and GFM tables; stripping TOC/footer/styles; handling entity escapes like `&lt;asm/xxx.h&gt;` and sail `#` comment lines)
2. **Split**: MarkdownNodeParser semantics — sections split at headings outside code fences, ancestor heading chains stored in `header_path` metadata; oversized sections (>1024 words) split again by line blocks without breaking code fences
3. **Assemble**: emit a LlamaIndex-shaped `docstore.json` (`docstore/data` + `docstore/ref_doc_info` + `docstore/metadata`, nodes carrying PREVIOUS/NEXT chains and SOURCE relations; server.py consumes only id/text/metadata, and the hash is the stable sha256(text))

Source→KB mapping (editable in build_kb.py's `KB_SPECS`):

| Knowledge base | Spec sources (under doc/) |
| --- | --- |
| core_isa_manuals | riscv-isa-manual (combined riscv-spec volume), riscv-asm-manual, riscv-profiles (RVA23/RVB23) |
| rvv_vector_extensions | integer-vector-absolute-difference (unpriv/priv volumes), riscv-crypto (scalar/vector), riscv-rvv-intrinsic-doc |
| special_instructions | riscv-bitmanip, riscv-bfloat16, riscv-zaamo-zalrsc, riscv-zabha, riscv-zalasr |
| docs_tools | riscv-optimization-guide, riscv-performance-events |

Requirements: `asciidoctor` (one-time `gem install --user-install --no-document asciidoctor`, or `dnf install rubygem-asciidoctor`; the `ASCIIDOCTOR` env var can point to the binary and `~/bin/` is probed automatically) + GitHub reachable (first spec fetch; runs offline once specs are under `doc/`). The whole run takes ~2-3 minutes (excluding clones).

> A rebuild produces different node counts/content than historical docstore.json files: some older documents (perf-events, bitmanip, crypto, ...) were split page-by-page from PDFs (first line `## Page N`); the current pipeline splits uniformly by heading level, giving more accurate provenance (header_path). After rebuilding, rerun `--vectors` and the self-tests.

```bash
# Full post-rebuild verification
bash build_kb.sh --vectors
python3 server.py --selftest "vsetvli" rvv_vector_extensions
python3 server.py --selftest "sh1add" special_instructions
git add docs/*/docstore.json docs/*/vectors.npz
```

Intermediate artifacts `build/` (HTML/Markdown) and the spec working copies under `doc/` (12GB local snapshots, re-fetchable from spec_repos.yml at any time) are git-ignored; only docstore.json / vectors.npz are committed. To rebuild vectors only (when docstore.json has changed): `python3 server.py --build-vectors [kb]` (needs fastembed, ~3.5 min for all 4 KBs).

## Self-tests

```bash
python3 server.py --selftest "vsetvli" rvv_vector_extensions
python3 server.py --selftest "怎么把循环向量化加速"          # Chinese semantics (hybrid mode)
RISCV_EMBED_OFFLINE=1 python3 server.py --selftest "sh1add"  # forced pure-BM25 fallback
```
