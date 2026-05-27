"""Opt-in watch daemon — polls each known agent's transcript directory on
an interval and ingests any chunks modified since the last poll.

Polling rather than inotify because:
* Cross-platform without conditional imports.
* The transcript files are append-only on the timescales we care about
  (seconds), so the polling window catches everything.
* Trivial to reason about — every cycle is a stateless `plan()` call
  filtered by `since=last_poll - overlap`.

Wired by the CLI as `asil watch <agents...> --interval 30`. The actual
ingest loop is in the CLI (it has the EpisodicStore + ModelRouter); this
module just provides the scheduling primitive."""

from __future__ import annotations

import contextlib
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True)
class WatchTick:
    """One iteration of the watch loop. `since` is the start of the
    window the ingester should look at."""

    iteration: int
    since: datetime
    started_at: datetime


def run_watch_loop(
    *,
    interval_seconds: int = 30,
    overlap_seconds: int = 60,
    on_tick: Callable[[WatchTick], None],
    max_iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run `on_tick(WatchTick(...))` every `interval_seconds`. `since`
    on each tick is `last_started - overlap_seconds` so brief stalls
    don't drop chunks. SIGINT / SIGTERM end the loop cleanly.

    `max_iterations` is a test hook — production passes None for
    forever, tests pass a small int.
    """
    stop = {"now": False}

    def _stop(_signum, _frame):
        stop["now"] = True

    with _install_signal(signal.SIGINT, _stop), _install_signal(signal.SIGTERM, _stop):
        i = 0
        while not stop["now"]:
            if max_iterations is not None and i >= max_iterations:
                return
            started = datetime.now()
            since = started - timedelta(seconds=interval_seconds + overlap_seconds)
            with contextlib.suppress(Exception):
                on_tick(WatchTick(iteration=i, since=since, started_at=started))
            i += 1
            # Sleep in small slices so a SIGINT during sleep ends promptly.
            slept = 0.0
            while slept < interval_seconds and not stop["now"]:
                step = min(0.5, interval_seconds - slept)
                sleep(step)
                slept += step


class _install_signal:
    """Tiny context manager so we restore the previous handler on exit."""

    def __init__(self, sig: int, handler) -> None:
        self._sig = sig
        self._handler = handler
        self._prev = None

    def __enter__(self) -> _install_signal:
        try:
            self._prev = signal.signal(self._sig, self._handler)
        except (ValueError, OSError):
            # Inside a non-main thread (e.g. tests) signal.signal raises.
            # Watching from a worker thread is allowed — just skip.
            self._prev = None
        return self

    def __exit__(self, *_a) -> None:
        if self._prev is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(self._sig, self._prev)
