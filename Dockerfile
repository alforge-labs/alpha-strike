FROM python:3.14-slim

WORKDIR /app

# uv をインストール
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ヘルスチェック用 curl をインストール
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# 依存パッケージを先にコピーしてレイヤーキャッシュを活用
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# アプリケーションコードをコピー
COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uv", "run", "uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
