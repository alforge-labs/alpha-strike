# OpenD セットアップガイド

moomoo証券（Futu）への発注には、ローカルで稼働する **OpenD** ゲートウェイが必要です。
Webhookサーバーは OpenD 経由でmoomooのAPIにアクセスします。

## OpenD のダウンロード・インストール

1. [moomoo証券の公式ページ](https://www.moomoo.com/us/download) または
   [moomoo OpenAPIドキュメント](https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html)
   から **OpenD** をダウンロード
2. インストーラーを実行（macOS/Windows/Linux対応）
3. moomooアカウントでログイン

## OpenD の起動

### macOS / Linux

```bash
./OpenD \
  -login_account <moomooアカウントID> \
  -login_pwd <パスワード> \
  -ip 127.0.0.1 \
  -port 11111
```

または OpenD の GUI アプリからログインして起動します。

## デモ口座（SIMULATE）での注文テスト

**テスト時は必ずデモ口座（SIMULATE）を使用してください。**

`.env` ファイルで以下を設定:
```
MOOMOO_TRD_ENV=SIMULATE
```

- `SIMULATE`: moomooのデモ取引環境（仮想資金で取引）
- `REAL`: 本番取引（実際のお金が動きます）

> **警告**: `MOOMOO_TRD_ENV=REAL` に設定すると実際の注文が発注されます。
> 動作確認が完了するまで必ず `SIMULATE` を使用してください。

## 接続設定

デフォルト設定（`.env.example` の値）:
```
MOOMOO_HOST=127.0.0.1
MOOMOO_PORT=11111
```

OpenD を別のポートで起動している場合は合わせて変更してください。

## 対応する asset_class と銘柄コード形式

| asset_class の値 | 対応市場 | 銘柄コード形式 | 例 |
|---|---|---|---|
| `US` | 米国株 | `US.ティッカー` | `US.AAPL`, `US.TSLA` |
| `HK` | 香港株 | `HK.XXXXX`（5桁） | `HK.00700`（テンセント） |
| その他 | 米国株として処理 | `US.ティッカー` | — |

## 起動順序（重要）

1. **OpenD を先に起動**してmoomooアカウントにログイン
2. その後 **Webhookサーバーを起動**

OpenD が起動していない状態でmoomooへの注文リクエストが来ると、
サーバーは接続確認後すぐにエラー（502）を返します。

## トラブルシューティング

### 「OpenD が起動していません」エラー

```
{"detail": "注文実行エラー: OpenD (127.0.0.1:11111) が起動していません。"}
```

→ OpenD を起動してmoomooアカウントにログインしてください。

### 認証エラー / 二要素認証

OpenD は初回起動時にSMSや認証アプリによる二要素認証を求める場合があります。
GUIアプリを使って先に認証を済ませてください。

### 「SIMULATE口座が見つからない」エラー

moomooアカウントでデモ取引が有効化されていない場合があります。
moomooアプリ内の「取引」→「模擬取引」から有効化してください。

### ポート衝突

11111番ポートが使われている場合:
```bash
# 使用中のプロセスを確認
lsof -i :11111

# OpenD を別ポートで起動（例: 11112）
./OpenD -port 11112 ...

# .env も合わせて変更
MOOMOO_PORT=11112
```
