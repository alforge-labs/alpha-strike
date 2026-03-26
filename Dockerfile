FROM python:3.14-slim

WORKDIR /app

# uv をインストール
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ヘルスチェック用 curl をインストール
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# 非 root ユーザーを作成（セキュリティベストプラクティス）
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/sh --create-home appuser

# 依存パッケージを先にコピーしてレイヤーキャッシュを活用
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# アプリケーションコードをコピー
COPY . .

# ファイルの所有権を appuser に変更
RUN chown -R appuser:appgroup /app

# 非 root ユーザーに切り替え
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["uv", "run", "uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
