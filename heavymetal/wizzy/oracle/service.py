"""/oracle @handle and /ledger: grade -> KolFacts -> writer (guarded) -> scroll with receipts."""

from __future__ import annotations

import datetime as dt
import time

from heavymetal.wizzy import guard, jobs, limits, scrolls
from heavymetal.wizzy.facts import Facts, KolFacts, Source, Subject
from heavymetal.wizzy.oracle import calls as calls_mod
from heavymetal.wizzy.oracle import grade, stats

USAGE = "Usage: /oracle @handle. I grade only calls I witnessed: CA posts that mentioned me."
ORACLE_INSTRUCTIONS = (
    "This is an ORACLE reading: the track record of calls posted by FACTS.kol.handle, graded in code. Use only "
    "evidence words (posted, price at post, price at 24h/7d/30d, graded, voided). Never describe the person: no "
    "adjectives about them, no claims about intent or payment, never 'scam', 'fraud' or 'rugger'. State hit "
    "rate, median and worst return per horizon exactly as given, the sigil and its rule, and how many calls were "
    "ambiguous or voided. scrollTitle: short. scrollBodyMarkdown: '## Record' with the numbers per horizon, then "
    "'## How calls are graded'."
)
GRADING_RULES = [
    "A call is a CA in an original post or reply by the author that I witnessed; cashtag-only posts are not counted.",
    "Baseline: price frozen when I saw the post, else the hourly candle open at post time.",
    "Graded at 24h, 7d and 30d with candles stamped after the post. Hit = above the baseline.",
    "A drained pool is graded at its last traded price. No market data voids a call; it never counts as a miss.",
    "Rug: down 90% or more, or no trades near the horizon after a 50% fall.",
]
HL = stats.HKEY


def horizons_for(ctx, user_id: int | None) -> tuple[int, ...]:
    """Free: 7d; Inner Circle: 24h, 7d and 30d (spec Free vs Inner Circle table)."""
    return grade.HORIZONS_H if user_id is not None and ctx.wdb.tier(user_id) == "inner" else (168,)


def oracle_facts(kol: KolFacts, now: float, history_name: str = "geckoterminal") -> Facts:
    return Facts(
        asOf=now,
        subject=Subject(kind="kol"),
        sources=[Source(name="wizzy witnessed calls", fetchedAt=now), Source(name=history_name, fetchedAt=now)],
        kol=kol,
        extra={"oracle": {"howGraded": GRADING_RULES}},
        unknowns=["deleted posts are only known when a collector re-reads them"] if not kol.calls else [],
    )


def _p(v: float | None) -> str:
    return "?" if v is None else f"{v:+.1f}%"


def _short(m: str | None) -> str:
    return f"{m[:4]}…{m[-4:]}" if m else "?"


def template(facts: Facts) -> tuple[str, str, str]:
    k = facts.kol
    lead = f"🔮 The Ledger remembers what was spoken. @{k.handle}: "
    if not k.calls:
        reading = lead + f"no calls witnessed in {k.windowDays} days. Sigil: {k.sigil}."
    else:
        parts = [f"{k.calls} calls witnessed in {k.windowDays} days."]
        for h in k.horizons:
            if h.graded:
                parts.append(
                    f"{HL[h.horizonH]}: {h.hits} of {h.graded} stood higher ({h.hitRatePct}%), "
                    f"median {_p(h.medianReturnPct)}, worst {_p(h.worstReturnPct)}."
                )
            else:
                parts.append(f"{HL[h.horizonH]}: none graded yet ({h.pending} unfolding, {h.voids} voided).")
        if k.ambiguous:
            parts.append(f"{k.ambiguous} cashtag-only posts not counted.")
        parts.append(f"Sigil: {k.sigil} ({k.sigilRule}).")
        reading = lead + " ".join(parts)
    body = ["## Record", f"- Calls: {k.calls} (witnessed {k.witnessed}, backfilled {k.backfilled})"]
    for h in k.horizons:
        body.append(
            f"- {HL[h.horizonH]}: graded {h.graded}, hits {h.hits}, hit rate {h.hitRatePct if h.hitRatePct is not None else '?'}%, "
            f"median {_p(h.medianReturnPct)}, worst {_p(h.worstReturnPct)}, voided {h.voids}, unfolding {h.pending}"
        )
    body += [
        f"- Rugs among calls: {k.rugs}",
        f"- Ambiguous (not counted): {k.ambiguous} · excluded: {k.excluded} · deleted: {k.deleted}",
        f"- Sigil: {k.sigil} ({k.sigilRule})",
        "## How calls are graded",
        *[f"- {r}" for r in GRADING_RULES],
    ]
    return reading, f"Oracle: @{k.handle}", "\n".join(body)


