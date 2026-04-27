.PHONY: run dev test run-dotenv

# 1Password 経由（開発者用）: op CLI が必要
run:
	op run --env-file=.env.op -- uv run python main.py

dev:
	op run --env-file=.env.op -- uv run uvicorn webhook_server:app --reload --host 0.0.0.0 --port 8080

test:
	op run --env-file=.env.op -- uv run pytest

# .env ファイル経由（エンドユーザー用）
run-dotenv:
	uv run python main.py
