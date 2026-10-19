#!/usr/bin/env python3
"""
spec -> markdown -> nodes -> docstore.json 一键构建器。

把 doc/ 下的 AsciiDoc spec 源转换为 HTML（asciidoctor），再转 Markdown，
按 MarkdownNodeParser 语义切分为节点，组装成 LlamaIndex docstore.json
（与 docs/*/docstore.json 同构；server.py 只读 id/text/metadata 三字段）。

用法：
  python3 tools/build_kb.py                     # 全部知识库
  python3 tools/build_kb.py core_isa_manuals    # 单个知识库（server.py 的 KB 短名）
  python3 tools/build_kb.py --markdown-only     # 只做 spec->markdown（不切分组装）
  python3 tools/build_kb.py --skip-convert      # 跳过 asciidoctor（markdown 已就绪）
  python3 tools/build_kb.py --build-vectors     # 顺带重建 vectors.npz（需 fastembed）
  python3 tools/build_kb.py --fetch             # 只拉取缺失的 spec 仓库（spec_repos.yml）

spec 源自动拉取：doc/<repo>/ 缺失时按 spec_repos.yml 的 url/pin 自动 clone
并 checkout 到钉住的提交（clone --depth 1 -b <pin>，pin 非分支名时先浅克
隆再 fetch 该提交）。网络不可用时手动放置仓库到 doc/ 即可。

依赖：asciidoctor（HTML 转换）+ Python 标准库；无 numpy/fastembed 也能跑。
产物：build/<kb>/<name>.md 与 docs/<kb_dir>/docstore.json。
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOC_ROOT = os.path.join(ROOT, "doc")
BUILD_ROOT = os.path.join(ROOT, "build")
KB_ROOT = os.environ.get("RISCV_KB_ROOT", os.path.join(ROOT, "docs"))

# 与 server.py 的 KNOWLEDGE_BASES 对应：短名 -> (目标目录, 文档清单)
# 每个文档：md 名 -> (spec 仓库目录, 入口 adoc 相对路径)
KB_SPECS: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    "core_isa_manuals": (
        "core_isa_manuals_bge_small_zh_v1_5",
        {
            "riscv-spec.md": ("riscv-isa-manual/riscv-isa-manual", "src/riscv-spec.adoc"),
            "riscv-asm.md": ("riscv-asm-manual", "src/riscv-asm.adoc"),
            "rva23-profile.md": ("riscv-profiles", "src/rva23-profile.adoc"),
            "rvb23-profile.md": ("riscv-profiles", "src/rvb23-profile.adoc"),
        },
    ),
    "rvv_vector_extensions": (
        "rvv_vector_extensions_bge_small_zh_v1_5",
        {
            "riscv-unprivileged.md": ("integer-vector-absolute-difference", "src/riscv-unprivileged.adoc"),
            "riscv-privileged.md": ("integer-vector-absolute-difference", "src/riscv-privileged.adoc"),
            "riscv-crypto-spec-scalar.md": ("riscv-crypto", "doc/scalar/riscv-crypto-spec-scalar.adoc"),
            "riscv-crypto-spec-vector.md": ("riscv-crypto", "doc/vector/riscv-crypto-spec-vector.adoc"),
            "v-intrinsic-spec.md": ("riscv-rvv-intrinsic-doc", "doc/rvv-intrinsic-spec.adoc"),
        },
    ),
    "special_instructions": (
        "special_instructions_bge_small_zh_v1_5",
        {
            "bitmanip-1.0.0-76-g517a903.md": ("riscv-bitmanip", "bitmanip/bitmanip.adoc"),
            "riscv-bfloat16.md": ("riscv-bfloat16", "doc/riscv-bfloat16.adoc"),
            "riscv-zaamo-zalrsc.md": ("riscv-zaamo-zalrsc", "zaamo-zalrsc.adoc"),
            "riscv-zabha.md": ("riscv-zabha", "src/riscv-zabha.adoc"),
            "riscv-zalasr-1p0.md": ("riscv-zalasr", "header.adoc"),
        },
    ),
    "docs_tools": (
        "docs_tools_bge_small_zh_v1_5",
        {
            "riscv-optimization-guide.md": ("riscv-optimization-guide", "riscv-optimization-guide.adoc"),
            "riscv-perf-events-latest.md": ("riscv-performance-events", "header.adoc"),
        },
    ),
}

# 切分参数（对齐 LlamaIndex MarkdownNodeParser 默认行为）
CHUNK_SIZE = 1024          # token 上限（词近似）
CHUNK_OVERLAP = 256        # 相邻块重叠（词近似）
INCLUDE_METADATA = True    # 节点 text 是否携带元数据头（现有 KB 未带 → False 由 build 时决定）
INCLUDE_PREV_NEXT_REL = True
MIN_NODE_CHARS = 40        # 过滤纯噪音碎片（页眉页脚残留）

EXCLUDED_KEYS = [
    "file_name", "file_type", "file_size",
    "creation_date", "last_modified_date", "last_accessed_date",
]

ASCIIDOCTOR = os.environ.get("ASCIIDOCTOR", "asciidoctor")

# spec 仓库清单（url/pin/dest），拉取逻辑见 fetch_spec_repos()
SPEC_REPOS_YML = os.path.join(ROOT, "spec_repos.yml")

# 个别仓库在 asciidoctor 之前需要的准备步骤（浅克隆拿不到的构建期依赖）：
#   - riscv-crypto：spec 文档 include 了 extern/riscv-bitmanip 的 insns/*.adoc
#     与 extern/sail-riscv 的模型片段（.gitmodules 有 7 个子模块，只有这 2 个被
#     文档引用，其余 gnu-toolchain 等数 GB 与文档无关）。pin 取自仓库
#     `git ls-tree HEAD extern/<name>`（160000 模式即 submodule 钉住的提交）
#   - riscv-performance-events：事件表 adoc_event_tables/ 由仓库自带脚本生成
PRE_CONVERT_HOOKS: dict[str, list[str]] = {
    "riscv-crypto": [
        "submodule|extern/riscv-bitmanip|https://github.com/ben-marshall/riscv-bitmanip.git",
        "submodule|extern/sail-riscv|https://github.com/riscv/sail-riscv.git",
        # git-commit.adoc：Makefile 生成的版本戳文件（分支@提交），只被文档头 include
        "write|doc/scalar/git-commit.adoc|main\n@\n",
        "write|doc/vector/git-commit.adoc|main\n@\n",
    ],
    "riscv-performance-events": [
        "run|python3 gen_event_tables_adocs.py|doc/riscv-performance-events",
    ],
    "integer-vector-absolute-difference": [
        # unpriv/priv 卷 include 的全局配置在 docs-resources submodule 里
        "submodule|docs-resources|https://github.com/riscv/docs-resources",
    ],
    "riscv-asm-manual": [
        # prelude.adoc include 的全局配置同样在 docs-resources submodule 里
        "submodule|docs-resources|https://github.com/riscv/docs-resources",
    ],
    "riscv-bitmanip": [
        # revision.adoc-snippet：Makefile 生成的 revdate/revnumber/revremark 头
        "write|bitmanip/autogenerated/revision.adoc-snippet|:revdate: 2024-03-20\n:revnumber: 1.0.0-76-g517a903\n:revremark: Frozen\n",
    ],
}


# --------------------------------------------------------------------------
# 第 0 步：spec 仓库自动拉取（缺失才 clone；已存在则跳过，不碰本地修改）
# --------------------------------------------------------------------------
def _parse_spec_repos() -> list[dict]:
    """解析 spec_repos.yml（手写的极简 YAML 子集：- url/pin/dest 三字段）。"""
    repos: list[dict] = []
    cur: dict = {}
    with open(SPEC_REPOS_YML, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if line.strip() == "-":  # 罕见：值另起一行，不支持
                continue
            m = re.match(r"^\s*-\s*(\w+):\s*(.+)$", line)
            if m:  # 列表首项
                if cur:
                    repos.append(cur)
                cur = {m.group(1): m.group(2).strip().strip("'\"")}
                continue
            m = re.match(r"^\s*(\w+):\s*(.+)$", line)
            if m and cur:
                cur[m.group(1)] = m.group(2).strip().strip("'\"")
    if cur:
        repos.append(cur)
    return repos


def _git(*args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败:\n{proc.stderr[-800:]}")
    return proc


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def fetch_spec_repos(repos_needed: set[str]) -> list[str]:
    """clone doc/ 下缺失的 spec 仓库。返回本次实际拉取的 dest 列表。

    pin 三种取法（GitHub 约束：按哈希 fetch 只认完整 40 位 SHA）：
      - 空        ：浅克隆默认分支尖端
      - 分支/标签名：浅克隆该 ref（ls-remote 验证存在）
      - 完整 40 位哈希：先浅克隆默认分支，再 fetch --depth 1 该 SHA 后 detach
    短哈希不可用（无法从 ls-remote 唯一解析历史提交），直接报错提示改写。
    """
    if not repos_needed:
        return []
    specs = {r["dest"]: r for r in _parse_spec_repos() if r.get("url") and r.get("dest")}
    fetched = []
    for dest in sorted(repos_needed):
        if os.path.isdir(os.path.join(DOC_ROOT, dest)):
            continue  # 已存在（完整 clone 或手工放置的快照）均跳过
        if dest not in specs:
            raise FileNotFoundError(
                f"doc/{dest} 不存在，且 spec_repos.yml 里没有它的拉取配置"
            )
        url, pin = specs[dest]["url"], specs[dest].get("pin", "")
        target = os.path.join(DOC_ROOT, dest)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        print(f"  [fetch] {url} -> doc/{dest}（pin: {pin or '默认分支'}）")
        if not pin:
            _git("clone", "--depth", "1", url, target)
        else:
            probe = _git("ls-remote", "--heads", "--tags", url, pin, check=False)
            if probe.returncode == 0 and probe.stdout.strip():
                _git("clone", "--depth", "1", "-b", pin, url, target)
            elif _FULL_SHA_RE.match(pin):
                _git("clone", "--depth", "1", url, target)
                _git("fetch", "--depth", "1", "origin", pin, cwd=target)
                _git("checkout", "--detach", "FETCH_HEAD", cwd=target)
            else:
                raise SystemExit(
                    f"[fetch] pin '{pin}'（{dest}）既不是分支/标签，也不是完整 40 位哈希。\n"
                    f"[fetch] GitHub 按哈希 fetch 只认完整 SHA，请在 spec_repos.yml 里改写为 40 位：\n"
                    f"[fetch]   git ls-remote {url}   # 或在已有 clone 里 git rev-parse <短哈希>"
                )
        fetched.append(dest)

    # 仓库级准备钩子（补浅克隆拿不到的构建期依赖：子模块片段/生成表）
    _run_pre_convert_hooks(repos_needed)
    return fetched


def _run_pre_convert_hooks(repos_needed: set[str]) -> None:
    """对刚拉取（或已存在）的仓库跑转换前准备：缺失的辅助子模块、生成表。

    幂等：目录/产物已存在则跳过，不重复下载或生成。
    """
    for dest, hooks in PRE_CONVERT_HOOKS.items():
        if dest not in repos_needed:
            continue
        repo_dir = os.path.join(DOC_ROOT, dest)
        for spec in hooks:
            if spec.startswith("run|"):
                _, cmd, cwd = spec.split("|", 2)
                cwd_abs = os.path.join(ROOT, cwd)
                marker_dir = os.path.join(cwd_abs, "adoc_event_tables")
                if os.path.isdir(marker_dir) and os.listdir(marker_dir):
                    continue
                print(f"  [hook] {dest}: {cmd}")
                # 生成器不自建输出目录（open('adoc_event_tables/...','w+') 直接失败）
                os.makedirs(marker_dir, exist_ok=True)
                r = subprocess.run(cmd, shell=True, cwd=cwd_abs, capture_output=True, text=True)
                if r.returncode != 0:
                    print(f"  [hook] 警告: {cmd} 失败（继续，文档可能缺生成表）: {r.stderr[-300:]}", file=sys.stderr)
            elif spec.startswith("write|"):
                # write|<相对仓库路径>|<内容>：生成小占位文件（版本戳等）
                _, rel, content = spec.split("|", 2)
                target_abs = os.path.join(repo_dir, rel)
                if os.path.isfile(target_abs):
                    continue
                print(f"  [hook] {dest}: 写占位 {rel}")
                os.makedirs(os.path.dirname(target_abs), exist_ok=True)
                with open(target_abs, "w", encoding="utf-8") as f:
                    f.write(content)
            elif spec.startswith("submodule|"):
                # submodule|<相对仓库的路径>|<url>：按主仓库 ls-tree 钉住的提交浅 fetch
                _, rel, url = spec.split("|", 2)
                target_abs = os.path.join(repo_dir, rel)
                if os.path.isdir(target_abs) and os.listdir(target_abs):
                    continue  # 已有内容（完整快照或上次补过）
                pin = ""
                tree = _git("ls-tree", "HEAD", rel, cwd=repo_dir, check=False)
                m = re.search(r"commit ([0-9a-f]{40})", tree.stdout)
                if m:
                    pin = m.group(1)
                print(f"  [hook] {dest}: 浅克隆子模块 {rel}（pin: {pin[:12] or '默认分支'}）")
                _git("clone", "--depth", "1", url, target_abs)
                if pin:
                    _git("fetch", "--depth", "1", "origin", pin, cwd=target_abs)
                    _git("checkout", "--detach", "FETCH_HEAD", cwd=target_abs)


# --------------------------------------------------------------------------
# 第 1 步：spec(AsciiDoc) -> HTML -> Markdown
# --------------------------------------------------------------------------
def adoc_to_html(repo: str, entry: str, out_html: str) -> None:
    src = os.path.join(DOC_ROOT, repo, entry)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"找不到 spec 源: {src}")
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    cmd = [
        ASCIIDOCTOR, "-b", "html5",
        "-a", "linkcss",           # 不内嵌 CSS（只取正文结构）
        "-a", "nofooter",
        "-o", out_html,
        src,
    ]
    env = dict(os.environ)
    env.setdefault("LANG", "C.UTF-8")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(src) or ".", env=env)
    if proc.returncode != 0 or not os.path.isfile(out_html):
        raise RuntimeError(f"asciidoctor 失败({src}):\n{proc.stderr[-2000:]}")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+\n")


def _decode_entities(s: str) -> str:
    # html.unescape 两遍：asciidoctor 输出 &amp;lt; 这类双重转义；
    # &lt;include.h&gt; 这类先剥标签再解码会被吃掉 → 代码路径先解码再剥标签
    return html_mod.unescape(html_mod.unescape(s))


def _cell_to_md(cell_html: str) -> str:
    """<td>/<th> 单元格：块级元素转 <br>，多段落压缩为一行。"""
    cell = re.sub(r"</?(p|div|ul|ol|li|pre|blockquote)[^>]*>", " ", cell_html)
    # 列表项前缀恢复可读性
    cell = cell.strip()
    text = _inline_html_to_md(cell)
    return re.sub(r"\s+", " ", text).strip()


def _inline_html_to_md(s: str) -> str:
    s = _decode_entities(s)
    s = re.sub(r"<code>(.*?)</code>", r"`\1`", s, flags=re.S)
    s = re.sub(r"<em>(.*?)</em>", r"*\1*", s, flags=re.S)
    s = re.sub(r"<strong>(.*?)</strong>", r"**\1**", s, flags=re.S)
    s = re.sub(r"<sub>(.*?)</sub>", r"~\1~", s, flags=re.S)
    s = re.sub(r"<sup>(.*?)</sup>", r"^\1^", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", " ", s)
    s = _TAG_RE.sub("", s)
    return s


def _table_to_md(tbl_html: str) -> str:
    """<table> -> GitHub 风格 Markdown 表（表头行为空时补空头，匹配现有 KB 风格）。"""
    tbl_html = re.sub(r"<colgroup>.*?</colgroup>", "", tbl_html, flags=re.S)
    rows: list[list[str]] = []
    thead = re.search(r"<thead>(.*?)</thead>", tbl_html, re.S)
    if thead:
        rows.append([_cell_to_md(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", thead.group(1), re.S)])
        tbl_html = tbl_html[: thead.start()] + tbl_html[thead.end():]  # 防重复输出表头
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl_html, re.S):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)
        if cells:
            rows.append([_cell_to_md(c) for c in cells])
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    if not any(c.strip() for c in rows[0]):
        # asciidoctor 无表头表：现有 KB 产出 "|  |  |  |  |\n| --- | --- |" 风格，照搬
        rows.insert(0, [""] * ncol)
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * ncol) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def html_to_markdown(html_path: str, md_path: str) -> int:
    """HTML -> Markdown：保留 h1-h6 层级、代码块、表格；剥离导航/页脚/样式。"""
    with open(html_path, encoding="utf-8") as f:
        doc = f.read()
    m = re.search(r'<body[^>]*>(.*)</body>', doc, re.S)
    body = m.group(1) if m else doc
    # 只取正文容器；zalasr/perf-events 等无 #content 的文档用整个 body
    m = re.search(r'<div id="content">(.*?)$', body, re.S)
    if m:
        body = m.group(1)
    for drop in (
        r'<div id="toc"[^>]*>.*?</div>\s*(?=<)',           # 目录
        r'<div class="toc"[^>]*>.*?(?=<h[1-6]|<div class="sect")',  # 老版 toc
        r'<div id="footer">.*?</body>',                     # 页脚
        r'<(script|style)[^>]*>.*?</\1>',                   # 脚本样式
    ):
        body = re.sub(drop, "", body, flags=re.S)

    out: list[str] = []
    # 逐块扫描：标题 / 代码 / 表格 / 列表 / 段落
    pos = 0
    block_re = re.compile(
        r"""(?P<h><h(?P<l>[1-6])[^>]*>(?P<ht>.*?)</h(?P=l)>)"""
        r"""|(?P<pre><pre[^>]*>(?P<pret>.*?)(?:</code>\s*)?</pre>)"""
        r"""|(?P<tbl><table[^>]*>.*?</table>)"""
        r"""|(?P<ul><ul[^>]*>(?P<ult>.*?)</ul>)"""
        r"""|(?P<ol><ol[^>]*>(?P<olt>.*?)</ol>)""",
        re.S,
    )

    def emit_text(chunk_html: str) -> None:
        # 非结构区块：段落/aside/admonition 等，剥标签后按双换网分段
        if not chunk_html.strip():
            return
        text = _inline_html_to_md(re.sub(r"</p>|<br\s*/?>", "\n\n", chunk_html))
        for para in re.split(r"\n\s*\n", text):
            para = _WS_RE.sub("\n", para).strip()
            if para:
                out.append(para + "\n")

    def list_items(ult: str, prefix: str) -> str:
        items = []
        for li in re.findall(r"<li[^>]*>(.*?)(?=<li[^>]*>|$)", ult, re.S):
            inner = []
            sub_m = re.search(r"(<ul[^>]*>.*?</ul>|<ol[^>]*>.*?</ol>)", li, re.S)
            main = li[: sub_m.start()] if sub_m else li
            main = re.sub(r"\s+", " ", _inline_html_to_md(main)).strip()
            if main:
                items.append(f"{prefix} {main}")
            if sub_m:
                sub_prefix = "-" if prefix.startswith("-") else "-"
                items.append(list_items(sub_m.group(1), prefix + "  " + sub_prefix.rstrip()))
        return "\n".join(i for i in items if i.strip())

    for m in block_re.finditer(body):
        emit_text(body[pos:m.start()])
        if m.group("h"):
            level = int(m.group("l"))
            title = re.sub(r"\s+", " ", _inline_html_to_md(m.group("ht"))).strip()
            if title:
                out.append("#" * level + " " + title + "\n")
        elif m.group("pre"):
            pre_html = m.group("pre")
            # asciidoctor 代码块：<pre class="highlight"><code data-lang="c">…</code></pre>
            lang_m = re.search(r'<code[^>]*data-lang="([^"]+)"', pre_html)
            lang = lang_m.group(1) if lang_m else ""
            code = _decode_entities(m.group("pret"))
            # 只剥真实 HTML 标签（字母开头、ASCII 标签名），保留解码出的 <asm/xxx.h> 尖括号文本
            code = re.sub(r"</?[a-zA-Z][a-zA-Z0-9]*(\s[^<>]*)?/?>", "", code).strip("\n")
            out.append(f"```{lang}\n{code}\n```\n")
        elif m.group("tbl"):
            md_tbl = _table_to_md(m.group("tbl"))
            if md_tbl:
                out.append(md_tbl + "\n")
        elif m.group("ul"):
            md_list = list_items(m.group("ult"), "-")
            if md_list:
                out.append(md_list + "\n")
        elif m.group("ol"):
            md_list = list_items(m.group("olt"), "1.")
            if md_list:
                out.append(md_list + "\n")
        pos = m.end()
    emit_text(body[pos:])

    md = "\n".join(out)
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    return len(md)


# --------------------------------------------------------------------------
# 第 2 步：Markdown -> 节点（MarkdownNodeParser 语义）
# --------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
_TOKEN_RE = re.compile(r"\S+")


def _iter_headings(md_text: str):
    """产出围栏代码块之外的 Markdown 标题（sail/shell 注释 `# xxx` 不算标题）。"""
    pos = 0
    for m in _HEADING_RE.finditer(md_text):
        before = md_text[pos:m.start()]
        fences = before.count("\n```") + before.startswith("```")
        if fences % 2 == 1:  # 在未闭合围栏内
            continue
        yield m
        pos = m.end()


def _split_code_aware(text: str) -> list[str]:
    lines = text.split("\n")
    blocks, cur, in_fence = [], [], False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            cur.append(ln)
            continue
        cur.append(ln)
        if not in_fence and len(cur) >= CHUNK_SIZE // 4:
            blocks.append("\n".join(cur).strip())
            cur = []
    if cur:
        blocks.append("\n".join(cur).strip())
    return [b for b in blocks if b]


def parse_markdown_nodes(md_text: str) -> list[dict]:
    """按标题切节；同一节超长再按行块二次切分；小块并入前节（对齐现有 KB 粒度）。"""
    # (level, title, start, end)
    marks = []
    for m in _iter_headings(md_text):
        marks.append((len(m.group(1)), m.group(2).strip(), m.start(), m.end()))
    if not marks:
        sections = [("/", "/", 0, len(md_text))]
    else:
        # 文档标题前的内容（少见）忽略或并入首节
        sections = []
        for i, (lvl, title, s, e) in enumerate(marks):
            end = marks[i + 1][2] if i + 1 < len(marks) else len(md_text)
            body = md_text[e:end].strip("\n")
            sections.append((lvl, title, e, end))

    # 组 header 栈：节 = 标题行 + 正文；记录该节所有祖先标题
    nodes: list[dict] = []
    stack: list[tuple[int, str]] = []
    for lvl, title, s, e in sections:
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        header_path = "/" + "/".join(t for _, t in stack) + "/" if stack else "/"
        stack.append((lvl, title))
        sec_text = md_text[s:e].strip("\n")
        if not sec_text.strip():
            continue
        if len(_TOKEN_RE.findall(sec_text)) <= CHUNK_SIZE:
            nodes.append({"text": sec_text, "header_path": header_path})
        else:
            # 二次切分：子节点标题降为普通文本，保留 header_path
            for j, chunk in enumerate(_split_code_aware(sec_text)):
                nodes.append({"text": chunk, "header_path": header_path})

    # 小节合并：正文过短的节点并给前一个同级节点（模拟 include_metadata 合并效果）
    merged: list[dict] = []
    for nd in nodes:
        if merged and len(nd["text"]) < 200 and nd["text"] == nd["text"].strip():
            # 200 字以下且独立成节 → 并入前一节点（保持原文顺序）
            merged[-1]["text"] += "\n\n" + nd["text"]
        else:
            merged.append(nd)
    return merged


# --------------------------------------------------------------------------
# 第 3 步：组装 docstore.json
# --------------------------------------------------------------------------
def _rel(node_id: str, node_type: str, meta: dict, text: str) -> dict:
    return {
        "node_id": node_id,
        "node_type": node_type,
        "metadata": meta,
        "hash": hashlib.sha256(text.encode()).hexdigest(),
        "class_name": "RelatedNodeInfo",
    }


def build_docstore(md_sources: list[tuple[str, str]], kb_dir: str, md_ns: dict[str, int]) -> str:
    """md_sources: [(md 路径, 逻辑 file_path)] -> docs/<kb_dir>/docstore.json"""
    data: dict[str, dict] = {}
    ref_info: dict[str, dict] = {}
    meta_map: dict[str, dict] = {}

    for md_path, logical_path in md_sources:
        with open(md_path, encoding="utf-8") as f:
            md_text = f.read()
        file_name = os.path.basename(md_path)
        today = date.today().isoformat()
        file_meta = {
            "file_path": logical_path,
            "file_name": file_name,
            "file_type": "text/markdown",
            "file_size": len(md_text.encode()),
            "creation_date": today,
            "last_modified_date": today,
        }
        src_id = str(uuid.uuid4())
        nodes = parse_markdown_nodes(md_text)
        node_ids = []
        prev_id = None
        for nd in nodes:
            if len(nd["text"]) < MIN_NODE_CHARS and not nd["text"].lstrip().startswith("#"):
                continue
            nid = str(uuid.uuid4())
            full_meta = dict(file_meta)
            full_meta["header_path"] = nd["header_path"]
            rels: dict[str, dict] = {
                "1": _rel(src_id, "4", {k: v for k, v in file_meta.items() if k != "header_path"}, md_text),
            }
            if prev_id is not None:
                rels["2"] = _rel(prev_id, "1", {}, "")
            data[nid] = {
                "__data__": {
                    "id_": nid,
                    "embedding": None,
                    "metadata": full_meta,
                    "excluded_embed_metadata_keys": list(EXCLUDED_KEYS),
                    "excluded_llm_metadata_keys": list(EXCLUDED_KEYS),
                    "relationships": rels,
                    "metadata_template": "{key}: {value}",
                    "metadata_separator": "\n",
                    "text": nd["text"],
                    "mimetype": "text/plain",
                    "metadata_seperator": "\n",
                    "text_template": "{metadata_str}\n\n{content}",
                    "class_name": "TextNode",
                },
                "__type__": "1",
            }
            meta_map[nid] = {
                "doc_hash": hashlib.sha256(md_text.encode()).hexdigest(),
                "ref_doc_id": src_id,
            }
            node_ids.append(nid)
            prev_id = nid
        # NEXT 关系补齐（顺序链）
        for a, b in zip(node_ids, node_ids[1:]):
            data[a]["__data__"]["relationships"]["3"] = _rel(b, "1", {}, "")
        ref_info[src_id] = {"node_ids": node_ids, "metadata": dict(file_meta, header_path="/")}
        md_ns[file_name] = len(node_ids)

    docstore = {
        "docstore/data": data,
        "docstore/ref_doc_info": ref_info,
        "docstore/metadata": meta_map,
    }
    out = os.path.join(KB_ROOT, kb_dir, "docstore.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(docstore, f, ensure_ascii=False)
    return out


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
def find_asciidoctor() -> str | None:
    cand = shutil.which(ASCIIDOCTOR)
    if cand:
        return cand
    # gem --user-install 装到 ~/bin
    home_bin = os.path.join(os.path.expanduser("~"), "bin", "asciidoctor")
    if os.path.isfile(home_bin) and os.access(home_bin, os.X_OK):
        return home_bin
    return None


def build_kb(kb: str, skip_convert: bool, markdown_only: bool) -> None:
    kb_dir, docs_map = KB_SPECS[kb]
    build_dir = os.path.join(BUILD_ROOT, kb)
    md_ns: dict[str, int] = {}
    md_sources: list[tuple[str, str]] = []

    if not skip_convert:
        # spec 源缺失时按 spec_repos.yml 自动拉取（已存在的仓库不动）
        repos_needed = {repo for repo, _ in docs_map.values()}
        fetch_spec_repos(repos_needed)
        ado = find_asciidoctor()
        if not ado:
            raise RuntimeError(
                "未找到 asciidoctor。安装：gem install --user-install asciidoctor "
                "（或设 ASCIIDOCTOR 指向可执行文件）"
            )
        os.environ["ASCIIDOCTOR"] = ado

    for md_name, (repo, entry) in sorted(docs_map.items()):
        html_out = os.path.join(build_dir, os.path.splitext(md_name)[0] + ".html")
        md_out = os.path.join(build_dir, md_name)
        if not skip_convert:
            print(f"  [convert] {repo}/{entry} -> {os.path.basename(md_out)}")
            adoc_to_html(repo, entry, html_out)
            n = html_to_markdown(html_out, md_out)
            print(f"    markdown: {n} 字符")
        if not os.path.isfile(md_out):
            raise FileNotFoundError(f"缺少 markdown 中间产物: {md_out}（先跑转换步骤）")
        # docstore 里的逻辑路径：中性前缀（检索结果 file_path 证据链用，
        # 不暴露任何构建环境信息）
        logical = f"/riscv-doc-rag/markdown/{kb}/{md_name}"
        md_sources.append((md_out, logical))

    if markdown_only:
        print(f"{kb}: markdown 已生成于 {build_dir}/（--markdown-only，跳过切分组装）")
        return

    out = build_docstore(md_sources, kb_dir, md_ns)
    total = sum(md_ns.values())
    detail = ", ".join(f"{k}={v}" for k, v in sorted(md_ns.items()))
    print(f"{kb}: {total} 节点 -> {out}\n  ({detail})")


def build_vectors(names: list[str]) -> None:
    """调用 server.py --build-vectors（需要 fastembed 可用）。"""
    for kb in names:
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "server.py"), "--build-vectors", kb],
            cwd=ROOT,
        )
        if r.returncode != 0:
            print(f"[warn] {kb} 向量构建失败（可稍后单独跑 --build-vectors）", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kb", nargs="*", help="知识库短名（默认全部）")
    ap.add_argument("--markdown-only", action="store_true", help="只做 spec->markdown")
    ap.add_argument("--skip-convert", action="store_true", help="跳过 asciidoctor 转换（复用 build/ 下 markdown）")
    ap.add_argument("--build-vectors", action="store_true", help="组装后调用 server.py 重建 vectors.npz")
    ap.add_argument("--fetch", action="store_true", help="只拉取缺失的 spec 仓库（按 spec_repos.yml）后退出")
    args = ap.parse_args()

    names = args.kb or list(KB_SPECS)
    unknown = [n for n in names if n not in KB_SPECS]
    if unknown:
        ap.error(f"未知知识库: {unknown}，可选: {', '.join(KB_SPECS)}")

    if args.fetch:
        repos_needed = {repo for n in names for repo, _ in KB_SPECS[n][1].values()}
        got = fetch_spec_repos(repos_needed)
        print("完成。" if not got else f"已拉取 {len(got)} 个仓库: {', '.join(got)}")
        return

    print(f"构建知识库: {', '.join(names)}")
    for kb in names:
        build_kb(kb, skip_convert=args.skip_convert, markdown_only=args.markdown_only)
    if args.build_vectors and not args.markdown_only:
        build_vectors(names)
    print("完成。")


if __name__ == "__main__":
    main()
