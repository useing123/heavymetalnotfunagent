"""Dev's past coins: who created a token, what else that wallet created, and how those coins ended.

Code computes, the model phrases. Every step here is a fixed rule over chain and API data:

1. Creator: the RugCheck report's creator, cross-checked with the signer of the mint's create tx (RPC). For
   launchpad creates (pump.fun, LaunchLab, Meteora DBC, Moonshot) the human is the instruction's user account,
   never the fee payer or the factory. If the two sources disagree the creator is unresolved.
2. Earlier tokens: the creator's Helius parsed CREATE history, capped at MAX_TOKENS tokens or WINDOW_DAYS days.
3. Outcomes per token (classify): graduated / rugged / dead / alive / unknown. Unknown never counts against the dev.
4. Funding: SOL sent to the creator in the FUNDING_WINDOW_S before a deploy, 1 hop (2 for the Inner Circle),
   stopping at labeled exchange, bridge and program wallets. Wording is "funded by", never identity.
5. Sigil (sigil_for): fresh hand, clean hands, mixed sigils, serial summoner, by fixed thresholds.
"""

from __future__ import annotations

import asyncio
import re
import time

from heavymetal.solana.fake_check import normalize_ticker
from heavymetal.wizzy.facts import DevFacts, Funder, PastCoin, Source

# ---------------------------------------------------------------------- thresholds (spec, "proposed")
MAX_TOKENS = 200
WINDOW_DAYS = 180
SERIAL_WINDOW_DAYS = 90
SERIAL_MIN_RUGS = 3
SERIAL_MIN_SHARE = 0.5
RUG_LIQ_DROP = 0.9  # liquidity fell 90% or more ...
RUG_DROP_WINDOW_S = 72 * 3600  # ... within 72 h of its peak ...
RUG_MIN_SOLD = 0.5  # ... while the creator sold 50% or more of what it held
DEAD_MAX_LIQ = 1_000.0
DEAD_MAX_VOL = 100.0
DEAD_MIN_AGE_DAYS = 7
ALIVE_MIN_LIQ = 10_000.0
ALIVE_MIN_VOL = 1_000.0  # "real" volume proxy: some volume ...
ALIVE_MIN_TXNS = 20  # ... from more than a handful of trades
FUNDING_WINDOW_S = 72 * 3600
FUNDING_MIN_SOL = 0.05
MAX_FUNDERS_PER_HOP = 3
LIGHT_MAX_TOKENS = 50  # /read and /scry: counts only, smaller scan
RUG_REPORTS_FULL = 25  # RugCheck report calls per /dev (most recent tokens first)
RUG_REPORTS_LIGHT = 8
LINKED_RUG_TRACES = 5  # earlier rugged deploys whose funders are compared with this one's
MAX_PAGES = MAX_TOKENS // 100 + 1

WSOL = "So11111111111111111111111111111111111111112"
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LAUNCHPADS = {
    PUMP_FUN: "pump.fun",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "bonk.fun (Raydium LaunchLab)",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": "Moonshot",
}
PUMP_USER_INDEX = 7  # pump.fun `create`: mint, mint_authority, bonding_curve, abc, global, mpl, metadata, user
GRADUATED_DEXES = {"pumpswap", "raydium"}
# Programs and factory/authority accounts that sign or own creates but are never the human creator.
PROGRAMS = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",  # pump.fun mint authority
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",  # pump.fun event authority
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # pump.fun migration account
    *LAUNCHPADS,
}
# Mint authorities that are programs (bridged / wrapped assets): dev history doesn't apply to these mints.
PROGRAM_AUTHORITIES = {
    "BCD75RNBHrJJpW4dXVagL5mPjzRLnVZq4YirJdjEYMV7",  # Wormhole token bridge mint authority
    *PROGRAMS,
}
# Small static label list: funding traces stop here (exchange hot wallets would link everyone).
LABELS = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "Coinbase",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken",
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "Gate.io",
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC",
    "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": "KuCoin",
    "wormDTUJ6AWPNvk59vGQbDvGJmqbDTdgWgAqcLBCgUb": "Wormhole bridge",
    "DEbrdGj3HsRsAzx6uH4MKyREKxVAfBydijLUF3ygsFfh": "deBridge",
    "src5qyZHqTqecJV4aY6Cb6zDZLMDzrDKKezs22MPHr4": "deBridge",
    **{p: "program" for p in PROGRAMS},
}
SYMBOL_RE = re.compile(r"[A-Z0-9_]{1,12}")


