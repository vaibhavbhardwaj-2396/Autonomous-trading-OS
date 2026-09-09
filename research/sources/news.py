"""
PERISHABLE — news arrival times.

The headline text is recoverable later from a dozen archives. What is not
recoverable is WHEN a system like ours could first have seen it, and that is the
only part an event study actually needs.

The knowledge_time decision here is the interesting one
-------------------------------------------------------
Every RSS item carries a `pubDate` from the publisher. It is tempting to use it,
and it is wrong. A publisher's timestamp is a claim: it can be backdated on
edit, it reflects their editorial clock rather than distribution, and for
syndicated items it is often the original wire time rather than the moment the
item reached a public feed.

So knowledge_time is `first_seen` — the moment OUR poller saw it — and pubDate
is kept in the payload as reference. This is deliberately pessimistic: if we
poll every 30 minutes, we record ourselves as knowing things up to 30 minutes
later than a faster system would have. That understates any edge we measure,
which is the safe direction to be wrong in.

The consequence is that polling frequency is a research parameter, not an
implementation detail. Poll every 30 minutes and every study built on this feed
inherits a 30-minute blur. That is recorded in the payload so a future
experiment can see exactly what resolution it is working with.
"""

from __future__ import annotations

import re
import hashlib
import datetime as dt
import xml.etree.ElementTree as ET
from typing import Optional

from ..store import Store, now_ist
from .base import Http, POLITE_DELAY, IST

DATASET = "news_arrival"
SOURCE = "rss"

# Broad market feeds. Symbol attribution happens at analysis time, not here —
# a recorder that tries to be clever about entity extraction is a recorder that
# silently drops the items its regex did not anticipate.
DEFAULT_FEEDS = {
    "moneycontrol_markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "moneycontrol_business": "https://www.moneycontrol.com/rss/business.xml",
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "bs_markets": "https://www.business-standard.com/rss/markets-106.rss",
}

_TAG = re.compile(r"<[^>]+>")


def _text(node, tag: str) -> str:
    el = node.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _parse_pubdate(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    v = value.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%a, %d %b %Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S"):
        try:
            d = dt.datetime.strptime(v, fmt)
            return d if d.tzinfo else d.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse(xml_bytes: bytes, feed_name: str, first_seen: Optional[dt.datetime] = None,
          poll_interval_minutes: Optional[int] = None) -> list[dict]:
    """RSS -> store rows. Pure; tested against a fixture."""
    first_seen = first_seen or now_ist()
    root = ET.fromstring(xml_bytes)

    rows = []
    for item in root.iter("item"):
        title = _TAG.sub("", _text(item, "title")).strip()
        link = _text(item, "link")
        if not title:
            continue
        published = _parse_pubdate(_text(item, "pubDate"))

        # Stable identity so re-polling the same feed does not duplicate. The
        # dedupe key in the store also covers this, but only if knowledge_time
        # matches — and it will not, because first_seen advances every poll.
        # So identity has to be explicit here.
        ident = hashlib.sha1(f"{feed_name}|{link or title}".encode()).hexdigest()[:16]

        rows.append({
            "dataset": DATASET,
            "entity": feed_name,
            # The event is the publication; our knowledge of it is the poll.
            "event_time": published or first_seen,
            "knowledge_time": first_seen,
            "source": f"{SOURCE}:{feed_name}",
            "source_ref": link or None,
            "confidence": "observed",
            "payload": {
                "id": ident,
                "feed": feed_name,
                "title": title,
                "link": link or None,
                "published_claimed": published.isoformat() if published else None,
                "description": _TAG.sub("", _text(item, "description"))[:600] or None,
                # Recorded so a later study knows the blur it inherited.
                "poll_interval_minutes": poll_interval_minutes,
            },
        })
    return rows


def _dedupe_against_store(store: Store, rows: list[dict], lookback_hours: int = 72
                          ) -> list[dict]:
    """Drop items already recorded, matching on the stable id rather than on
    content — otherwise every poll re-inserts the same headline with a new
    first_seen and the arrival timestamp becomes meaningless."""
    if not rows:
        return rows
    since = now_ist() - dt.timedelta(hours=lookback_hours)
    known = set()
    for r in store.view(now_ist()).observations(DATASET, event_from=since,
                                                latest_only=False):
        pid = (r.get("payload") or {}).get("id")
        if pid:
            known.add(pid)
    return [r for r in rows if r["payload"]["id"] not in known]


def load(store: Store, feeds: Optional[dict] = None, http: Optional[Http] = None,
         poll_interval_minutes: Optional[int] = None) -> dict:
    import time
    feeds = feeds or DEFAULT_FEEDS
    http = http or Http()
    first_seen = now_ist()

    seen = new = 0
    per_feed, failures = {}, {}
    for name, url in feeds.items():
        try:
            raw = http.get(url)
            rows = parse(raw, name, first_seen, poll_interval_minutes)
            fresh = _dedupe_against_store(store, rows)
            inserted = store.append_many(fresh)
            seen += len(rows)
            new += inserted
            per_feed[name] = {"items": len(rows), "new": inserted}
        except Exception as e:
            failures[name] = f"{type(e).__name__}: {e}"
        time.sleep(POLITE_DELAY)

    # Every feed dead means the network or the host is broken, not that the news
    # was quiet. Reporting that as a successful empty run is how a recorder goes
    # silent for weeks without anyone noticing.
    if feeds and not per_feed:
        raise RuntimeError(
            f"news: all {len(feeds)} feed(s) failed, nothing recorded. "
            f"First error: {next(iter(failures.values()), 'unknown')}")

    return {"rows_seen": seen, "rows_new": new,
            "per_feed": per_feed, "failures": failures}
