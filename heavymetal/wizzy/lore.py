"""Lore vs chain (/lore): check a token's marketing claims against chain facts computed in code.

``/lore <ticker|CA>`` checks the common claims (LP locked, authorities revoked, dev holds 0%, fair launch, holders
distributed, no tax, graduated). ``/lore <CA> "claim text"`` maps free text to a fixed claim-type enum with
keyword rules (no model), then checks it the same way. Every verdict (TRUE, FALSE or UNPROVEN) comes from code;
a claim that doesn't map, or that the chain can't see (CTO, audits, partnerships), is UNPROVEN.

Claim text is untrusted: it is cleaned (links removed, clipped), kept in ``Facts.untrusted`` and never authorizes
a number for the output guard. Verdict lines use the code's canonical label, not the user's words.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from heavymetal.wizzy.build import _ok, _safe, build_token_facts, clean_untrusted
from heavymetal.wizzy.facts import Facts, Source

Verdict = Literal["TRUE", "FALSE", "UNPROVEN"]
ClaimType = Literal[
    "lp_locked",
    "authorities_revoked",
    "mint_revoked",
    "freeze_revoked",
    "dev_holds_zero",
    "dev_holding",
    "dev_burned",
    "fair_launch",
    "distributed",
    "no_tax",
    "graduated",
    "age",
    "offchain",
    "unknown",
]

RUNES = "ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃ"
LP_TOLERANCE_PTS = 5.0  # spec: partial burns/locks match a claimed percentage within 5 points
DEV_ZERO_PCT = 0.01  # creator balance at or below this share of supply counts as "holds 0%"
DISTRIBUTED_TOP10_PCT = 30.0  # same "healthy distribution" line as signals/audit.py
AGE_TOLERANCE = 0.2  # age claims match within 20% (at least 1 hour)
TAX_TOLERANCE_PCT = 0.5
MAX_CLAIM_CHARS = 120
GRADUATED_DEXES = {"pumpswap", "raydium", "raydium-cpmm", "raydium-clmm", "meteora", "meteoradbc", "orca"}
CURVE_DEXES = {"pumpfun", "pump.fun", "pump-fun"}
CUSTOM_CLAIMS = {"free": 1, "inner": 5}  # per reading, by tier

LABEL = {
    "lp_locked": "LP locked",
    "authorities_revoked": "authorities renounced",
    "mint_revoked": "mint authority revoked",
    "freeze_revoked": "freeze authority revoked",
    "dev_holds_zero": "dev holds 0%",
    "dev_holding": "dev still holding",
    "dev_burned": "dev burned supply",
    "fair_launch": "fair launch, no insiders",
    "distributed": "holders distributed",
    "no_tax": "no tax",
    "graduated": "graduated",
    "age": "token age",
    "offchain": "off-chain claim",
    "unknown": "unmapped claim",
}
LESSON = {
    "lp_locked": "lp-lock",
    "authorities_revoked": "authorities",
    "mint_revoked": "authorities",
    "freeze_revoked": "authorities",
    "dev_holds_zero": "holder-concentration",
    "dev_holding": "holder-concentration",
    "dev_burned": "rugs",
    "fair_launch": "rugs",
    "distributed": "holder-concentration",
    "no_tax": "rugs",
    "graduated": "bonding-curves",
}
DEFAULT_CLAIMS: tuple[str, ...] = (
    "lp_locked",
    "mint_revoked",
    "freeze_revoked",
    "dev_holds_zero",
    "fair_launch",
    "distributed",
    "no_tax",
    "graduated",
)


# ---------------------------------------------------------------------- typed facts
class ChainLore(BaseModel):
    """What the chain (and the sources that read it) says, each field None when unknown."""

    mint: str
    launchpad: str | None = None  # "pump.fun" when the mint has pump.fun's vanity suffix
    dex: str | None = None  # dex of the most liquid pair
    graduated: bool | None = None
    lpLockedPct: float | None = None
    mintAuthority: str | None = None
    freezeAuthority: str | None = None
    authoritiesKnown: bool = False
    isToken2022: bool | None = None
    transferFeeBps: int | None = None
    transferHook: bool | None = None
    creator: str | None = None
    creatorHoldsPct: float | None = None
    insidersDetected: int | None = None
    top10Pct: float | None = None
    pairAgeHours: float | None = None


class ParsedClaim(BaseModel):
    type: ClaimType
    negated: bool = False  # "LP not locked": the verdict is flipped
    claimedPct: float | None = None  # "LP 80% locked", "5% tax"
    claimedAgeHours: float | None = None
    raw: str = ""  # cleaned user text (untrusted), empty for default claims


class ClaimCheck(BaseModel):
    n: int
    type: ClaimType
    label: str
    verdict: Verdict
    evidence: str
    sources: list[str] = Field(default_factory=list)
    custom: bool = False
    lesson: str | None = None


# ---------------------------------------------------------------------- deterministic claim parsing
NEG_RE = re.compile(r"\b(not|isn'?t|aren'?t|wasn'?t|never|no longer|unlocked|unburn\w*)\b|n't\b", re.I)
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
AGE_RE = re.compile(r"(\d{1,4}(?:\.\d+)?)\s*(h|hrs?|hours?|d|days?|w|wks?|weeks?|mo|months?)\b", re.I)
AGE_UNIT_H = {"h": 1, "d": 24, "w": 168, "m": 720}

# Order matters: specific rules first ("dev burned" before LP "burned", "no tax" before negation handling).
RULES: list[tuple[str, re.Pattern]] = [
    (
        "offchain",
        re.compile(
            r"\b(cto|community take ?over|audit(ed)?|partner(ship)?s?|doxx?ed|kyc|listing|listed on|cex|kols?|backed by|team|roadmap|utility)\b",
            re.I,
        ),
    ),
    (
        "dev_burned",
        re.compile(r"\b(dev|devs|creator|team)\b.*\bburn\w*|\bburn\w*\b.*\b(supply|bag|tokens|allocation)\b", re.I),
    ),
    (
        "dev_holds_zero",
        re.compile(
            r"\b(no dev (wallet|bag|tokens|supply)|dev (holds|has|owns) (0|zero|none|nothing)|(0|zero)\s*% dev|dev sold (all|everything|out)|dev (wallet )?(is )?empty)\b",
            re.I,
        ),
    ),
    (
        "dev_holding",
        re.compile(
            r"\b(dev|creator)\b.*\b(holding|holds|hodl\w*|still in|hasn'?t sold|never sold|diamond hand\w*)\b", re.I
        ),
    ),
    (
        "fair_launch",
        re.compile(
            r"\b(fair ?launch\w*|stealth launch\w*|no (presale|pre-sale|insiders?|bundles?|snipers?|team allocation|vc)s?|unbundled|zero insiders)\b",
            re.I,
        ),
    ),
    ("no_tax", re.compile(r"\b(tax\w*|fees?|transfer fee)\b", re.I)),
    (
        "authorities_revoked",
        re.compile(
            r"\b(authorities|contract renounced|safu contract)\b|\bmint\b.*\bfreez\w*|\bfreez\w*.*\bmint\b"
            r"|^(?!.*\b(mint\w*|freez\w*)\b).*\b(renounc\w*|authority (revoked|disabled|burn\w*))",
            re.I,
        ),
    ),
    (
        "mint_revoked",
        re.compile(
            r"\b(mint(able)?|minting)\b.*\b(revoked|renounced|disabled|off|burn\w*)\b|\bno mint\w*\b|\bnot mintable\b|\bunmintable\b",
            re.I,
        ),
    ),
    ("freeze_revoked", re.compile(r"\bfreez\w*\b", re.I)),
    (
        "lp_locked",
        re.compile(
            r"\b(lp|liquidity|pool)\b.*\b(lock\w*|burn\w*|unlock\w*)\b|\b(lock\w*|burn\w*)\b.*\b(lp|liquidity)\b", re.I
        ),
    ),
    (
        "graduated",
        re.compile(
            r"\b(graduat\w*|bonded|bonding curve (complete|done|finished)|migrated|completed the curve)\b", re.I
        ),
    ),
    (
        "distributed",
        re.compile(
            r"\b(distribut\w*|no whales?|top (holders|10|ten)|holders? (spread|healthy)|decentrali[sz]ed)\b", re.I
        ),
    ),
    ("age", re.compile(r"\b(launched|created|born|deployed|live|old|since)\b", re.I)),
]
POLAR = {"lp_locked", "mint_revoked", "freeze_revoked", "authorities_revoked", "graduated", "distributed"}


def parse_claim(text: str) -> ParsedClaim:
    """Map untrusted free text to one claim type with fixed keyword rules. Unmatched text is 'unknown'."""
    from heavymetal.wizzy.build import URL_RE

    t = re.sub(r"\s+", " ", URL_RE.sub(" ", (text or "")[:400])).strip().lower()[:MAX_CLAIM_CHARS]
    raw = clean_untrusted(text[:400] if text else "")
    kind = "unknown"
    for k, rx in RULES:
        if rx.search(t):
            kind = k
            break
    if kind == "freeze_revoked" and not re.search(r"revok|renounc|disabl|no freez|off|burn|not|n't|can'?t", t):
        kind = "unknown"
    negated = bool(kind in POLAR and NEG_RE.search(t))
    if kind == "lp_locked" and re.search(r"\bunlock", t):
        negated = True
    pct = None
    m = PCT_RE.search(t)
    if m and kind in ("lp_locked", "no_tax"):
        pct = float(m.group(1))
    if kind == "no_tax" and pct is None and re.search(r"\b(no|zero|0)\s*(tax|fee)", t):
        pct = 0.0
    age_h = None
    if kind == "age":
        am = AGE_RE.search(t)
        if am:
            age_h = float(am.group(1)) * AGE_UNIT_H[am.group(2)[0].lower()]
        else:
            kind = "unknown"
    return ParsedClaim(type=kind, negated=negated, claimedPct=pct, claimedAgeHours=age_h, raw=raw)


def split_claims(args: str) -> tuple[str, list[str]]:
    """'<subject> "claim one" "claim two"' -> (subject, [claims]). Curly quotes accepted."""
    s = (args or "").replace("“", '"').replace("”", '"').replace("«", '"').replace("»", '"')
    claims = [c.strip() for c in re.findall(r'"([^"]{1,400})"', s) if c.strip()]
    subject = re.sub(r'"[^"]*"?', " ", s).split()
    return (subject[0] if subject else ""), claims


# ---------------------------------------------------------------------- verdicts (code only)
def _flip(v: Verdict, negated: bool) -> Verdict:
    if not negated or v == "UNPROVEN":
        return v
    return "FALSE" if v == "TRUE" else "TRUE"


def _fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def judge(c: ParsedClaim, chain: ChainLore) -> tuple[Verdict, str, list[str]]:
    """(verdict, evidence, source names) for one claim. Missing data is UNPROVEN, never a guess."""
    k = c.type
    if k == "lp_locked":
        if chain.launchpad == "pump.fun" and chain.graduated is False:
            return "UNPROVEN", "still on the pump.fun bonding curve, so there is no LP to lock yet", ["dexscreener"]
        if chain.lpLockedPct is None:
            if chain.launchpad == "pump.fun" and chain.graduated:
                return "UNPROVEN", "RugCheck has no LP lock figure; the migration burn is not decoded yet", ["rugcheck"]
            return "UNPROVEN", "no LP lock figure from RugCheck", ["rugcheck"]
        want = c.claimedPct if c.claimedPct is not None else 100.0
        lp = chain.lpLockedPct
        ok = lp >= want - LP_TOLERANCE_PTS
        tail = "" if c.claimedPct is None else f" (claimed {_fmt(want)}%)"
        return (
            _flip("TRUE" if ok else "FALSE", c.negated),
            f"RugCheck reports {_fmt(lp)}% of LP locked or burned{tail}",
            ["rugcheck"],
        )
    if k in ("mint_revoked", "freeze_revoked", "authorities_revoked"):
        if not chain.authoritiesKnown:
            return "UNPROVEN", "the mint account could not be read", ["rpc mint account"]
        mint_off, freeze_off = chain.mintAuthority is None, chain.freezeAuthority is None
        if k == "mint_revoked":
            ok, ev = mint_off, "mint authority is revoked" if mint_off else "mint authority is still set"
        elif k == "freeze_revoked":
            ok, ev = freeze_off, "freeze authority is revoked" if freeze_off else "freeze authority is still set"
        else:
            ok = mint_off and freeze_off
            ev = f"mint authority {'revoked' if mint_off else 'set'}, freeze authority {'revoked' if freeze_off else 'set'}"
        return _flip("TRUE" if ok else "FALSE", c.negated), ev, ["rpc mint account"]
    if k in ("dev_holds_zero", "dev_holding", "dev_burned"):
        if not chain.creator:
            return "UNPROVEN", "the creator wallet is unknown", ["rugcheck report"]
        if chain.creatorHoldsPct is None:
            return "UNPROVEN", "the creator's balance could not be read", ["rpc token accounts"]
        held = chain.creatorHoldsPct
        srcs = ["rugcheck report (creator)", "rpc token accounts"]
        empty = held <= DEV_ZERO_PCT
        ev = (
            "the creator wallet holds none of the supply"
            if empty
            else f"the creator wallet holds {_fmt(held)}% of supply"
        )
        if k == "dev_holds_zero":
            return ("TRUE" if empty else "FALSE"), ev + " (linked wallets not traced)", srcs
        if k == "dev_holding":
            return ("FALSE" if empty else "TRUE"), ev, srcs
        if not empty:
            return "FALSE", ev + "; nothing to call burned", srcs
        return "UNPROVEN", ev + "; burn vs sale is not traced yet", srcs
    if k == "fair_launch":
        if chain.insidersDetected is not None and chain.insidersDetected > 0:
            return "FALSE", f"RugCheck links {chain.insidersDetected} insider wallets", ["rugcheck report"]
        if chain.insidersDetected is None:
            return "UNPROVEN", "no insider data; create-slot buyers are not traced yet", ["rugcheck report"]
        return "UNPROVEN", "no insider wallets flagged, but create-slot buyers are not traced yet", ["rugcheck report"]
    if k == "distributed":
        if chain.top10Pct is None:
            return "UNPROVEN", "holder concentration unavailable", ["rpc largest holders"]
        ok = chain.top10Pct < DISTRIBUTED_TOP10_PCT
        ev = f"top 10 accounts hold {_fmt(chain.top10Pct)}% (pool accounts included)"
        return _flip("TRUE" if ok else "FALSE", c.negated), ev, ["rpc largest holders"]
    if k == "no_tax":
        if chain.transferFeeBps is None:
            return "UNPROVEN", "the mint's extensions could not be read", ["rpc mint account"]
        fee = chain.transferFeeBps / 100
        want = c.claimedPct if c.claimedPct is not None else 0.0
        ok = abs(fee - want) <= TAX_TOLERANCE_PCT
        if not chain.isToken2022:
            ev = "classic SPL token: no transfer fee is possible"
        else:
            ev = f"Token-2022 transfer fee is {_fmt(fee)}%" + ("; a transfer hook is set" if chain.transferHook else "")
        if want == 0 and chain.transferHook:
            ok = False
        tail = "" if want == 0 else f" (claimed {_fmt(want)}%)"
        return ("TRUE" if ok else "FALSE"), ev + tail, ["rpc mint account"]
    if k == "graduated":
        if chain.launchpad != "pump.fun":
            return "UNPROVEN", "not a pump.fun mint, so there is no curve to graduate from", ["rpc mint account"]
        if chain.graduated is None:
            return "UNPROVEN", "no trading pair found", ["dexscreener"]
        ev = f"main pair trades on {chain.dex}" if chain.graduated else "still trading on the pump.fun curve"
        return _flip("TRUE" if chain.graduated else "FALSE", c.negated), ev, ["dexscreener"]
    if k == "age":
        if chain.pairAgeHours is None or c.claimedAgeHours is None:
            return "UNPROVEN", "no pair creation time", ["dexscreener"]
        tol = max(1.0, AGE_TOLERANCE * c.claimedAgeHours)
        ok = abs(chain.pairAgeHours - c.claimedAgeHours) <= tol
        days = chain.pairAgeHours / 24
        ev = (
            f"the main pair is {_fmt(days)} days old"
            if days >= 2
            else f"the main pair is {_fmt(chain.pairAgeHours)} hours old"
        )
        return ("TRUE" if ok else "FALSE"), ev, ["dexscreener pairCreatedAt"]
    if k == "offchain":
        return "UNPROVEN", "the chain cannot see who leads a community, audits or partners", []
    return "UNPROVEN", "not a claim I can check on chain", []


def check_claims(chain: ChainLore, claims: list[ParsedClaim], custom: bool) -> list[ClaimCheck]:
    out = []
    for i, c in enumerate(claims):
        v, ev, srcs = judge(c, chain)
        label = LABEL[c.type]
        if c.negated:
            label = f"not {label}"
        if c.claimedPct is not None and c.type == "lp_locked":
            label = f"LP {_fmt(c.claimedPct)}% locked"
        if c.claimedPct and c.type == "no_tax":
            label = f"{_fmt(c.claimedPct)}% tax"
        if c.type == "age" and c.claimedAgeHours is not None:
            label = (
                f"about {_fmt(c.claimedAgeHours / 24)} days old"
                if c.claimedAgeHours >= 48
                else f"about {_fmt(c.claimedAgeHours)} hours old"
            )
        out.append(
            ClaimCheck(
                n=i + 1,
                type=c.type,
                label=label,
                verdict=v,
                evidence=ev,
                sources=srcs,
                custom=custom,
                lesson=LESSON.get(c.type),
            )
        )
    return out


# ---------------------------------------------------------------------- collectors
def _extensions(acc: dict | None) -> tuple[bool | None, int | None, bool | None, str | None, str | None, bool]:
    """(is_2022, fee_bps, hook, mint_auth, freeze_auth, known) from a jsonParsed getAccountInfo result."""
    v = (acc or {}).get("value") if isinstance(acc, dict) else None
    if not v or not isinstance(v.get("data"), dict):
        return None, None, None, None, None, False
    parsed = v["data"].get("parsed") or {}
    if parsed.get("type") != "mint":
        return None, None, None, None, None, False
    info = parsed.get("info") or {}
    is_2022 = v.get("owner") == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    fee, hook = 0, False
    for ext in info.get("extensions") or []:
        st = ext.get("state") or {}
        if ext.get("extension") == "transferFeeConfig":
            cfg = st.get("newerTransferFee") or st.get("olderTransferFee") or {}
            fee = max(fee, int(cfg.get("transferFeeBasisPoints") or 0))
        elif ext.get("extension") == "transferHook" and st.get("programId"):
            hook = True
    return is_2022, fee, hook, info.get("mintAuthority"), info.get("freezeAuthority"), True


async def _creator_pct(env, creator: str, mint: str, supply_ui: float | None) -> float | None:
    if not supply_ui:
        return None
    accts = await env.rpc.token_accounts(creator)
    held = sum(a.get("ui_amount") or 0 for a in accts if a.get("mint") == mint)
    return round(100 * held / supply_ui, 2)


async def build_lore_facts(env, subject, claims: list[ParsedClaim] | None = None) -> tuple[Facts, list[ClaimCheck]]:
    """Token facts plus the chain lore and one check per claim (default claims when none are given)."""
    mint = subject.key
    facts, report, acc = await asyncio.gather(
        build_token_facts(env, subject),
        _safe(env.rug.report(mint)),
        _safe(env.rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])),
    )
    now = time.time()
    t = facts.token
    rep = report if _ok(report) else {}
    is_2022, fee, hook, mint_auth, freeze_auth, known = _extensions(acc if _ok(acc) else None)
    creator = rep.get("creator") if isinstance(rep.get("creator"), str) else None
    held = await _safe(_creator_pct(env, creator, mint, t.supply.total)) if creator else None
    dex = (t.dex or "").lower() or None
    launchpad = "pump.fun" if mint.endswith("pump") else None
    graduated = None if dex is None else (dex not in CURVE_DEXES)
    insiders = rep.get("insiders_detected")
    chain = ChainLore(
        mint=mint,
        launchpad=launchpad,
        dex=dex,
        graduated=graduated if launchpad else None,
        lpLockedPct=t.lpLockedPct,
        mintAuthority=mint_auth,
        freezeAuthority=freeze_auth,
        authoritiesKnown=known,
        isToken2022=is_2022,
        transferFeeBps=fee,
        transferHook=hook,
        creator=creator,
        creatorHoldsPct=held if _ok(held) else None,
        insidersDetected=int(insiders)
        if isinstance(insiders, (int, float)) and not isinstance(insiders, bool)
        else None,
        top10Pct=t.holders.top10Pct,
        pairAgeHours=t.ageHours,
    )
    facts.sources += [
        Source(name="rugcheck report (creator, insiders)", fetchedAt=now, ok=_ok(report)),
        Source(name="rpc mint account (authorities, extensions)", fetchedAt=now, ok=known),
    ]
    if creator:
        facts.sources.append(Source(name="rpc creator token accounts", fetchedAt=now, ok=_ok(held)))
    custom = bool(claims)
    parsed = claims or [ParsedClaim(type=k) for k in DEFAULT_CLAIMS if k != "graduated" or launchpad]
    checks = check_claims(chain, parsed, custom)
    facts.extra.pop("chart", None)
    facts.extra["lore"] = {
        "chain": chain.model_dump(exclude_none=True),
        "claims": [c.model_dump(exclude_none=True) for c in checks],
        "verdictVocabulary": ["TRUE", "FALSE", "UNPROVEN"],
        "asOf": now,
    }
    for c, p in zip(checks, parsed, strict=True):
        if p.raw:
            facts.untrusted[f"claim{c.n}"] = p.raw
    facts.unknowns += [
        "origin timeline (first posts naming the CA, first buyers and their funders) is not built yet",
        "linked dev wallets are not traced; dev checks cover the creator wallet only",
    ]
    return facts, checks


# ---------------------------------------------------------------------- writer instructions and template
LORE_INSTRUCTIONS = (
    "This is a LORE reading: claims about a token checked against the chain. FACTS.extra.lore.claims holds one "
    "check per claim with a verdict computed in code: TRUE, FALSE or UNPROVEN. Write one line per claim in order, "
    "starting with a rune (ᚠ ᚢ ᚦ ᚨ ...), the claim label in quotes, the verdict word exactly as given, and the "
    "evidence in your own short words. Never change a verdict, never add claims, never quote FACTS.untrusted. "
    "Open with one short wizard line. Scroll sections: Claims (with each claim's sources), Chain, Unknowns."
)


def template(facts: Facts) -> tuple[str, str, str]:
    lore = facts.extra.get("lore") or {}
    claims = lore.get("claims") or []
    sym = f"${facts.token.symbol}" if facts.token and facts.token.symbol else "this token"
    lines = [f"📜 I unrolled the oldest scroll of {sym}."]
    family = lore.get("family")
    if family and family != "other":
        lines.append(f"Family: {family}.")
    for c in claims:
        rune = RUNES[(c["n"] - 1) % len(RUNES)]
        lines.append(f"{rune} '{c['label']}' {c['verdict']}: {c['evidence']}")
    body = ["## Claims"]
    for c in claims:
        rune = RUNES[(c["n"] - 1) % len(RUNES)]
        src = ", ".join(c.get("sources") or []) or "none (not checkable on chain)"
        body += [f"- {rune} **{c['label']}**: {c['verdict']}", f"  - {c['evidence']}", f"  - sources: {src}"]
    ch = lore.get("chain") or {}
    body += ["## Chain"]
    for key, name in (
        ("launchpad", "Launchpad"),
        ("dex", "Main pair dex"),
        ("lpLockedPct", "LP locked %"),
        ("creator", "Creator"),
        ("creatorHoldsPct", "Creator holds %"),
        ("insidersDetected", "Insider wallets (RugCheck)"),
        ("top10Pct", "Top 10 %"),
        ("transferFeeBps", "Transfer fee (bps)"),
        ("pairAgeHours", "Pair age (hours)"),
    ):
        if key in ch:
            body.append(f"- {name}: {ch[key]}")
    body += ["## Unknowns", *([f"- The mist hides: {u}" for u in facts.unknowns] or ["- Nothing hidden."])]
    return "\n".join(lines), f"Lore of {sym}", "\n".join(body)


def pick_lesson(checks: list[ClaimCheck]) -> str | None:
    for v in ("FALSE", "UNPROVEN", "TRUE"):
        for c in checks:
            if c.verdict == v and c.lesson:
                return c.lesson
    return None


# ---------------------------------------------------------------------- pipeline and surfaces
async def lore_read(ctx, args: str, *, user_id: int | None, surface: str):
    """Lore reading for '<ticker|CA> ["claim" ...]'. Returns (Scroll, url, cached, dropped_claims)."""
    from heavymetal.wizzy import scrolls
    from heavymetal.wizzy.prophecy.narratives import tag_cached
    from heavymetal.wizzy.reading import llm_allowed
    from heavymetal.wizzy.resolve import ResolveError, resolve_subject
    from heavymetal.wizzy.writer import write

    target, texts = split_claims(args)
    tier = ctx.wdb.tier(user_id) if user_id is not None else "free"
    cap = CUSTOM_CLAIMS.get(tier, CUSTOM_CLAIMS["free"])
    dropped = max(0, len(texts) - cap)
    texts = texts[:cap]
    rid = ctx.wdb.add_request(user_id, surface, args[:500], "lore")
    t0 = time.time()
    async with ctx.readings_sem:
        try:
            subject = await resolve_subject(ctx.env, target)
        except ResolveError:
            ctx.wdb.finish_request(rid, "unresolved")
            raise
        if subject.kind != "token":
            ctx.wdb.finish_request(rid, "unresolved")
            raise ResolveError("lore reads tokens, and that address is a wallet")
        sid = ctx.wdb.subject("token", subject.key, subject.symbol)
        if not texts:
            prev = scrolls.recent_for_subject(ctx.wdb, sid, "lore", ctx.wcfg.dedupe_ca_minutes * 60)
            if prev is not None and not any(c.get("custom") for c in (prev.meta or {}).get("claims", [])):
                ctx.wdb.finish_request(rid, "reused", sid)
                return prev, scrolls.public_url(ctx.wcfg.base_url, prev.slug), True, dropped
        parsed = [parse_claim(x) for x in texts]
        facts, checks = await build_lore_facts(ctx.env, subject, parsed)
        try:
            facts.extra["lore"]["family"] = tag_cached(ctx.wdb, subject.key, facts.token.symbol, subject.name)
        except Exception:  # noqa: BLE001 - the family line is optional
            pass
        lesson = pick_lesson(checks)
        if lesson:
            facts.extra["lore"]["lesson"] = lesson
        written = await write(
            ctx.writer if llm_allowed(ctx) else None,
            facts,
            effort=ctx.wcfg.writer_effort,
            instructions=LORE_INSTRUCTIONS,
            template_fn=template,
        )
        meta = {
            "claims": [{"type": c.type, "verdict": c.verdict, "custom": c.custom} for c in checks],
            "lesson": lesson,
            "receipts": facts.extra["lore"]["claims"],
        }
        slug = scrolls.save(
            ctx.wdb,
            label=f"lore-{facts.token.symbol or 'token'}",
            kind="lore",
            title=written.title,
            reading=written.reading,
            body_md=written.body,
            omen=facts.token.omen.kind if facts.token.omen else None,
            subject_id=sid,
            request_id=rid,
            price_usdc=ctx.wcfg.scroll_price_usdc,
            meta=meta,
        )
    ctx.wdb.finish_request(
        rid, "templated" if written.templated else "ok", sid, written.cost_usd, int((time.time() - t0) * 1000)
    )
    return scrolls.get(ctx.wdb, slug), scrolls.public_url(ctx.wcfg.base_url, slug), False, dropped


USAGE = 'Usage: /lore <ticker or CA>, or /lore <CA> "claim text"'


async def lore_reply(ctx, user_id: int, args: str, surface: str):
    from heavymetal.wizzy import limits
    from heavymetal.wizzy.bots.core import Button, Reply
    from heavymetal.wizzy.resolve import ResolveError

    if not (args or "").strip():
        return Reply("error", text=USAGE)
    v = limits.check_reading(ctx, user_id, surface)
    if not v.allowed:
        return Reply(
            "limited", text=f"The old scrolls need rest. Try again in about {max(1, v.retry_after_s // 60)} min."
        )
    try:
        scroll, url, _cached, dropped = await lore_read(ctx, args, user_id=user_id, surface=surface)
    except ResolveError as e:
        return Reply("error", text=f"The mists hide that: {e}. {USAGE}")
    text = scroll.reading
    lesson = (scroll.meta or {}).get("lesson")
    tail = f"receipts at {url}" + (f" · lesson: {lesson}" if lesson else "")
    if dropped:
        tail += f" · {dropped} extra claim(s) skipped (Inner Circle checks up to {CUSTOM_CLAIMS['inner']})"
    return Reply("reading", scroll.title, f"{text}\n{tail}", url=url, buttons=[Button("📜 Receipts", url=url)])


def register() -> None:
    from heavymetal.wizzy.bots import core, tg

    async def intent(ctx, user_id, it, surface):
        return await lore_reply(ctx, user_id, it.arg, surface)

    async def command(ctx, message, args):
        uid = ctx.wdb.user(tg_id=str(message.from_user.id))["id"]
        return await lore_reply(ctx, uid, args, "tg")

    core.EXTRA_INTENTS["lore"] = intent
    tg.EXTRA_COMMANDS["lore"] = command
    tg.EXTRA_HELP.append('\n/lore &lt;ticker|CA&gt; ["claim"] · check a token\'s claims against the chain')