def short(addr: str | None) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 10 else (addr or "?")


def link(mint: str) -> str:
    return f"https://dexscreener.com/solana/{mint}"


def _ok(x) -> bool:
    return x is not None and not isinstance(x, Exception)


async def _safe(coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        return e


def _helius(env):
    h = getattr(env, "helius", None)
    return h if h is not None and h.enabled else None


# ---------------------------------------------------------------------- 1. creator
def _keys(tx: dict) -> list[dict]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k if isinstance(k, dict) else {"pubkey": k, "signer": i == 0} for i, k in enumerate(keys)]


def _instructions(tx: dict) -> list[dict]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    out = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        out += inner.get("instructions") or []
    return out


def signer_from_tx(tx: dict, mint: str) -> tuple[str | None, str | None]:
    """(human creator, launchpad name) from a jsonParsed create tx; (None, None) if it doesn't create `mint`.

    Launchpad creates: the first signer in the launchpad instruction's accounts that isn't the mint, a program or a
    factory account (the instruction's user account), not the fee payer, which may be a relayer.
    Plain SPL creates: the signer paying for initializeMint, i.e. the fee payer.
    """
    keys = _keys(tx)
    signers = {k["pubkey"] for k in keys if k.get("signer")}
    for ix in _instructions(tx):
        pid = ix.get("programId")
        accts = ix.get("accounts") or []
        if pid in LAUNCHPADS and mint in accts:
            for a in accts:
                if a in signers and a != mint and a not in PROGRAMS:
                    return a, LAUNCHPADS[pid]
            return None, LAUNCHPADS[pid]
    for ix in _instructions(tx):
        p = ix.get("parsed")
        if (
            isinstance(p, dict)
            and p.get("type", "").startswith("initializeMint")
            and p.get("info", {}).get("mint") == mint
        ):
            payer = keys[0]["pubkey"] if keys else None
            return (payer if payer not in PROGRAMS else None), None
    return None, None


def created_mint(htx: dict, creator: str | None = None) -> str | None:
    """The mint a Helius-parsed CREATE tx made (launchpad instruction account 0, else the first token moved)."""
    for ix in htx.get("instructions") or []:
        accts = ix.get("accounts") or []
        if ix.get("programId") in LAUNCHPADS and accts:
            if creator and creator not in accts and htx.get("feePayer") != creator:
                return None  # someone else's launch that merely touched this wallet
            return accts[0]
    if creator and htx.get("feePayer") != creator:
        return None
    for tr in htx.get("tokenTransfers") or []:
        if tr.get("mint") and tr["mint"] != WSOL:
            return tr["mint"]
    for acc in htx.get("accountData") or []:
        for ch in acc.get("tokenBalanceChanges") or []:
            if ch.get("mint") and ch["mint"] != WSOL:
                return ch["mint"]
    return None


def _helius_user(htx: dict, mint: str) -> tuple[str | None, str | None]:
    """Fallback when the RPC tx is unavailable (no signer flags): the launchpad instruction's user account.

    The fee payer counts only when it is one of the instruction's accounts (so a relayer that merely pays is never
    taken); otherwise pump.fun's legacy `create` user index is used.
    """
    payer = htx.get("feePayer")
    for ix in htx.get("instructions") or []:
        accts = ix.get("accounts") or []
        pid = ix.get("programId")
        if pid not in LAUNCHPADS or not accts or accts[0] != mint:
            continue
        if payer and payer in accts[1:] and payer not in PROGRAMS:
            return payer, LAUNCHPADS[pid]
        if pid == PUMP_FUN and len(accts) > PUMP_USER_INDEX and accts[PUMP_USER_INDEX] not in PROGRAMS:
            return accts[PUMP_USER_INDEX], LAUNCHPADS[pid]
        return None, LAUNCHPADS[pid]
    return None, None


