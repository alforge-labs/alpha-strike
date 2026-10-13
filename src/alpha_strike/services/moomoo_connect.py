"""OpenD（moomoo/Futu）への接続を有界化する設定。

futu SDK のコンテキストコンストラクタは既定 ``is_async_connect=False`` で、
``futu/common/open_context_base.py`` の中が出口のない ``while True`` になっている。

    while True:
        ret = self._init_connect_sync()
        if ret == RET_OK:
            return
        else:
            if not self._auto_reconnect:   # 既定 True なので抜けない
                return
        sleep(self._reconnect_interval)

OpenD が画像認証待ちで固まるとここを永久に回り続け、呼び出し側からキャンセルできない。
2026-10-12 の障害（OpenD トークン失効 4 回目）ではこれで米国 7 営業日分の発注が欠落した。

対策は SDK 自身が用意している 2 つの口を使うこと。コンテキスト生成時に
``is_async_connect=True`` を渡してコンストラクタを即座に返させ、
``set_sync_query_connect_timeout`` でクエリの接続待ちを打ち切る。
これで OpenD 死亡時の挙動が「無限ハング」から「有界エラー」に変わり、
下流の ``ret != RET_OK`` 経路（既存）が正しく働くようになる。

**ただしトレード系だけ口が無い。** ``OpenQuoteContext`` は ``is_async_connect`` を受け取るが、
``OpenSecTradeContext`` は受け取らない（``OpenTradeContextBase.__init__`` が
``super().__init__()`` へ渡さないため、公開 API では回避できない）。そのため
``bounded_context_class`` でサブクラスを作り、SDK の ``is_async_connect=True`` 分岐と
同じ経路を再現する。
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_ENV_VAR = "MOOMOO_CONNECT_TIMEOUT_SECONDS"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


def get_moomoo_connect_timeout() -> float:
    """OpenD 接続待ちクエリのタイムアウト秒。既定 10.0。

    不正値・0 以下・非有限値（nan / inf）は既定へフォールバックする。
    **ここで None や 0 を通してはならない。**
    ``set_sync_query_connect_timeout(None)`` は SDK 側で「タイムアウト無効」を意味し、
    この修正そのものが無効化される。nan は ``elapsed >= nan`` が常に偽、inf は
    永久に到達しないため、どちらも実質的にタイムアウト無効と同じになる。
    """
    raw = os.getenv(_TIMEOUT_ENV_VAR, str(DEFAULT_CONNECT_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s が数値ではありません、既定の %s を使用",
            _TIMEOUT_ENV_VAR,
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
        return DEFAULT_CONNECT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s が 0 以下または有限値ではありません、既定の %s を使用",
            _TIMEOUT_ENV_VAR,
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
        return DEFAULT_CONNECT_TIMEOUT_SECONDS
    return value


def apply_connect_timeout(ctx: Any) -> None:
    """生成済みコンテキストへ接続タイムアウトを設定する。

    ``is_async_connect=True`` と対で使う。コンストラクタは即座に返るので、
    実際の接続待ちはこのタイムアウトで打ち切られる。
    """
    ctx.set_sync_query_connect_timeout(get_moomoo_connect_timeout())


def _require_private_members(base: type) -> None:
    """有界化が依存する SDK の private メンバの存在を確かめる。

    無ければ ``RuntimeError`` で**大きな音を立てて**落とす。黙って素のコンストラクタへ
    フォールバックすると、この修正が潰そうとしている「無言の無限ハング」がそのまま戻って
    くる。CI では実 futu を使った有界時間テストが検知器になる。
    """
    missing = [
        name
        for name in ("_init_connect_sync", "_wait_reconnect")
        if not hasattr(base, name)
    ]
    if missing:
        raise RuntimeError(
            "futu SDK の内部実装が変わりました（見つからないメンバ: "
            f"{', '.join(missing)}）。接続の有界化が機能しないため中止します。"
            "moomoo_connect.bounded_context_class の実装を SDK に合わせて更新してください。"
        )


def bounded_context_class(base: type, *, ret_ok: int) -> type:
    """``base`` の接続を有界化したサブクラスを返す。

    **なぜサブクラスが必要か**: ``OpenQuoteContext`` は ``is_async_connect=True`` を受け取るが、
    ``OpenSecTradeContext`` は受け取らない。``OpenTradeContextBase.__init__`` が
    ``super().__init__()`` へ ``is_async_connect`` を渡さないため、公開 API では
    コンストラクタの無限リトライを避けられない。

    **なぜ安全か**: ``OpenTradeContextBase.__init__`` は自分のフィールド設定をすべて
    ``super().__init__()`` の前に済ませ、その後に処理が無い。したがって親の ``while True`` を
    1 周で抜けさせても、オブジェクトは完全に初期化された状態になる（futu 10.09.6908 で確認）。

    **やっていること**: 構築中だけ ``_init_connect_sync`` に成功を返させて ``while True`` を
    1 周で抜け、構築後に ``_wait_reconnect(0)`` を呼ぶ。これは SDK の
    ``is_async_connect=True`` 分岐とまったく同じ経路である。

    ``base`` と ``ret_ok`` を引数で受け取るのは、呼び出し側それぞれの ``futu`` 参照と
    食い違わせないため（``moomoo_handler`` はモジュールレベル、``status_service`` は
    関数内の遅延 import）。テストから偽の基底クラスを渡して実 futu 無しで検証できる利点もある。

    毎回クラスを生成するが、コストはマイクロ秒でネットワーク往復に対して無視できる。
    キャッシュするとモックした基底クラス由来のクラスが後続テストに漏れるため、しない。
    """
    _require_private_members(base)

    class _BoundedContext(base):  # type: ignore[misc, valid-type]
        """接続待ちで無限リトライしないコンテキスト。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self._defer_connect = True
            try:
                super().__init__(*args, **kwargs)
            finally:
                self._defer_connect = False
            # SDK の is_async_connect=True 分岐と同一の経路へ渡す
            self._wait_reconnect(0)

        def _init_connect_sync(self):  # type: ignore[no-untyped-def]
            # 構築中は成功を装い、親コンストラクタの while True を 1 周で抜けさせる
            if getattr(self, "_defer_connect", False):
                return ret_ok
            return super()._init_connect_sync()

    return _BoundedContext
