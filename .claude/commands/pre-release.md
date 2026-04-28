---
name: pre-release
description: ローカルビルド検証からリリースまでのフローを実行する
command: true
---

# pre-release コマンド

alpha-strike のリリース前にローカルバイナリを作成・検証し、問題なければリリースする。

## 使い方

```
/pre-release [patch|minor|major]
```

引数を省略した場合は `patch` として扱う。

## 実行手順

1. **作業ディレクトリの確認**

   alpha-strike リポジトリのルートにいること、かつ `main` ブランチにいることを確認する。

   ```bash
   git branch --show-current
   git status
   ```

   未コミットの変更があれば中断してユーザーに確認を求める。

2. **ローカルビルド検証**

   ```bash
   bash verify-build.sh
   ```

   失敗した場合は中断してエラー内容をユーザーに報告する。

3. **バージョン確認**

   現在のバージョンと、バンプ後のバージョンをユーザーに提示して確認を求める。

   ```bash
   grep '^version' pyproject.toml
   ```

4. **リリース実行**

   ユーザーの承認を得てから実行する。

   ```bash
   bash release.sh ${PART}
   ```

## 注意

- `release.sh` は `git push --tags` まで実行するため、実行前に必ずユーザーの承認を取ること。
- `git-cliff` と `bump-my-version` が必要（`uv sync --all-groups` で導入済みのはず）。
