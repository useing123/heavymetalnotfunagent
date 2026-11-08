"""Optional X timeline collector behind a provider interface. Off unless wizzy.oracle_x_collector is true.

v1 of /oracle runs on witnessed calls only (mentions Wizzy already polls). This collector reads a KOL\'s own
recent posts through the X API when enabled, under a per-day budget, and stores only derived facts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from heavymetal.wizzy.oracle import calls as calls_mod

COST_POST_READ = 0.005  # USD per post read, X pay-per-use (check the X Developer Console)
COST_USER_READ = 0.010


@dataclass
class Post:
    id: str
    created_at: float | None
    text: str
    is_retweet: bool = False
    is_quote: bool = False


class TimelineProvider(Protocol):
    async def user_id(self, handle: str) -> str | None: ...

    async def posts(self, user_id: str, since_id: str | None, max_results: int) -> list[Post]: ...


class NullTimeline:
    """Default: no X reads. /oracle uses witnessed calls only."""

    async def user_id(self, handle: str) -> str | None:
        return None

    async def posts(self, user_id: str, since_id: str | None, max_results: int) -> list[Post]:
        return []


class TweepyTimeline:
    """GET /2/users/by/username and /2/users/:id/tweets (retweets excluded server-side)."""

    def __init__(self, client):
        self.c = client  # tweepy.asynchronous.AsyncClient

    async def user_id(self, handle: str) -> str | None:
        resp = await self.c.get_user(username=handle)
        return str(resp.data.id) if resp and resp.data else None

    async def posts(self, user_id: str, since_id: str | None, max_results: int) -> list[Post]:
        params = {
            "max_results": max(5, min(100, max_results)),
            "tweet_fields": ["created_at", "referenced_tweets"],
            "exclude": ["retweets"],
        }
        if since_id:
            params["since_id"] = since_id
        resp = await self.c.get_users_tweets(user_id, **params)
        out = []
        for t in resp.data or []:
            refs = {r.type for r in (t.referenced_tweets or [])}
            created = t.created_at.timestamp() if getattr(t, "created_at", None) else None
            out.append(Post(str(t.id), created, t.text or "", "retweeted" in refs, "quoted" in refs))
        return out


def spent_today(wdb, now: float | None = None) -> float:
    now = now or time.time()
    row = wdb.one(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM requests WHERE surface='x_api' AND raw LIKE 'oracle%' "
        "AND created_at>=?",
        (now - 86400,),
    )
    return float(row["c"])


def _cost(wdb, label: str, usd: float) -> None:
    rid = wdb.add_request(None, "x_api", label, "x_api")
    wdb.finish_request(rid, "api", cost_usd=usd)


async def collect(ctx, provider: TimelineProvider, handle: str, max_posts: int = 100, now: float | None = None) -> dict:
    """Read new posts by one handle (budgeted, incremental via since_id) and record their calls."""
    w = ctx.wcfg
    if not w.oracle_x_collector:
        return {"skipped": "collector off"}
    h = calls_mod.norm_handle(handle)
    if h is None:
        return {"skipped": "bad handle"}
    budget_left = w.oracle_x_daily_budget_usd - spent_today(ctx.wdb, now)
    if budget_left < COST_USER_READ + COST_POST_READ * 5:
        return {"skipped": "x budget"}
    key = f"oracle_uid:{h}"
    uid = ctx.wdb.meta(key)
    if uid is None:
        uid = await provider.user_id(h)
        _cost(ctx.wdb, "oracle user", COST_USER_READ)
        budget_left -= COST_USER_READ
        if uid is None:
            return {"skipped": "unknown handle"}
        ctx.wdb.set_meta(key, uid)
    n = int(min(max_posts, budget_left / COST_POST_READ))
    since = ctx.wdb.meta(f"oracle_since:{h}")
    posts = await provider.posts(uid, since, n)
    _cost(ctx.wdb, "oracle posts", COST_POST_READ * len(posts))
    new = []
    for p in posts:
        new += calls_mod.record_post(
            ctx.wdb,
            handle=h,
            post_id=p.id,
            text=p.text,
            author_id=uid,
            posted_at=p.created_at,
            via="timeline",
            is_retweet=p.is_retweet,
            is_quote=p.is_quote,
            now=now,
        )
    if posts:
        ctx.wdb.set_meta(f"oracle_since:{h}", max(posts, key=lambda p: int(p.id)).id)
    return {"posts": len(posts), "calls": len(new)}
