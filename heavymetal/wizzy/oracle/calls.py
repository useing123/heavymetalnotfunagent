"""Calls: extract them from post text in code and store derived facts only (never the post text).

A call is (handle, mint, post timestamp, post id). Post text is untrusted: it is read here with regexes, in memory,
and dropped. The intent label comes from a fixed enum and can only exclude a call, never add one.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from heavymetal.solana.constants import is_valid_pubkey
from heavymetal.solana.fake_check import CANONICAL_MINTS

HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
QUESTION_RE = re.compile(
    r"\?|\b(read|scry|omen|check|thoughts|legit|safe|rug ?check|what is|is this|is it|oracle|ledger)\b", re.I
)
WARNING_RE = re.compile(r"\b(rug(ged)?|scam|honeypot|avoid|stay away|careful|beware|dump(ed|ing)?|exit)\b", re.I)
INTENTS = ("call", "question", "warning")
SNOWFLAKE_EPOCH_MS = 1288834974657
WITNESS_WINDOW_S = 15 * 60  # seen within minutes of posting = witnessed
QUOTE_MINTS = {CANONICAL_MINTS[k] for k in ("SOL", "USDC", "USDT") if k in CANONICAL_MINTS}  # quote assets, not calls


def norm_handle(text: str) -> str | None:
    m = HANDLE_RE.match((text or "").strip())
    return m.group(1).lower() if m else None


def snowflake_ts(post_id: str) -> float | None:
    """X post ids encode their creation time; used when the API omits created_at."""
    try:
        return ((int(post_id) >> 22) + SNOWFLAKE_EPOCH_MS) / 1000
    except (TypeError, ValueError):
        return None


def post_url(handle: str, post_id: str) -> str:
    return f"https://x.com/{handle}/status/{post_id}"


def intent_of(text: str) -> str:
    """Fixed-enum label from code rules. Anything but "call" is excluded from the record."""
    if WARNING_RE.search(text or ""):
        return "warning"
    if QUESTION_RE.search(text or ""):
        return "question"
    return "call"


@dataclass
class Extracted:
    addrs: list[str]
    cashtags: list[str]
    intent: str


def extract(text: str) -> Extracted:
    """Base58 addresses (bare or inside pump.fun/DexScreener links) and cashtags, deduped in order."""
    text = (text or "")[:2000]
    addrs: list[str] = []
    for m in ADDR_RE.finditer(text):
        a = m.group(0)
        if a not in addrs and is_valid_pubkey(a) and a not in QUOTE_MINTS:
            addrs.append(a)
    tags = list(dict.fromkeys(t.upper() for t in CASHTAG_RE.findall(text)))
    return Extracted(addrs[:3], tags[:3], intent_of(text))


def record_post(
    wdb,
    *,
    handle: str,
    post_id: str,
    text: str,
    author_id: str | None = None,
    posted_at: float | None = None,
    via: str = "mention",
    is_retweet: bool = False,
    is_quote: bool = False,
    now: float | None = None,
) -> list[int]:
    """Store the calls in one post. Returns new call ids. The text itself is never stored."""
    h = norm_handle(handle)
    if h is None or not str(post_id).isdigit() or is_retweet or is_quote:
        return []
    now = now or time.time()
    ts = posted_at or snowflake_ts(post_id) or now
    ex = extract(text)
    source = "witnessed" if now - ts <= WITNESS_WINDOW_S else "backfilled"
    rows: list[tuple] = []
    if ex.addrs:
        status = "counted" if ex.intent == "call" else "excluded"
        reason = None if status == "counted" else ex.intent
        rows = [(a, status, reason) for a in ex.addrs]
    elif ex.cashtags and ex.intent == "call":
        rows = [(None, "ambiguous", "cashtag only: not resolved as of post time")]
    ids = []
    for addr, status, reason in rows:
        cur = wdb._x(
            "INSERT OR IGNORE INTO kol_calls(handle, author_id, post_id, addr, ts, seen_ts, source, via, intent, "
            "status, reason, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, author_id, str(post_id), addr or "", ts, now, source, via, ex.intent, status, reason, now),
        )
        if cur.rowcount:
            ids.append(cur.lastrowid)
    return ids


def calls_for(wdb, handle: str, since: float) -> list:
    return wdb.all("SELECT * FROM kol_calls WHERE handle=? AND ts>=? ORDER BY ts", (handle, since))


def mark_deleted(wdb, post_id: str) -> None:
    """X terms: keep only derived facts and the fact that a post was deleted."""
    wdb._x("UPDATE kol_calls SET deleted=1 WHERE post_id=?", (str(post_id),))
