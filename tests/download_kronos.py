"""一次性把 Kronos 默认权重下载到 HF 缓存（用 LFS 直链，绕开不稳定的 xet CDN）

挂卷已经把 /root/.cache/huggingface 映射到宿主机 data/hf_cache/，
所以下完一次以后容器重启/重建镜像都不会再下。
"""
import os
import sys
import time

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

from huggingface_hub import snapshot_download

REPOS = ["NeoQuasar/Kronos-Tokenizer-base", "NeoQuasar/Kronos-small"]

for repo in REPOS:
    print(f"\n=== downloading {repo} ===", flush=True)
    t0 = time.time()
    try:
        p = snapshot_download(repo, cache_dir="/root/.cache/huggingface/hub",
                              max_workers=4)
        print(f"OK in {time.time() - t0:.1f}s -> {p}", flush=True)
    except Exception as e:
        print(f"FAILED ({type(e).__name__}): {e}", flush=True)
        sys.exit(1)

print("\nALL DONE", flush=True)
