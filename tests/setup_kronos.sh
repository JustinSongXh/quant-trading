#!/bin/bash
# 在 Docker 容器中安装 Kronos 依赖并克隆模型代码
# 用法: docker exec quant-app bash /app/tests/setup_kronos.sh

set -e

echo "=== 安装 PyTorch + Kronos 依赖 ==="
pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
pip install --no-cache-dir einops huggingface_hub safetensors

echo "=== 克隆 Kronos 模型代码 ==="
if [ ! -d /app/tests/kronos_repo ]; then
    git clone --depth 1 https://github.com/shiyu-coder/Kronos.git /app/tests/kronos_repo
else
    echo "Kronos 代码已存在，跳过克隆"
fi

echo "=== 安装完成 ==="
