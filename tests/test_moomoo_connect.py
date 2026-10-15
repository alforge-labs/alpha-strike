"""OpenD 接続の有界化のユニットテスト。

WHY: futu SDK のコンテキストコンストラクタは既定 is_async_connect=False で、
出口のない while True で再接続を繰り返す（futu/common/open_context_base.py）。
OpenD が画像認証待ちで固まると 1 回の呼び出しでイベントループ全体が凍結し、
2026-10-12 には米国 7 営業日分の発注が欠落した。

このテストが守るのは 2 つ:
1. 不正な設定値でタイムアウトが None に落ちないこと（None は SDK で
   「タイムアウト無効」を意味するため、修正そのものが無効化される）
2. OpenD が居なくても呼び出しが有界時間で戻ること
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from alpha_strike.services.moomoo_connect import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    apply_connect_timeout,
    bounded_context_class,
    get_moomoo_connect_timeout,
)


class TestGetMoomooConnectTimeout:
    def test_未設定なら既定値(self, monkeypatch):
        monkeypatch.delenv("MOOMOO_CONNECT_TIMEOUT_SECONDS", raising=False)
        assert get_moomoo_connect_timeout() == DEFAULT_CONNECT_TIMEOUT_SECONDS

    def test_明示値を読む(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_CONNECT_TIMEOUT_SECONDS", "3.5")
        assert get_moomoo_connect_timeout() == pytest.approx(3.5)


class TestApplyConnectTimeout:
    def test_コンテキストへタイムアウトを設定する(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_CONNECT_TIMEOUT_SECONDS", "4")
        ctx = MagicMock()
        apply_connect_timeout(ctx)
        ctx.set_sync_query_connect_timeout.assert_called_once_with(4.0)


class TestInvalidTimeoutNeverDisablesTheGuard:
    """不正値でタイムアウトが無効化されないこと。

    set_sync_query_connect_timeout(None) は SDK で「タイムアウト無効」を意味する。
    不正値が None に落ちると、この修正は入っているのに無限ハングに戻る。
    """

    @pytest.mark.parametrize(
        "raw", ["abc", "", "0", "-1", "-0.5", "nan", "inf", "-inf"]
    )
    def test_不正値は既定へフォールバックする(self, monkeypatch, raw):
        monkeypatch.setenv("MOOMOO_CONNECT_TIMEOUT_SECONDS", raw)
        assert get_moomoo_connect_timeout() == DEFAULT_CONNECT_TIMEOUT_SECONDS

    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "nan", "inf"])
    def test_不正値でもNoneを渡さない(self, monkeypatch, raw):
        monkeypatch.setenv("MOOMOO_CONNECT_TIMEOUT_SECONDS", raw)
        ctx = MagicMock()
        apply_connect_timeout(ctx)
        (called,) = ctx.set_sync_query_connect_timeout.call_args.args
        assert called is not None
        assert called > 0


def _parse_child_result(done: subprocess.CompletedProcess[str]) -> object:
    """子プロセスの stdout から ``RESULT:`` 行を探して JSON として返す。

    futu はコンソールログを ``sys.stdout`` に吐く（``futu/common/ft_logger.py``）。
    子が結果を print してから ``os._exit`` するまでに再接続 Timer が 1 行書く窓があるため、
    最終行を決め打ちするとときどき落ちて flake 扱いで無視される。前置で行を特定する。
    """
    import json

    for line in done.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:") :])
    pytest.fail(
        "子プロセスの出力に RESULT: 行がありません。\n"
        f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    )


class TestMarketStateReturnsWithinBoundedTime:
    """OpenD が居なくても get_market_state が有界時間で戻ること。

    修正前は futu のコンストラクタが出口のない while True で再接続を繰り返す。
    **子プロセスで実行する**のは、スレッド＋join(timeout) だと回帰時にテスト自体は
    FAIL できても、futu が残す非 daemon スレッド（CallbackExecutor）のせいで
    pytest プロセスが終了せず CI ジョブが固まるため。子プロセスなら
    subprocess.run(timeout) が確実に殺して、回帰を「ハング」でなく「FAIL」にできる。
    """

    _CHILD = """
