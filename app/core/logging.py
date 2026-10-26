"""
app/core/logging.py
===================

Logging configuration for the app.

Called once, at startup, from `main.py`. After that, every module uses
`logging.getLogger(__name__)` and never touches configuration again.

## Why stdlib logging and not something fancier

`structlog`, `loguru`, and friends add structure and ergonomics. They
also add a dependency, a mental model, and a migration story when you
want to move away. For a learning project, the stdlib `logging` module
is what every Python engineer should know cold. It's not the prettiest
API in the standard library (arguably one of the ugliest), but it's the
one you'll meet in every codebase you touch.

The two lessons worth absorbing here:

  1. **Logging is configured once, at process start.** Libraries don't
     configure logging; they emit log records and let the *application*
     decide what to do with them. That separation is why
     `getLogger(__name__)` appears in every module and `basicConfig`
     appears exactly once.

  2. **Structured output matters more than pretty output.** In dev, a
     human-readable line is nice. In production, you want JSON so that a
     log aggregator can parse it, index it, and let you query it. The
     same log event should be representable in both formats — that's the
     goal here.

## The two formatters

  - **Dev formatter** — human-readable. Timestamp, level, logger name,
    message. Compact, colored-ish (we're not adding color here to keep
    the dependency surface at zero), easy to scan.

  - **JSON formatter** — one JSON object per line. Timestamp in ISO 8601
    UTC, level, logger name, message, plus any extra fields. This is
    what log shippers (Filebeat, Fluent Bit, the Datadog agent) expect.

The choice between them is driven by `settings.ENV`. `dev` gets the
human format; anything else gets JSON.

## The request ID

We attach a `request_id` to every log record via a context variable set
by the middleware in `middleware.py`. The formatters below include it
when present. This is what lets you correlate every log line emitted
during one request — you grep for the id and see the whole story in
order. Without it, you're left matching timestamps, which is unreliable
under concurrency.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings


# ---------------------------------------------------------------------------
# Request ID context variable
# ---------------------------------------------------------------------------
#
# `ContextVar` is the async-safe way to carry a value across function
# calls without threading it through arguments. The middleware sets it
# at the start of a request; the formatter reads it when formatting a
# log record. Between those two points, any code that logs — a service,
# a query function, a router — gets the id automatically.
#
# Why `ContextVar` and not a global? Because FastAPI runs requests
# concurrently. A global would let one request's id leak into another's
# log lines. ContextVars are scoped to the current async task, which is
# exactly what we want.
#
# Why a sentinel default of "-"? So the formatter can print something
# sane when a log record is emitted outside a request — e.g. during
# startup, or from a script. Showing "None" in a log line is sloppy;
# a dash reads as "not applicable."

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class DevFormatter(logging.Formatter):
    """
    Human-readable log format for development.

    Output looks like:

        2026-10-03T12:34:56.789Z  INFO   [abc123]  app.services.auth_service  user logged in: id=42

    The request id is in brackets when present, `-` otherwise. Fields are
    separated by two spaces so that the eye can scan down the level and
    logger columns without getting lost.

    The timestamp is UTC in ISO 8601 with milliseconds. Local time in logs
    is a subtle footgun — different machines have different timezones,
    and a log aggregator can't tell which one produced a line. UTC always,
    everywhere, for anything that might be aggregated.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Build the timestamp from `record.created`, which is the Unix
        # epoch time when the log call happened. We convert to UTC and
        # format with milliseconds.
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        request_id = request_id_var.get()

        # `record.getMessage()` applies any %-style formatting the caller
        # passed (e.g. `logger.info("user=%s", uid)`). If we just used
        # `record.msg`, we'd print the raw format string.
        message = record.getMessage()

        # Include exception info if any, formatted as a traceback. This
        # is what `logger.exception()` and `logger.error(..., exc_info=True)`
        # produce.
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)

        return (
            f"{ts_str}  "
            f"{record.levelname:<5}  "
            f"[{request_id}]  "
            f"{record.name}  "
            f"{message}"
        )


class JsonFormatter(logging.Formatter):
    """
    One-JSON-object-per-line log format for production.

    Output looks like:

        {"ts":"2026-10-03T12:34:56.789Z","level":"INFO","logger":"app.services.auth_service","request_id":"abc123","message":"user logged in: id=42"}

    Fields:

      - `ts`          ISO 8601 UTC timestamp.
      - `level`       Log level name.
      - `logger`      Logger name (usually the module path).
      - `request_id`  Request id, or "-" outside a request.
      - `message`     Formatted message.
      - `exc_info`    Only present if an exception was logged — the
                      traceback as a string.

    Every field is a top-level key so that log aggregators can index them
    individually. Nesting (e.g. putting request context under a `ctx`
    object) makes simple queries like "all ERROR lines for request X"
    harder, so we flatten.

    We use `json.dumps` with `default=str` so that any non-JSON-able
    object in the message (a datetime, a UUID) gets stringified rather
    than raising. This is a tradeoff: less typed output, but a log line
    that never crashes. A crashing logger is worse than a lossy one.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        payload: dict[str, Any] = {
            "ts": ts_str,
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """
    Configure the root logger. Call once, at application startup.

    ## Why the root logger

    Configuring the root logger means every child logger (created via
    `logging.getLogger(__name__)` in each module) inherits the handler
    and level. We don't touch child loggers individually — that would be
    dozens of places to keep in sync.

    ## Why we clear existing handlers first

    If `configure_logging` runs twice (say, in tests that re-import the
    app), the default behavior would add a second handler and every log
    line would be emitted twice. Removing existing handlers makes the
    function idempotent. This matters in test suites where the app is
    created fresh per test.

    ## Why we don't set `propagate = False`

    Propagation is what lets third-party libraries (uvicorn, httpx) have
    their log records flow up to our root handler. We want that — their
    logs are useful, and we want them in the same format as ours. If a
    specific library is too chatty, we set its *level* (e.g. bump httpx
    to WARNING), not its propagation.

    ## Level

    The root logger's level is `settings.LOG_LEVEL`. This is the floor
    for everything below it. A module could raise its own level to be
    more verbose (e.g. `logging.getLogger("app.db").setLevel(DEBUG)`),
    but by default everything respects the root setting.
    """
    root = logging.getLogger()

    # Remove any handlers that were previously attached. See docstring.
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Pick the formatter by environment.
    if settings.ENV == "dev":
        formatter: logging.Formatter = DevFormatter()
    else:
        formatter = JsonFormatter()

    # Send logs to stdout, not stderr. This is a deliberate choice:
    # container orchestrators (Docker, Kubernetes) capture both, but
    # conventional tools often assume stdout for application logs. If
    # we later need to split streams (errors to stderr), that's a
    # straightforward change here.
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())

    # Uvicorn installs its own handlers on its loggers. On startup it
    # may emit a couple of log lines before we run configure_logging,
    # which would come out in uvicorn's default format. To get *all*
    # output in our format, we detach uvicorn's handlers and let its
    # loggers propagate to the root instead.
    #
    # This is a small detail but it makes a visible difference: without
    # it, you'd see uvicorn's startup banner in one format and your
    # app's logs in another.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """
    Convenience wrapper around `logging.getLogger`.

    Exists so that modules can do `from app.core.logging import get_logger`
    and then `logger = get_logger(__name__)` — which reads slightly
    better than the stdlib equivalent, and gives us a single place to
    change if we ever want to swap in a different logger factory (say,
    to attach per-module default context).

    Not required — `logging.getLogger(__name__)` works identically and
    is arguably more Pythonic. This is a matter of taste.
    """
    return logging.getLogger(name)