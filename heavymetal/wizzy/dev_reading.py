"""/dev <ticker | CA | link>: the hand behind a token. Facts come from wizzy.dev; the writer only phrases them."""

from __future__ import annotations

import time

from heavymetal.wizzy.dev import PROGRAM_AUTHORITIES, DevCache, dev_facts, short
from heavymetal.wizzy.facts import DevFacts, Facts, Subject
from heavymetal.wizzy.omen import ESTABLISHED
from heavymetal.wizzy.writer import FOOTER, usd, with_footer

WRITER_COINS = 20  # the writer sees the newest coins; the scroll's full list is rendered by code
SIGIL_GLYPH = {"serial summoner": "☠", "mixed sigils": "🌫", "clean hands": "✨", "fresh hand": "🌱"}
SIGIL_HEAD = {
    "serial summoner": "The bones remember this hand.",
    "mixed sigils": "This hand has left both marks and scars.",
    "clean hands": "No rugs on record for this hand.",
    "fresh hand": "A fresh hand: no past to read, which is not the same as clean.",
}
DISCLAIMER = "Facts only: outcomes are what the chain recorded, not a verdict on intent."
DEV_INSTRUCTIONS = (
    "This is a DEV HISTORY reading (/dev). Narrate FACTS.dev: the creator (short form), its sigil, the counts of "
    "earlier tokens by outcome, and FACTS.extra.omenEffect. Funding: say 'funded by', never that wallets are the "
    "same person. A fresh hand is 'no past to read', never 'clean'. If FACTS.dev.creatorExited is true, say the "
    "original creator no longer holds the token. If the creator is unresolved, say so and stop. Unknown outcomes "
    "never count against the dev. End the reading with: " + DISCLAIMER + " Scroll sections: The creator, The "
    "record, Funding, Unknowns (the past-coin list is appended by code, don't write it)."
)


def omen_effect(mint: str, d: DevFacts, mint_authority: str | None = None) -> str:
    if mint in ESTABLISHED or mint_authority in PROGRAM_AUTHORITIES:
        return "none: established or program-authority mint, exempt from dev history"
    if d.status != "resolved":
        return "none: creator not cross-checked"
    if d.sigil == "serial summoner":
        return "pulls the omen to cursed (dev history)"
    if d.sigil == "mixed sigils":
        return "caps the omen at murky (dev history)"
    return "none"


def funding_line(d: DevFacts) -> str:
    if not d.funders:
        return ""
    hop1 = [f for f in d.funders if f.hop == 1]
    labeled = sorted({f.label for f in d.funders if f.label and f.label != "program"})
    line = f"Funded by {len(hop1)} wallet(s) in the 72 h before deploy"
    if d.fundersLinkedRugs:
        rugs = max(f.fundedRugs for f in d.funders)
        line += f"; {d.fundersLinkedRugs} of them also funded up to {rugs} earlier rugged launches of this creator"
    if labeled:
        line += f"; the trace stops at labeled wallets ({', '.join(labeled)})"
    return line + "."


def coin_rows(d: DevFacts) -> list[str]:
    rows = []
    for c in d.pastCoins:
        sym = f"${c.symbol}" if c.symbol else short(c.mint)
        age = f"{c.ageDays} days" if c.ageDays is not None else "age ?"
        rows.append(
            f"- {sym} `{c.mint}` · {age} · peak liquidity seen {usd(c.peakLiquidityUsd)} · "
            f"**{c.outcome}** ({c.why}) · [source]({c.link})"
        )
    return rows or ["- No earlier tokens found in the window."]


def template(facts: Facts) -> tuple[str, str, str]:
    d = facts.dev
    eff = facts.extra.get("omenEffect", "none")
    if d is not None and d.creator is not None and d.sigil is None:
        line = d.line or f"Dev {short(d.creator)}: the creator's history couldn't be read."
        body = ["## The creator", f"- Wallet: `{d.creator}` ({d.status.replace('_', ' ')})", "## Unknowns"]
        body += [f"- The mist hides: {u}" for u in d.unknowns] or ["- Nothing hidden."]
        return f"🌫 {line} {DISCLAIMER}", f"The hand behind {short(facts.subject.mint)}: unread", "\n".join(body)
    if d is None or d.creator is None:
        why = "the sources disagree" if d is not None and d.status == "unresolved" else "the mists hide it"
        reading = f"🌫 The hand behind this token can't be named: {why}. {DISCLAIMER}"
        return (
            reading,
            "The hand: unresolved",
            "\n".join(["## Unknowns", *[f"- {u}" for u in (d.unknowns if d else [])]]),
        )
    parts = [f"{SIGIL_GLYPH[d.sigil]} {SIGIL_HEAD[d.sigil]}", d.line]
    if fl := funding_line(d):
        parts.append(fl)
    parts.append(f"Sigil: {d.sigil}" + (f" · {eff}" if not eff.startswith("none") else "") + ".")
    parts.append(DISCLAIMER)
    body = [
        "## The creator",
        f"- Wallet: `{d.creator}` ({d.status.replace('_', ' ')}; {', '.join(d.creatorSources) or 'no source'})",
        *([f"- Launchpad: {d.launchpad}"] if d.launchpad else []),
        *(["- The original creator no longer holds this token."] if d.creatorExited else []),
        "## The record",
        f"- {d.line}",
        f"- Sigil: {d.sigil}; omen effect: {eff}",
        "## Funding",
        *(
            [
                f"- funded by `{f.wallet}` · {f.solIn} SOL · hop {f.hop}" + (f" · {f.label}" if f.label else "")
                for f in d.funders
            ]
            or ["- No SOL funding found in the 72 h before deploy (or not traced)."]
        ),
        "## Unknowns",
        *([f"- The mist hides: {u}" for u in d.unknowns] or ["- Nothing hidden."]),
    ]
    return "\n".join(parts), f"The hand behind {short(facts.subject.mint)}: {d.sigil}", "\n".join(body)


