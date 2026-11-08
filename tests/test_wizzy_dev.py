"""Dev's past coins: creator resolution, outcome rules, sigils, funding traces, omen interaction and the /dev reading.

All offline: scripted Helius / RPC / RugCheck / DexScreener fakes.
"""

from __future__ import annotations

import asyncio

import pytest
from wizzy_fakes import NEW_MINT, FakeDex, FakeRPC, FakeRug, ScriptedWriter, make_ctx, pair, reader_env

from heavymetal.wizzy import dev, dev_reading, guard
from heavymetal.wizzy.build import build_token_facts
from heavymetal.wizzy.dev import DevCache, classify, dev_facts, sigil_for
from heavymetal.wizzy.facts import DevFacts, Omen, PastCoin
from heavymetal.wizzy.omen import apply_dev
from heavymetal.wizzy.resolve import Subject as Resolved
from heavymetal.wizzy.router import rules

NOW = 1_800_000_000.0
DAY = 86400.0
CREATOR = "Cr8tor1111111111111111111111111111111111111"
RELAYER = "Re1ayer111111111111111111111111111111111111"
FUNDER = "FundA11111111111111111111111111111111111111"
FUNDER2 = "FundB11111111111111111111111111111111111111"
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def mint(i: int) -> str:
    return f"Past{i:02d}" + "1" * 34 + "pump"


@pytest.fixture(autouse=True)
def fresh_dev_cache(monkeypatch):
    monkeypatch.setattr(dev, "MEMORY_CACHE", DevCache())


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------- fakes
def create_htx(m: str, ts: float, user: str = CREATOR, payer: str | None = None, sig: str | None = None) -> dict:
    accts = [m, "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM", "curve", "acurve", "global", "mpl", "meta", user]
    return {
        "signature": sig or f"sig-{m[:6]}",
        "timestamp": ts,
        "type": "CREATE",
        "feePayer": payer or user,
        "instructions": [{"programId": dev.PUMP_FUN, "accounts": accts}],
        "tokenTransfers": [{"mint": m, "toUserAccount": user, "tokenAmount": 1000}],
    }


def transfer_htx(src: str, dst: str, sol: float, ts: float, sig: str) -> dict:
    return {
        "signature": sig,
        "timestamp": ts,
        "type": "TRANSFER",
        "nativeTransfers": [{"fromUserAccount": src, "toUserAccount": dst, "amount": int(sol * 1e9)}],
    }


class FakeHelius:
    enabled = True

    def __init__(self, creates=None, transfers=None, swaps=None, mint_creates=None):
        self.creates = creates or {}  # address -> [htx] newest first
        self.transfers = transfers or {}
        self.mint_creates = mint_creates or {}
        self.swaps = swaps or []
        self.calls: list[tuple] = []

    async def history(self, address, *, tx_type=None, before=None, limit=100):
        self.calls.append((address, tx_type, before))
        src = self.mint_creates if address in self.mint_creates else self.creates
        rows = (src if tx_type == "CREATE" else self.transfers).get(address, [])
        if before:  # like Helius: only txs older than `before` (looked up across every scripted tx)
            every = [t for d in (self.creates, self.transfers, self.mint_creates) for v in d.values() for t in v]
            ts = next((t["timestamp"] for t in every if t["signature"] == before), None)
            rows = [t for t in rows if ts is not None and t["timestamp"] < ts]
        return rows[:limit]

    async def wallet_swaps(self, address, limit=100):
        return self.swaps


class DevRPC(FakeRPC):
    def __init__(self, txs=None, holdings=None, **kw):
        super().__init__(**kw)
        self.txs = txs or {}
        self.holdings = holdings if holdings is not None else []

    async def signatures(self, address, limit=20):
        return [{"signature": f"sig-{address[:6]}"}]

    async def transaction(self, sig):
        if sig not in self.txs:
            raise RuntimeError("tx unavailable")
        return self.txs[sig]

    async def token_accounts(self, addr):
        return self.holdings


