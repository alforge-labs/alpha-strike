# alpha-strike デプロイ手順（新バージョンの反映）

新しいバージョンを本番 VM（oracle-strike）に反映するための runbook。**初回プロビジョニング**は [vm-provisioning.md](vm-provisioning.md)、**ペーパー go-live チェック**は [paper-trading-go-live.md](paper-trading-go-live.md)、**futu-api の昇格（OpenD バイナリと結合）と互換台帳**は [futu-opend-upgrade.md](futu-opend-upgrade.md) を参照。

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
#     futu-api は transitive 依存だが OpenD バイナリと結合するため、互換台帳の既知良ペアを
#     明示 pin する（無指定だと最新へ勝手に解決されドリフトする）。版は futu-opend-upgrade.md 参照。
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python -U \
  "alpha-strike==0.6.0" "futu-api==10.06.6608"

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

## 2.5 signal watchdog の systemd timer（v1.3.0+）

シグナル途絶監視は **alpha-strike 本体とは別プロセス**で動く。本体のイベントループが OpenD の
同期呼び出しで凍結しても、プロセスが落ちても、監視だけは独立して動き続ける
（2026-08-23 の障害はこれが無くて 5 営業日気づけなかった）。

**unit はリポジトリ管理外**で、既存の `alpha-strike.service` と同じく VM 上にのみ存在する。

```bash
sudo tee /etc/systemd/system/alpha-strike-watchdog.service >/dev/null <<'EOF'
[Unit]
Description=alpha-strike signal outage watchdog (oneshot)

[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/alpha-strike
EnvironmentFile=/etc/alpha-strike/.env
ExecStart=/opt/alpha-strike/.venv/bin/alpha-strike-watchdog
StandardOutput=journal
StandardError=journal
EOF

sudo tee /etc/systemd/system/alpha-strike-watchdog.timer >/dev/null <<'EOF'
[Unit]
Description=Run alpha-strike signal outage watchdog hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now alpha-strike-watchdog.timer
```

`Persistent=true` にすると、VM 停止中に飛ばした実行を起動後に 1 回補完する。

### 本体 unit の変更（v1.3.0+）

`/etc/systemd/system/alpha-strike.service` を `sudo systemctl edit --full alpha-strike` で編集する。

| 項目 | 変更前 | 変更後 | 理由 |
|---|---|---|---|
| `Requires=moomoo-opend.service` | あり | **`Wants=moomoo-opend.service`** | moomoo-opend のクラッシュループに巻き込まれるのを止める。OpenD 障害中も webhook を受けてシグナルを記録する |
| `Restart=on-failure` | | **`Restart=always`** | OpenD 接続失敗で exit 0 終了すると on-failure では再起動されない |

`After=moomoo-opend.service` は維持する（起動順序の指定は引き続き必要）。適用には

```bash
sudo systemctl restart alpha-strike
```

を忘れないこと（`systemctl edit --full` は unit ファイルを書き換えるだけで、実行中のプロセスには反映されない）。

### systemd が無い環境（docker-compose 等）

`docker-compose.yml` / `Dockerfile` で運用している場合は systemd timer を使えないため、
host 側の cron から毎時 1 回コンテナ内で `alpha-strike-watchdog` を実行する
（`docker-compose.yml` のサービス名は `webhook-server`）。

```cron
0 * * * * cd /opt/alpha-strike && docker compose exec -T webhook-server alpha-strike-watchdog
```

### 確認

```bash
systemctl list-timers alpha-strike-watchdog.timer
sudo systemctl start alpha-strike-watchdog.service   # 手動 1 回実行
journalctl -u alpha-strike-watchdog -n 20
```

`journalctl -u alpha-strike-watchdog -n 20` に **2 行**出ること。

```
signal watchdog: 最終受信=... 実効 ...h / しきい値 ...h
order watchdog: signal_id=... 判定 N 件（正常 M / 異常 K / 保留 P）
```

**起動ログだけでは「動いている」の確認にならない**ので、この行を見ること。

**確認基準は 2 つです。**

1. `最終受信=` が `None` でないこと（`None` は `LIVE_EVENTS_PATH` の食い違い）
2. `order watchdog` の行で **判定件数が 0 でないこと**。0 件なら `signal_received` を
   読めていないので、同じく `LIVE_EVENTS_PATH` を疑う

