# alpha-strike VM プロビジョニング手順書

**対象**: Oracle Cloud Infrastructure (OCI) Always Free 枠での E2.1.Micro 追加プロビジョニング
**用途**: alpha-strike (webhook サーバー) + OpenD (futu/moomoo) の同居運用
**前提**: alpha-bot がすでに別の E2.1.Micro で稼働中
**公開方式**: Cloudflare Tunnel（webhook + SSH 共に Tunnel 経由、最終的にゼロインバウンド構成）

---

## 0. 事前準備チェックリスト

- [ ] OCI コンソールにログイン可能
- [ ] Always Free 枠の E2.1.Micro 利用状況を確認（**最大 2 台まで無料**、残枠 1 台分あり）
- [ ] SSH キーペア準備済み
- [ ] alpha-bot VM の **VCN 名・サブネット名・リージョン** を確認（同一 VCN に置く前提）
- [ ] **ムームードメイン管理画面**にログイン可能（`alforgelabs.com` の nameserver 変更で使用）
- [ ] **Cloudflare アカウント** を保有、Zero Trust の Free plan を有効化済み
- [ ] ローカル PC に **Homebrew (macOS) または apt (Linux)** が利用可能（§7 で `cloudflared` をインストール）

> **Cloudflare Zero Trust Free plan の有効化**: Cloudflare ダッシュボード → Zero Trust → 初回アクセス時に Free plan を選択。クレジットカード登録は求められるが、Free 範囲内なら課金は発生しない。
>
> **本手順書の最終ゴール**: webhook も SSH も Cloudflare Tunnel 経由でアクセスし、OCI 側の NSG はインバウンドルール 0 件（完全 egress only）にする。自宅 IP が DHCP で変動しても影響を受けない構成。

---

## 1. ドメインの Cloudflare 移管（最初の山場）

Cloudflare Tunnel を Free plan で利用するには、ゾーン全体を Cloudflare の権威 DNS で管理する必要がある。現在 `alforgelabs.com` の DNS はムームードメインで管理されており、これを Cloudflare に移管する。

### 1-1. 現状の DNS レコード（移管前にスナップショット）

```
A   alforgelabs.com           → 185.199.108.153 / 109.153 / 110.153 / 111.153  (GitHub Pages)
CNAME www.alforgelabs.com     → alforge-labs.github.io.
TXT alforgelabs.com           v=spf1 include:_spf.google.com ~all
TXT alforgelabs.com           google-site-verification=mfpvsKsOVuaDEEuNsszr1CAibx_fmLbME94KmyW_8hI
TXT alforgelabs.com           google-site-verification=u5KSmZUt-7jN2huCrOaEdJgFruwK-Lm2rdqCbfZCsr8
MX                            （なし）
CAA                           （なし）
```

> **移管前にやること**: ムームードメインの DNS 管理画面でも同じレコードを目視確認しておく。dig 結果と差異がないことを確認。

### 1-2. Cloudflare にドメインを追加

1. Cloudflare ダッシュボード → **Account home** から、以下のいずれかでドメイン追加画面を開く：
   - **Domains** カードの **Add a domain** ボタンをクリック
   - または、画面右上の **+ Add** ボタン → **Domain** を選択
   - または、左サイドバーの **Domains** メニュー → ドメイン追加画面へ遷移
2. ドメイン入力欄に `alforgelabs.com` を入力 → **Continue**
3. プラン選択画面で **Free** を選択 → **Continue**
4. Cloudflare の自動 DNS スキャナーが既存レコードを検出 → 表示される一覧と上記 §1-1 が一致するか確認
5. レコードを **Continue** で確定

> **UI 表記の注意**: 古い Cloudflare 記事や手順書では「Add a site」と記載されていることがあるが、2026 年現在の UI では **Add a domain** に変更されている。機能としては同等。

### 1-3. GitHub Pages 共存設定（重要・罠あり）

**A レコードと `www` CNAME は必ず DNS only（グレー雲）に設定する**。

