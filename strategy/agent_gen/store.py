"""策略源码落盘：_staging 暂存 → custom 启用 / _rejected 归档（#30）

三态目录与生命周期：

    _staging/<run_id>__<key>.py    待校验，闸门 2/3 在此读取
    custom/<key>.py                已启用，被 _discover_custom() 自动发现
    _rejected/<run_id>__<key>.py   校验失败归档，人工清理

staging 文件名带 run_id（每个生成会话一个，存在 st.session_state 里跨 rerun 存活），
所以两个标签页同时生成同名策略互不覆盖，同一次生成的回灌重试则正确覆盖上一版。
`key == 文件名` 这条不变量只对 custom/ 成立——staging 里的文件由闸门 3 按路径加载。

provider 永远不碰文件系统，落盘只经本模块，路径全部由这里拼出，模型只能影响 key 字面量。
"""

import ast
import logging
import re
import shutil
import sys
import time
from pathlib import Path

from strategy.trade_strategy import (
    KEY_PATTERN, _CUSTOM_DIR, _registry_lock, refresh_registry, taken_keys,
)

logger = logging.getLogger(__name__)

STAGING_DIR = _CUSTOM_DIR / "_staging"
REJECTED_DIR = _CUSTOM_DIR / "_rejected"

# 每次 stage() 顺手清理超过此时长的 staging 残留（会话中途被放弃时留下）
STALE_HOURS = 24

# run_id 由前端生成（uuid4().hex 前若干位），此处只做文件名安全性校验
RUN_ID_PATTERN = re.compile(r"^[a-z0-9]{4,32}$")


class StoreError(Exception):
    """落盘相关错误的基类"""


class InvalidKeyError(StoreError):
    """key 不合法（字面量不符合 KEY_PATTERN）"""


class KeyTakenError(StoreError):
    """key 已被内置策略或已启用的自定义策略占用"""


# ========== 校验 ==========

def validate_key(key: str, *, check_taken: bool = True) -> None:
    """校验 key 合法且未被占用。闸门 2 与 promote() 共用。

    Args:
        check_taken: 只校验字面量合法性时置 False（例如改名前的预检）
    """
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise InvalidKeyError(
            f"key={key!r} 不合法：需小写字母开头，仅含小写字母/数字/下划线，长度 3~32"
        )
    if check_taken and key in taken_keys():
        raise KeyTakenError(f"key={key!r} 已被占用")


def _check_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
        raise StoreError(f"run_id={run_id!r} 不合法：需 4~32 位小写字母或数字")


def staging_path(run_id: str, key: str) -> Path:
    _check_run_id(run_id)
    validate_key(key, check_taken=False)
    return STAGING_DIR / f"{run_id}__{key}.py"


def custom_path(key: str) -> Path:
    validate_key(key, check_taken=False)
    return _CUSTOM_DIR / f"{key}.py"


# ========== 三态迁移 ==========

def stage(run_id: str, key: str, code: str) -> Path:
    """把模型交付的源码写进 _staging/，返回路径。同 run_id 同 key 覆盖上一版。"""
    path = staging_path(run_id, key)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    sweep_staging()
    path.write_text(code, encoding="utf-8")
    logger.info("暂存策略 %s → %s", key, path.name)
    return path


def reject(run_id: str, key: str, reason: str = "") -> Path | None:
    """把 staging 里的文件归档进 _rejected/。文件不存在返回 None。"""
    src = staging_path(run_id, key)
    if not src.exists():
        return None
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    dst = REJECTED_DIR / src.name
    src.replace(dst)  # 同一文件系统内原子移动，且允许覆盖同名旧归档
    logger.info("归档失败策略 %s → _rejected/%s（%s）", key, dst.name, reason or "未说明")
    return dst


def drop_staging(run_id: str, key: str) -> bool:
    """直接丢掉 staging 文件（不归档）。返回是否真的删了。

    用于模型在回灌中改名重交：旧 key 的那版既没被拒也没被启用，进 _rejected
    只是给人工清理添活。
    """
    path = staging_path(run_id, key)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def promote(run_id: str, key: str) -> Path:
    """把通过全部闸门的策略从 _staging/ 移入 custom/ 并立即生效。

    全程持注册表锁：判重与 rename 之间不能有窗口，否则另一标签页可能抢先占用同名
    key 而被这里静默覆盖。rename 在同一文件系统内原子，因此 custom/<key>.py 不存在
    「写到一半被 import」的中间态——不得改成读出再写入。

    Raises:
        KeyTakenError: key 已被内置或已启用策略占用
        StoreError: staging 文件不存在，或落盘后未能被自动发现（此时已回滚归档）
    """
    with _registry_lock:
        validate_key(key)  # 锁内复查，覆盖闸门 2 到本次点击之间的 TOCTOU 窗口

        src = staging_path(run_id, key)
        if not src.exists():
            raise StoreError(f"暂存文件不存在：{src.name}（会话可能已过期，请重新生成）")

        dst = custom_path(key)
        src.rename(dst)

        registry = refresh_registry()
        if key not in registry:
            # 闸门都过了却发现不到，说明存在闸门没覆盖到的结构问题。
            # 回滚到 _rejected/ 而不是把哑文件留在 custom/ 里。
            dst.rename(src)
            reject(run_id, key, "落盘后未能被自动发现")
            raise StoreError(
                f"策略 {key} 落盘后未能被注册（详见日志中 _discover_custom 的 warning），已归档"
            )

        logger.info("启用策略 %s → custom/%s", key, dst.name)
        return dst