class DevRug(FakeRug):
    def __init__(self, creator=CREATOR, rugged=None, **kw):
        super().__init__(**kw)
        self.creator = creator
        self.rugged = rugged or {}

    async def report(self, m):
        return {"total_holders": 100, "top10_pct": 30.0, "creator": self.creator, "rugged": self.rugged.get(m, False)}


def rpc_create_tx(m: str, user: str = CREATOR, payer: str | None = None, ts: float = NOW - DAY) -> dict:
    payer = payer or user
    keys = [{"pubkey": payer, "signer": True}, {"pubkey": m, "signer": True}]
    if user != payer:
        keys.append({"pubkey": user, "signer": True})
    accts = [m, "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM", "curve", "acurve", "global", "mpl", "meta", user]
    return {
        "blockTime": ts,
        "transaction": {
            "message": {"accountKeys": keys, "instructions": [{"programId": dev.PUMP_FUN, "accounts": accts}]}
        },
        "meta": {},
    }


def dex_pair(m, *, dex="pumpswap", liq=20_000.0, vol=5_000.0, trades=100, symbol="OLD"):
    p = pair(mint=m, symbol=symbol, liq=liq)
    p.update({"dex": dex, "volume": {"h24": vol}, "txns_h24": {"buys": trades // 2, "sells": trades - trades // 2}})
    return p


def scenario(
    tmp_path, *, past, rugged=None, pairs=None, transfers=None, holdings=None, rug_creator=CREATOR, mint_ts=None
):
    """An env where NEW_MINT was created by CREATOR at NOW-1d and `past` = [(mint, ts)] were created earlier."""
    mint_ts = mint_ts or NOW - DAY
    rows = [create_htx(NEW_MINT, mint_ts, sig="sig-new")] + [create_htx(m, ts) for m, ts in past]
    helius = FakeHelius(creates={CREATOR: rows}, transfers=transfers or {})
    env = reader_env(
        tmp_path,
        rpc=DevRPC(txs={"sig-new": rpc_create_tx(NEW_MINT, ts=mint_ts)}, holdings=holdings),
        rug=DevRug(creator=rug_creator, rugged=rugged),
        dex=FakeDex(pairs=pairs if pairs is not None else []),
    )
    env.helius = helius
    return env


# ---------------------------------------------------------------------- 3. outcome rules
def test_rugcheck_rugged_wins():
    assert classify(now=NOW, created_at=NOW - DAY, pair=dex_pair("m"), rugged=True)[0] == "rugged"


def test_liquidity_crash_with_creator_selling_is_rugged():
    pts = [(NOW - 10 * DAY, 50_000.0), (NOW - 9 * DAY, 4_000.0)]
    out = classify(now=NOW, created_at=NOW - 11 * DAY, pair=None, liq_points=pts, sold_share=0.6)
    assert out[0] == "rugged"


@pytest.mark.parametrize(
    "pts,sold",
    [
        ([(NOW - 10 * DAY, 50_000.0), (NOW - 9 * DAY, 4_000.0)], 0.4),  # creator sold too little
        ([(NOW - 10 * DAY, 50_000.0), (NOW - 6 * DAY, 4_000.0)], 0.9),  # drop slower than 72 h
        ([(NOW - 10 * DAY, 50_000.0), (NOW - 9 * DAY, 6_000.0)], 0.9),  # only an 88% drop
        ([(NOW - 10 * DAY, 50_000.0), (NOW - 9 * DAY, 4_000.0)], None),  # sells unknown
    ],
)
def test_liquidity_crash_needs_every_condition(pts, sold):
    assert classify(now=NOW, created_at=NOW - 11 * DAY, pair=None, liq_points=pts, sold_share=sold)[0] == "unknown"


@pytest.mark.parametrize("dex", ["pumpswap", "raydium"])
def test_graduated_by_pool(dex):
    assert classify(now=NOW, created_at=NOW - DAY, pair=dex_pair("m", dex=dex, liq=10.0, vol=0))[0] == "graduated"


def test_graduated_by_bonding_curve_complete():
    assert classify(now=NOW, created_at=NOW, pair=None, bonding_complete=True)[0] == "graduated"


def test_alive_thresholds():
    ok = dex_pair("m", dex="meteora", liq=10_000.0, vol=1_000.0, trades=20)
    assert classify(now=NOW, created_at=NOW - 30 * DAY, pair=ok)[0] == "alive"
    thin = dex_pair("m", dex="meteora", liq=10_000.0, vol=1_000.0, trades=19)  # a handful of trades isn't real volume
    assert classify(now=NOW, created_at=NOW - 30 * DAY, pair=thin)[0] == "unknown"
    low = dex_pair("m", dex="meteora", liq=9_999.0, vol=50_000.0, trades=500)
    assert classify(now=NOW, created_at=NOW - 30 * DAY, pair=low)[0] == "unknown"


def test_dead_needs_a_week():
    quiet = dex_pair("m", dex="meteora", liq=999.0, vol=99.0, trades=1)
    assert classify(now=NOW, created_at=NOW - 7 * DAY, pair=quiet)[0] == "dead"
    assert classify(now=NOW, created_at=NOW - 6 * DAY, pair=quiet)[0] == "unknown"
    assert classify(now=NOW, created_at=None, pair=quiet)[0] == "unknown"


def test_bonding_curve_pairs_without_liquidity():
    curve = dex_pair("m", dex="pumpfun", vol=10.0)
    curve["liquidity_usd"] = None
    assert classify(now=NOW, created_at=NOW - 8 * DAY, pair=curve)[0] == "dead"
    assert classify(now=NOW, created_at=NOW - 2 * DAY, pair=curve)[0] == "unknown"


def test_missing_data_is_unknown():
    assert classify(now=NOW, created_at=NOW - 30 * DAY, pair=None)[0] == "unknown"
    p = dex_pair("m", dex="meteora")
    p["liquidity_usd"] = None
    assert classify(now=NOW, created_at=NOW - 30 * DAY, pair=p)[0] == "unknown"


# ---------------------------------------------------------------------- 5. sigils
def coins(*spec):
    return [PastCoin(mint=f"m{i}", outcome=o, createdAt=NOW - age * DAY) for i, (o, age) in enumerate(spec)]


def test_sigil_fresh_hand_when_nothing_readable():
    assert sigil_for([], NOW)[0] == "fresh hand"
    assert sigil_for(coins(("unknown", 1), ("unknown", 2)), NOW)[0] == "fresh hand"


def test_sigil_serial_summoner_thresholds():
    assert sigil_for(coins(*[("rugged", 5)] * 3, *[("dead", 5)] * 3), NOW)[:2] == ("serial summoner", 3)
    # 3 rugs but under half of readable launches
    assert sigil_for(coins(*[("rugged", 5)] * 3, *[("dead", 5)] * 4), NOW)[0] == "mixed sigils"
    # half or more, but only 2 rugs
    assert sigil_for(coins(("rugged", 5), ("rugged", 5)), NOW)[0] == "mixed sigils"
    # rugs older than 90 days don't make a serial summoner
    assert sigil_for(coins(*[("rugged", 120)] * 5), NOW)[0] == "mixed sigils"


def test_unknown_never_counts_against_the_dev():
    sig, rugs, known, share = sigil_for(coins(*[("rugged", 5)] * 3, *[("unknown", 5)] * 20), NOW)
    assert (sig, rugs, known, share) == ("serial summoner", 3, 3, 1.0)


def test_sigil_clean_hands():
    assert sigil_for(coins(("graduated", 3), ("dead", 30), ("unknown", 1)), NOW)[0] == "clean hands"


# ---------------------------------------------------------------------- omen interaction
def devf(sigil, status="resolved"):
    return DevFacts(creator=CREATOR, status=status, sigil=sigil, rugs90d=4, known90d=5)


def test_serial_summoner_pulls_to_cursed_with_reason():
    o = apply_dev(Omen(kind="blessed", reasons=["LP locked"]), NEW_MINT, devf("serial summoner"))
    assert o.kind == "cursed" and o.reasons[0].startswith("Dev history: serial summoner")


def test_mixed_caps_at_murky_but_never_raises():
    assert apply_dev(Omen(kind="blessed"), NEW_MINT, devf("mixed sigils")).kind == "murky"
    assert apply_dev(Omen(kind="hexed"), NEW_MINT, devf("mixed sigils")).kind == "hexed"
    assert apply_dev(Omen(kind="cursed"), NEW_MINT, devf("clean hands")).kind == "cursed"
    assert apply_dev(Omen(kind="murky"), NEW_MINT, devf("fresh hand")).kind == "murky"


def test_omen_exemptions():
    serial = devf("serial summoner")
    assert apply_dev(Omen(kind="blessed"), BONK, serial).kind == "blessed"  # established
    wormhole = "BCD75RNBHrJJpW4dXVagL5mPjzRLnVZq4YirJdjEYMV7"
    assert apply_dev(Omen(kind="blessed"), NEW_MINT, serial, wormhole).kind == "blessed"  # program authority
    assert apply_dev(Omen(kind="blessed"), NEW_MINT, devf("serial summoner", "single_source")).kind == "blessed"


# ---------------------------------------------------------------------- 1. creator
def test_launchpad_user_not_relayer_fee_payer():
    tx = rpc_create_tx(NEW_MINT, user=CREATOR, payer=RELAYER)
    assert dev.signer_from_tx(tx, NEW_MINT) == (CREATOR, "pump.fun")


def test_plain_spl_create_uses_fee_payer_and_other_txs_are_ignored():
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": CREATOR, "signer": True}],
                "instructions": [{"parsed": {"type": "initializeMint2", "info": {"mint": NEW_MINT}}}],
            }
        }
    }
    assert dev.signer_from_tx(tx, NEW_MINT) == (CREATOR, None)
    assert dev.signer_from_tx(tx, "Other111111111111111111111111111111111111111") == (None, None)