| レコード | プロキシ状態 | 理由 |
|---|---|---|
| `alforgelabs.com` A × 4 | **DNS only（グレー雲）** | Cloudflare プロキシ経由だと GitHub Pages 側で Let's Encrypt の HTTPS 証明書発行が失敗する |
| `www.alforgelabs.com` CNAME | **DNS only（グレー雲）** | 同上 |
| TXT × 3 | （プロキシ不可、そのまま） | DNS のみ |

> **過去の典型ハマり**: GitHub Pages のカスタムドメインを Cloudflare プロキシ経由にすると、GitHub の自動証明書更新時に ACME チャレンジが Cloudflare の Edge IP に向き、検証が失敗する → HTTPS が壊れる。**DNS only に設定すれば回避可能**。
>
> `strike.alforgelabs.com` および `ssh.alforgelabs.com`（後で Tunnel で追加）はオレンジ雲（Tunnel 経由）で OK。GitHub Pages 配下のレコードだけグレー雲にする。

### 1-4. Cloudflare 指定の nameserver を取得

レコード確認後、Cloudflare が「以下の nameserver に変更してください」と 2 つ提示する。例：

```
xxx.ns.cloudflare.com
yyy.ns.cloudflare.com
```

これをメモ。

### 1-5. ムームードメインで nameserver 変更

1. ムームードメイン管理画面 → **コントロールパネル** → **ドメイン操作** → **ネームサーバ設定変更**
2. `alforgelabs.com` を選択 → **GMOペパボ以外のネームサーバを使用する** を選択
3. ネームサーバ 1 / 2 に Cloudflare 指定の値を入力
4. 設定変更を確定

### 1-6. 反映待ち・確認

反映までは通常数時間、最大 48 時間。以下で確認：

```bash
dig +short NS alforgelabs.com
# 期待: xxx.ns.cloudflare.com / yyy.ns.cloudflare.com

dig +short A alforgelabs.com
# 期待: 185.199.108.153 / 109.153 / 110.153 / 111.153

dig +short CNAME www.alforgelabs.com
# 期待: alforge-labs.github.io.

dig +short TXT alforgelabs.com
# 期待: SPF + google-site-verification × 2 がそのまま返る
```

Cloudflare ダッシュボード上でも該当ドメインのステータスが **「Active」** に変わったことを確認。

### 1-7. 移管完了の動作確認

- [ ] `https://alforgelabs.com` にブラウザでアクセス → LP が従来通り表示される
- [ ] `https://www.alforgelabs.com` も同様にアクセスできる
- [ ] HTTPS 証明書のエラーが出ない（GitHub Pages の Let's Encrypt が動作中）
- [ ] Google Search Console で「所有権の確認」が外れていない（TXT 維持確認）

---

## 2. ネットワーク設計

### 2-1. VCN 配置方針

- **同一 VCN・同一 public subnet に配置**: alpha-bot と同じ VCN 内に置き、プライベート IP で内部通信できるようにする

### 2-2. セキュリティリスト（または NSG）設計（**初期セットアップ用・暫定**）

新規 NSG を `nsg-alpha-strike` として作成。**最終的には Ingress 0 件にするが、初回セットアップ時のみ SSH を一時許可する**：

| 方向 | プロトコル | ポート | ソース/宛先 | 用途 | 状態 |
|---|---|---|---|---|---|
| Ingress | TCP | 22 | 自宅 IP/32 もしくは現在の自宅 IP（取得時点） | SSH（**§8 で削除予定**） | 一時 |
| Egress | All | All | 0.0.0.0/0 | 外向き全許可（Tunnel 通信に必須） | 恒久 |

> **自宅 IP が DHCP で変動する場合**: 初期セットアップ時はその瞬間の自宅 IP（`curl ifconfig.io` で取得）を /32 で許可。Cloudflare Access SSH への切替（§7）が完了したら、この Ingress ルールは §8 で削除する。それ以降は IP 変動に一切影響されない。
>
> **Cloudflare Tunnel の動作原理**: VM 側の `cloudflared` プロセスが Cloudflare エッジへ **アウトバウンド接続を確立** し、その上で webhook も SSH も中継する。インバウンド開放は完全に不要になる。

### 2-3. リージョン

alpha-bot と同一リージョン必須。

---

## 3. Compute インスタンス作成

### 3-1. OCI コンソール操作

1. 左メニュー → **Compute** → **Instances** → **Create instance**
2. 以下を入力：

