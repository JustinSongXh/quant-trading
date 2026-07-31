"""生成任务的进程管理：起、轮询、杀、自动回收（#30）

每个「一轮 advance()」是一个独立进程（见 runner.py），按 owner（Streamlit 的
session id）索引。这样才做到：

- 用户点「停止」→ 杀进程组，token 立刻停花
- 用户**刷新页面** → 新会话拿到新 session id，旧 job 再也不会被轮询，
  由后台回收线程按心跳超时杀掉（见 STALE_SECONDS）
- 互不影响：一个会话的 job 崩了/被杀，跟别的会话无关

为什么用心跳而不是去问 Streamlit「哪些会话还活着」：那要摸
`runtime.get_instance()._session_mgr` 私有 API，版本一升就可能断。心跳只用公开行为
（前端每 ~1s 轮询一次），代价是最坏多花 STALE_SECONDS 的 token——相比原来白花
一整轮（最长两三分钟）已经是量级差别。

回收线程是必须的：只在「有人操作页面」时清扫的话，用户刷新完就干别的去了，
没有任何脚本运行，孤儿 job 就一直跑。
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from strategy.agent_gen.runner import INPUT_NAME, OUTPUT_NAME

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

# 多久没被轮询就认为主人跑了（刷新/关页面）。前端约每秒轮一次。
STALE_SECONDS = 8.0
# 回收线程的扫描间隔
REAP_INTERVAL = 2.0
# 同时在跑的生成任务上限——订阅额度是共享的，别让几个人同时把它打满
MAX_CONCURRENT = 2
# 单个任务的兜底硬超时：模型侧卡住时不至于永远占着额度
HARD_TIMEOUT = 900.0


class JobError(Exception):
    """起任务失败（并发已满等）"""


@dataclass
class Job:
    owner: str
    job_dir: Path
    proc: subprocess.Popen
    started_at: float
    last_seen: float
    label: str = ""
    _killed: bool = field(default=False, repr=False)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def is_running(self) -> bool:
        return self.proc.poll() is None


_jobs: dict[str, Job] = {}
_lock = threading.RLock()
_reaper: threading.Thread | None = None


# ========== 对外 ==========

def start(owner: str, state_dict: dict, user_text: str,
          smoke_options: dict | None = None, label: str = "") -> Job:
    """为 owner 起一个生成任务。同一 owner 已有任务时先杀掉旧的。"""
    with _lock:
        existing = _jobs.get(owner)
        if existing is not None:
            _kill(existing, "同一会话发起了新任务")
            _jobs.pop(owner, None)
            _cleanup(existing)   # 漏了这行会在 /tmp 里堆 codegen_* 目录

        running = sum(1 for j in _jobs.values() if j.is_running())
        if running >= MAX_CONCURRENT:
            raise JobError(
                f"同时最多 {MAX_CONCURRENT} 个生成任务在跑（当前 {running} 个），"
                "等前面的跑完再试"
            )

        job_dir = Path(tempfile.mkdtemp(prefix="codegen_"))
        (job_dir / INPUT_NAME).write_text(
            json.dumps({"state": state_dict, "user_text": user_text,
                        "smoke_options": smoke_options or {}}, ensure_ascii=False),
            encoding="utf-8",
        )

        proc = subprocess.Popen(
            [sys.executable, "-m", "strategy.agent_gen.runner", str(job_dir)],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # 独立进程组：杀的时候整组一起走，claude CLI 和 smoke 的孙进程不会漏
            start_new_session=True,
        )
        now = time.monotonic()
        job = Job(owner=owner, job_dir=job_dir, proc=proc,
                  started_at=now, last_seen=now, label=label)
        _jobs[owner] = job
        _ensure_reaper()
        logger.info("生成任务已启动 owner=%s pid=%s", owner, proc.pid)
        return job


def get(owner: str) -> Job | None:
    with _lock:
        return _jobs.get(owner)


def poll(owner: str) -> dict | None:
    """轮询：顺便刷新心跳。返回 None 表示还在跑，否则返回 runner 的 output。

    进程已退出却没有 output.json → 判为被杀或崩溃，返回一个失败 payload。
    """
    with _lock:
        job = _jobs.get(owner)
        if job is None:
            return None
        job.last_seen = time.monotonic()

        out = job.job_dir / OUTPUT_NAME
        if out.exists():
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                return {"ok": False, "error": f"结果文件损坏：{e}", "detail": ""}

        if not job.is_running():
            stderr = ""
            try:
                if job.proc.stderr is not None:
                    stderr = job.proc.stderr.read().decode(errors="replace")[-3000:]
            except Exception:
                pass
            if job._killed:
                return {"ok": False, "error": "已取消", "detail": stderr, "cancelled": True}
            return {"ok": False,
                    "error": f"生成进程异常退出（exitcode={job.proc.returncode}）",
                    "detail": stderr}

        if job.elapsed > HARD_TIMEOUT:
            _kill(job, f"超过硬超时 {HARD_TIMEOUT:.0f}s")
            return {"ok": False, "error": f"生成超时（>{HARD_TIMEOUT:.0f}s），已终止",
                    "detail": ""}
        return None


def finish(owner: str) -> None:
    """结果已被取走，清掉任务与临时目录"""
    with _lock:
        job = _jobs.pop(owner, None)
    if job is not None:
        _cleanup(job)


def cancel(owner: str, reason: str = "用户取消") -> bool:
    """用户点「停止」。返回是否真的杀掉了一个在跑的任务。"""
    with _lock:
        job = _jobs.get(owner)
        if job is None:
            return False
        was_running = job.is_running()
        _kill(job, reason)
        _jobs.pop(owner, None)
    _cleanup(job)
    return was_running


def sweep() -> int:
    """杀掉心跳超时的任务（主人刷新/关了页面）。返回清理数量。"""
    now = time.monotonic()
    victims = []
    with _lock:
        for owner, job in list(_jobs.items()):
            idle = now - job.last_seen
            if idle > STALE_SECONDS:
                _kill(job, f"{idle:.0f}s 无人轮询，判定主人已离开")
                _jobs.pop(owner, None)
                victims.append(job)
            elif job.elapsed > HARD_TIMEOUT:
                _kill(job, f"超过硬超时 {HARD_TIMEOUT:.0f}s")
                _jobs.pop(owner, None)
                victims.append(job)
    for job in victims:
        _cleanup(job)
    return len(victims)


def active_count() -> int:
    with _lock:
        return sum(1 for j in _jobs.values() if j.is_running())


# ========== 内部 ==========

def _kill(job: Job, reason: str) -> None:
    if not job.is_running():
        return
    job._killed = True
    logger.info("终止生成任务 owner=%s pid=%s：%s", job.owner, job.proc.pid, reason)
    try:
        # 杀整个进程组，而不是只杀 python——claude CLI 是它的子进程
        os.killpg(os.getpgid(job.proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        job.proc.terminate()
    try:
        job.proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(job.proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        job.proc.kill()
    try:
        job.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("生成任务 pid=%s SIGKILL 后仍未退出", job.proc.pid)


def _cleanup(job: Job) -> None:
    try:
        if job.proc.stderr is not None:
            job.proc.stderr.close()
    except Exception:
        pass
    shutil.rmtree(job.job_dir, ignore_errors=True)


def _ensure_reaper() -> None:
    """懒启动后台回收线程。

    必须有它：只靠「有人操作页面时清扫」的话，用户刷新完就去干别的了，
    没有任何脚本在跑，孤儿任务会一直烧到自己结束。
    """
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return
    _reaper = threading.Thread(target=_reap_loop, name="codegen-reaper", daemon=True)
    _reaper.start()


def _reap_loop() -> None:
    while True:
        time.sleep(REAP_INTERVAL)
        try:
            sweep()
        except Exception:  # 回收线程绝不能死
            logger.exception("生成任务回收线程出错")
