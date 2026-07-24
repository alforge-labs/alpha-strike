# futu-api × OpenD 昇格 Runbook と互換台帳

`futu-api`（PyPI の moomoo/Futu Python SDK）を上げる手順と、**動作確認済みの `futu-api` × OpenD バイナリの組（既知良ペア）** の台帳。新バージョンの**検知**は Dependabot に任せ、**反映**は OpenD とロックステップで行う。

## なぜ futu-api 単独で上げてはいけないか

- `futu-api`（Python SDK）と **OpenD ゲートウェイ・バイナリ**は同系列でリリースされ、**バージョンを揃える前提**で作られている。大きくズレると proto フィールド不整合や注文ルーティング失敗を起こしうる（[deployment.md](deployment.md) のリスク前提）。
- **CI は OpenD を持たない**（テストは FakeProvider）。したがって **Dependabot PR が CI 緑でも OpenD 互換は未検証**。緑＝マージ可、ではない。
- VM の導入は `uv pip install -U "alpha-strike==X"` で行うが、これは **`alpha-strike` は pin しても transitive の `futu-api` は最新へ解決**してしまう。明示 pin しないと VM だけ futu-api が勝手に先行ドリフトする（現に台帳の初期エントリは OpenD 10.5.6508 に対し futu-api が 10.06.6608 と先行している）。

## 互換台帳（既知良ペア）

新バージョンを本番反映して SIMULATE スモークが通るたびに、**先頭に1行追記**する。VM への導入時は最新行の `futu-api` を明示 pin する。