| 項目 | 値 |
|---|---|
| **Name** | `alpha-strike-01` |
| **Compartment** | alpha-bot と同一 |
| **Placement (Availability Domain)** | 容量がある AD を選択 |
| **Image** | **Canonical Ubuntu 24.04** |
| **Shape** | VM.Standard.E2.1.Micro (Always Free-eligible) |
| **VCN** | alpha-bot と同一 VCN を選択 |
| **Subnet** | public subnet を選択 |
| **Public IPv4 address** | Assign（cloudflared のアウトバウンド接続に必要） |
| **NSG** | `nsg-alpha-strike` をアタッチ |
| **SSH keys** | 既存の公開鍵を貼り付け or アップロード |
| **Boot volume size** | 50 GB（Always Free 枠は合計 200GB まで無料） |

3. **Create** をクリック → プロビジョニング完了まで 1〜2 分待機
4. **Public IP** をメモ（初回 SSH 用、後段では使わない）

---

## 4. 初回 SSH 接続確認（暫定経路）

§7 で Cloudflare Access SSH に切り替えるまでの **一時的な経路**。

### 4-1. ローカルから疎通テスト

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@<public-ip>
```

### 4-2. SSH config に登録（暫定）

`~/.ssh/config` に追記：

```
# 暫定設定（§7 完了後に §7-4 の Cloudflare Access 経由設定で上書きする）
Host alpha-strike-direct
  HostName <public-ip>
  User ubuntu
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 60
```

以降 §5〜§6 の作業は `ssh alpha-strike-direct` で接続して行う。

---

## 5. 初期 OS セットアップ

VM 内にログインした状態で以下を実行。

### 5-1. システム更新

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install ufw fail2ban htop tmux git curl ca-certificates
```

### 5-2. タイムゾーン

```bash
sudo timedatectl set-timezone Asia/Tokyo
```

### 5-3. ホスト名

```bash
sudo hostnamectl set-hostname alpha-strike-01
echo "127.0.1.1 alpha-strike-01" | sudo tee -a /etc/hosts
```

### 5-4. ufw（OS 側ファイアウォール・**初期暫定**）

**§8 で 22/tcp ルールを削除する** が、初回セットアップ中は SSH を維持するため以下を設定：

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp   # §8 で削除予定
sudo ufw enable
sudo ufw status verbose
```

### 5-5. SSH ハードニング

`/etc/ssh/sshd_config.d/99-hardening.conf` を作成：

```
PasswordAuthentication no
PermitRootLogin no
ClientAliveInterval 60
ClientAliveCountMax 3
```

```bash
sudo systemctl restart ssh
```

> **重要**: Cloudflare Access SSH 経由に切り替えた後も、内部の sshd は localhost:22 で待ち受け続ける必要がある（cloudflared が localhost:22 にプロキシする）。sshd の停止はしない。

### 5-6. iptables 確認（OCI Ubuntu イメージ固有）

Ubuntu 24.04 の OCI イメージにも iptables の REJECT ルールが入っている可能性がある。OUTPUT が ACCEPT であることを確認（Cloudflare エッジへのアウトバウンドが通る状態）：

```bash
sudo iptables -L INPUT --line-numbers
sudo iptables -L OUTPUT --line-numbers
```

### 5-7. fail2ban 有効化

```bash
sudo systemctl enable --now fail2ban
sudo systemctl status fail2ban
```

SSH ブルートフォース対策のデフォルト jail が自動有効化される。

---

## 6. Cloudflare Tunnel 接続（webhook 用）

VM とアプリの間に挟む形で webhook 用の Tunnel を確立する。

### 6-1. cloudflared インストール（VM 側）

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt -y install cloudflared
cloudflared --version
```

### 6-2. Cloudflare ダッシュボードで Tunnel 作成

1. Cloudflare ダッシュボード → **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel**
2. Tunnel type: **Cloudflared**
3. Tunnel name: `alpha-strike-prod`
4. **Save tunnel** → トークンが発行される
5. インストールコマンドが表示されるので、VM 側でそのまま実行：

```bash
sudo cloudflared service install <TOKEN>
```

これで `cloudflared.service` が systemd ユニットとして登録・起動される。