def test_helius_fallback_user_for_create_v2_and_legacy():
    v2 = {
        "feePayer": CREATOR,
        "instructions": [{"programId": dev.PUMP_FUN, "accounts": [NEW_MINT, "a", "b", "c", "g", CREATOR, "x", "y"]}],
    }
    assert dev._helius_user(v2, NEW_MINT) == (CREATOR, "pump.fun")
    legacy = create_htx(NEW_MINT, NOW, user=CREATOR, payer=RELAYER)
    assert dev._helius_user(legacy, NEW_MINT) == (CREATOR, "pump.fun")


def test_creator_resolved_when_sources_agree(tmp_path):
    env = scenario(tmp_path, past=[])
    who = run(dev.resolve_creator(env, NEW_MINT))
    assert who["status"] == "resolved" and who["creator"] == CREATOR and who["launchpad"] == "pump.fun"


def test_creator_unresolved_when_sources_disagree(tmp_path):
    env = scenario(tmp_path, past=[], rug_creator=FUNDER)
    env.helius.mint_creates = {NEW_MINT: [create_htx(NEW_MINT, NOW - DAY, sig="sig-new")]}
    d = run(dev_facts(env, NEW_MINT, now=NOW))
    assert d.status == "unresolved" and d.creator is None and d.sigil is None
    assert "disagree" in d.line