async def _create_tx_via_history(env, creator: str | None, mint: str) -> dict | None:
    """The mint's create tx from Helius: first the mint's own CREATE history, then the named creator's."""
    h = _helius(env)
    if h is None:
        return None
    own = await _safe(h.history(mint, tx_type="CREATE"))
    for t in own if _ok(own) else []:
        if created_mint(t) == mint:
            return t
    if not creator:
        return None
    before = None
    for _ in range(MAX_PAGES):
        page = await h.history(creator, tx_type="CREATE", before=before)
        for t in page:
            if created_mint(t) == mint:
                return t
        if len(page) < 100:
            return None
        before = page[-1].get("signature")
    return None


async def _oldest_sig(env, mint: str) -> str | None:
    """The mint's first signature when its whole history fits one page (young or quiet tokens)."""
    sigs = await env.rpc.signatures(mint, 1000)
    if not sigs or len(sigs) >= 1000:
        return None
    return sigs[-1].get("signature")


async def resolve_creator(env, mint: str, report: dict | None = None) -> dict:
    out = {"creator": None, "status": "unavailable", "sources": [], "launchpad": None, "createdAt": None}
    out |= {"sig": None, "unknowns": []}
    if report is None:
        rep = await _safe(env.rug.report(mint))
        report = rep if _ok(rep) else {}
    rug_creator = (report or {}).get("creator") or None
    if rug_creator in PROGRAMS:
        out["unknowns"].append("RugCheck named a launchpad or program as creator; ignored")
        rug_creator = None
    htx = await _safe(_create_tx_via_history(env, rug_creator, mint))
    htx = htx if _ok(htx) else None
    sig = htx.get("signature") if htx else None
    if sig is None:
        got = await _safe(_oldest_sig(env, mint))
        sig = got if _ok(got) else None
    chain_creator, pad, chain_src = None, None, "rpc create tx signer"
    if sig:
        tx = await _safe(env.rpc.transaction(sig))
        if _ok(tx) and tx:
            chain_creator, pad = signer_from_tx(tx, mint)
            out["createdAt"] = tx.get("blockTime")
        if chain_creator is None and htx:
            chain_creator, pad = _helius_user(htx, mint)
            chain_src = "helius parsed create tx user"
        if out["createdAt"] is None and htx:
            out["createdAt"] = htx.get("timestamp")
    out["launchpad"], out["sig"] = pad, sig if chain_creator else None
    if rug_creator:
        out["sources"].append("rugcheck report creator")
    if chain_creator:
        out["sources"].append(chain_src)
    if rug_creator and chain_creator and rug_creator != chain_creator:
        out["status"] = "unresolved"
        out["unknowns"].append("creator: RugCheck and the create tx disagree")
    elif rug_creator and chain_creator:
        out["creator"], out["status"] = rug_creator, "resolved"
    elif rug_creator or chain_creator:
        out["creator"], out["status"] = rug_creator or chain_creator, "single_source"
        out["unknowns"].append("creator: only one source could be read, not cross-checked")
    else:
        out["unknowns"].append("creator: neither RugCheck nor the create tx named one")
    return out


# ---------------------------------------------------------------------- 2. earlier tokens
async def creator_tokens(
    env, creator: str, *, now: float, max_tokens: int = MAX_TOKENS, known: list[dict] | None = None
) -> tuple[list[dict], bool]:
    """Tokens the creator made in the last WINDOW_DAYS (newest first), merged with a cached list.

    Incremental: paging stops at the first signature already in `known`. Returns (tokens, truncated).
    """
    h = _helius(env)
    if h is None:
        raise RuntimeError("dev history needs a Helius key")
    known = known or []
    seen_sigs = {t["sig"] for t in known}
    oldest = now - WINDOW_DAYS * 86400
    fresh: list[dict] = []
    before, truncated, hit_known = None, False, False
    for _ in range(MAX_PAGES):
        page = await h.history(creator, tx_type="CREATE", before=before)
        for t in page:
            if t.get("signature") in seen_sigs:
                hit_known = True
                break
            ts = t.get("timestamp") or 0
            if ts and ts < oldest:
                hit_known = True  # past the window: stop
                break
            if t.get("transactionError"):
                continue
            m = created_mint(t, creator)
            if m:
                fresh.append({"mint": m, "ts": ts, "sig": t.get("signature"), "source": "helius parsed history"})
        if hit_known or len(page) < 100:
            break
        before = page[-1].get("signature")
    else:
        truncated = True
    merged, mints = [], set()
    for t in fresh + known:
        if t["mint"] in mints or (t.get("ts") or 0) < oldest:
            continue
        mints.add(t["mint"])
        merged.append(t)
    if len(merged) > max_tokens:
        merged, truncated = merged[:max_tokens], True
    return merged, truncated