### 6-3. Public hostname 設定（webhook）

Cloudflare ダッシュボードの同 Tunnel 画面で **Public Hostnames** タブ → **Add a public hostname**：

| 項目 | 値 |
|---|---|
| **Subdomain** | `strike` |
| **Domain** | `alforgelabs.com` |
| **Path** | （空欄） |
| **Service Type** | `HTTP` |
| **URL** | `localhost:8000` |

> **メモ**: alpha-strike を 8000 番で起動する前提。アプリデプロイ時に変更したければ後で修正可能。
>
> **DNS への影響**: Cloudflare が `strike.alforgelabs.com` の CNAME を Tunnel に向けて自動作成する。**オレンジ雲（プロキシ ON）** で問題なし。

### 6-4. Tunnel 起動確認

```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 50 --no-pager
```

`Connection registered` のログが出ていれば OK。
ブラウザで `https://strike.alforgelabs.com` にアクセス → **502 Bad Gateway** が返れば正常（Tunnel は繋がっているが localhost:8000 がまだ起動していないだけ）。

---

## 7. Cloudflare Access for SSH への切替

webhook 用に立てた同じ Tunnel に SSH も追加で乗せ、Cloudflare Access の認証層で SSH を保護する。**自宅 IP が DHCP で変動しても、ホテル・スマホテザリングなど任意の場所から接続可能** になる。

### 7-1. Tunnel に SSH の Public Hostname を追加（VM 側設定不要）

Cloudflare ダッシュボード → **Zero Trust** → **Networks** → **Tunnels** → `alpha-strike-prod` → **Public Hostnames** タブ → **Add a public hostname**：

| 項目 | 値 |
|---|---|
| **Subdomain** | `ssh` |
| **Domain** | `alforgelabs.com` |
| **Path** | （空欄） |
| **Service Type** | `SSH` |
| **URL** | `localhost:22` |

> Cloudflare が `ssh.alforgelabs.com` の CNAME を Tunnel に向けて自動作成（オレンジ雲）。

### 7-2. Cloudflare Access Application 設定（認証ポリシー）

`ssh.alforgelabs.com` へのアクセスを認証で守る。

1. Cloudflare ダッシュボード → **Zero Trust** → **Access** → **Applications** → **Add an application**
2. Application type: **Self-hosted**
3. 以下を入力：

| 項目 | 値 |
|---|---|
| **Application name** | `alpha-strike SSH` |
| **Session Duration** | `24 hours`（任意） |
| **Application domain** | `ssh.alforgelabs.com` |

4. **Next** → ポリシー追加：

| 項目 | 値 |
|---|---|
| **Policy name** | `Allow owner email` |
| **Action** | `Allow` |
| **Session duration** | `Same as application session timeout` |
| **Configure rules: Include** | `Emails` → `yoshiaki@sakae.org`（自分のメールアドレス） |

5. **Next** → **Add application**

> **認証方式**: デフォルトで One-time PIN（指定メールに 6 桁コード送信）が利用可能。Google SSO / GitHub SSO を追加したい場合は **Settings → Authentication** で IdP を有効化。

### 7-3. ローカル PC への cloudflared インストール

macOS:

```bash
brew install cloudflared
cloudflared --version
```

Linux:

```bash
# Ubuntu/Debian の場合
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt -y install cloudflared
```

### 7-4. ローカル PC の SSH config を Cloudflare Access 経由に変更

`~/.ssh/config` に追記（既存の `Host alpha-strike-direct` はそのまま残し、新規エントリを追加）：

```
Host alpha-strike
  HostName ssh.alforgelabs.com
  User ubuntu
  IdentityFile ~/.ssh/id_ed25519
  ProxyCommand cloudflared access ssh --hostname %h
  ServerAliveInterval 60
```

> `ProxyCommand cloudflared access ssh --hostname %h` が肝。SSH トラフィックを Cloudflare Access 経由で Tunnel にルーティングする。

### 7-5. 接続テスト

```bash
ssh alpha-strike
```

初回接続時：
1. ターミナルにブラウザ認証 URL が表示される
2. ブラウザで Cloudflare Access の認証画面 → メールアドレス入力 → One-time PIN を受け取り入力
3. 認証成功後、SSH セッションが確立

