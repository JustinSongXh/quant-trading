"""在独立进程里跑一轮 orchestrator.advance()（#30）

为什么必须独立进程：Streamlit 的「停止脚本」是协作式的——`request_stop()` 只在脚本
碰到 interrupt point（`st.*` 调用处）时生效。而一轮 advance() 整个阻塞在
`asyncio.run()` 里，一个 `st.*` 都不调，所以用户刷新页面后旧脚本会一直跑到模型返回：
token 全花完，结果又因为 st.session_state 按浏览器会话隔离而被丢掉。钱花了东西没了。

搬到独立进程后，取消就是杀进程组（`claude` CLI 与 smoke 的孙进程一起走）。

不能只搬第一次模型调用：_run_gates 内部还有最多 MAX_FIX 次回灌调用，一样长、
一样要能杀。所以搬的是整个 advance()。

用法（jobs.py 负责调）：

    python -m strategy.agent_gen.runner <job_dir>

`<job_dir>/input.json`  {"state": {...}, "user_text": "...", "smoke_options": {...}}
`<job_dir>/output.json` {"ok": true, "state": {...}} | {"ok": false, "error": "...", "detail": "..."}
"""

import json
import logging
import os
import sys
import traceback
from pathlib import Path

INPUT_NAME = "input.json"
OUTPUT_NAME = "output.json"


def _write_output(job_dir: Path, payload: dict) -> None:
    """先写临时文件再 rename：轮询方不会读到写了一半的 JSON"""
    tmp = job_dir / (OUTPUT_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(job_dir / OUTPUT_NAME)


def run_job(job_dir: Path) -> dict:
    from strategy.agent_gen import orchestrator, registry

    spec = json.loads((job_dir / INPUT_NAME).read_text(encoding="utf-8"))
    state = orchestrator.GenState.from_dict(spec["state"])
    provider = registry.get_provider(state.provider_key, **state.provider_params)
    orchestrator.advance(provider, state, spec["user_text"],
                         smoke_options=spec.get("smoke_options") or {})
    return {"ok": True, "state": state.to_dict()}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if len(argv) != 1:
        print("用法：python -m strategy.agent_gen.runner <job_dir>", file=sys.stderr)
        return 2

    job_dir = Path(argv[0])
    try:
        payload = run_job(job_dir)
    except Exception as e:
        payload = {"ok": False, "error": f"{type(e).__name__}: {e}",
                   "detail": traceback.format_exc()}
    # 始终返回 0：失败信息在 output.json 里，用 exitcode 表达失败会和「进程被杀」混淆
    _write_output(job_dir, payload)
    return 0


if __name__ == "__main__":
    # 子进程自己也许会再起孙进程（smoke），保持在同一进程组里方便整组杀
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main(sys.argv[1:]))
