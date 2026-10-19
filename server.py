#!/usr/bin/env python3
"""
本地 stdio MCP 知识库检索服务（riscv-doc-rag）。

对 docs/ 下 4 个知识库做混合检索（BM25 词法 + 向量语义，RRF 融合）。

工具名与远端 HTTP MCP 服务保持一致，Skill/Agent 侧无需区分本地或远端：

  - search_core_isa_manuals(query, top_k)      RISC-V ISA 手册（指令编码/CSR/扩展定义）
  - search_rvv_vector_extensions(query, top_k) RVV 1.0 向量扩展（vsetvli/向量指令/LMUL/SEW/intrinsic）
  - search_special_instructions(query, top_k)  特殊指令扩展（Zba/Zbb/Zbc/Zbs 等位/字节操作）
  - search_docs_tools(query, top_k)            工具链与优化指南（编译选项/性能优化）
  - build_vector_index(kb_name)                构建向量索引 vectors.npz（一次性，维护者用）
  - list_knowledge_bases()                     列出知识库规模与检索模式

检索架构（索引构建一次，查询毫秒级）：
  - BM25：中英混合分词（ASCII 词 + CJK 单字/二元组），指令名/扩展名精确命中强
  - 向量：bge-small-zh-v1.5（512 维，ONNX CPU 推理），中文查询↔英文文档跨语言语义检索强；
    向量随知识库分发（docs/*/vectors.npz），查询端无需重算
  - 融合：RRF（Reciprocal Rank Fusion, k=60），两路 top-10 融合后取 top_k
  - 降级：模型不可用（未下载/离线）→ 纯 BM25，服务永远可用

运行方式：
  - MCP stdio（默认，由 Claude Code 通过 .mcp.json 拉起）: python3 server.py
  - 构建向量索引: python3 server.py --build-vectors [kb_name]
  - 终端自检: python3 server.py --selftest "vsetvli" [知识库短名]

环境变量：
  RISCV_KB_ROOT            知识库根目录（默认：本文件同级的 docs/）
  RISCV_EMBED_MODEL        向量模型名（默认 BAAI/bge-small-zh-v1.5）
  RISCV_EMBED_OFFLINE=1    禁用向量检索（强制纯 BM25，用于测试/离线断网）
  HF_ENDPOINT              模型下载镜像（默认 https://hf-mirror.com）
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
from collections import Counter
from typing import Any

from mcp.server.fastmcp import FastMCP

# 压掉 "Processing request of type ..." 之类 INFO 日志，保持 stdio 输出干净
logging.getLogger("mcp.server").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

# --------------------------------------------------------------------------
# 知识库注册表：工具名后缀 -> 目录名
# --------------------------------------------------------------------------
KB_ROOT = os.environ.get(
    "RISCV_KB_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs"),
)

KNOWLEDGE_BASES: dict[str, dict[str, str]] = {
    "core_isa_manuals": {
        "dir": "core_isa_manuals_bge_small_zh_v1_5",
        "description": "RISC-V ISA 手册（指令编码、CSR、扩展定义）",
    },
    "rvv_vector_extensions": {
        "dir": "rvv_vector_extensions_bge_small_zh_v1_5",
        "description": "RVV 1.0 向量扩展（vsetvli、向量指令、LMUL/SEW、intrinsic）",
    },
    "special_instructions": {
        "dir": "special_instructions_bge_small_zh_v1_5",
        "description": "特殊指令扩展（Zba/Zbb/Zbc/Zbs 等位操作/字节操作）",
    },
    "docs_tools": {
        "dir": "docs_tools_bge_small_zh_v1_5",
        "description": "工具链与优化指南（编译选项、性能优化实践）",
    },
}

# 每条命中返回的正文截断长度（防止 top_k 全文撑爆上下文）
SNIPPET_LIMIT = 1000
DEFAULT_TOP_K = 5
MAX_TOP_K = 10
VECTOR_FETCH_DEPTH = 10  # 两路检索各取 top-10 参与 RRF 融合

# --------------------------------------------------------------------------
# 分词：ASCII 词（按 _ / . 天然切分，__riscv_vsetvl -> riscv, vsetvl）
#       + CJK 单字与二元组（覆盖中文查询/中文文档）
# --------------------------------------------------------------------------
_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[一-鿿]+")


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = _ASCII_WORD.findall(text)
    for run in _CJK_RUN.findall(text):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


# --------------------------------------------------------------------------
# 纯 Python BM25Okapi（避免引入 rank_bm25 依赖）
# --------------------------------------------------------------------------
class BM25:
    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(corpus)
        self.tfs = [Counter(toks) for toks in corpus]
        self.doc_len = [len(toks) for toks in corpus]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 1.0
        df: Counter[str] = Counter()
        for toks in corpus:
            df.update(set(toks))
        self.idf = {
            t: math.log(1.0 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()
        }

    def search(self, query_tokens: list[str], top_k: int) -> list[tuple[float, int]]:
        query_terms = set(query_tokens)
        scored: list[tuple[float, int]] = []
        for i in range(self.n):
            tf = self.tfs[i]
            dl = self.doc_len[i]
            s = 0.0
            for t in query_terms:
                f = tf.get(t)
                if not f:
                    continue
                idf = self.idf.get(t, 0.0)
                s += idf * f * (self.k1 + 1) / (
                    f + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
                )
            if s > 0.0:
                scored.append((s, i))
        scored.sort(key=lambda x: -x[0])
        return scored[:top_k]


# --------------------------------------------------------------------------
# 向量检索（fastembed + ONNX CPU；向量由 --build-vectors 预计算，查询期只做矩阵点积）
# --------------------------------------------------------------------------
class VectorIndex:
    """预计算向量索引。docs/<kb>/vectors.npz: {ids: (N,), matrix: (N,512) float32 归一化}"""

    MODEL_NAME = os.environ.get("RISCV_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

    def __init__(self):
        self._model = None
        self._loaded: dict[str, tuple[list[str], Any]] = {}
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    @property
    def available(self) -> bool:
        """模型是否可用（RISCV_EMBED_OFFLINE=1 或 fastembed 缺失/加载失败 → False）"""
        if os.environ.get("RISCV_EMBED_OFFLINE") == "1":
            return False
        try:
            self._get_model()
            return True
        except Exception:
            return False

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(
                model_name=self.MODEL_NAME, providers=["CPUExecutionProvider"]
            )
        return self._model

    def load(self, kb_name: str) -> tuple[list[str], Any] | None:
        """返回 (node_ids, matrix)；无 vectors.npz 或 numpy 缺失 → None（调用方降级纯 BM25）"""
        path = os.path.join(KB_ROOT, KNOWLEDGE_BASES[kb_name]["dir"], "vectors.npz")
        if not os.path.isfile(path):
            return None
        if kb_name not in self._loaded:
            try:
                import numpy as np
            except ImportError:
                return None
            with np.load(path) as z:
                self._loaded[kb_name] = (list(z["ids"]), z["matrix"])
        return self._loaded[kb_name]

    def query(self, kb_name: str, q: str, top_k: int) -> list[tuple[float, str]]:
        """返回 [(cosine 相似度, node_id)]，降序；模型/索引/numpy 不可用时返回 []"""
        loaded = self.load(kb_name)
        if loaded is None or not self.available:
            return []
        ids, matrix = loaded
        try:
            import numpy as np
        except ImportError:
            return []

        model = self._get_model()
        qv = np.asarray(next(iter(model.embed([q]))), dtype=np.float32)
        qv = qv / np.linalg.norm(qv)
        sims = matrix @ qv
        order = np.argsort(-sims)[:top_k]
        return [(float(sims[i]), ids[i]) for i in order]


# --------------------------------------------------------------------------
# docstore + 索引加载（懒加载 + 进程内缓存）
# --------------------------------------------------------------------------
_kb_cache: dict[str, tuple[list[dict[str, Any]], BM25]] = {}
_vector_index = VectorIndex()


def _load_kb(name: str) -> tuple[list[dict[str, Any]], BM25]:
    if name in _kb_cache:
        return _kb_cache[name]
    path = os.path.join(KB_ROOT, KNOWLEDGE_BASES[name]["dir"], "docstore.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    docs: list[dict[str, Any]] = []
    for node in raw.get("docstore/data", {}).values():
        data = node.get("__data__") or {}
        text = data.get("text") or ""
        if not text.strip():
            continue
        meta = data.get("metadata") or {}
        docs.append(
            {
                "id": data.get("id_", ""),
                "file_path": meta.get("file_path", ""),
                "file_name": meta.get("file_name", ""),
                "header_path": meta.get("header_path", ""),
                "text": text,
            }
        )
    if not docs:
        raise RuntimeError(f"知识库 {name} 无有效节点: {path}")
    index = BM25([tokenize(d["text"]) for d in docs])
    _kb_cache[name] = (docs, index)
    return docs, index


def _fuse_rrf(
    bm25_hits: list[tuple[float, int]],
    vec_hits: list[tuple[float, str]],
    id2idx: dict[str, int],
    top_k: int,
) -> list[tuple[float, int]]:
    """RRF（k=60）融合 BM25 与向量两路排名，返回 [(rrf 分, doc 下标)]。"""
    rrf: dict[int, float] = {}
    K = 60
    for rank, (_score, i) in enumerate(bm25_hits):
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (K + rank + 1)
    for rank, (_score, nid) in enumerate(vec_hits):
        i = id2idx.get(nid)
        if i is None:
            continue
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (K + rank + 1)
    fused = [(s, i) for i, s in sorted(rrf.items(), key=lambda x: -x[1])[:top_k]]
    return fused


def _search(name: str, query: str, top_k: int) -> dict[str, Any]:
    top_k = max(1, min(top_k, MAX_TOP_K))
    docs, bm25 = _load_kb(name)
    bm25_hits = bm25.search(tokenize(query), top_k=VECTOR_FETCH_DEPTH)
    vec_hits = _vector_index.query(name, query, top_k=VECTOR_FETCH_DEPTH)
    if vec_hits:
        id2idx = {d["id"]: i for i, d in enumerate(docs)}
        fused = _fuse_rrf(bm25_hits, vec_hits, id2idx, top_k)
        mode = "hybrid"
    else:
        fused = bm25_hits[:top_k]
        mode = "bm25"
    results = []
    for score, i in fused:
        text = docs[i]["text"]
        snippet = text[:SNIPPET_LIMIT]
        if len(text) > SNIPPET_LIMIT:
            snippet += f"\n...[已截断，原文共 {len(text)} 字符]"
        results.append(
            {
                "score": round(score, 4),
                "file_path": docs[i]["file_path"],
                "file_name": docs[i]["file_name"],
                "header_path": docs[i]["header_path"],
                "content": snippet,
            }
        )
    return {
        "knowledge_base": name,
        "query": query,
        "mode": mode,
        "hit_count": len(results),
        "results": results,
    }


def _build_vectors(kb_name: str = "") -> dict[str, Any]:
    """一次性构建 vectors.npz（维护者本地跑，产物入 git；查询端零计算）。

    文本加 bge 检索指令前缀后整体 embed（节点粒度，不切片）。
    """
    targets = [kb_name] if kb_name else list(KNOWLEDGE_BASES)
    for kb in targets:
        if kb not in KNOWLEDGE_BASES:
            return {"error": f"未知知识库 {kb}，可选: {', '.join(KNOWLEDGE_BASES)}"}
    model = _vector_index._get_model()
    import numpy as np

    built = []
    for kb in targets:
        docs, _ = _load_kb(kb)
        ids = [d["id"] for d in docs]
        # bge 系模型建议为「文档侧」加检索指令前缀以对齐训练分布
        texts = [f"为这个句子生成表示以用于检索相关文章：{d['text']}" for d in docs]
        matrix = np.asarray(
            list(model.embed(texts, batch_size=32)), dtype=np.float32
        )
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / norms
        out = os.path.join(KB_ROOT, KNOWLEDGE_BASES[kb]["dir"], "vectors.npz")
        np.savez_compressed(out, ids=np.array(ids), matrix=matrix)
        built.append(
            {"kb": kb, "nodes": len(ids), "dim": int(matrix.shape[1]), "file": out}
        )
    return {"model": _vector_index.MODEL_NAME, "built": built}


# --------------------------------------------------------------------------
# MCP 工具（检索工具命名与远端服务一致）
# --------------------------------------------------------------------------
mcp = FastMCP("riscv-doc-rag")


@mcp.tool()
def search_core_isa_manuals(query: str, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
    """检索 RISC-V ISA 手册（指令编码、CSR、扩展定义）。返回 file_path/header_path 证据链与正文片段。"""
    return _search("core_isa_manuals", query, top_k)


@mcp.tool()
def search_rvv_vector_extensions(
    query: str, top_k: int = DEFAULT_TOP_K
) -> dict[str, Any]:
    """检索 RVV 1.0 向量扩展文档（vsetvli、向量指令、LMUL/SEW、__riscv_* intrinsic）。返回 file_path/header_path 证据链与正文片段。"""
    return _search("rvv_vector_extensions", query, top_k)


@mcp.tool()
def search_special_instructions(
    query: str, top_k: int = DEFAULT_TOP_K
) -> dict[str, Any]:
    """检索特殊指令扩展文档（Zba/Zbb/Zbc/Zbs 等位操作/字节操作指令）。返回 file_path/header_path 证据链与正文片段。"""
    return _search("special_instructions", query, top_k)


@mcp.tool()
def search_docs_tools(query: str, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
    """检索工具链与优化指南（编译选项、性能优化实践、llvm-mca 等）。返回 file_path/header_path 证据链与正文片段。"""
    return _search("docs_tools", query, top_k)


@mcp.tool()
def build_vector_index(kb_name: str = "") -> dict[str, Any]:
    """构建向量索引 vectors.npz（默认全部 4 个库；需 fastembed 与模型，离线环境报错但不影响检索降级）。维护者一次性操作，普通用户无需调用。"""
    return _build_vectors(kb_name)


@mcp.tool()
def list_knowledge_bases() -> dict[str, Any]:
    """列出本地知识库（名称、描述、节点数、检索模式）。用于确认本地检索可用。"""
    out = []
    for name, info in KNOWLEDGE_BASES.items():
        docs, _ = _load_kb(name)
        has_vectors = _vector_index.load(name) is not None
        hybrid = has_vectors and _vector_index.available
        out.append(
            {
                "name": name,
                "description": info["description"],
                "node_count": len(docs),
                "vector_ready": has_vectors,
                "mode": "hybrid" if hybrid else "bm25",
                "directory": info["dir"],
            }
        )
    return {"kb_root": KB_ROOT, "knowledge_bases": out}


# --------------------------------------------------------------------------
# 终端入口
# --------------------------------------------------------------------------
def _selftest(query: str, name: str | None) -> int:
    targets = [name] if name else list(KNOWLEDGE_BASES)
    for kb in targets:
        if kb not in KNOWLEDGE_BASES:
            print(f"未知知识库: {kb}（可选: {', '.join(KNOWLEDGE_BASES)}）")
            return 2
        result = _search(kb, query, 3)
        print(f"== {kb} ({result['hit_count']} hits, mode={result['mode']}) ==")
        for hit in result["results"]:
            print(f"  [{hit['score']:>8.3f}] {hit['file_name']} {hit['header_path']}")
        if not result["results"]:
            print("  (无命中)")
    return 0


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        if len(sys.argv) < 3:
            print("用法: server.py --selftest <query> [kb]", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(
            _selftest(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        )
    if len(sys.argv) >= 2 and sys.argv[1] == "--build-vectors":
        print(
            json.dumps(
                _build_vectors(sys.argv[2] if len(sys.argv) > 2 else ""),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    # 手动在终端裸跑（stdin 是 TTY）→ 打印就绪提示到 stderr 后进入 stdio 循环。
    # stdout 被 JSON-RPC 协议独占，任何日志混入都会破坏协议帧。
    if sys.stdin.isatty():
        vec_state = "hybrid(向量+BM25)" if _vector_index.available else "bm25(纯词法)"
        print(
            f"[riscv-doc-rag] MCP stdio server 就绪，检索模式: {vec_state}\n"
            f"[riscv-doc-rag] 正在等待 stdin 上的 MCP JSON-RPC 消息（Ctrl+C 退出）。\n"
            f"[riscv-doc-rag] 手动测试建议: python3 server.py --selftest \"vsetvli\" rvv_vector_extensions",
            file=sys.stderr,
        )
    mcp.run()


if __name__ == "__main__":
    main()