def receipts_md(k: KolFacts) -> str:
    """Code-built receipts: every row links the post and the candle source."""
    if not k.receipts:
        return ""
    lines = [
        "## Receipts",
        "| Posted (UTC) | Mint | Source | 24h | 7d | 30d | Post | Prices |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in k.receipts:
        when = dt.datetime.fromtimestamp(r.postedAt, dt.UTC).strftime("%Y-%m-%d %H:%M")
        rets = [_p(r.returnsPct.get(x)) if r.returnsPct.get(x) is not None else "–" for x in ("24h", "7d", "30d")]
        status = r.source + (" · ambiguous" if r.status == "ambiguous" else "") + (" · deleted" if r.deleted else "")
        status += " · rug" if r.rug else ""
        price = f"[candles]({r.priceSourceUrl})" if r.priceSourceUrl else "–"
        lines.append(f"| {when} | {r.mint or '–'} | {status} | {' | '.join(rets)} | [post]({r.postUrl}) | {price} |")
    lines.append("\nOnly what the chain recorded after each post; no claim about intent.")
    return "\n".join(lines)


async def oracle(ctx, handle_text: str, *, user_id: int | None = None, surface: str = "cli", now: float | None = None):
    """Grade what's due for the handle, build facts, write, save a scroll. Returns (Facts, Written, url)."""
    from heavymetal.wizzy.reading import llm_allowed
    from heavymetal.wizzy.writer import write

    h = calls_mod.norm_handle(handle_text)
    if h is None:
        raise ValueError("not an X handle")
    now = now or time.time()
    rid = ctx.wdb.add_request(user_id, surface, f"oracle @{h}", "oracle")
    rows = calls_mod.calls_for(ctx.wdb, h, now - stats.WINDOW_DAYS * 86400)
    await grade.grade_calls(ctx, rows, now=now)
    if ctx.wcfg.oracle_x_collector:
        jobs.enqueue(ctx.wdb, "oracle_collect", {"handle": h}, dedupe_key=f"oracle_collect:{h}:{int(now // 86400)}")
    kol = stats.build(ctx.wdb, h, now, horizons_for(ctx, user_id))
    facts = oracle_facts(kol, now)
    written = await write(
        ctx.writer if llm_allowed(ctx) else None,
        facts,
        effort="low",
        instructions=ORACLE_INSTRUCTIONS,
        template_fn=template,
        extra_check=guard.check_people_words,
    )
    body = written.body + ("\n\n" + receipts_md(kol) if kol.receipts else "")
    sid = ctx.wdb.subject("kol", f"kol:{h}", None)
    slug = scrolls.save(
        ctx.wdb,
        label=f"oracle-{h}",
        kind="oracle",
        title=written.title,
        reading=written.reading,
        body_md=body,
        omen=None,
        subject_id=sid,
        request_id=rid,
        price_usdc=ctx.wcfg.scroll_price_usdc,
        meta={"handle": h, "sigil": kol.sigil},
    )
    ctx.wdb.finish_request(rid, "templated" if written.templated else "ok", sid, written.cost_usd)
    return facts, written, scrolls.public_url(ctx.wcfg.base_url, slug)


def ledger_text(ctx, now: float | None = None) -> str:
    now = now or time.time()
    w = ctx.wcfg
    table = stats.ledger(ctx.wdb, now, w.oracle_ledger_min_calls, w.oracle_ledger_optout)
    week = dt.datetime.fromtimestamp(now, dt.UTC).isocalendar().week
    head = [
        f"📜 Archmage's Ledger · week {week}",
        f"Ranked by 7d median return, {w.oracle_ledger_min_calls}+ witnessed calls.",
    ]
    if not table:
        return "\n".join(head + ["No caller has enough witnessed, graded calls yet. The Ledger waits."])
    rows = [
        f"{i}. @{r['handle']}  {_p(r['medianReturnPct'])} median · {r['calls']} calls · worst {_p(r['worstReturnPct'])}"
        for i, r in enumerate(table[:10], 1)
    ]
    return "\n".join(head + rows)


async def record_mentions(ctx, tweets: list[dict], wizzy_user_id: str | None = None) -> int:
    """Witnessed calls: CA posts that mention Wizzy, recorded for their author. Baselines freeze via a job."""
    if not (ctx.wcfg.oracle_enabled and ctx.wcfg.oracle_witness_mentions):
        return 0
    n = 0
    for t in tweets:
        if not t.get("username") or t.get("author_id") == str(wizzy_user_id):
            continue
        ids = calls_mod.record_post(
            ctx.wdb,
            handle=t["username"],
            post_id=t["id"],
            text=t.get("text") or "",
            author_id=t.get("author_id"),
            posted_at=t.get("created_at"),
            via="mention",
        )
        for cid in ids:
            jobs.enqueue(ctx.wdb, "oracle_freeze", {"id": cid}, dedupe_key=f"oracle_freeze:{cid}")
        n += len(ids)
    return n


async def freeze_job(ctx, svc, payload: dict) -> dict:
    return {"result": await grade.freeze_baseline(ctx, int(payload["id"]))}


async def collect_job(ctx, svc, payload: dict) -> dict:
    from heavymetal.wizzy.oracle.collector import NullTimeline, TweepyTimeline, collect

    x = getattr(svc, "x", None)
    provider = TweepyTimeline(x.c) if x is not None and hasattr(x, "c") else NullTimeline()
    return await collect(ctx, provider, payload["handle"])


async def oracle_reply(ctx, user_id: int, handle_text: str, surface: str):
    from heavymetal.wizzy.bots.core import Button, Reply

    if not ctx.wcfg.oracle_enabled:
        return Reply("chitchat", text="The oracle is resting.")
    if calls_mod.norm_handle(handle_text) is None:
        return Reply("error", text=USAGE)
    v = limits.check_reading(ctx, user_id, surface)
    if not v.allowed:
        return Reply("limited", text=f"The candles need rest. Try again in about {max(1, v.retry_after_s // 60)} min.")
    _facts, written, url = await oracle(ctx, handle_text, user_id=user_id, surface=surface)
    return Reply("oracle", written.title, written.reading, url=url, buttons=[Button("📜 Receipts", url=url)])


def register() -> None:
    from heavymetal.wizzy import services
    from heavymetal.wizzy.bots import core, tg

    services.JOB_HANDLERS.update({"oracle_freeze": freeze_job, "oracle_collect": collect_job})

    async def oracle_intent(ctx, user_id, it, surface):
        return await oracle_reply(ctx, user_id, it.arg, surface)

    async def ledger_intent(ctx, user_id, it, surface):
        return core.Reply("ledger", text=ledger_text(ctx))

    core.EXTRA_INTENTS["oracle"] = oracle_intent
    core.EXTRA_INTENTS["ledger"] = ledger_intent

    async def oracle_cmd(ctx, message, args):
        return await oracle_reply(ctx, ctx.wdb.user(tg_id=str(message.from_user.id))["id"], args.strip(), "tg")

    async def ledger_cmd(ctx, message, args):
        return core.Reply("ledger", text=ledger_text(ctx))

    tg.EXTRA_COMMANDS["oracle"] = oracle_cmd
    tg.EXTRA_COMMANDS["ledger"] = ledger_cmd
    tg.EXTRA_HELP.append("\n/oracle @handle · a caller's graded record · /ledger · the Archmage's Ledger")
