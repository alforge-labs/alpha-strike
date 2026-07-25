# Changelog

alpha-strike の全バージョン変更履歴です。


## [1.0.4] - 2026-07-25


### その他

- バージョン 1.0.4 にバンプ



### ドキュメント

- CHANGELOG を v1.0.3 に更新



### バグ修正

- idempotency を複合キー化し同一バーの銘柄別シグナル欠落を修正 (#127)



## [1.0.3] - 2026-07-25


### その他

- バージョン 1.0.3 にバンプ



### ドキュメント

- CHANGELOG を v1.0.2 に更新



### バグ修正

- ntfy 通知ヘッダを RFC 2047 でエンコードし絵文字・日本語タイトルの送信失敗を修正 (#125)



## [1.0.2] - 2026-07-24


### その他

- **deps**: bump fastapi in the fastapi group across 1 directory (#118)


- **deps-dev**: bump ruff in the lint group across 1 directory (#120)


- **deps**: futu-api を 10.9.6908 へ昇格 + OpenD 10.9 ロックステップ台帳追記 (#124)


- バージョン 1.0.2 にバンプ



### ドキュメント

- CHANGELOG を v1.0.1 に更新



## [1.0.1] - 2026-07-23


### その他

- バージョン 1.0.1 にバンプ



### ドキュメント

- CHANGELOG を v1.0.0 に更新



### バグ修正

- release.sh に uv.lock のバージョン追従処理を追加 (#121) (#122)



## [1.0.0] - 2026-07-21


### その他

- uv.lock の project version を 0.9.0 に同期（v0.9.0 リリース時の取りこぼし）


- バージョン 1.0.0 にバンプ



### ドキュメント

- CHANGELOG を v0.9.0 に更新



## [0.9.0] - 2026-07-19


### CI/CD

- **deps**: bump actions/checkout from 6 to 7 (#104)



### その他

- **deps**: bump starlette from 1.2.1 to 1.3.1 (#101)


- **deps-dev**: bump ruff from 0.15.15 to 0.15.16 in the lint group (#99)


- **deps**: pydantic-settings を 2.14.2 へ bump（Dependabot 警告 #7 対応） (#103)


- **deps**: bump slowapi from 0.1.9 to 0.1.10 (#108)


- **deps-dev**: bump bump-my-version from 1.3.0 to 1.4.1 (#111)


- **deps**: bump fastapi in the fastapi group across 1 directory (#105)


- **deps-dev**: bump pytest in the test group across 1 directory (#106)


- **deps-dev**: bump ruff in the lint group across 1 directory (#107)


- **deps**: bump fastapi from 0.138.0 to 0.138.1 in the fastapi group (#114)


- **deps-dev**: bump ruff from 0.15.18 to 0.15.20 in the lint group (#116)


- バージョン 0.9.0 にバンプ



### ドキュメント

- CHANGELOG を v0.8.1 に更新


- **ops**: 互換台帳に alpha-strike 0.8.1 / futu-api 10.7.6708 を追記


- **readme**: AlphaForge への送客 CTA を先頭に追加 (#109)


- pre-release コマンドの git-cliff 導入手段の誤記を修正 (#112)


- X (@Alforge_bot) フォローバッジと project.urls を追加 (#113)



### 新機能

- **cli**: 起動時に AlphaForge への送客 CTA を表示（C3） (#110)


- alpha-visualizer でのライブ実績可視化導線を README と起動バナーに追加 (#117)



## [0.8.1] - 2026-06-16


### その他

- **deps-dev**: bump pytest-asyncio in the test group (#86)


- **deps-dev**: bump ruff from 0.15.14 to 0.15.15 in the lint group (#87)


- bumpversion の current_version を実態(v0.8.0)に同期


- バージョン 0.8.1 にバンプ



### ドキュメント

- CLAUDE.md を v0.8 の実態に追随 (#98)



### バグ修正

- **security**: CodeQL py/log-injection #36 を修正（market_state の ticker ログをサニタイズ） (#97)


- **carryover**: lookback を土日除外の実効時間で計測し週末跨ぎの取りこぼしを解消 (#100)



## [0.8.0] - 2026-06-10


### その他

- **release**: v0.8.0 (#96)



### バグ修正

- 依存脆弱性4件の解消 + go_live_smoke の VM パス/SDK 修正 (#93)



### 新機能

- **smoke**: go_live_smoke の step 5 を self-clean 化（約定済みテスト発注を相殺） (#94)


- **carryover**: クローズ後 SIMULATE シグナルを翌オープンで自動約定 (#89) (#95)



## [0.7.2] - 2026-06-09


### その他

- **deps**: starlette を 1.0.0 から 1.2.1 へ更新 (#85)



### バグ修正

- futu-api 10.7 昇格（protobuf 解消）+ SIMULATE→DAY フォールバック (v0.7.2) (#88)



## [0.7.1] - 2026-06-06


### その他

- **release**: v0.7.1 (#84)



### バグ修正

- **events**: GTC 注文の翌営業日約定を遅延再照合でイベントログに反映 (#79) (#83)



## [0.7.0] - 2026-06-06


### その他

- **deps**: bump futu-api in the futu-sdk group (#69)


- **release**: v0.7.0 (#82)



### 新機能

- target_qty による closed-loop 数量解決を追加 (#80) (#81)



## [0.6.2] - 2026-06-06


### その他

- バージョン 0.6.2 にバンプ (#78)



### バグ修正

- **moomoo**: 米国市場の成行注文を GTC で発注しクローズ後注文の約定ゼロを解消 (#77)



## [0.6.1] - 2026-06-03


### その他

- **deps**: bump fastapi from 0.135.2 to 0.136.3 in the fastapi group (#68)


- **deps-dev**: bump pytest from 9.0.2 to 9.0.3 in the test group (#70)


- **deps-dev**: bump ruff from 0.15.7 to 0.15.14 in the lint group (#71)


- **deps**: bump requests from 2.32.5 to 2.34.2 (#72)


- バージョン 0.6.1 にバンプ (#75)



### ドキュメント

- **readme**: 環境変数表を実装に同期（ja/en） (#73)



### バグ修正

- **deps**: 未使用の moomoo-api 依存を削除し protobuf 二重登録の再燃を防止 (#66)


- **sell-guard**: moomoo SELL の over-sell を broker 実保有で clamp/skip (#74)



## [0.6.0] - 2026-05-30


### その他

- バージョン 0.6.0 にバンプ



### ドキュメント

- **status-api**: Cloudflare Access (/status*) の Service Token 設定手順を追記 (#62)



### バグ修正

- **codeql**: Code scanning 3 件解消 + .env.op に status_curl 用 op 参照を追加 (#64)



### 新機能

- **scripts**: status API 運用ヘルパー status_curl.sh を追加 (#63)


- **reconcile**: 約定照合結果を OrderReconciledEvent として永続化 (#57) (#65)



## [0.5.1] - 2026-05-30


### その他

- バージョン 0.5.1 にバンプ



### バグ修正

- **status-service**: SDK import を futu に統一して protobuf 二重登録の 502 を解消 (#61)



## [0.5.0] - 2026-05-30


### その他

- バージョン 0.5.0 にバンプ



### ドキュメント

- CHANGELOG を v0.4.1 に更新


- **ops**: 新バージョン反映のデプロイ runbook を追加 (#60)



### 新機能

- **moomoo**: CRYPTO + SIMULATE を OpenD 接続前に ValueError で早期拒否 (#54)


- **models**: WebhookPayload + 4 event に portfolio_id / sub_strategy_id を追加 (#55)


- **scripts**: SIMULATE 保有建玉の成行決済ヘルパー flatten_simulate_positions.py を追加 (#56)


- **status-api**: read-only トレード状況 API を追加 (#57 Phase 1) (#58)


- **notify**: 約定 reconcile + ntfy プッシュ通知を追加 (#57 Phase 2) (#59)



## [0.4.1] - 2026-05-18


### その他

- バージョン 0.4.1 にバンプ



### ドキュメント

- CHANGELOG を v0.4.0 に更新



### バグ修正

- **security**: CodeQL Code Scanning の 17 件の指摘を修正 (#52)



## [0.4.0] - 2026-05-18


### その他

- バージョン 0.4.0 にバンプ



### ドキュメント

- CHANGELOG を v0.3.0 に更新



### 新機能

- **safety**: kill switch (maintenance mode) を実装 (#50)


- **safety**: idempotency (signal_id 重複拒否) を実装 (#51)



## [0.3.0] - 2026-05-18


### その他

- バージョン 0.3.0 にバンプ



### ドキュメント

- CHANGELOG を v0.2.0 に更新



### 新機能

- **pkg**: PyPI 公開可能な src layout + alpha-strike CLI に再構成 (#48)



## [0.2.0] - 2026-05-18


### その他

- プロジェクト初期化（uv環境・依存関係設定）


- リポジトリ初期化（.gitignoreのみ）


- .gitignoreに.agentフォルダを追加


- data/ ディレクトリを .gitignore に追加


- SOLID Phase 1 実装プランをalpha-forgeから移動


- バージョン 0.2.0 にバンプ



### テスト

- moomoo ハンドラーのテストを追加しカバレッジを 95% に向上



### ドキュメント

- ドキュメントをOANDA対応に更新・README/CLAUDE.mdを新規作成


- add .github/copilot-instructions.md for Copilot CLI (#15)


- SOLID Phase 1 リファクタリングに合わせてドキュメントを更新


- setup.mdを更新、verify-build.shを追加 (#21)


- CLAUDE.md の冒頭ボイラープレートを整理 (#24)


- **ops**: VM プロビジョニング手順書を追加 (#25)


- TradingView 連携を Cloudflare Tunnel 構成に更新 + ペーパートレード Go-Live チェックリスト追加 (#35)


- **tradingview**: Webhook 利用可能プランを Premium → Essential に訂正 (#39)



### バグ修正

- セキュリティと品質問題を修正（コードレビュー対応）


- セキュリティレビュー指摘事項を修正


- **python**: python-reviewの指摘事項対応（blackフォーマット適用、pygments脆弱性修正）


- comprehensive code review fixes (#13)


- リリース先をalforge-labs/alforge-labs.github.ioに変更 (#19)


- **ops**: cleanup_simulate_orders.py を CANCEL→DELETE フォールバック方式に変更 (#29)


- **ops**: cleanup_simulate_orders.py で order_list_query に status_filter を適用 (#30)


- **ops**: show_simulate_status.py の --json モードで stderr WARN が stdout に混ざる問題を修正 (#32)


- **ops**: show_simulate_status.py の --json で SDK の stdout ログを redirect (#33)


- **ops**: show_simulate_status.py の --json で ctx.close() 後の on_disconnect ログを待つ (#34)


- **build**: SDK 10.5.6508 用 PyInstaller データバンドル + smoke test 修正 (#42)



### リファクタリング

- OandaHandler・MoomooHandler クラスに変換、handlers/__init__.py を更新（OCP/DIP対応）


- webhook_server を薄いHTTPレイヤーに整理（SRP/OCP対応完了）



### 新機能

- TradingView Webhook サーバー初期実装（moomoo証券対応）


- IG証券対応を廃止しOANDA証券に切り替え


- 耐障害性と自動復旧機能を整備（24/7安定稼働対応）


- VPS リモートホスティング対応（nginx / docker-compose 更新） (#14)


- FillEvent/TradeClosedEvent のキャプチャと /events/trade-closed エンドポイントを追加


- BrokerHandler Protocol を追加


- OrderRouter サービスを追加（Strategy/OCP パターン）


- FillEventService を抽出（SRP対応）


- バージョン管理基盤を整備（bump-my-version + git-cliff）


- CI/CD バイナリリリースパイプラインを追加 (#16) (#18)


- 1Password CLI 統合・起動時バリデーション強化 (#20)


- /pre-release コマンドを追加 (#22)


- EULA・LICENSE・README ライセンスセクションを追加 (#23)


- .envrcファイルを追加し、dotenvを設定


- **ops**: VM (oracle-strike) 週次 apt メンテナンススクリプトを追加 (#26)


- **ops**: メモリ・swap・サービス監視スクリプト check_memory.sh を追加 (#27)


- **ops**: SIMULATE 環境 pending 注文の一括キャンセルスクリプトを追加 (#28)


- **ops**: SIMULATE 口座状態確認スクリプト show_simulate_status.py を追加 (#31)


- **payload**: asset_class に CRYPTO を追加 + futu/moomoo SDK を 10.5.6508 に統一 (#38)


- **oss**: Apache-2.0 OSS リリース準備（alpha-visualizer 同等の OSS 構成を整備） (#43)