def delete(key: str) -> bool:
    """删除已启用的策略并立即从注册表移除。返回是否真的删了文件。

    必须同时清 sys.modules 与 __pycache__：否则同名 key 重新生成时，
    importlib.import_module 会先命中旧模块对象。
    """
    with _registry_lock:
        path = custom_path(key)
        existed = path.exists()
        path.unlink(missing_ok=True)
        sys.modules.pop(f"strategy.custom.{key}", None)
        for pyc in (_CUSTOM_DIR / "__pycache__").glob(f"{key}.cpython-*.pyc"):
            pyc.unlink(missing_ok=True)
        refresh_registry()
        if existed:
            logger.info("删除策略 %s", key)
        return existed


def sweep_staging(max_age_hours: float = STALE_HOURS) -> int:
    """清理 _staging/ 中过期的残留文件，返回清理数量。

    生成会话中途被放弃时 staging 文件会一直留着。每次 stage() 顺手扫一遍，
    不引调度器。_rejected/ 不自动清（留给用户在前端人工删）。
    """
    if not STAGING_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    n = 0
    for path in STAGING_DIR.glob("*.py"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                n += 1
        except OSError as e:  # 并发删除等，不值得因此中断落盘
            logger.warning("清理 staging 残留 %s 失败：%s", path.name, e)
    if n:
        logger.info("清理 %d 个过期 staging 文件", n)
    return n


# ========== 改名 ==========

def _find_key_literal(tree: ast.Module) -> ast.Constant:
    """定位策略类里 `key = "..."` 的字符串字面量节点"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            targets = (stmt.targets if isinstance(stmt, ast.Assign)
                       else [stmt.target] if isinstance(stmt, ast.AnnAssign) else [])
            for t in targets:
                if (isinstance(t, ast.Name) and t.id == "key"
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    found.append(stmt.value)
    if len(found) != 1:
        raise StoreError(f"源码中应恰好有 1 处类级 key 赋值，实际 {len(found)} 处，无法安全改名")
    return found[0]


def rewrite_key(code: str, new_key: str) -> str:
    """把源码里的 `key = "..."` 改成 new_key，其余字节原样保留。

    按 AST 节点的行列位置切片替换，不用正则、不重排代码——改完必须重跑闸门 2，
    但无需重跑闸门 3（key 不参与 on_bar 逻辑，行为不变）。
    """
    validate_key(new_key)

    node = _find_key_literal(ast.parse(code))
    if node.lineno != node.end_lineno:
        raise StoreError("key 字面量跨行，无法安全改名")

    lines = code.splitlines(keepends=True)
    # ast 的 col_offset 是 utf-8 字节偏移，同行有中文注释时按字符切会错位
    raw = lines[node.lineno - 1].encode("utf-8")
    patched = raw[:node.col_offset] + f'"{new_key}"'.encode() + raw[node.end_col_offset:]
    lines[node.lineno - 1] = patched.decode("utf-8")
    return "".join(lines)


# ========== 前端管理 ==========

def list_custom() -> list[dict]:
    """列出 custom/ 下的策略文件（含未能注册的坏文件，供前端展示与删除）"""
    if not _CUSTOM_DIR.is_dir():
        return []
    from strategy.trade_strategy import _registry

    registry = _registry()
    out = []
    for path in sorted(_CUSTOM_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        cls = registry.get(path.stem)
        out.append({
            "key": path.stem,
            "path": str(path),
            "mtime": path.stat().st_mtime,
            "registered": cls is not None,
            "name": cls().name if cls is not None else "(未能加载)",
        })
    return out


def read_source(key: str) -> str:
    """读取已启用策略的源码（前端「查看源码」）"""
    path = custom_path(key)
    if not path.exists():
        raise StoreError(f"策略 {key} 不存在")
    return path.read_text(encoding="utf-8")


def clear_rejected() -> int:
    """清空 _rejected/，返回删除数量（前端人工清理入口）"""
    if not REJECTED_DIR.is_dir():
        return 0
    n = 0
    for path in REJECTED_DIR.glob("*.py"):
        path.unlink(missing_ok=True)
        n += 1
    shutil.rmtree(REJECTED_DIR / "__pycache__", ignore_errors=True)
    return n
