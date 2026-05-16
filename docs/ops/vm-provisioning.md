# alpha-strike VM プロビジョニング手順書

**対象**: Oracle Cloud Infrastructure (OCI) Always Free 枠での E2.1.Micro 追加プロビジョニング
**用途**: alpha-strike (webhook サーバー) + OpenD (futu/moomoo) の同居運用
**前提**: alpha-bot がすでに別の E2.1.Micro で稼働中
**公開方式**: Cloudflare Tunnel（インバウンドポート開放なし、TLS 終端は Cloudflare 側）

---

## 0. 事前準備チェックリスト

- [ ] OCI コンソールにログイン可能
- [ ] Always Free 枠の E2.1.Micro 利用状況を確認（**最大 2 台まで無料**、残枠 1 台分あり）
- [ ] SSH キーペア準備済み
- [ ] alpha-bot VM の **VCN 名・サブネット名・リージョン** を確認（同一 VCN に置く前提）
- [ ] `alforge-labs.com` の DNS が **Cloudflare に委譲済み**（Cloudflare Tunnel の前提条件）
- [ ] Cloudflare アカウントに Zero Trust の Free plan を有効化済み

> **Cloudflare Zero Trust Free plan の有効化**: Cloudflare ダッシュボード → Zero Trust → 初回アクセス時に Free plan を選択。クレジットカード登録は求められるが、Free 範囲内なら課金は発生しない。

---

## 1. ネットワーク設計

### 1-1. VCN 配置方針

- **同一 VCN・同一 public subnet に配置**: alpha-bot と同じ VCN 内に置き、プライベート IP で内部通信できるようにする

### 1-2. セキュリティリスト（または NSG）設計

新規 NSG を `nsg-alpha-strike` として作成、**SSH のみ開ける**：

| 方向 | プロトコル | ポート | ソース/宛先 | 用途 |
|---|---|---|---|---|
| Ingress | TCP | 22 | 自宅 IP/32 | SSH（自分の IP に限定） |
| Egress | All | All | 0.0.0.0/0 | 外向き全許可（Cloudflare Tunnel のアウトバウンド接続に必須） |

> **Cloudflare Tunnel の動作原理**: VM 側の `cloudflared` プロセスが Cloudflare エッジへ **アウトバウンド接続を確立** し、その上で webhook トラフィックを受ける。インバウンド 443/80 を開ける必要が一切ない。
>
> **SSH すら閉じる選択肢**: 後段で Cloudflare Access SSH を導入すれば、22 も閉じてゼロインバウンドにできる。今回はまず SSH 22 を残し、運用が安定したら検討する。

### 1-3. リージョン

alpha-bot と同一リージョン必須。

---

## 2. Compute インスタンス作成

### 2-1. OCI コンソール操作

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
4. **Public IP** をメモ（SSH 用、DNS には使わない）

---

## 3. SSH 接続確認

### 3-1. ローカルから疎通テスト

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@<public-ip>
```

### 3-2. SSH config に登録

`~/.ssh/config` に追記：

```
Host alpha-strike
  HostName <public-ip>
  User ubuntu
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 60
```

以降は `ssh alpha-strike` でアクセス可能。

---

## 4. 初期 OS セットアップ

VM 内にログインした状態で以下を実行。

### 4-1. システム更新

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install ufw fail2ban htop tmux git curl ca-certificates
```

### 4-2. タイムゾーン

```bash
sudo timedatectl set-timezone Asia/Tokyo
```

### 4-3. ホスト名

```bash
sudo hostnamectl set-hostname alpha-strike-01
echo "127.0.1.1 alpha-strike-01" | sudo tee -a /etc/hosts
```

### 4-4. ufw（OS 側ファイアウォール）

SSH 以外は全閉。Cloudflare Tunnel はアウトバウンドのみで動作するのでインバウンド許可は不要：

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw enable
sudo ufw status verbose
```

### 4-5. SSH ハードニング

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

### 4-6. iptables 確認（OCI Ubuntu イメージ固有）

Ubuntu 24.04 の OCI イメージにも iptables の REJECT ルールが入っている可能性がある。**今回は SSH のみ使うので追加開放は不要**だが、念のため現状を確認：

```bash
sudo iptables -L INPUT --line-numbers
sudo iptables -L OUTPUT --line-numbers
```

OUTPUT に REJECT が無いこと（Cloudflare エッジへのアウトバウンドが通る状態）を確認。通常は ACCEPT がデフォルト。

### 4-7. fail2ban 有効化

```bash
sudo systemctl enable --now fail2ban
sudo systemctl status fail2ban
```

SSH ブルートフォース対策のデフォルト jail が自動有効化される。

---

## 5. Cloudflare Tunnel 接続（プロビジョニングの最後）

VM とアプリの間に挟む形でこの段階で Tunnel を確立しておくと、アプリデプロイ時にすぐ公開できる。

### 5-1. cloudflared インストール

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt -y install cloudflared
cloudflared --version
```