認証は **24 時間有効**（§7-2 で設定した Session Duration）、その間は再認証不要。

### 7-6. 動作確認

- [ ] `ssh alpha-strike` で Cloudflare Access 認証画面が出る
- [ ] One-time PIN 認証後に SSH ログインできる
- [ ] `hostname` の結果が `alpha-strike-01`
- [ ] `who` で自分のセッションが表示される
- [ ] 自宅 IP が変わってもアクセス可能（モバイル回線テザリング等でも動作確認しておくと安心）

---

## 8. インバウンドポート完全閉鎖（ゼロインバウンド構成へ）

Cloudflare Access SSH が動いたら、暫定で開けていた SSH 22 を完全に閉じる。

### 8-1. NSG から SSH 22 ルールを削除

OCI コンソール → **Networking** → **Virtual Cloud Networks** → 対象 VCN → **Network Security Groups** → `nsg-alpha-strike` → Ingress の SSH 22 ルールを削除。

最終的に NSG は以下の状態になる：

| 方向 | プロトコル | ポート | ソース/宛先 | 用途 |
|---|---|---|---|---|
| Egress | All | All | 0.0.0.0/0 | 外向き全許可（Tunnel 通信） |

Ingress ルールはゼロ。

### 8-2. ufw から 22/tcp を削除

VM 内で（このときの SSH 接続は Cloudflare Access 経由）：

```bash
sudo ufw delete allow 22/tcp
sudo ufw status verbose
```

> **重要**: sshd 自体は引き続き localhost:22 で待ち受け続ける（cloudflared が `localhost:22` にプロキシするため）。`sudo systemctl status ssh` が `active` のままであることを確認。

### 8-3. 外部からの 22 が閉じていることを確認

ローカル PC の別ターミナルで：

```bash
nc -vz <public-ip> 22
# 期待: connection timed out（または refused）
```

Cloudflare Access 経由はまだ通る：

```bash
ssh alpha-strike
# 期待: Cloudflare Access 認証 → ログイン成功
```

### 8-4. 暫定 SSH config エントリの削除

ローカル PC の `~/.ssh/config` から `Host alpha-strike-direct` のセクションを削除（もう使わない）。

---

## 9. 疎通確認チェックリスト

- [ ] ローカル → VM へ `ssh alpha-strike`（Cloudflare Access 経由）でログインできる
- [ ] `<public-ip>:22` には外部から接続できない（`nc -vz` でタイムアウト）
- [ ] VM → インターネット egress（`curl -I https://google.com` が 200/301）
- [ ] VM → alpha-bot VM へ private IP で疎通（`ping <alpha-bot-private-ip>`）
- [ ] `dig +short strike.alforgelabs.com` が Cloudflare のプロキシ IP を返す
- [ ] `dig +short ssh.alforgelabs.com` が Cloudflare のプロキシ IP を返す
- [ ] `https://strike.alforgelabs.com` にブラウザアクセスで 502 が返る
- [ ] `https://alforgelabs.com` の LP が従来通り表示される
- [ ] `dmesg | grep -i kill` で何も出ない
- [ ] `free -m` で空きメモリが 700MB 以上ある
- [ ] `sudo systemctl is-active cloudflared` が `active`
- [ ] `sudo systemctl is-active fail2ban` が `active`
- [ ] `sudo systemctl is-active ssh` が `active`（localhost:22 で待ち受け続けている）

---

## 10. プロビジョニング完了基準

以下を全て満たしたら次フェーズ（swap 設定・OpenD インストール・アプリデプロイ）へ進む：

- [ ] `alforgelabs.com` の DNS が Cloudflare 管理に切り替わり、既存 LP が無事動作している
- [ ] Cloudflare Access SSH 経由で `ssh alpha-strike` でログインできる
- [ ] OS が Ubuntu 24.04、最新、JST、ホスト名 `alpha-strike-01`
- [ ] **NSG の Ingress ルールが 0 件、ufw も 22/tcp が削除済み（完全ゼロインバウンド）**
- [ ] Cloudflare Tunnel が systemd で常駐し、`strike.alforgelabs.com` と `ssh.alforgelabs.com` が Cloudflare エッジまで疎通
- [ ] alpha-bot VM とプライベート IP で双方向疎通

