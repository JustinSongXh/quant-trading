"""strategy/agent_gen/store.py 落盘测试（确定性，无需联网、不调用 LLM）

覆盖 #30 评论「策略命名冲突与并发加锁」定稿的全部约定：
1. key 校验：字面量合法性 / 撞内置 / 撞已启用
2. staging 命名：run_id 隔离（两标签页不互覆盖）、同 run_id 重试覆盖
3. promote：判重 + 原子 rename + 立即生效；撞名抛 KeyTakenError；发现失败自动回滚归档
4. delete：清 sys.modules 与 __pycache__，同名 key 可重新启用
5. rewrite_key：按字节位置改写，除 key 字面量外逐字节不变
6. sweep_staging：过期残留被清、新文件保留
7. 并发 stage 同一 key：文件名不同、内容互不覆盖
"""

import os, shutil, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy.agent_gen import store
from strategy.agent_gen.store import InvalidKeyError, KeyTakenError, StoreError
from strategy.trade_strategy import (
    _BUILTIN, _CUSTOM_DIR, get_trade_strategy, list_trade_strategies, refresh_registry,
)

RUN_A = "aaaa1111"
RUN_B = "bbbb2222"

GOOD = '''\
from strategy.trade_strategy import TradeStrategy


class GeneratedStrategy(TradeStrategy):
    key = "{key}"          # 策略标识，须与文件名一致
    name = "{name}"
    description = "测试用策略"
    example = "无"
    custom = True
    PARAMS = []

    def on_bar(self, broker, ctx):
        pass
'''


def keys() -> list[str]:
    return [s["key"] for s in list_trade_strategies()]


def cleanup(*ks, runs=(RUN_A, RUN_B)):
    for k in ks:
        (_CUSTOM_DIR / f"{k}.py").unlink(missing_ok=True)
        sys.modules.pop(f"strategy.custom.{k}", None)
        for r in runs:
            (store.STAGING_DIR / f"{r}__{k}.py").unlink(missing_ok=True)
            (store.REJECTED_DIR / f"{r}__{k}.py").unlink(missing_ok=True)
    shutil.rmtree(_CUSTOM_DIR / "__pycache__", ignore_errors=True)
    refresh_registry()