### 5-2. Cloudflare ダッシュボードで Tunnel 作成

1. Cloudflare ダッシュボード → **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel**
2. Tunnel type: **Cloudflared**
3. Tunnel name: `alpha-strike-prod`
4. **Save tunnel** → トークンが発行される
5. インストールコマンドが表示されるので、VM 側でそのまま実行：

```bash
sudo cloudflared service install <TOKEN>
```

これで `cloudflared.service` が systemd ユニットとして登録・起動される。

### 5-3. Public hostname 設定

Cloudflare ダッシュボードの同 Tunnel 画面で **Public Hostnames** タブ → **Add a public hostname**：

| 項目 | 値 |
|---|---|
| **Subdomain** | `strike` |
| **Domain** | `alforge-labs.com` |
| **Path** | （空欄） |
| **Service Type** | `HTTP` |
| **URL** | `localhost:8000` |

> **メモ**: alpha-strike を 8000 番で起動する前提。アプリデプロイ時に変更したければ後で修正可能。

DNS CNAME は Cloudflare が **自動で作成**（手動 A レコード設定は不要）。

### 5-4. Tunnel 起動確認

```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 50 --no-pager
```

`Connection registered` のログが出ていれば OK。
ブラウザで `https://strike.alforge-labs.com` にアクセス → **502 Bad Gateway** が返れば正常（Tunnel は繋がっているが localhost:8000 がまだ起動していないだけ）。

---

## 6. 疎通確認チェックリスト

- [ ] ローカル → VM へ `ssh alpha-strike` でログインできる
- [ ] VM → インターネット egress（`curl -I https://google.com` が 200/301）
- [ ] VM → alpha-bot VM へ private IP で疎通（`ping <alpha-bot-private-ip>`）
- [ ] `dig +short strike.alforge-labs.com` が Cloudflare のプロキシ IP を返す（CNAME 経由）
- [ ] `https://strike.alforge-labs.com` にブラウザアクセスで 502 が返る
- [ ] `dmesg | grep -i kill` で何も出ない
- [ ] `free -m` で空きメモリが 700MB 以上ある（ベースライン）
- [ ] `sudo systemctl is-active cloudflared` が `active`
- [ ] `sudo systemctl is-active fail2ban` が `active`

---

## 7. プロビジョニング完了基準

以下を全て満たしたら次フェーズ（swap 設定・OpenD インストール・アプリデプロイ）へ進む：

- [ ] SSH で `alpha-strike` ホスト名で接続できる
- [ ] OS が Ubuntu 24.04、最新、JST、ホスト名 `alpha-strike-01`
- [ ] ufw + NSG の二層防御で SSH のみインバウンド許可
- [ ] Cloudflare Tunnel が systemd で常駐し、`strike.alforge-labs.com` が Cloudflare エッジまで疎通
- [ ] alpha-bot VM とプライベート IP で双方向疎通

---

## 8. 次フェーズ予告

プロビジョニング完了後、以下を順に実施：

1. swap 4GB + zram 設定（OOM 対策）
2. uv セットアップ（Python 3.12 は 24.04 標準搭載）
3. OpenD インストール（futu/moomoo OpenD バイナリ展開）
4. alpha-strike デプロイ + systemd 化（OOMScoreAdjust=-500 込み）
5. メモリ監視 + LINE/X 通知 cron
6. alpha-bot → alpha-strike webhook 疎通テスト
7. ペーパートレード稼働開始

---

## 補足：Cloudflare Tunnel を採用したメリット

| 項目 | 直接 443 公開 | Cloudflare Tunnel |
|---|---|---|
| インバウンドポート開放 | 80/443 必要 | **0 ポート（egress のみ）** |
| TLS 証明書管理 | Let's Encrypt + certbot を VM 側で運用 | **Cloudflare が自動管理** |
| Public IP の秘匿 | できない | **完全秘匿** |
| DDoS 防御 | OCI レイヤーのみ | **Cloudflare の DDoS 防御が前段に入る** |
| WAF | 自前構築 | **Free plan の WAF 一部利用可** |
| 認証層追加 | 自前 | **Cloudflare Access で IdP 連携可（将来）** |
| コスト | 無料 | **無料** |

---

## 補足：リソース見積もり（E2.1.Micro 1GB RAM）

| プロセス | RAM 目安 |
|---|---|
| OS（Ubuntu 24.04 最小構成） | 200〜300 MB |
| cloudflared（Tunnel エージェント） | 30〜50 MB |
| OpenD（Java ベース、常駐） | 250〜400 MB |
| alpha-strike（FastAPI/Uvicorn） | 150〜250 MB |
| **合計** | **630〜1000 MB** |

1 GB RAM に対して常時 **80〜95% 使用** の張り付き運用となるため、本番資金投入前に必ずペーパートレードで `dmesg | grep -i kill` を観測し、OOM Killer 発動の痕跡が無いことを確認すること。swap 4 GB と zram を次フェーズで導入することで、瞬間的なメモリピークは吸収できる想定。