---

## 11. 次フェーズ予告

プロビジョニング完了後、以下を順に実施：

1. swap 4GB + zram 設定（OOM 対策）
2. uv セットアップ（Python 3.12 は 24.04 標準搭載）
3. OpenD インストール（futu/moomoo OpenD バイナリ展開）
4. alpha-strike デプロイ + systemd 化（OOMScoreAdjust=-500 込み）
5. メモリ監視 + LINE/X 通知 cron
6. alpha-bot → alpha-strike webhook 疎通テスト
7. ペーパートレード稼働開始

---

## 補足：Cloudflare Tunnel + Access を採用したメリット

| 項目 | 直接 443/22 公開 | Cloudflare Tunnel + Access |
|---|---|---|
| インバウンドポート開放 | 80/443/22 必要 | **0 ポート（egress のみ）** |
| TLS 証明書管理 | Let's Encrypt + certbot を VM 側で運用 | **Cloudflare が自動管理** |
| Public IP の秘匿 | できない | **完全秘匿** |
| DDoS 防御 | OCI レイヤーのみ | **Cloudflare の DDoS 防御が前段** |
| WAF | 自前構築 | **Free plan の WAF 一部利用可** |
| SSH の IP 制限 | 自宅 IP/32（DHCP 変動で壊れる） | **メールアドレスベース認証（場所を選ばない）** |
| SSH ブルートフォース対策 | fail2ban 等を自前で運用 | **Cloudflare Access の認証層で完全遮断** |
| 接続元の監査ログ | 自前で SSH ログ収集 | **Cloudflare 側で全アクセスログ自動記録** |
| コスト | 無料 | **無料** |

---

## 補足：リソース見積もり（E2.1.Micro 1GB RAM）

| プロセス | RAM 目安 |
|---|---|
| OS（Ubuntu 24.04 最小構成） | 200〜300 MB |
| cloudflared（Tunnel エージェント、webhook + SSH 兼用） | 30〜50 MB |
| OpenD（Java ベース、常駐） | 250〜400 MB |
| alpha-strike（FastAPI/Uvicorn） | 150〜250 MB |
| **合計** | **630〜1000 MB** |

1 GB RAM に対して常時 **80〜95% 使用** の張り付き運用となるため、本番資金投入前に必ずペーパートレードで `dmesg | grep -i kill` を観測し、OOM Killer 発動の痕跡が無いことを確認すること。swap 4 GB と zram を次フェーズで導入することで、瞬間的なメモリピークは吸収できる想定。

> cloudflared は webhook と SSH の両 Public Hostname を 1 プロセスで処理するため、SSH 用に追加メモリは発生しない。

---

## 補足：ムームードメイン移管時のロールバック手順

万一 Cloudflare 移管後に LP が壊れる等の問題が発生した場合の戻し手順：

1. ムームードメイン管理画面 → **ネームサーバ設定変更**
2. `alforgelabs.com` を選択 → **GMOペパボのネームサーバを使用する** に戻す
3. 旧 DNS 設定（§1-1 のレコード一覧）がムームードメイン側に残っていることを確認
4. 反映に数時間〜48h

> **予防策**: 移管前にムームードメインの DNS 管理画面のスクリーンショットを必ず保存。設定差分を後から復元できるようにする。

---

## 補足：Cloudflare Access SSH が動かなくなった場合の緊急復旧手順

Cloudflare Tunnel 障害・Access ポリシー誤設定で SSH 不能になった場合の復旧経路：

1. OCI コンソール → **Compute** → 対象インスタンス → **Console connection** → **Launch Cloud Shell connection**
2. ブラウザ上から Console 接続でログイン（SSH を経由しない）
3. NSG に SSH 22 ルールを一時追加（現在の自宅 IP/32）
4. ufw も `sudo ufw allow 22/tcp` で再開放
5. 通常 SSH でログインして cloudflared / Access 設定を修復
6. 修復完了後、再度 §8 の手順でゼロインバウンドに戻す

> Console connection は OCI Always Free 枠で利用可能。**この経路を必ず一度試しておく** ことで、最悪のケースでも復旧手段を確保できる。