def test_rugcheck_naming_a_launchpad_is_ignored(tmp_path):
    env = scenario(tmp_path, past=[], rug_creator=dev.PUMP_FUN)
    env.helius.mint_creates = {NEW_MINT: [create_htx(NEW_MINT, NOW - DAY, sig="sig-new")]}
    who = run(dev.resolve_creator(env, NEW_MINT))
    assert who["creator"] == CREATOR and who["status"] == "single_source"
    assert any("launchpad or program" in u for u in who["unknowns"])


def test_single_source_when_create_tx_unreadable(tmp_path):
    env = scenario(tmp_path, past=[])
    env.rpc.txs = {}
    env.helius.creates = {}
    who = run(dev.resolve_creator(env, NEW_MINT))
    assert who["status"] == "single_source" and who["sources"] == ["rugcheck report creator"]


# ---------------------------------------------------------------------- 2-5 end to end
def serial_env(tmp_path, **kw):
    past = [(mint(i), NOW - (2 + i) * DAY) for i in range(5)]
    rugged = {mint(0): True, mint(1): True, mint(2): True}
    pairs = [dex_pair(mint(3), dex="meteora", liq=500.0, vol=10.0, trades=2), dex_pair(mint(4), symbol="GRAD")]
    return scenario(tmp_path, past=past, rugged=rugged, pairs=pairs, **kw)


