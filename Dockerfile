FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc curl && rm -rf /var/lib/apt/lists/*

# Node 20 + Claude Code CLI（claude-agent-sdk 的底层依赖，用于「AI 生成策略」）
# 走 npmmirror：本部署环境到 nodejs.org / registry.npmjs.org 会静默卡死
ENV NODE_VERSION=20.18.1
RUN curl -fsSL --connect-timeout 15 --max-time 300 -o /tmp/node.tar.gz \
      "https://cdn.npmmirror.com/binaries/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz" \
    && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.gz \
    && npm config set registry https://registry.npmmirror.com \
    && npm i -g @anthropic-ai/claude-code \
    && npm cache clean --force

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt fastapi uvicorn

COPY . .

EXPOSE 8501 8000
