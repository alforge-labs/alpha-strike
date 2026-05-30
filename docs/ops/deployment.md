# alpha-strike デプロイ手順（新バージョンの反映）

新しいバージョンを本番 VM（oracle-strike）に反映するための runbook。**初回プロビジョニング**は [vm-provisioning.md](vm-provisioning.md)、**ペーパー go-live チェック**は [paper-trading-go-live.md](paper-trading-go-live.md) を参照。

## デプロイ方式の前提

alpha-strike は **PyPI 配布**。本番は PyPI からインストールした版を systemd で起動する（ソースの git checkout からは動かない）。

| 項目 | 値 |
|------|-----|
| 配布 | PyPI パッケージ `alpha-strike`（`v*` タグ push で `.github/workflows/release.yml` が自動 publish） |
| 稼働 venv | `/opt/alpha-strike/.venv`（**uv 管理**・所有 `ubuntu`） |
| 起動 | systemd `alpha-strike`（`ExecStart=/opt/alpha-strike/.venv/bin/alpha-strike --host 0.0.0.0 --port 8080`） |
| 環境変数 | systemd EnvironmentFile = `/etc/alpha-strike/.env`（`root:root 600`・編集に sudo） |
| データ | `/opt/alpha-strike/data`（イベント JSONL 等） |

> 実際のパス・権限・ポートは環境で変わりうる。コマンド実行前に `systemctl show alpha-strike -p ExecStart -p EnvironmentFiles --value` で確認すること。

## 1. リリース（メンテナー / 開発機）

```bash
cd alpha-strike
# 作業ツリーがクリーン & テスト緑であること
uv run pytest -q && uv run ruff check src/ tests/

# バージョンバンプ（新機能=minor / バグ修正=patch）
uv run bump-my-version bump minor      # 例: 0.4.1 -> 0.5.0（pyproject.toml + タグ対象を更新 + commit）

# タグを push すると release.yml が PyPI へ publish する
git push && git push --tags
```

`v*` タグの push をトリガーに GitHub Actions（`release.yml`）が lint → test → wheel/sdist ビルド → **PyPI publish** を実行する。Actions の完了と [PyPI のバージョン](https://pypi.org/project/alpha-strike/) 反映を確認してから VM 更新に進む。

## 2. VM へ反映（oracle-strike）

> US 市場の**開場中は webhook 再起動で取りこぼしリスク**があるため、可能なら**市場休場時間帯**に実施する。

```bash
ssh oracle-strike

# (a) venv のパッケージを新バージョンへ更新（uv は ~/.local/bin/uv、venv は ubuntu 所有なので sudo 不要）
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python -U "alpha-strike==0.5.0"

# (b) 必要な環境変数を /etc/alpha-strike/.env に追記（root:root 600 のため sudo）
#     既存値は上書きしないよう sudo nano 等で編集する
sudo nano /etc/alpha-strike/.env
#   例（新規キーのみ追記）:
#   STATUS_API_TOKEN=<openssl rand -hex 32 で生成>
#   NTFY_TOPIC=<通知を受け取るトピック。不要なら空のまま>

# (c) サービス再起動（system サービスのため sudo）
sudo systemctl restart alpha-strike

# (d) スモークテスト
sudo systemctl is-active alpha-strike
curl -s localhost:8080/health            # {"status":"ok"}
curl -s localhost:8080/health/ready      # {"status":"ready", ...}（OpenD/OANDA 疎通）
# status API（STATUS_API_TOKEN 設定時）
curl -s -H "Authorization: Bearer $STATUS_API_TOKEN" localhost:8080/status | head
```

## 3. status API のネットワーク保護（Cloudflare Access）

`/status*` は口座残高・建玉という機微情報を返すため、コード層の Bearer トークンに加えて
**Cloudflare Access（ゼロトラスト）** でも保護する。手順は [status-api.md](../status-api.md) の「認証（二重防御）」節を参照。
要点: `/status*` を Access アプリ対象にし、`/webhook` は対象外（WAF IP 許可のまま分離）。

## 4. ロールバック

問題があれば直前バージョンを pin して再起動する。

```bash
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python "alpha-strike==0.4.1"
sudo systemctl restart alpha-strike
curl -s localhost:8080/health
```

> PyPI は同一バージョンの再 publish ができないため、リリース後に不具合が見つかった場合は
> パッチバージョン（例: 0.5.1）を切り直す。

## チェックリスト

- [ ] `uv run pytest` 緑 & `ruff` クリーン
- [ ] `bump-my-version bump` でバージョン更新 + タグ
- [ ] タグ push → Actions の PyPI publish 成功 / PyPI に新版反映
- [ ] VM venv を新版へ更新（`uv pip install -U`）
- [ ] `/etc/alpha-strike/.env` に必要な環境変数を追記
- [ ] `systemctl restart` + `/health` `/health/ready` スモーク
- [ ] （status API 公開時）Cloudflare Access で `/status*` 保護
