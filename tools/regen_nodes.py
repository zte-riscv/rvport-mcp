#!/usr/bin/env python3
"""
从 docstore.json 再生 nodes/*.txt 人读导出（曾验证全部 2308 节点逐字节一致）。

nodes/ 已从 git 移除（12MB 可再生产物）；需要人读导出或 diff 时运行本脚本：

  python3 tools/regen_nodes.py            # 全部知识库
  python3 tools/regen_nodes.py docs_tools # 指定知识库
"""

from __future__ import annotations

import json
import os
import sys

KB_ROOT = os.environ.get(
    "RISCV_KB_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs"),
)

# 与原导出一致的 metadata 键序（迭代顺序敏感，勿改）
META_ORDER = [
    "file_path",
    "file_name",
    "file_type",
    "file_size",
    "creation_date",
    "last_modified_date",
    "header_path",
]


def regen(kb_dir: str) -> int:
    docstore = os.path.join(kb_dir, "docstore.json")
    with open(docstore, encoding="utf-8") as f:
        data = json.load(f)["docstore/data"]
    nodes_dir = os.path.join(kb_dir, "nodes")
    os.makedirs(nodes_dir, exist_ok=True)
    for i, key in enumerate(data):  # 迭代顺序即 node 编号顺序
        node = data[key]["__data__"]
        m = node["metadata"]
        header = "=== METADATA (上下文信息) ===\n"
        header += "".join(f"{k}: {m[k]}\n" for k in META_ORDER)
        header += "\n========================================\n\n"
        body = "=== CONTENT (LLM 看到的正文) ===\n" + node["text"]
        tail = f"\n\n--- Node Char Length: {len(node['text'])} ---"
        with open(os.path.join(nodes_dir, f"node_{i:04d}.txt"), "w", encoding="utf-8") as out:
            out.write(header + body + tail)
    return len(data)


def main() -> None:
    targets = sys.argv[1:]
    if targets:
        dirs = [os.path.join(KB_ROOT, t) for t in targets]
        for t, d in zip(targets, dirs):
            if not os.path.isdir(d):
                print(f"未知知识库目录: {d}", file=sys.stderr)
                raise SystemExit(2)
    else:
        dirs = [
            os.path.join(KB_ROOT, d)
            for d in sorted(os.listdir(KB_ROOT))
            if os.path.isfile(os.path.join(KB_ROOT, d, "docstore.json"))
        ]
    for d in dirs:
        n = regen(d)
        print(f"{os.path.basename(d)}: {n} 节点 -> nodes/")


if __name__ == "__main__":
    main()