def test_serial_summoner_end_to_end(tmp_path):
    d = run(dev_facts(serial_env(tmp_path), NEW_MINT, now=NOW))
    assert d.status == "resolved" and d.pastCount == 5
    assert d.counts == {"rugged": 3, "dead": 0, "alive": 0, "graduated": 1, "unknown": 1}
    assert d.sigil == "serial summoner" and d.rugs90d == 3 and d.known90d == 4
    assert "serial summoner" in d.line and "3 rugged" in d.line
    grad = next(c for c in d.pastCoins if c.mint == mint(4))
    assert grad.symbol == "GRAD" and grad.link == f"https://dexscreener.com/solana/{mint(4)}"
    assert grad.peakLiquidityUsd == 20_000.0 and grad.ageDays == 6.0


def test_fresh_wallet_is_fresh_hand_not_clean(tmp_path):
    d = run(dev_facts(scenario(tmp_path, past=[]), NEW_MINT, now=NOW))
    assert d.sigil == "fresh hand" and d.pastCount == 0
    assert "no past to read" in d.line and "clean" in d.line  # "not the same as clean"
    assert "clean hands" not in d.line


def test_later_tokens_are_not_past_coins(tmp_path):
    env = scenario(tmp_path, past=[])
    env.helius.creates[CREATOR].insert(0, create_htx(mint(9), NOW - 0.5 * DAY))
    assert run(dev_facts(env, NEW_MINT, now=NOW)).pastCount == 0


def test_window_cap_180_days(tmp_path):
    env = scenario(tmp_path, past=[(mint(1), NOW - 10 * DAY), (mint(2), NOW - 200 * DAY)])
    d = run(dev_facts(env, NEW_MINT, now=NOW))
    assert [c.mint for c in d.pastCoins] == [mint(1)]


def test_cto_creator_no_longer_holds(tmp_path):
    d = run(dev_facts(scenario(tmp_path, past=[], holdings=[]), NEW_MINT, now=NOW))
    assert d.creatorExited is True and "no longer holds" in d.line
    held = [{"mint": NEW_MINT, "ui_amount": 5.0}]
    assert run(dev_facts(scenario(tmp_path, past=[], holdings=held), NEW_MINT, now=NOW)).creatorExited is False


def test_unknown_market_data_never_counts(tmp_path):
    env = serial_env(tmp_path)

    async def boom(mints):
        raise RuntimeError("dexscreener down")

    env.dex.tokens_batch = boom
    env.rug.rugged = {}
    d = run(dev_facts(env, NEW_MINT, now=NOW))
    assert d.counts["unknown"] == 5 and d.counts["rugged"] == 0 and d.sigil == "fresh hand"
    assert "market data for earlier tokens" in d.unknowns


def test_no_helius_key_gives_unknown_not_a_sigil(tmp_path):
    env = scenario(tmp_path, past=[])
    env.helius = None
    d = run(dev_facts(env, NEW_MINT, now=NOW))
    assert d.sigil is None and d.line == "" and "dev history (needs a Helius key)" in d.unknowns


def test_own_create_missing_means_incomplete_history(tmp_path):
    env = scenario(tmp_path, past=[])
    env.helius.creates[CREATOR] = []
    d = run(dev_facts(env, NEW_MINT, now=NOW))
    assert d.sigil is None and "couldn't be read in full" in d.line


