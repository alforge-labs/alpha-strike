"""ログ出力用サニタイザ (CodeQL: py/log-injection 対策)。

webhook payload 由来などユーザーが制御可能な値をログに出力する前に、
改行・タブ・NULL などの制御文字を除去する。攻撃者が改行を含む値で
偽のログ行（例: 偽 CRITICAL エントリ）を注入する手口を遮断する。

``webhook_server`` 内のプライベート関数だったものを、``services`` 層からも
依存方向を壊さずに使えるよう共通モジュールに切り出した。
"""

from __future__ import annotations

_LOG_SANITIZE_TABLE = str.maketrans(
    "",
    "",
    "".join(chr(c) for c in range(0x20)) + "\x7f",
)


def safe_for_log(value: object, max_len: int = 100) -> str:
    """ログ出力用に安全化した文字列を返す。

    - 改行 / タブ / NULL 等の制御文字 (0x00-0x1F, 0x7F) を除去
    - ``max_len`` 文字に切り詰める（過大なログを防ぐ）
    - 非文字列は ``str()`` で文字列化してからサニタイズ
    """
    s = str(value).translate(_LOG_SANITIZE_TABLE)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s