import json, os, socket, sys

from alpha_strike.services.market_state import MoomooMarketStateProvider

# 誰も listen していないポートを得る（connect は即 refused になる）
probe = socket.socket()
probe.bind(("127.0.0.1", 0))
closed_port = probe.getsockname()[1]
probe.close()

provider = MoomooMarketStateProvider(host="127.0.0.1", port=closed_port)
print("RESULT:" + json.dumps(provider.get_market_state(["US.TQQQ"])), flush=True)
os._exit(0)
"""

    def test_OpenD不在でも有界時間で戻る(self, monkeypatch):
        import subprocess
        import sys

        monkeypatch.setenv("MOOMOO_CONNECT_TIMEOUT_SECONDS", "2")
        try:
            done = subprocess.run(
                [sys.executable, "-c", self._CHILD],
                capture_output=True,
                text=True,
                timeout=20.0,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "get_market_state が 20 秒以内に戻りませんでした。"
                "futu コンストラクタの無限リトライに掛かっています"
            )

        assert done.returncode == 0, done.stderr
        # 取得失敗は空 dict で返す（is_market_open が None に倒す）
        assert _parse_child_result(done) == {}


class _FakeContextBase:
    """futu コンテキストの最小の偽基底クラス。

    MagicMock はサブクラス化できないため、ファクトリの検証には実クラスが要る。
    SDK の該当部分だけを模す。

    **コンストラクタが ``_init_connect_sync`` を呼ぶところまで模す**のが要点。本物は
    成功するまで ``while True`` で呼び続ける。ここでは OpenD 死亡を表す失敗値
    (``CONNECT_FAILED``) を返し、最大 3 周で打ち切る（回帰時にテストが固まらないため）。
    有界化サブクラスが効いていれば構築中の呼び出しは 0 回、効いていなければ 3 回になる。
    """

    CONNECT_FAILED = 1

    instances: list["_FakeContextBase"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.connect_attempts = 0
        self.waited: int | None = None
        self.timeout: float | None = None
        self.closed = False
        _FakeContextBase.instances.append(self)
        for _ in range(3):  # SDK は while True。固まらないよう上限を付ける
            if self._init_connect_sync() == 0:
                break

    def _init_connect_sync(self):  # noqa: ANN202
        self.connect_attempts += 1
        return self.CONNECT_FAILED

    def _wait_reconnect(self, wait_reconnect_interval: int = 6) -> None:
        self.waited = wait_reconnect_interval

    def set_sync_query_connect_timeout(self, timeout: float) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


class TestBoundedContextClass:
    """トレード系コンテキストの有界化。

    OpenSecTradeContext は is_async_connect を受け取らないため、サブクラスで SDK の
    非同期接続分岐を再現している。親コンストラクタの while True を 1 周で抜けさせる
    仕掛けが効いていることを、構築後の状態で固定する。
    """

    def setup_method(self):
        _FakeContextBase.instances.clear()

    def test_基底クラスのサブクラスを返す(self):
        cls = bounded_context_class(_FakeContextBase, ret_ok=0)
        assert issubclass(cls, _FakeContextBase)

    def test_構築後にwait_reconnectへ渡す(self):
        """SDK の is_async_connect=True 分岐と同じ経路へ渡していること。"""
        cls = bounded_context_class(_FakeContextBase, ret_ok=0)
        ctx = cls(host="127.0.0.1", port=11111)
        assert ctx.waited == 0

    def test_構築中は接続を遅延させる(self):
        """構築中の _init_connect_sync は実接続を呼ばない（while True を 1 周で抜ける）。"""
        cls = bounded_context_class(_FakeContextBase, ret_ok=0)
        ctx = cls(host="127.0.0.1", port=11111)
        assert ctx.connect_attempts == 0

    def test_構築後は実接続に委譲する(self):
        """構築が終われば本来の接続処理に戻る（再接続タイマーが呼ぶ経路）。"""
        cls = bounded_context_class(_FakeContextBase, ret_ok=0)
        ctx = cls(host="127.0.0.1", port=11111)
        assert ctx._init_connect_sync() == _FakeContextBase.CONNECT_FAILED
        assert ctx.connect_attempts == 1

    def test_引数を基底クラスへ渡す(self):
        cls = bounded_context_class(_FakeContextBase, ret_ok=0)
        ctx = cls(filter_trdmarket="US", host="h", port=1, security_firm="NONE")
        assert ctx.kwargs == {
            "filter_trdmarket": "US",
            "host": "h",
            "port": 1,
            "security_firm": "NONE",
        }


class TestPrivateMemberGuard:
    """SDK の private メンバが消えたら黙って無限ハングに戻らないこと。

    override が呼ばれなくなると親の while True が復活する。これはこの修正が潰そうと
    している「無言の無限ハング」そのものなので、例外で落とす。
    """

    def test_init_connect_syncが無ければRuntimeError(self):
        class _NoInit:
            def _wait_reconnect(self, n: int = 6) -> None: ...

        with pytest.raises(RuntimeError, match="_init_connect_sync"):
            bounded_context_class(_NoInit, ret_ok=0)

    def test_wait_reconnectが無ければRuntimeError(self):
        class _NoWait:
            def _init_connect_sync(self):  # noqa: ANN202
                return 0

        with pytest.raises(RuntimeError, match="_wait_reconnect"):
            bounded_context_class(_NoWait, ret_ok=0)

    def test_実futuのOpenSecTradeContextは通る(self):
        """実 SDK に対する検知器。futu 更新でここが落ちたら有界化が壊れている。"""
        import futu

        bounded_context_class(futu.OpenSecTradeContext, ret_ok=futu.RET_OK)


class TestAllContextSitesAreBounded:
    """コンテキスト生成の 3 箇所すべてが有界化されていること。

    1 箇所でも素のコンストラクタに戻ると、そこだけで全体が凍結する。

    **モックの差し替え方が 2 通りある。** market_state / status_service は関数内の遅延
    import なので sys.modules を差し替える。moomoo_handler は try/except ImportError の
    モジュールレベル import で実 futu への参照を既に保持しているため、
    monkeypatch.setattr(moomoo_handler, "futu", fake) を使う。
    """

    def _fake_futu(self):
        fake = MagicMock()
        fake.RET_OK = 0
        fake.OpenSecTradeContext = _FakeContextBase
        return fake

    def test_market_state(self, monkeypatch):
        """OpenQuoteContext は is_async_connect を受け取るのでサブクラス不要。"""
        import sys

        from alpha_strike.services.market_state import MoomooMarketStateProvider

        fake = self._fake_futu()
        fake.OpenQuoteContext.return_value.get_market_state.return_value = (1, None)
        monkeypatch.setitem(sys.modules, "futu", fake)

        MoomooMarketStateProvider(host="127.0.0.1", port=11111).get_market_state(
            ["US.TQQQ"]
        )

        assert fake.OpenQuoteContext.call_args.kwargs["is_async_connect"] is True
        fake.OpenQuoteContext.return_value.set_sync_query_connect_timeout.assert_called_once()

    def test_moomoo_handler(self, monkeypatch):
        from alpha_strike.handlers import moomoo_handler

        _FakeContextBase.instances.clear()
        fake = self._fake_futu()
        # モジュールレベル import 済みなので setattr で差し替える
        monkeypatch.setattr(moomoo_handler, "futu", fake)

        ctx = moomoo_handler._get_trade_context("US", "127.0.0.1", 11111)

        assert isinstance(ctx, _FakeContextBase), "有界化サブクラスを経由していない"
        assert ctx.waited == 0, "_wait_reconnect(0) に渡っていない"
        assert ctx.timeout is not None and ctx.timeout > 0

    def test_moomoo_handler_タイムアウト設定に失敗したらcloseして例外を伝播する(
        self, monkeypatch
    ):
        """apply_connect_timeout が投げたら ctx を閉じ、例外は握り潰さず伝播させる。

        async 接続はコンストラクタ時点で非 daemon の再接続タイマーを起動する。設定に
        失敗したまま呼び出し側へ返すと誰も close() せずタイマーが漏れる。一方で例外を
        握り潰すと、タイムアウト未設定のコンテキストで発注して無限ハングに戻る。
        """
        from alpha_strike.handlers import moomoo_handler

        class _TimeoutSetFails(_FakeContextBase):
            def set_sync_query_connect_timeout(self, timeout: float) -> None:
                raise RuntimeError("timeout 設定に失敗")

        _FakeContextBase.instances.clear()
        fake = self._fake_futu()
        fake.OpenSecTradeContext = _TimeoutSetFails
        monkeypatch.setattr(moomoo_handler, "futu", fake)

        with pytest.raises(RuntimeError, match="timeout 設定に失敗"):
            moomoo_handler._get_trade_context("US", "127.0.0.1", 11111)

        (ctx,) = _FakeContextBase.instances
        assert ctx.closed, "失敗時に close() せず再接続タイマーを漏らしている"

    def test_status_service(self, monkeypatch):
        import sys

        from alpha_strike.services.status_service import (
            AccountSummary,
            MoomooStatusProvider,
        )

        _FakeContextBase.instances.clear()
        fake = self._fake_futu()
        monkeypatch.setitem(sys.modules, "futu", fake)

        provider = MoomooStatusProvider(host="127.0.0.1", port=11111)
        # 3 つのクエリは本テストの対象外。Fake の組み立てを避けるため潰す
        monkeypatch.setattr(provider, "_query_account", lambda *a, **k: AccountSummary())
        monkeypatch.setattr(provider, "_query_positions", lambda *a, **k: [])
        monkeypatch.setattr(provider, "_query_orders", lambda *a, **k: [])

        provider.get_status(trd_env="SIMULATE")

        assert _FakeContextBase.instances, "有界化サブクラスを経由していない"
        ctx = _FakeContextBase.instances[-1]
        assert ctx.waited == 0
        assert ctx.timeout is not None and ctx.timeout > 0
        assert ctx.closed, "再接続タイマーを残さないよう finally で close すること"


class TestTradeContextReturnsWithinBoundedTime:
    """トレード系コンテキストが、OpenD が居なくても実 futu で有界時間に戻ること。

    Step 7 と同じ子プロセス方式（スレッド方式は回帰時に pytest が終了しない）。
    **これが SDK 更新時の検知器になる**: private メンバが残っていても意味が変われば
    ここで落ちる。
    """

    _CHILD = """