# ---------------------------------------------------------------------- 3. outcomes
def liquidity_crash(points: list[tuple[float, float]]) -> bool:
    """True if liquidity fell RUG_LIQ_DROP or more within RUG_DROP_WINDOW_S of a peak."""
    pts = sorted(p for p in points if p[1] is not None)
    for i, (t0, peak) in enumerate(pts):
        if peak <= 0:
            continue
        for t1, liq in pts[i + 1 :]:
            if t1 - t0 > RUG_DROP_WINDOW_S:
                break
            if liq <= peak * (1 - RUG_LIQ_DROP):
                return True
    return False


def classify(
    *,
    now: float,
    created_at: float | None,
    pair: dict | None,
    rugged: bool | None = None,
    liq_points: list[tuple[float, float]] | None = None,
    sold_share: float | None = None,
    bonding_complete: bool | None = None,
) -> tuple[str, str]:
    """(outcome, why) for one token. Missing data gives "unknown", which never counts against the dev."""
    if rugged is True:
        return "rugged", "RugCheck marked it rugged"
    if liq_points and sold_share is not None and sold_share >= RUG_MIN_SOLD and liquidity_crash(liq_points):
        return "rugged", "liquidity fell 90% or more within 72 h of its peak while the creator sold half or more"
    dex = (pair or {}).get("dex")
    if bonding_complete is True or (dex or "").lower() in GRADUATED_DEXES:
        return "graduated", f"a {dex} pool exists" if dex else "bonding curve complete"
    if pair is None:
        return "unknown", "no market data"
    liq = pair.get("liquidity_usd")
    vol = (pair.get("volume") or {}).get("h24")
    tx = pair.get("txns_h24") or {}
    trades = (tx.get("buys") or 0) + (tx.get("sells") or 0)
    age_days = (now - created_at) / 86400 if created_at else None
    if liq is None and vol is not None and (dex or "").lower() == "pumpfun":
        # Bonding-curve pairs carry no liquidity figure: never graduated and no trading for a week is dead.
        if vol < DEAD_MAX_VOL and age_days is not None and age_days >= DEAD_MIN_AGE_DAYS:
            return "dead", "never left the bonding curve; under $100 daily volume for a week or more"
        return "unknown", "still on the bonding curve"
    if liq is None or vol is None:
        return "unknown", "liquidity or volume missing"
    if liq >= ALIVE_MIN_LIQ and vol >= ALIVE_MIN_VOL and trades >= ALIVE_MIN_TXNS:
        return "alive", "liquidity and trading still present"
    if liq < DEAD_MAX_LIQ and vol < DEAD_MAX_VOL:
        if age_days is not None and age_days >= DEAD_MIN_AGE_DAYS:
            return "dead", "under $1K liquidity and under $100 daily volume for a week or more"
        return "unknown", "quiet, but younger than a week"
    return "unknown", "between the alive and dead thresholds"


def sigil_for(coins: list[PastCoin], now: float) -> tuple[str, int, int, float | None]:
    """(sigil, rugs in 90d, known outcomes in 90d, rug share in 90d). Unknown outcomes are left out."""
    known = [c for c in coins if c.outcome != "unknown"]
    cutoff = now - SERIAL_WINDOW_DAYS * 86400
    recent = [c for c in known if (c.createdAt or 0) >= cutoff]
    rugs90 = sum(1 for c in recent if c.outcome == "rugged")
    share = round(rugs90 / len(recent), 3) if recent else None
    if not known:
        return "fresh hand", rugs90, len(recent), share
    if rugs90 >= SERIAL_MIN_RUGS and share is not None and share >= SERIAL_MIN_SHARE:
        return "serial summoner", rugs90, len(recent), share
    if any(c.outcome == "rugged" for c in known):
        return "mixed sigils", rugs90, len(recent), share
    return "clean hands", rugs90, len(recent), share