def test_incremental_cache_refresh(tmp_path):
    env = scenario(tmp_path, past=[(mint(1), NOW - 3 * DAY)])
    cache = DevCache(make_ctx(tmp_path).wdb, ttl_s=0)  # always stale: refresh incrementally
    run(dev_facts(env, NEW_MINT, now=NOW, light=True, cache=cache))
    env.helius.creates[CREATOR].insert(0, create_htx(mint(2), NOW - 0.1 * DAY))
    env.helius.calls.clear()
    d = run(dev_facts(env, NEW_MINT, now=NOW, light=True, cache=cache))
    assert d.pastCount == 1
    profile, _ = cache.get(CREATOR)
    assert {t["mint"] for t in profile["tokens"]} == {NEW_MINT, mint(1), mint(2)}


# ---------------------------------------------------------------------- 4. funding
def funded_env(tmp_path, hop2=False):
    dep = NOW - DAY
    transfers = {
        CREATOR: [
            transfer_htx(FUNDER, CREATOR, 3.0, dep - 3600, "t1"),
            transfer_htx(BINANCE, CREATOR, 9.0, dep - 7200, "t2"),
            transfer_htx(FUNDER2, CREATOR, 0.01, dep - 3600, "t3"),  # dust: ignored
            transfer_htx(FUNDER2, CREATOR, 5.0, dep - 80 * 3600, "t4"),  # outside 72 h
            # before an earlier rugged deploy (mint(2) at NOW-4d), the same FUNDER sent SOL
            transfer_htx(FUNDER, CREATOR, 1.0, NOW - 4 * DAY - 600, "t5"),
        ]
    }
    if hop2:
        transfers[FUNDER] = [transfer_htx(FUNDER2, FUNDER, 4.0, dep - 5000, "h2")]
    return serial_env(tmp_path, transfers=transfers)


def test_funding_one_hop_stops_at_labels_and_links_rugs(tmp_path):
    d = run(dev_facts(funded_env(tmp_path), NEW_MINT, now=NOW))
    by = {f.wallet: f for f in d.funders}
    assert set(by) == {FUNDER, BINANCE}
    assert by[BINANCE].label == "Binance" and by[BINANCE].fundedRugs == 0
    assert (
        by[FUNDER].solIn == 3.0 and by[FUNDER].fundedRugs == 3 and d.fundersLinkedRugs == 1
    )  # t5 sits in the 72 h before 3 rug deploys
    line = dev_reading.funding_line(d)
    assert line.startswith("Funded by 2 wallet(s)") and "Binance" in line
    assert "same person" not in line


def test_funding_two_hops_for_inner_circle(tmp_path):
    env = funded_env(tmp_path, hop2=True)
    one = run(dev_facts(env, NEW_MINT, now=NOW, hops=1))
    two = run(dev_facts(env, NEW_MINT, now=NOW, hops=2))
    assert FUNDER2 not in {f.wallet for f in one.funders}
    assert any(f.wallet == FUNDER2 and f.hop == 2 for f in two.funders)
    assert not any(c[0] == BINANCE for c in env.helius.calls)  # never traced past a label


# ---------------------------------------------------------------------- /read, /scry and /dev surfaces
def test_read_facts_carry_dev_line_and_lower_omen(tmp_path):
    env = serial_env(tmp_path)
    env.dex.pairs.append(pair())
    facts = run(build_token_facts(env, Resolved("token", NEW_MINT, "MOSS"), light=True, dev_cache=DevCache()))
    assert facts.dev is not None and facts.dev.pastCoins == [] and facts.dev.sigil == "serial summoner"
    assert facts.token.omen.kind == "cursed"
    assert any("serial summoner" in r for r in facts.token.omen.reasons)
    from heavymetal.wizzy.writer import template

    reading, _, body = template(facts)
    assert facts.dev.line in reading and "## The hand behind it" in body


def test_established_mint_skips_dev(tmp_path):
    env = serial_env(tmp_path)
    facts = run(build_token_facts(env, Resolved("token", BONK, "BONK"), light=True, dev_cache=DevCache()))
    assert facts.dev is None


def test_scry_shows_dev_line(tmp_path):
    from heavymetal.wizzy.bots import core

    ctx = make_ctx(tmp_path)
    serial = serial_env(tmp_path)
    serial.dex.pairs.append(pair())
    ctx.env.__dict__.update(rpc=serial.rpc, rug=serial.rug, dex=serial.dex, helius=serial.helius)
    reply = run(core.scry(ctx, NEW_MINT))
    assert "serial summoner" in reply.text and reply.omen == "cursed"