import json, os, socket

import futu

from alpha_strike.services.moomoo_connect import (
    apply_connect_timeout,
    bounded_context_class,
)

# 誰も listen していないポートを得る（connect は即 refused になる）
probe = socket.socket()
probe.bind(("127.0.0.1", 0))
closed_port = probe.getsockname()[1]
probe.close()

ctx_cls = bounded_context_class(futu.OpenSecTradeContext, ret_ok=futu.RET_OK)
ctx = ctx_cls(
    filter_trdmarket=futu.TrdMarket.US,
    host="127.0.0.1",
    port=closed_port,
    security_firm=futu.SecurityFirm.NONE,
)
try:
    apply_connect_timeout(ctx)
    ret, _ = ctx.accinfo_query(trd_env=futu.TrdEnv.SIMULATE)
finally:
    ctx.close()
print("RESULT:" + json.dumps({"ret": ret, "ret_ok": futu.RET_OK}), flush=True)
os._exit(0)
"""

    def test_OpenD不在でも有界時間で失敗を返す(self, monkeypatch):
        import subprocess
        import sys

        monkeypatch.setenv("MOOMOO_CONNECT_TIMEOUT_SECONDS", "2")
        try:
            done = subprocess.run(
                [sys.executable, "-c", self._CHILD],
                capture_output=True,
                text=True,
                timeout=20.0,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "トレード系コンテキストの accinfo_query が 20 秒以内に戻りませんでした。"
                "futu コンストラクタの無限リトライに掛かっています"
            )

        assert done.returncode == 0, done.stderr
        out = _parse_child_result(done)
        # 接続できていないので RET_OK ではなく失敗が返る（下流の ret != RET_OK 経路）
        assert out["ret"] != out["ret_ok"]