`最終受信=None 実効 0.0h` と出た場合は、この行自体は出ているため一見機能しているように
見えるが、実際は監視できていない。watchdog プロセスが本体プロセスと異なる `.env`
（異なる `LIVE_EVENTS_PATH`）を読んでいる食い違いを疑うこと（`alpha-strike-watchdog` の
`WorkingDirectory` が `/opt/alpha-strike` になっているか、`EnvironmentFile` が本体と同じ
`/etc/alpha-strike/.env` を指しているかを確認する）。

**新しい systemd unit は増えません。** 既存の `alpha-strike-watchdog.service` が
2 本のチェックを実行するようになるだけです。

### futu-api のバージョン確認（v1.4.0 で追加）

トレード系コンテキストの有界化は futu SDK の内部実装（`_init_connect_sync` /
`_wait_reconnect`）に依存します。SDK 更新でこれが変わると**最初の発注で RuntimeError**
になります（黙ってハングするよりは良いですが、発注 1 件を落とします）。

```bash
/opt/alpha-strike/.venv/bin/python -c "import futu; print(futu.__version__)"
```

動作確認済みのバージョンは **10.09.6908**（`uv.lock` 表記では `10.9.6908`）です。
`futu.__version__` はゼロ埋めの `10.09.6908` を返し、`uv.lock` と `uv pip show` は
`10.9.6908` と表記しますが、同じ版です。

これと違う場合は、デプロイ後に `/status` が HTTP 200 **かつ `account` の値（`total_assets` 等）が `None`（JSON では `null`）でない**
ことを必ず確認してください（OpenD に接続できないと `/status` は 502 `Connect timeout` を返します）。
SDK 不一致の確認は、口座と建玉の中身まで見る次節の「SIMULATE スモークテスト」で行ってください
（`order watchdog` のログ行は futu を一切使わないので、SDK 不一致の確認にはなりません）。

### SIMULATE スモークテスト（v1.4.0 で追加・必須）

**健全な OpenD に対する発注の往復は CI でカバーできていません。** 有界化はサブクラスで
SDK の非同期接続分岐を再現しているため、「OpenD が死んでいるとき有界時間で戻る」ことは
自動テストで固定していますが、「OpenD が生きているとき今までどおり発注できる」ことは
偽 OpenD に対する手動確認のみです（偽 OpenD は InitConnect にしか応答しません）。

したがって**デプロイ後、市場オープン中に次の 2 つを必ず実地で確認してください**。

1. `/status` が HTTP 200 で口座と建玉を返すこと（参照経路）
2. **実際に 1 件発注が通ること**（発注経路）。次のシグナルが `order_recorded` →
   `order_reconciled` まで到達するのを確認する。carry-over 経由なら寄付の直後に出る

これを飛ばすと、**参照はできるのに発注だけ落ちる**状態に気づけません。

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

**v1.3.0 以降からロールバックする場合**は、あわせて timer も止めること。旧版には
`alpha-strike-watchdog` コマンドが無いため、動かしたままにすると毎時 failed になる。

```bash
sudo systemctl disable --now alpha-strike-watchdog.timer
```

## チェックリスト

- [ ] `uv run pytest` 緑 & `ruff` クリーン
- [ ] `bump-my-version bump` でバージョン更新 + タグ
- [ ] タグ push → Actions の PyPI publish 成功 / PyPI に新版反映
- [ ] VM venv を新版へ更新（`uv pip install -U`）
- [ ] `/etc/alpha-strike/.env` に必要な環境変数を追記
- [ ] `systemctl restart` + `/health` `/health/ready` スモーク
- [ ] （status API 公開時）Cloudflare Access で `/status*` 保護
- [ ] （v1.3.0+ 新規導入時）§2.5 の新規 timer を配置し `systemctl enable --now alpha-strike-watchdog.timer`
- [ ] （v1.3.0+ 新規導入時）本体 unit を `Wants=` / `Restart=always` へ変更し `systemctl restart alpha-strike`
- [ ] （v1.3.0+ 新規導入時）`alpha-strike-watchdog` を手動 1 回実行し、`最終受信=` に実日時（`None` でない）が出ることを確認