def sold_shares(swaps: list[dict], creates: list[dict] | None = None) -> dict[str, float]:
    """Share of each token the creator sold, from its parsed swaps (plus tokens received in create txs)."""
    got: dict[str, float] = {}
    sold: dict[str, float] = {}
    for c in creates or []:
        got[c["mint"]] = got.get(c["mint"], 0) + (c.get("initial") or 0)
    for s in swaps:
        if s.get("failed"):
            continue
        for m, d in (s.get("token_deltas") or {}).items():
            if d > 0:
                got[m] = got.get(m, 0) + d
            else:
                sold[m] = sold.get(m, 0) - d
    return {m: round(min(sold.get(m, 0) / g, 1.0), 3) for m, g in got.items() if g > 0}


# ---------------------------------------------------------------------- 4. funding
async def funders_of(
    env, wallet: str, *, before_sig: str | None, deploy_ts: float, hops: int = 1, hop: int = 1
) -> list[Funder]:
    """Wallets that sent >= FUNDING_MIN_SOL to `wallet` in the window before deploy_ts. Stops at labels."""
    h = _helius(env)
    if h is None or not deploy_ts:
        return []
    page = await h.history(wallet, tx_type="TRANSFER", before=before_sig)
    sums: dict[str, tuple[float, float, str | None]] = {}
    for t in page:
        ts = t.get("timestamp") or 0
        if not (deploy_ts - FUNDING_WINDOW_S <= ts <= deploy_ts):
            continue
        for nt in t.get("nativeTransfers") or []:
            src = nt.get("fromUserAccount")
            if nt.get("toUserAccount") != wallet or not src or src == wallet:
                continue
            sol = float(nt.get("amount") or 0) / 1e9
            prev = sums.get(src, (0.0, ts, t.get("signature")))
            sums[src] = (prev[0] + sol, min(prev[1], ts), prev[2] if prev[1] <= ts else t.get("signature"))
    rows = sorted(((w, v) for w, v in sums.items() if v[0] >= FUNDING_MIN_SOL), key=lambda x: -x[1][0])
    out: list[Funder] = []
    for w, (sol, ts, sig) in rows[:MAX_FUNDERS_PER_HOP]:
        label = LABELS.get(w)
        out.append(Funder(wallet=w, solIn=round(sol, 3), hop=hop, label=label))
        if label is None and hop < hops:
            got = await _safe(funders_of(env, w, before_sig=sig, deploy_ts=ts, hops=hops, hop=hop + 1))
            if _ok(got):
                out += [f for f in got if f.wallet not in {x.wallet for x in out} and f.wallet != wallet]
    return out


# ---------------------------------------------------------------------- caching
class DevCache:
    """Per-creator token list and per-mint liquidity observations. Backed by wizzy.db when given, else memory."""

    def __init__(self, wdb=None, ttl_s: float = 6 * 3600):
        self.wdb = wdb
        self.ttl_s = ttl_s
        self.mem: dict[str, tuple[float, dict]] = {}
        self.liq: dict[str, list[tuple[float, float]]] = {}

    def get(self, wallet: str) -> tuple[dict | None, bool]:
        """(profile, fresh). A stale profile is still returned so the refresh can be incremental."""
        if self.wdb is not None:
            r = self.wdb.one("SELECT data, updated_at FROM dev_profiles WHERE wallet=?", (wallet,))
            if r is None:
                return None, False
            import json

            return json.loads(r["data"]), time.time() - r["updated_at"] < self.ttl_s
        hit = self.mem.get(wallet)
        return (hit[1], time.time() - hit[0] < self.ttl_s) if hit else (None, False)

    def put(self, wallet: str, profile: dict) -> None:
        if self.wdb is not None:
            self.wdb._x(
                "INSERT INTO dev_profiles(wallet, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(wallet) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (wallet, self.wdb.dumps(profile), time.time()),
            )
            return
        if len(self.mem) > 2000:
            self.mem.clear()
        self.mem[wallet] = (time.time(), profile)

    def observe(self, mint: str, ts: float, liq: float | None) -> list[tuple[float, float]]:
        """Record a liquidity reading and return all readings for the mint (oldest first)."""
        if self.wdb is not None:
            if liq is not None:
                self.wdb._x("INSERT OR IGNORE INTO dev_liquidity(mint, ts, liq) VALUES(?,?,?)", (mint, ts, liq))
            rows = self.wdb.all("SELECT ts, liq FROM dev_liquidity WHERE mint=? ORDER BY ts DESC LIMIT 50", (mint,))
            return sorted((r["ts"], r["liq"]) for r in rows)
        pts = self.liq.setdefault(mint, [])
        if liq is not None:
            pts.append((ts, liq))
            del pts[:-50]
        return sorted(pts)