def facts_for(mint: str, symbol: str | None, d: DevFacts, effect: str) -> Facts:
    shown = d.model_copy(update={"pastCoins": d.pastCoins[:WRITER_COINS]})
    return Facts(
        asOf=d.asOf or time.time(),
        subject=Subject(kind="token", mint=mint, symbol=symbol),
        sources=d.sources,
        dev=shown,
        unknowns=d.unknowns,
        extra={"omenEffect": effect, "coinsShownToWriter": len(shown.pastCoins)},
    )


async def dev_read(ctx, text: str, *, user_id: int | None, surface: str):
    """Returns (Scroll, url, cached)."""
    from heavymetal.wizzy import scrolls
    from heavymetal.wizzy.reading import llm_allowed
    from heavymetal.wizzy.resolve import ResolveError, resolve_subject
    from heavymetal.wizzy.writer import write

    rid = ctx.wdb.add_request(user_id, surface, text, "dev")
    t0 = time.time()
    async with ctx.readings_sem:
        subject = await resolve_subject(ctx.env, text)
        if subject.kind != "token":
            ctx.wdb.finish_request(rid, "unresolved")
            raise ResolveError("/dev reads tokens; send a ticker, contract address or link")
        sid = ctx.wdb.subject("token", subject.key, subject.symbol)
        prev = scrolls.recent_for_subject(ctx.wdb, sid, "dev", ctx.wcfg.dedupe_ca_minutes * 60)
        if prev is not None:
            ctx.wdb.finish_request(rid, "reused", sid)
            return prev, scrolls.public_url(ctx.wcfg.base_url, prev.slug), True
        inner = user_id is not None and ctx.wdb.tier(user_id) == "inner"
        d = await dev_facts(ctx.env, subject.key, hops=2 if inner else 1, cache=DevCache(ctx.wdb, ctx.wcfg.dev_ttl_s))
        chain = None
        try:
            chain = await ctx.env.rpc.mint_info(subject.key)
        except Exception:  # noqa: BLE001 - authority unknown: no exemption
            chain = None
        effect = omen_effect(subject.key, d, (chain or {}).get("mint_authority"))
        facts = facts_for(subject.key, subject.symbol, d, effect)
        written = await write(
            ctx.writer if llm_allowed(ctx) else None,
            facts,
            effort=ctx.wcfg.writer_effort,
            instructions=DEV_INSTRUCTIONS,
            template_fn=template,
        )
        coins = "\n\n## Past coins\n" + "\n".join(coin_rows(d))
        body = with_footer(written.body.replace(FOOTER, "").rstrip() + coins)
        omen = (
            {"serial summoner": "cursed", "mixed sigils": "murky"}.get(d.sigil or "") if effect[:4] != "none" else None
        )
        slug = scrolls.save(
            ctx.wdb,
            label=f"{subject.symbol or 'token'}-dev",
            kind="dev",
            title=written.title,
            reading=written.reading,
            body_md=body,
            omen=omen,
            subject_id=sid,
            request_id=rid,
            price_usdc=ctx.wcfg.scroll_price_usdc,
            paywalled=not inner,
            meta={"sigil": d.sigil, "creator": d.creator, "status": d.status, "counts": d.counts},
        )
    ctx.wdb.finish_request(
        rid, "templated" if written.templated else "ok", sid, written.cost_usd, int((time.time() - t0) * 1000)
    )
    return scrolls.get(ctx.wdb, slug), scrolls.public_url(ctx.wcfg.base_url, slug), False


async def dev_reply(ctx, user_id: int, text: str, surface: str):
    from heavymetal.wizzy import limits
    from heavymetal.wizzy.bots.core import Button, Reply
    from heavymetal.wizzy.resolve import ResolveError

    v = limits.check_reading(ctx, user_id, surface)
    if not v.allowed:
        mins = max(1, v.retry_after_s // 60)
        return Reply("limited", text=f"The bones need rest. Try again in about {mins} min. (/inner lifts limits)")
    try:
        scroll, url, _cached = await dev_read(ctx, text, user_id=user_id, surface=surface)
    except ResolveError as e:
        return Reply("error", text=f"The mists hide that: {e}. Send /dev <ticker | CA | link>.")
    return Reply("reading", scroll.title, scroll.reading, url=url, buttons=[Button("📜 Full scroll", url=url)])


def register() -> None:
    from heavymetal.wizzy.bots import core, tg

    async def intent(ctx, user_id, it, surface):
        return await dev_reply(ctx, user_id, it.arg, surface)

    async def command(ctx, message, args):
        from heavymetal.wizzy.bots.core import Reply

        if not args.strip():
            return Reply("error", text="Usage: /dev <ticker | CA | link>")
        uid = ctx.wdb.user(tg_id=str(message.from_user.id))["id"]
        return await dev_reply(ctx, uid, args, "tg")

    core.EXTRA_INTENTS["dev"] = intent
    tg.EXTRA_COMMANDS["dev"] = command
    tg.EXTRA_HELP.append("\n/dev &lt;ticker | CA | link&gt; · the hand behind a token: its creator's past coins")
