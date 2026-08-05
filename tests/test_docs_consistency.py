"""issue #45：文档与代码的一致性（纯读文件，不打网络）

README 之所以会烂成 215 行错处一堆，不是因为没人愿意改，而是因为**没有任何东西提醒它该改**。
这个文件把最容易漂移的几处变成红色用例：

1. README 的页面清单 == `app.py::PAGES` 的键（顺序也一致）——直接防住「写 3 页面实际 7 页」；
2. README 的顶层包清单 == 仓库实际顶层目录；
3. 文档里提到的仓库路径必须真实存在；
4. 文档里不许出现已删除的名字（`fusion.py` / `notify` / `api/server.py` / PushPlus …）；
5. `docs/strategies.md` 的技术分量权重表 == `SIGNAL_COMPONENT_WEIGHTS` 逐项一致。

第 5 条是刻意的：那张表是最招人抄错、也最容易过期的一处（改动前 README 写的是 6 维、
权重全错、还漏了 supertrend）。
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
README = os.path.join(ROOT, "README.md")
DOCS = os.path.join(ROOT, "docs")
DOC_FILES = ("pages.md", "data-sources.md", "architecture.md", "strategies.md")

# 已删除/改名的东西，文档里再出现就是误导（历史说明也不要——写在 issue 里）
GONE = ("fusion.py", "fuse_signals", "notify/wechat.py", "api/server.py",
        "PUSHPLUS", "pushplus", "settings.STOCK_POOL", "_get_sector_kline")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _all_docs():
    """{相对路径: 内容}，README + docs/*.md"""
    out = {"README.md": _read(README)}
    for name in DOC_FILES:
        out[f"docs/{name}"] = _read(os.path.join(DOCS, name))
    return out


# ---------- 1. 页面清单 ----------
def _pages_from_app():
    """从 app.py 源码里读 PAGES 表的键（不 import app——那会执行整个 Streamlit 脚本）"""
    src = _read(os.path.join(ROOT, "app.py"))
    body = src.split("PAGES = {", 1)[1].split("}", 1)[0]
    return re.findall(r'"([^"]+)":\s*page_', body)


def _pages_from_readme():
    """README「## 页面」那节表格第一列"""
    section = _read(README).split("## 页面", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|", section, re.M)
    return [r for r in rows if r not in ("页面", ":---", "---")
            and not set(r) <= set("-: ")]


def test_readme_page_list_matches_routing_table():
    app_pages = _pages_from_app()
    readme_pages = _pages_from_readme()
    assert len(app_pages) == 7, f"路由表页面数变了（{len(app_pages)}），文档要跟着改"
    assert readme_pages == app_pages, (
        f"README 的页面清单与 app.py::PAGES 不一致\n"
        f"  README: {readme_pages}\n  app.py: {app_pages}")


def test_docs_pages_covers_every_page():
    """docs/pages.md 要给每一页一个小节"""
    doc = _read(os.path.join(DOCS, "pages.md"))
    for name in _pages_from_app():
        assert name in doc, f"docs/pages.md 少了「{name}」这一页"


# ---------- 2. 顶层包清单 ----------
def test_readme_top_level_packages_match_repo():
    section = _read(README).split("## 代码结构", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"`([a-z_]+)/`", section))
    # 顶层代码包 = 带 __init__.py 的目录，外加 tests/（它不是包但要出现在文档里）
    actual = {"tests"}
    for name in os.listdir(ROOT):
        path = os.path.join(ROOT, name)
        if (os.path.isdir(path) and not name.startswith(".")
                and name != "__pycache__"
                and os.path.exists(os.path.join(path, "__init__.py"))):
            actual.add(name)
    assert documented == actual, (
        f"README 的顶层包清单与仓库不一致\n  文档: {sorted(documented)}\n"
        f"  实际: {sorted(actual)}")


# ---------- 3. 提到的路径必须存在 ----------
def test_referenced_paths_exist():
    pat = re.compile(r"`([a-zA-Z_][\w./-]*\.(?:py|json|md|txt|yml|example))`")
    # 只校验「看起来是仓库相对路径」的：含 / 的，或根目录的已知文件。
    # 裸文件名（如表格里的 `technical.py`，目录由上下文给出）无法可靠定位，跳过。
    root_files = {f for f in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, f))}
    missing = []
    for rel, src in _all_docs().items():
        for path in set(pat.findall(src)):
            if "/" not in path and path not in root_files:
                continue
            candidate = os.path.join(ROOT, path)
            if os.path.exists(candidate):
                continue
            # config/stocks.json、.env 这类不入 git 的本机文件允许缺失
            if path in ("config/stocks.json", ".env", "docker-compose.yml"):
                continue
            missing.append(f"{rel}: {path}")
    assert not missing, "文档里提到的路径不存在：\n  " + "\n  ".join(missing)


# ---------- 4. 不许出现已删除的名字 ----------
def test_no_removed_names_in_docs():
    hits = []
    for rel, src in _all_docs().items():
        for gone in GONE:
            if gone in src:
                hits.append(f"{rel}: {gone}")
    assert not hits, ("文档里出现了已删除/改名的东西（历史说明请写在 issue 里）：\n  "
                      + "\n  ".join(hits))


# ---------- 5. 权重表与代码一致 ----------
def test_strategy_doc_weights_match_code():
    from analysis.technical import SIGNAL_COMPONENT_WEIGHTS

    doc = _read(os.path.join(DOCS, "strategies.md"))
    section = doc.split("## 技术信号", 1)[1].split("\n## ", 1)[0]
    rows = dict(re.findall(r"^\|\s*([a-z_]+)\s*\|\s*([0-9.]+)\s*\|", section, re.M))
    assert rows, "docs/strategies.md 里没找到技术分量权重表"
    got = {k: float(v) for k, v in rows.items()}
    assert got == SIGNAL_COMPONENT_WEIGHTS, (
        "docs/strategies.md 的权重表与 analysis/technical.py 不一致\n"
        f"  文档: {got}\n  代码: {SIGNAL_COMPONENT_WEIGHTS}")


def test_strategy_doc_states_single_source_not_fusion():
    """这条防的是最贵的一类误导：文档描述一个不存在的加权融合公式"""
    doc = _read(os.path.join(DOCS, "strategies.md"))
    assert "单源选择" in doc
    assert "综合分数" not in doc, "「综合分数 = 技术×w + 缠论×w」这个公式并不存在"
    from strategy.decision import SOURCE_COLUMNS

    for source in SOURCE_COLUMNS:
        assert source in doc, f"信号源 {source} 没写进 docs/strategies.md"


# ---------- 6. README 保持是「说明+指路」 ----------
README_MAX_LINES = 100


def test_readme_stays_thin_and_links_to_docs():
    src = _read(README)
    n = len(src.splitlines())
    assert n <= README_MAX_LINES, \
        f"README 应只做说明与指路（≤{README_MAX_LINES} 行），实际 {n} 行——细节请写进 docs/"
    for name in DOC_FILES:
        assert f"docs/{name}" in src, f"README 没有指向 docs/{name}"
    assert "CLAUDE.md" in src, "README 应指向 CLAUDE.md（开发约定）"


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"[OK] {name}")
    print(f"\n全部 {len(tests)} 项测试通过!")
