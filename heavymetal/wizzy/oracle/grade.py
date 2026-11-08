"""Grade calls by fixed rules, Prophecies-style: frozen baseline, maturity, data stamped after the call.

Rules (spec feature 2, step 4-5):
- Horizons: 24 h, 7 d (168 h) and 30 d (720 h) after the post.
- Baseline: the price Wizzy froze when it witnessed the call (live pair price); otherwise the open of the hourly
  candle containing the post time. No baseline means the call is void at every horizon.
- Value at a horizon: the close of the last hourly candle that starts after the post and before the horizon.
  A drained or dead pool is graded at its last traded price (a rug is a real loss). The call is void only when no
  market data exists at all. A fetch error leaves the horizon pending; it is retried, never counted.
- Hit: value above the baseline. Return: value / baseline - 1, in percent.
- Rug: a graded return of -90% or worse, or no trades in the 24 h before the horizon after a fall of 50% or more.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

HORIZONS_H = (24, 168, 720)
HOUR = 3600
RUG_RETURN_PCT = -90.0
DRAIN_QUIET_S = 24 * HOUR
DRAIN_FALL_PCT = -50.0
VOID_AFTER_S = 48 * HOUR  # keep retrying fetch errors this long after maturity, then void
MAX_FETCH_PER_RUN = 20  # GeckoTerminal is shared and rate limited


@dataclass
class Grade:
    status: str  # pending | graded | void
    outcome: str | None = None  # hit | miss
    value: float | None = None
    ret_pct: float | None = None
    value_ts: float | None = None
    drained: bool = False
    reason: str | None = None

    @property
    def rug(self) -> bool:
        return self.status == "graded" and (
            (self.ret_pct is not None and self.ret_pct <= RUG_RETURN_PCT)
            or (self.drained and (self.ret_pct or 0) <= DRAIN_FALL_PCT)
        )


def baseline_from_candles(post_ts: float, candles: list[dict]) -> float | None:
    """Open of the hourly candle containing the post time (price at the post, before anything after it)."""
    for c in candles:
        if c["ts"] <= post_ts < c["ts"] + HOUR and c.get("open"):
            return float(c["open"])
    return None


def grade(baseline: float | None, post_ts: float, horizon_h: int, candles: list[dict] | None, now: float) -> Grade:
    """Pure grading rule for one call at one horizon. candles=None means the fetch failed."""
    horizon_ts = post_ts + horizon_h * HOUR
    if now < horizon_ts:
        return Grade("pending", reason="not matured")
    if not baseline or baseline <= 0:
        return Grade("void", reason="no baseline price at post time")
    if candles is None:
        if now - horizon_ts > VOID_AFTER_S:
            return Grade("void", reason="price source unavailable")
        return Grade("pending", reason="price source unavailable, will retry")
    if not candles:
        return Grade("void", reason="no market data")
    after = [c for c in candles if post_ts < c["ts"] < horizon_ts and c.get("close")]
    if after:
        last = after[-1]
    else:
        before = [c for c in candles if c["ts"] <= post_ts and c.get("close")]
        if not before:
            return Grade("void", reason="no market data")
        last = before[-1]  # no trade since the post: the last traded price stands
    value = float(last["close"])
    ret = round((value / baseline - 1) * 100, 1)
    drained = horizon_ts - last["ts"] > DRAIN_QUIET_S
    return Grade(
        "graded",
        "hit" if value > baseline else "miss",
        value,
        ret,
        float(last["ts"]),
        drained,
        "last traded price (no trades near the horizon)" if drained else None,
    )


class PriceHistory(Protocol):
    name: str

    async def pool(self, mint: str) -> dict | None: ...  # {"pool", "liquidity_usd", "price_usd"} or None

    async def candles(self, pool: str, before_ts: float, limit: int) -> list[dict]: ...

    def url(self, pool: str) -> str: ...


class GeckoHistory:
    """Hourly OHLCV from GeckoTerminal (public API, no key): the candle source for baselines and grades."""

    name = "geckoterminal"

    def __init__(self, gecko):
        self.gecko = gecko

    async def pool(self, mint: str) -> dict | None:
        pools = await self.gecko.token_pools(mint)
        return pools[0] if pools else None

    async def candles(self, pool: str, before_ts: float, limit: int) -> list[dict]:
        return await self.gecko.ohlcv(pool, timeframe="hour", aggregate=1, limit=limit, before_ts=int(before_ts))

    def url(self, pool: str) -> str:
        return f"https://www.geckoterminal.com/solana/pools/{pool}"


async def resolve_mint(env, addr: str) -> tuple[str | None, dict | None]:
    """A posted address is a mint, or a pair (DexScreener links) whose base token is the mint."""
    try:
        pairs = await env.dex.token_pairs(addr)
    except Exception:  # noqa: BLE001
        return None, None
    if pairs:
        best = max(pairs, key=lambda p: p.get("liquidity_usd") or 0)
        return addr, best
    try:
        p = await env.dex.pair(addr)
    except Exception:  # noqa: BLE001
        p = None
    if p and (p.get("base") or {}).get("address"):
        return p["base"]["address"], p
    return None, None


async def freeze_baseline(ctx, call_id: int, now: float | None = None) -> str:
    """Witnessed calls: freeze the live price within minutes of the post, like a Prophecy baseline."""
    row = ctx.wdb.one("SELECT * FROM kol_calls WHERE id=?", (call_id,))
    if row is None or row["status"] != "counted" or row["baseline_price"] is not None:
        return "skip"
    now = now or time.time()
    mint, pair = await resolve_mint(ctx.env, row["addr"])
    if mint is None:
        ctx.wdb._x("UPDATE kol_calls SET status='excluded', reason='not a traded token' WHERE id=?", (call_id,))
        return "excluded"
    price = (pair or {}).get("price_usd")
    if row["source"] == "witnessed" and price and now - row["ts"] <= 3600:
        ctx.wdb._x(
            "UPDATE kol_calls SET mint=?, baseline_price=?, baseline_liq=?, baseline_ts=?, baseline_source=? WHERE id=?",
            (mint, float(price), pair.get("liquidity_usd"), now, "dexscreener pair price at witness time", call_id),
        )
        return "frozen"
    ctx.wdb._x("UPDATE kol_calls SET mint=? WHERE id=?", (mint, call_id))
    return "deferred"  # backfilled or late: baseline comes from the candle containing the post


def _store(wdb, call_id: int, h: int, g: Grade, source: str | None) -> None:
    wdb._x(
        "INSERT INTO kol_grades(call_id, horizon_h, status, outcome, value_price, ret_pct, drained, value_ts, source, "
        "reason, graded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(call_id, horizon_h) DO UPDATE SET "
        "status=excluded.status, outcome=excluded.outcome, value_price=excluded.value_price, "
        "ret_pct=excluded.ret_pct, drained=excluded.drained, value_ts=excluded.value_ts, source=excluded.source, "
        "reason=excluded.reason, graded_at=excluded.graded_at",
        (
            call_id,
            h,
            g.status,
            g.outcome,
            g.value,
            g.ret_pct,
            int(g.drained),
            g.value_ts,
            source,
            g.reason,
            time.time(),
        ),
    )


async def grade_calls(ctx, rows: list, history: PriceHistory | None = None, now: float | None = None) -> int:
    """Grade every matured, ungraded horizon of these calls. Returns how many grades were stored."""
    now = now or time.time()
    history = history or GeckoHistory(ctx.env.gecko)
    stored, fetched = 0, 0
    for row in rows:
        if row["status"] != "counted":
            continue
        done = {
            r["horizon_h"]
            for r in ctx.wdb.all("SELECT horizon_h FROM kol_grades WHERE call_id=? AND status!='pending'", (row["id"],))
        }
        todo = [h for h in HORIZONS_H if h not in done and now >= row["ts"] + h * HOUR]
        if not todo and row["mint"] is not None:
            continue
        if row["mint"] is None and row["baseline_price"] is None:
            if await freeze_baseline(ctx, row["id"], now) == "excluded":
                continue
            row = ctx.wdb.one("SELECT * FROM kol_calls WHERE id=?", (row["id"],))
        if not todo:
            continue
        if fetched >= MAX_FETCH_PER_RUN:
            break
        fetched += 1
        candles, pool = await _candles_for(ctx, row, history, min(now, row["ts"] + max(todo) * HOUR + HOUR))
        baseline = row["baseline_price"]
        if baseline is None and candles:
            baseline = baseline_from_candles(row["ts"], candles)
            if baseline:
                ctx.wdb._x(
                    "UPDATE kol_calls SET baseline_price=?, baseline_ts=?, baseline_source=?, pool=? WHERE id=?",
                    (baseline, row["ts"], f"{history.name} hourly candle open at post time", pool, row["id"]),
                )
        for h in todo:
            g = grade(baseline, row["ts"], h, candles, now)
            if g.status != "pending":
                _store(ctx.wdb, row["id"], h, g, history.url(pool) if pool else None)
                stored += 1
    return stored


async def _candles_for(ctx, row, history: PriceHistory, before_ts: float) -> tuple[list[dict] | None, str | None]:
    try:
        pool = row["pool"]
        if not pool:
            info = await history.pool(row["mint"]) if row["mint"] else None
            if not info or not info.get("pool"):
                return [], None  # no market ever found for this mint
            pool = info["pool"]
            ctx.wdb._x("UPDATE kol_calls SET pool=? WHERE id=?", (pool, row["id"]))
        hours = int((before_ts - row["ts"]) // HOUR) + 3
        return await history.candles(pool, before_ts, min(1000, hours)), pool
    except Exception:  # noqa: BLE001 - network or API error: pending, retried later
        return None, row["pool"]
