FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc curl && rm -rf /var/lib/apt/lists/*

# 「AI 生成策略」(#30) 在这里**什么都不装**，两层原因：
# 1) 不需要 Node + @anthropic-ai/claude-code——claude-agent-sdk 的 linux-x64 wheel
#    自带 _bundled/claude（262M 静态 ELF，ldd 只有 libc/libm/libpthread/librt），
#    而 _find_cli() 先查 bundled 再查 PATH，npm 装的那个根本轮不上。
# 2) 各家模型 SDK 本身也不进镜像——依赖跟着 provider 走（CodegenProvider.REQUIRES），
#    要用哪家就 docker exec quant-app pip install 哪家。
# 详见 tests/test_codegen_deps.py 与 #30 的评论。

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501