MEMORY_CACHE = DevCache()


# ---------------------------------------------------------------------- the pipeline
def summary_line(d: DevFacts) -> str:
    who = f"Dev {short(d.creator)}"
    if d.status == "unresolved":
        return "Dev: the creator can't be pinned down; the sources disagree."
    if d.creator is None:
        return ""
    if d.sigil is None:
        return f"{who}: the creator's history couldn't be read in full; no sigil."
    c = d.counts
    if d.sigil == "fresh hand":
        if d.pastCount:
            line = f"{who}: fresh hand, {d.pastCount} earlier tokens but none with a readable outcome."
        else:
            line = f"{who}: fresh hand, no past to read (that is not the same as clean)."
    else:
        line = (
            f"{who}: {d.sigil}. {d.pastCount} earlier tokens in {d.windowDays} days: {c.get('rugged', 0)} rugged, "
            f"{c.get('dead', 0)} went dark, {c.get('alive', 0)} still breathe, {c.get('graduated', 0)} graduated"
            + (f", {c['unknown']} unread." if c.get("unknown") else ".")
        )
    if d.creatorExited:
        line += " The original creator no longer holds this token."
    return line


async def dev_facts(
    env,
    mint: str,
    *,
    report: dict | None = None,
    light: bool = False,
    hops: int = 1,
    cache: DevCache | None = None,
    now: float | None = None,
) -> DevFacts:
    """Dev history for a token. light=True (for /read and /scry) skips funding, sells and the creator's holdings."""
    now = now or time.time()
    cache = cache or MEMORY_CACHE
    who = await resolve_creator(env, mint, report)
    d = DevFacts(
        creator=who["creator"],
        status=who["status"],
        creatorSources=who["sources"],
        launchpad=who["launchpad"],
        createdAt=who["createdAt"],
        windowDays=WINDOW_DAYS,
        asOf=now,
        unknowns=list(who["unknowns"]),
    )
    d.sources.append(Source(name="creator (rugcheck + rpc create tx)", fetchedAt=now, ok=bool(d.creator)))
    if d.creator is None:
        d.line = summary_line(d)
        return d
    if _helius(env) is None:
        d.unknowns.append("dev history (needs a Helius key)")
        return d
    profile, fresh = cache.get(d.creator)
    tokens = (profile or {}).get("tokens") or []
    truncated = bool((profile or {}).get("truncated"))
    if not fresh:
        got = await _safe(creator_tokens(env, d.creator, now=now, known=tokens))
        if not _ok(got):
            d.sources.append(Source(name="helius parsed history (creator creates)", fetchedAt=now, ok=False))
            d.unknowns.append("the creator's earlier tokens (history unavailable)")
            return d
        tokens, truncated = got
        cache.put(d.creator, {"tokens": tokens, "truncated": truncated})
    d.sources.append(Source(name="helius parsed history (creator creates)", fetchedAt=now))
    created = d.createdAt or next((t["ts"] for t in tokens if t["mint"] == mint), None)
    past = [t for t in tokens if t["mint"] != mint and (created is None or (t.get("ts") or 0) <= created)]
    if light:
        truncated = truncated or len(past) > LIGHT_MAX_TOKENS
        past = past[:LIGHT_MAX_TOKENS]
    d.truncated = truncated
    mints = [t["mint"] for t in past]

    pairs = await _safe(env.dex.tokens_batch(mints)) if mints else {}
    d.sources.append(Source(name="dexscreener pairs", fetchedAt=now, ok=_ok(pairs)))
    pairs = pairs if _ok(pairs) else None
    if pairs is None:
        d.unknowns.append("market data for earlier tokens")
    n_reports = RUG_REPORTS_LIGHT if light else RUG_REPORTS_FULL
    sem = asyncio.Semaphore(4)

    async def rug(m):
        async with sem:
            return await _safe(env.rug.report(m))

    reports = await asyncio.gather(*(rug(m) for m in mints[:n_reports]))
    rugged = {m: r.get("rugged") for m, r in zip(mints, reports, strict=False) if _ok(r)}
    d.sources.append(Source(name=f"rugcheck reports (latest {n_reports})", fetchedAt=now, ok=bool(rugged) or not mints))
    sold: dict[str, float] = {}
    if not light:
        swaps = await _safe(_helius(env).wallet_swaps(d.creator, 100))
        d.sources.append(Source(name="helius creator swaps", fetchedAt=now, ok=_ok(swaps)))
        if _ok(swaps):
            sold = sold_shares(swaps)
        else:
            d.unknowns.append("the creator's sells")

    coins: list[PastCoin] = []
    for t in past:
        m = t["mint"]
        p = (pairs or {}).get(m) if pairs is not None else None
        liq = p.get("liquidity_usd") if p else None
        pts = cache.observe(m, now, liq) if pairs is not None else []
        outcome, why = (
            classify(
                now=now,
                created_at=t.get("ts"),
                pair=p,
                rugged=rugged.get(m),
                liq_points=pts,
                sold_share=sold.get(m),
            )
            if pairs is not None or rugged.get(m)
            else ("unknown", "market data unavailable")
        )
        sym = normalize_ticker(((p or {}).get("base") or {}).get("symbol"))
        coins.append(
            PastCoin(
                mint=m,
                symbol=sym if SYMBOL_RE.fullmatch(sym or "") else None,
                createdAt=t.get("ts"),
                ageDays=round((now - t["ts"]) / 86400, 1) if t.get("ts") else None,
                peakLiquidityUsd=round(max(v for _, v in pts), 2) if pts else None,
                liquidityUsd=round(liq, 2) if liq is not None else None,
                volume24hUsd=round((p.get("volume") or {}).get("h24") or 0, 2) if p else None,
                dex=(p or {}).get("dex"),
                outcome=outcome,
                why=why,
                link=link(m),
                source=t.get("source") or "helius parsed history",
            )
        )
    d.pastCoins = coins
    d.pastCount = len(coins)
    d.counts = {k: sum(1 for c in coins if c.outcome == k) for k in ("rugged", "dead", "alive", "graduated", "unknown")}
    d.sigil, d.rugs90d, d.known90d, d.rugShare90d = sigil_for(coins, now)
    in_window = d.createdAt is None or d.createdAt >= now - WINDOW_DAYS * 86400
    if in_window and not any(t["mint"] == mint for t in tokens):
        # The token's own create is missing from the creator's history, so the history is incomplete.
        d.unknowns.append("this token's own create isn't in the creator's parsed history; the list may be incomplete")
        if d.sigil == "fresh hand":
            d.sigil = None  # an empty list we can't trust is not a fresh hand

    if not light:
        holds = await _safe(env.rpc.token_accounts(d.creator))
        if _ok(holds):
            d.creatorExited = not any(h.get("mint") == mint and (h.get("ui_amount") or 0) > 0 for h in holds)
        await _funding(env, d, who.get("sig"), past, hops)
    d.line = summary_line(d)
    return d


async def _funding(env, d: DevFacts, sig: str | None, past: list[dict], hops: int) -> None:
    now = d.asOf
    got = await _safe(funders_of(env, d.creator, before_sig=sig, deploy_ts=d.createdAt or now, hops=hops))
    d.sources.append(Source(name=f"helius SOL transfers ({hops} hop)", fetchedAt=now, ok=_ok(got)))
    if not _ok(got):
        d.unknowns.append("funding wallets")
        return
    d.funders = got
    rug_mints = {c.mint for c in d.pastCoins if c.outcome == "rugged"}
    rug_deploys = [t for t in past if t["mint"] in rug_mints][:LINKED_RUG_TRACES]
    if not d.funders or not rug_deploys:
        return
    traced = await asyncio.gather(
        *(_safe(funders_of(env, d.creator, before_sig=t.get("sig"), deploy_ts=t.get("ts") or 0)) for t in rug_deploys)
    )
    for f in d.funders:
        if f.label:
            continue  # exchanges fund everyone; never a link
        f.fundedRugs = sum(1 for got in traced if _ok(got) and any(g.wallet == f.wallet for g in got))
    d.fundersLinkedRugs = sum(1 for f in d.funders if f.fundedRugs)
