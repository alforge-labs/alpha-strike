"""市場休場（土日）を除外した実効経過時間の計算。

carry-over の lookback (#89) と signal watchdog の途絶判定が共有する。暦時間で測ると
金曜クローズ後のシグナルは土日だけで 48h を超え、月曜寄付前に stale 判定されてしまう。

祝日は考慮しない（YAGNI）。米国市場カレンダーを持ち込むほどの精度は不要で、土日除外なら
「祝日 1 日を挟んだ最大 53 実効時間」まで吸収でき、運用上はこれで足りる。
"""

from __future__ import annotations

from datetime import datetime, timedelta


def weekend_hours_between(start: datetime, end: datetime) -> float:
    """``start``〜``end`` に含まれる土日(市場休場)の時間数を返す。"""
    if end <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur < end:
        day_end = min(
            end,
            (cur + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
        )
        if cur.weekday() >= 5:  # 5=土, 6=日
            total += (day_end - cur).total_seconds() / 3600.0
        cur = day_end
    return total


def effective_hours_between(start: datetime, end: datetime) -> float:
    """土日を除外した実効経過時間。``end <= start`` なら 0.0。"""
    if end <= start:
        return 0.0
    elapsed = (end - start).total_seconds() / 3600.0
    return elapsed - weekend_hours_between(start, end)
