"""Aggregate graded calls into KolFacts and a sigil by fixed thresholds. No model call anywhere here."""

from __future__ import annotations

import statistics
import time

from heavymetal.wizzy.facts import KolFacts, KolHorizon, KolReceipt
from heavymetal.wizzy.oracle import calls as calls_mod
from heavymetal.wizzy.oracle.grade import DRAIN_FALL_PCT, HORIZONS_H, RUG_RETURN_PCT

WINDOW_DAYS = 90
SIGIL_MIN_GRADED = 5  # sample-size floor: under this the stars are unread
SIGILS = {
    "unread": ("unread stars", f"fewer than {SIGIL_MIN_GRADED} calls graded at 7d"),
    "held": ("calls held", "7d hit rate 60% or more and a positive 7d median"),
    "faded": ("calls faded", "7d hit rate 25% or less, or a 7d median of -50% or worse"),
    "mixed": ("mixed sigils", "between the other thresholds"),
}
HKEY = {24: "24h", 168: "7d", 720: "30d"}


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def sigil(h7: KolHorizon) -> tuple[str, str]:
    if h7.graded < SIGIL_MIN_GRADED:
        return SIGILS["unread"]
    if (h7.hitRatePct or 0) >= 60 and (h7.medianReturnPct or 0) > 0:
        return SIGILS["held"]
    if (h7.hitRatePct or 0) <= 25 or (h7.medianReturnPct or 0) <= -50:
        return SIGILS["faded"]
    return SIGILS["mixed"]


def build(wdb, handle: str, now: float | None = None, horizons: tuple[int, ...] = HORIZONS_H) -> KolFacts:
    now = now or time.time()
    rows = calls_mod.calls_for(wdb, handle, now - WINDOW_DAYS * 86400)
    grades: dict[int, dict[int, object]] = {}
    for g in wdb.all(
        "SELECT g.* FROM kol_grades g JOIN kol_calls c ON c.id=g.call_id WHERE c.handle=? AND c.ts>=?",
        (handle, now - WINDOW_DAYS * 86400),
    ):
        grades.setdefault(g["call_id"], {})[g["horizon_h"]] = g
    counted = [r for r in rows if r["status"] == "counted"]
    out = KolFacts(
        handle=handle,
        windowDays=WINDOW_DAYS,
        calls=len(counted),
        witnessed=sum(r["source"] == "witnessed" for r in counted),
        backfilled=sum(r["source"] == "backfilled" for r in counted),
        ambiguous=len({r["post_id"] for r in rows if r["status"] == "ambiguous"}),
        excluded=sum(r["status"] == "excluded" for r in rows),
        deleted=len({r["post_id"] for r in rows if r["deleted"]}),
        sigil="",
        sigilRule="",
    )
    rugs = set()
    for h in HORIZONS_H:
        hz = KolHorizon(horizonH=h)
        rets, worst = [], None
        for r in counted:
            g = grades.get(r["id"], {}).get(h)
            if g is None or g["status"] == "pending":
                hz.pending += 1
                continue
            if g["status"] == "void":
                hz.voids += 1
                continue
            hz.graded += 1
            hz.hits += g["outcome"] == "hit"
            rets.append(g["ret_pct"])
            if g["ret_pct"] <= RUG_RETURN_PCT or (g["drained"] and g["ret_pct"] <= DRAIN_FALL_PCT):
                rugs.add(r["id"])
            if worst is None or g["ret_pct"] < worst[0]:
                worst = (g["ret_pct"], r)
        if hz.graded:
            hz.hitRatePct = _r(100 * hz.hits / hz.graded)
            hz.medianReturnPct = _r(statistics.median(rets))
        if worst:
            hz.worstReturnPct = _r(worst[0])
            hz.worstMint = worst[1]["mint"]
            hz.worstPostUrl = calls_mod.post_url(handle, worst[1]["post_id"])
        if h in horizons:
            out.horizons.append(hz)
    out.rugs = len(rugs)
    h7 = next((x for x in out.horizons if x.horizonH == 168), KolHorizon(horizonH=168))
    out.sigil, out.sigilRule = sigil(h7)
    for r in rows:
        if r["status"] not in ("counted", "ambiguous"):
            continue
        gs = grades.get(r["id"], {})
        out.receipts.append(
            KolReceipt(
                postUrl=calls_mod.post_url(handle, r["post_id"]),
                postedAt=r["ts"],
                mint=r["mint"],
                source=r["source"],
                status=r["status"],
                baselinePriceUsd=r["baseline_price"],
                baselineSource=r["baseline_source"],
                priceSourceUrl=next((g["source"] for g in gs.values() if g["source"]), None),
                returnsPct={
                    HKEY[h]: (gs[h]["ret_pct"] if h in gs and gs[h]["status"] == "graded" else None) for h in HORIZONS_H
                },
                rug=r["id"] in rugs,
                deleted=bool(r["deleted"]),
            )
        )
    return out


def ledger(wdb, now: float | None = None, min_calls: int = 10, optout: list[str] | None = None, days: int = 7):
    """Archmage\'s Ledger: handles ranked by 7d median return on WITNESSED calls posted in the window."""
    now = now or time.time()
    skip = {h.lower().lstrip("@") for h in optout or []}
    rows = wdb.all(
        "SELECT c.handle, g.ret_pct FROM kol_calls c JOIN kol_grades g ON g.call_id=c.id "
        "WHERE c.source='witnessed' AND c.status='counted' AND g.horizon_h=168 AND g.status='graded' AND c.ts>=?",
        (now - (days + 7) * 86400,),
    )
    by: dict[str, list[float]] = {}
    for r in rows:
        if r["handle"] not in skip:
            by.setdefault(r["handle"], []).append(r["ret_pct"])
    table = [
        {"handle": h, "calls": len(v), "medianReturnPct": _r(statistics.median(v)), "worstReturnPct": _r(min(v))}
        for h, v in by.items()
        if len(v) >= min_calls
    ]
    return sorted(table, key=lambda x: (-x["medianReturnPct"], x["handle"]))