def expect(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return e
    raise AssertionError(f"应抛 {exc.__name__} 但没抛：{fn.__name__}({a})")


# ---------- 1. key 校验 ----------
def test_validate_key():
    for bad in ["", "A_bad", "1abc", "ab", "a" * 33, "has-dash", "has space",
                "../escape", "with.dot", None, 123]:
        expect(InvalidKeyError, store.validate_key, bad)

    for good in ["abc", "dip_buy", "a1_2", "a" * 32]:
        store.validate_key(good)  # 不该抛

    for builtin in _BUILTIN:
        expect(KeyTakenError, store.validate_key, builtin)

    # 撞已启用的自定义策略
    try:
        (_CUSTOM_DIR / "t_exist.py").write_text(GOOD.format(key="t_exist", name="X"), "utf-8")
        refresh_registry()
        expect(KeyTakenError, store.validate_key, "t_exist")
        store.validate_key("t_exist", check_taken=False)  # 只校验字面量时应放行
    finally:
        cleanup("t_exist")
    print("[OK] key 校验：非法字面量 / 撞内置 / 撞已启用 全部拒绝")


def test_taken_counts_broken_files():
    """坏文件不会被注册，但占着 custom/<key>.py 路径，必须算作已占用"""
    try:
        (_CUSTOM_DIR / "t_broken.py").write_text("class Broken(:\n", "utf-8")
        refresh_registry()
        assert "t_broken" not in keys(), "坏文件不该被注册"
        expect(KeyTakenError, store.validate_key, "t_broken")
        print("[OK] 未能注册的坏文件仍占用 key，不会被 promote 静默覆盖")
    finally:
        cleanup("t_broken")


# ---------- 2. staging 命名 ----------
def test_staging_run_id_isolation():
    try:
        pa = store.stage(RUN_A, "t_same", GOOD.format(key="t_same", name="来自A"))
        pb = store.stage(RUN_B, "t_same", GOOD.format(key="t_same", name="来自B"))
        assert pa != pb and pa.exists() and pb.exists()
        assert pa.name == f"{RUN_A}__t_same.py", pa.name
        assert "来自A" in pa.read_text("utf-8") and "来自B" in pb.read_text("utf-8")

        # 同 run_id 重试：覆盖上一版
        pa2 = store.stage(RUN_A, "t_same", GOOD.format(key="t_same", name="A第二版"))
        assert pa2 == pa and "A第二版" in pa.read_text("utf-8")

        # staging 不被自动发现
        refresh_registry()
        assert "t_same" not in keys()
        print("[OK] staging 按 run_id 隔离：两会话同 key 互不覆盖，同会话重试正确覆盖")
    finally:
        cleanup("t_same")


def test_bad_run_id_rejected():
    for bad in ["", "ab", "UPPER123", "has-dash", "a" * 33, None]:
        expect(StoreError, store.stage, bad, "t_x", "pass")
    print("[OK] 非法 run_id 被拒（文件名安全）")


# ---------- 3. promote ----------
def test_promote_success():
    try:
        store.stage(RUN_A, "t_promote", GOOD.format(key="t_promote", name="已启用策略"))
        dst = store.promote(RUN_A, "t_promote")

        assert dst == _CUSTOM_DIR / "t_promote.py" and dst.exists()
        assert not (store.STAGING_DIR / f"{RUN_A}__t_promote.py").exists(), "staging 应已移走"
        assert "t_promote" in keys(), "promote 后应立即可选（无需再手动 refresh）"
        assert get_trade_strategy("t_promote").name == "已启用策略"
        print("[OK] promote：移入 custom/ 并立即生效")
    finally:
        cleanup("t_promote")


def test_promote_key_taken():
    try:
        # 撞内置
        store.stage(RUN_A, "default", GOOD.format(key="default", name="冒名"))
        expect(KeyTakenError, store.promote, RUN_A, "default")
        assert get_trade_strategy("default").__class__ is _BUILTIN["default"], "内置被覆盖了"
        assert (store.STAGING_DIR / f"{RUN_A}__default.py").exists(), "失败时不该动 staging"

        # 撞已启用
        store.stage(RUN_A, "t_dup", GOOD.format(key="t_dup", name="第一个"))
        store.promote(RUN_A, "t_dup")
        store.stage(RUN_B, "t_dup", GOOD.format(key="t_dup", name="第二个"))
        expect(KeyTakenError, store.promote, RUN_B, "t_dup")
        assert get_trade_strategy("t_dup").name == "第一个", "已启用策略被覆盖了"
        print("[OK] promote 判重：撞内置 / 撞已启用均拒绝，原文件不受影响")
    finally:
        cleanup("default", "t_dup")


def test_promote_missing_staging():
    expect(StoreError, store.promote, RUN_A, "t_never_staged")
    print("[OK] staging 文件不存在时 promote 报错而非静默成功")


def test_promote_rollback_when_undiscoverable():
    """闸门没拦住的结构问题（这里造 key≠文件名）→ 回滚归档，不把哑文件留在 custom/"""
    try:
        store.stage(RUN_A, "t_rollback", GOOD.format(key="wrong_inner", name="内外不一致"))
        err = expect(StoreError, store.promote, RUN_A, "t_rollback")
        assert not isinstance(err, KeyTakenError)

        assert not (_CUSTOM_DIR / "t_rollback.py").exists(), "哑文件被留在 custom/ 了"
        assert (store.REJECTED_DIR / f"{RUN_A}__t_rollback.py").exists(), "未归档到 _rejected"
        assert "t_rollback" not in keys() and "wrong_inner" not in keys()
        print("[OK] promote 后发现不到 → 自动回滚并归档，custom/ 不留残骸")
    finally:
        cleanup("t_rollback")


# ---------- 4. delete ----------
def test_delete_and_recreate():
    try:
        store.stage(RUN_A, "t_del", GOOD.format(key="t_del", name="第一版"))
        store.promote(RUN_A, "t_del")
        assert "t_del" in keys()

        assert store.delete("t_del") is True
        assert "t_del" not in keys() and not (_CUSTOM_DIR / "t_del.py").exists()
        assert "strategy.custom.t_del" not in sys.modules, "sys.modules 未清"
        assert store.delete("t_del") is False, "重复删除应返回 False"

        # 同名重建：不能命中旧模块缓存
        store.stage(RUN_B, "t_del", GOOD.format(key="t_del", name="第二版"))
        store.promote(RUN_B, "t_del")
        assert get_trade_strategy("t_del").name == "第二版", "取到了删除前的旧版本"
        print("[OK] delete 清干净（sys.modules + __pycache__），同名 key 可重新启用")
    finally:
        cleanup("t_del")


# ---------- 5. rewrite_key ----------
def test_rewrite_key():
    src = GOOD.format(key="old_name", name="改名测试")   # 该行尾部带中文注释
    out = store.rewrite_key(src, "new_name")

    assert 'key = "new_name"' in out
    assert "old_name" not in out
    # 除 key 字面量外逐字节不变（含同行中文注释未被 utf-8 偏移错位）
    assert out.replace('"new_name"', '"old_name"') == src, "改写波及了其它字节"
    assert "# 策略标识，须与文件名一致" in out

    # 单引号写法同样支持
    assert 'key = "quoted"' in store.rewrite_key(
        src.replace('"old_name"', "'old_name'"), "quoted")

    expect(InvalidKeyError, store.rewrite_key, src, "Bad-Key")
    expect(KeyTakenError, store.rewrite_key, src, "default")
    expect(StoreError, store.rewrite_key, "class A:\n    pass\n", "any_key")
    expect(SyntaxError, store.rewrite_key, "class A(:\n", "any_key")
    print("[OK] rewrite_key：按字节位置精确改写，非法/撞名/无 key 均报错")


# ---------- 6. sweep ----------
def test_sweep_staging():
    try:
        fresh = store.stage(RUN_A, "t_fresh", "pass\n")
        stale = store.stage(RUN_B, "t_stale", "pass\n")
        old = time.time() - (store.STALE_HOURS + 1) * 3600
        os.utime(stale, (old, old))

        assert store.sweep_staging() == 1
        assert fresh.exists() and not stale.exists()
        print(f"[OK] sweep_staging 清理 >{store.STALE_HOURS}h 残留，新文件保留")
    finally:
        cleanup("t_fresh", "t_stale")


def test_reject():
    try:
        store.stage(RUN_A, "t_rej", "pass\n")
        dst = store.reject(RUN_A, "t_rej", "冒烟回测超时")
        assert dst.exists() and dst.parent == store.REJECTED_DIR
        assert not (store.STAGING_DIR / f"{RUN_A}__t_rej.py").exists()
        assert store.reject(RUN_A, "t_rej") is None, "文件不存在时应返回 None"
        print("[OK] reject 归档到 _rejected/")
    finally:
        cleanup("t_rej")


# ---------- 7. 并发 ----------
def test_concurrent_stage_distinct_names():
    """两个标签页同时生成同名策略：各自的 staging 文件名不同，内容互不覆盖"""
    try:
        paths = {}

        def worker(run_id):
            paths[run_id] = store.stage(run_id, "t_race",
                                        GOOD.format(key="t_race", name=run_id))

        threads = [threading.Thread(target=worker, args=(r,)) for r in (RUN_A, RUN_B)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert paths[RUN_A].name != paths[RUN_B].name, paths
        for run_id, path in paths.items():
            assert f'name = "{run_id}"' in path.read_text("utf-8"), f"{run_id} 的内容被覆盖"
        print("[OK] 并发 stage 同一 key：文件名不同，内容互不覆盖")
    finally:
        cleanup("t_race")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n全部 {len(tests)} 项测试通过!")