def dev_ctx(tmp_path, writer=None):
    ctx = make_ctx(tmp_path, writer=writer)
    serial = serial_env(tmp_path)
    serial.dex.pairs.append(pair())
    ctx.env.__dict__.update(rpc=serial.rpc, rug=serial.rug, dex=serial.dex, helius=serial.helius)
    return ctx


def test_dev_reading_template_and_scroll(tmp_path):
    ctx = dev_ctx(tmp_path)
    scroll, url, cached = run(dev_reading.dev_read(ctx, NEW_MINT, user_id=None, surface="cli"))
    assert not cached and "/s/" in url and scroll.kind == "dev" and scroll.omen == "cursed"
    assert "The bones remember this hand." in scroll.reading and dev_reading.DISCLAIMER in scroll.reading
    assert "pulls the omen to cursed" in scroll.reading
    assert "## Past coins" in scroll.body_md and scroll.body_md.count("NFA. DYOR") == 1
    assert scroll.body_md.index("## Past coins") < scroll.body_md.index("NFA. DYOR")
    for i in range(5):
        assert f"https://dexscreener.com/solana/{mint(i)}" in scroll.body_md
    assert "peak liquidity seen $20.0K" in scroll.body_md and "**rugged**" in scroll.body_md
    again, _, cached = run(dev_reading.dev_read(ctx, NEW_MINT, user_id=None, surface="cli"))
    assert cached and again.slug == scroll.slug


def test_dev_writer_guard_rejects_invented_values_then_falls_back(tmp_path):
    bad = {
        "reading": "This dev rugged 47 coins and is the same wallet as 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
        "scrollTitle": "x",
        "scrollBodyMarkdown": "visit scam.xyz",
    }
    w = ScriptedWriter([bad])
    scroll, _, _ = run(dev_reading.dev_read(dev_ctx(tmp_path, w), NEW_MINT, user_id=None, surface="cli"))
    assert len(w.calls) == 3  # first try + 2 retries
    assert "47" not in scroll.reading and "The bones remember this hand." in scroll.reading  # the /dev template


def test_dev_writer_output_with_fact_values_passes(tmp_path):
    good = {
        "reading": "☠ The bones remember this hand: 5 earlier tokens, 3 rugged, 1 graduated. Serial summoner.",
        "scrollTitle": "The hand",
        "scrollBodyMarkdown": "## The record\n- 3 of 4 readable launches rugged.",
    }
    w = ScriptedWriter([good])
    scroll, _, _ = run(dev_reading.dev_read(dev_ctx(tmp_path, w), NEW_MINT, user_id=None, surface="cli"))
    assert len(w.calls) == 1 and scroll.reading.startswith("☠ The bones remember")


def test_guard_allows_only_dev_fact_values():
    d = DevFacts(creator=CREATOR, status="resolved", pastCount=14, counts={"rugged": 9}, sigil="serial summoner")
    facts = dev_reading.facts_for(NEW_MINT, None, d, "pulls the omen to cursed (dev history)")
    allowed = guard.allowed_from(facts.for_writer())
    assert guard.check(f"{CREATOR} summoned 14 tokens, 9 rugged.", allowed) == []
    assert guard.check("It summoned 15 tokens, 13 rugged.", allowed)
    assert guard.check(f"funded by {FUNDER}", allowed)  # an address that isn't in facts


def test_dev_router_intent():
    assert rules(f"who is the dev of {NEW_MINT}").kind == "dev"
    assert rules("creator of $MOSS?").kind == "dev"
    assert rules(f"{NEW_MINT}").kind == "token"


def test_dev_command_is_registered():
    from heavymetal.wizzy.bots import core, tg
    from heavymetal.wizzy.features import register_all

    register_all()
    assert "dev" in tg.EXTRA_COMMANDS and "dev" in core.EXTRA_INTENTS
