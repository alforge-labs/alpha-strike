# Changelog

alpha-strike の全バージョン変更履歴です。


## [0.7.1] - 2026-06-06


### その他

- バージョン 0.7.1 にバンプ



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



