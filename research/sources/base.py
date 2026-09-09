"""
The shape every source module takes, and why it takes it.

Each source splits into two halves that fail for completely different reasons:

    fetch()   talks to the network. Fails because a site is down, an endpoint
              moved, a cookie expired, or an IP got rate-limited. Cannot be
              unit-tested meaningfully and should not be.

    parse()   is pure. Takes bytes or dicts, returns rows ready for the store.
              This is where knowledge_time is assigned — the single most
              consequential decision in the whole project, because getting it
              wrong does not raise an error, it produces a study that quietly
              cheats and reports a number you will believe.

So parse() is tested against fixtures and fetch() is not. When a source breaks
on the VPS at 6pm, that split tells you within one minute whether the endpoint
moved or the data changed shape.

THE KNOWLEDGE_TIME RULE, restated because it is easy to get subtly wrong:

    knowledge_time = the earliest moment WE could have acted on this fact.

Not when it happened. Not when the publisher says it happened. When it became
reachable by a system like ours. Where the two are ambiguous, choose the LATER
one. A study that under-uses information reports an edge smaller than the truth;
a study that over-uses it reports an edge that does not exist. Only one of those
two errors costs money.
"""

from __future__ import annotations

import ssl
import json
import time
import gzip
import urllib.error
import urllib.request
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..store import Store, now_ist, iso

DEFAULT_TIMEOUT = 30
POLITE_DELAY = 0.5

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_CTX = ssl.create_default_context()


@dataclass
class SourceResult:
    """What every source run reports. Never raises past the recorder — a source
    that dies must not take the other five down with it."""
    source: str
    ok: bool
    rows_seen: int = 0
    rows_new: int = 0
    error: Optional[str] = None
    detail: dict = field(default_factory=dict)
    started_at: str = ""
    elapsed_s: float = 0.0

    def line(self) -> str:
        if not self.ok:
            return f"  ✗ {self.source:<18} FAILED  {self.error}"
        return (f"  ✓ {self.source:<18} {self.rows_new:>6} new "
                f"/ {self.rows_seen:>6} seen  ({self.elapsed_s:.1f}s)")


def run_source(name: str, fn: Callable[[], dict]) -> SourceResult:
    """Wrap a source run so failure is data, not an exception.

    The recorder runs unattended on a schedule. If one feed 500s at 18:00, the
    other four still have to record, because the days they would have covered
    are not recoverable later.
    """
    started = time.time()
    try:
        d = fn() or {}
        return SourceResult(
            source=name, ok=True,
            rows_seen=int(d.get("rows_seen", 0)),
            rows_new=int(d.get("rows_new", 0)),
            detail={k: v for k, v in d.items() if k not in ("rows_seen", "rows_new")},
            started_at=iso(now_ist()), elapsed_s=round(time.time() - started, 2),
        )
    except Exception as e:
        return SourceResult(
            source=name, ok=False, error=f"{type(e).__name__}: {e}",
            started_at=iso(now_ist()), elapsed_s=round(time.time() - started, 2),
        )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Http:
    """A tiny cookie-aware client.

    NSE's public JSON endpoints reject a request that arrives without the
    cookies its HTML pages set, so a bare GET returns 401 while the same URL
    works in a browser. Priming the jar with a homepage GET first is the whole
    trick, and it has to be redone when the cookies expire.
    """

    def __init__(self, headers: Optional[dict] = None) -> None:
        import http.cookiejar
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = {"User-Agent": UA, "Accept-Encoding": "gzip",
                        **(headers or {})}
        self._primed: set[str] = set()

    def get(self, url: str, headers: Optional[dict] = None,
            timeout: int = DEFAULT_TIMEOUT) -> bytes:
        req = urllib.request.Request(url, headers={**self.headers, **(headers or {})})
        with self.opener.open(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return raw

    def get_json(self, url: str, headers: Optional[dict] = None,
                 timeout: int = DEFAULT_TIMEOUT) -> Any:
        return json.loads(self.get(url, headers, timeout).decode("utf-8", "replace"))

    def prime(self, homepage: str) -> None:
        """Fetch a homepage once per process to collect cookies."""
        if homepage in self._primed:
            return
        try:
            self.get(homepage)
            self._primed.add(homepage)
            time.sleep(POLITE_DELAY)
        except Exception:
            # A failed prime is not fatal — the real request will report the
            # actual problem, and pretending otherwise hides it one layer up.
            pass


# ---------------------------------------------------------------------------
# Shared knowledge_time helpers
# ---------------------------------------------------------------------------

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# NSE publishes the daily bhavcopy in the early evening. A session's close is
# therefore NOT knowable at 15:30 that day, which rules out an entire family of
# "enter at today's close on today's signal" strategies. Encoding it here means
# no experiment can accidentally assume otherwise.
BHAVCOPY_PUBLISH_TIME = dt.time(18, 0)


def bhavcopy_knowledge_time(session_date: dt.date) -> dt.datetime:
    return dt.datetime.combine(session_date, BHAVCOPY_PUBLISH_TIME, tzinfo=IST)


def parse_bse_datetime(value: str) -> Optional[dt.datetime]:
    """BSE returns several shapes across endpoints and eras."""
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d %b %Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:26], fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None