| 確認日 | alpha-strike | futu-api | OpenD バイナリ | 検証 |
|--------|--------------|----------|----------------|------|
| 2026-07-24 | 1.0.2 | 10.9.6908 | 10.9.6918 (`moomoo_OpenD_10.9.6918_Ubuntu18.04`) | OpenD 10.7.6718→10.9.6918 と futu-api 10.7.6708→10.9.6908 を同メンテナンスウィンドウでロックステップ昇格（#119 起因）。旧 10.7 は `/opt/moomoo_OpenD_10.7` に残置＝ロールバック可。切替後 `get_global_state` で `trd_logined=True` / `qot_logined=True` / `server_ver=1009` / `program_status=READY` を確認。`/health/ready` が moomoo=ok / oanda=ok。VM localhost の webhook スモークで誤 passphrase→401・実 passphrase→200（US 休場中のため carry-over queued 動作、テスト注文は abandon 済み）。注記: Cloudflare WAF がメンテナ Mac IP の `/webhook` POST を 403 ブロックするため `go_live_smoke.sh`（Mac 起点）は使えず、VM 内 localhost:8080 で検証した |
| 2026-06-16 | 0.8.1 | 10.7.6708 | 10.7.6718 (`moomoo_OpenD_10.7.6718_Ubuntu18.04`) | carry-over lookback を土日除外の実効時間化し週末跨ぎの取りこぼしを解消 (#100)。SDK / OpenD は不変（futu-api / OpenD 10.7 ペア維持）。VM デプロイ後 `systemctl is-active`=active・`/status` 200 を確認。直後に GLD 83 株が target 通り約定しヘッジ回復（TQQQ 175 + GLD 83） |
| 2026-06-10 | 0.8.0 | 10.7.6708 | 10.7.6718 (`moomoo_OpenD_10.7.6718_Ubuntu18.04`) | carry-over エミュレーション (#89) 追加。同一 futu-api / OpenD 10.7 ペアで動作（SDK/OpenD は不変）。市場オープン判定 `OpenQuoteContext.get_market_state` を VM 実機で確認（US=AFTERNOON=open）。デプロイ後に carry-over loop 起動ログ + `go_live_smoke` で検証 |
| 2026-06-09 | 0.7.2 | 10.7.6708 | 10.7.6718 (`moomoo_OpenD_10.7.6718_Ubuntu18.04`) | oracle-strike で 10.7 へ昇格（futu-api / OpenD を 10.7 で揃えた）。**moomoo 10.7 は paper(SIMULATE) の GTC を拒否**するため alpha-strike は SIMULATE→DAY 強制・REAL は GTC 維持 (#88)。VM の futu-api 10.7 プローブで GTC 拒否 / DAY 受理を確認、`go_live_smoke` で 200 確認 |
| 2026-06-01 | 0.6.0 | 10.06.6608 | 10.5.6508 (`moomoo_OpenD_10.5.6508_Ubuntu18.04`) | oracle-strike で `active` / `/health` OK。SIMULATE は #49 系運用で稼働中 |

> 初期エントリは futu-api が OpenD より先行した状態。問題は出ていないが、**次回昇格時に両者を揃える**のが望ましい。

### 現在版の確認コマンド

```bash
ssh oracle-strike
# futu-api（venv にインストールされた版）
/opt/alpha-strike/.venv/bin/python -c "import futu; print(futu.__version__)"
# OpenD バイナリ版（パスにバージョンが入る）
systemctl cat moomoo-opend | grep -E "ExecStart|WorkingDirectory"
```

## 検知（Dependabot）

- `.github/dependabot.yml` の **`uv` エコシステム**が `uv.lock` を直接見て、`futu-api` の確定バージョンが上がると **週次（月曜）で PR** を出す（`futu-sdk` グループ単独）。`cooldown: 7日` で公開直後は寝かせる。
- この PR は **auto-merge 禁止**。下記 Runbook を完了してからマージする。
- 「公開した瞬間に知りたい」場合は PyPI JSON API（`https://pypi.org/pypi/futu-api/json`）を日次ポーリングして ntfy 通知する軽量ワークフローを足してもよい（現状は週次 Dependabot で十分なため未導入）。

## 昇格 Runbook（OpenD ロックステップ）

> US 市場の**開場中は webhook 再起動の取りこぼしリスク**があるため、可能なら**休場時間帯**に実施する。

1. **Dependabot PR を確認**（`futu-sdk` グループ）。futu/moomoo の changelog で破壊的変更・最低 OpenD 要件を確認する。
2. **対応する OpenD バイナリを入手**：同系列バージョンの OpenD（Ubuntu 版）を moomoo/Futu の配布元から取得し、VM の `/opt/moomoo_OpenD/` 配下に**新バージョンを並行配置**（旧バージョンは残す＝ロールバック用）。
3. **VM をロックステップ更新**（同一メンテナンスウィンドウ内で OpenD と futu-api を揃える）：
   ```bash
   ssh oracle-strike
   # (a) OpenD: moomoo-opend unit の ExecStart/WorkingDirectory を新パスへ向け替えて再起動
   sudo systemctl edit --full moomoo-opend   # ExecStart/WorkingDirectory を新版ディレクトリに
   sudo systemctl daemon-reload && sudo systemctl restart moomoo-opend
   # (b) futu-api: alpha-strike 新版とともに台帳の新ペアを明示 pin
   ~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python -U \
     "alpha-strike==<新版>" "futu-api==<新 futu-api>"
   sudo systemctl restart alpha-strike
   ```
4. **SIMULATE スモーク**（OpenD 互換を実証する唯一の手段）：
   ```bash
   curl -s localhost:8080/health/ready      # OpenD/OANDA 疎通 ready
   # TradingView→alpha-strike→moomoo SIMULATE を一気通貫で検証
   bash scripts/go_live_smoke.sh            # 引数は scripts 内のヘルプ参照
   ```
   `/status` が口座・建玉を 200 で返し、SIMULATE 発注が通ることを確認する。
5. **マージ＆リリース**：Dependabot PR をマージ → `bump-my-version` でリリース → `release.yml` が PyPI publish（[deployment.md](deployment.md)）。
6. **台帳追記**：本ファイルの互換台帳に新しい `(alpha-strike, futu-api, OpenD)` ペアを1行追記してコミットする。

## ロールバック

SIMULATE スモークが失敗したら、**OpenD と futu-api を両方とも直前の既知良ペアへ戻す**：

```bash
ssh oracle-strike
# OpenD を旧パスへ戻す
sudo systemctl edit --full moomoo-opend   # ExecStart/WorkingDirectory を旧版へ
sudo systemctl daemon-reload && sudo systemctl restart moomoo-opend
# futu-api（と alpha-strike）を旧ペアへ pin
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python \
  "alpha-strike==<旧版>" "futu-api==<旧 futu-api>"
sudo systemctl restart alpha-strike
curl -s localhost:8080/health/ready
```

> 旧 OpenD ディレクトリを消さずに残しておけば、向け替えだけで即ロールバックできる。
