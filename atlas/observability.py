"""Opt-in, bounded request telemetry. Never label metrics or logs with user input."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import Counter

from sqlalchemy import case, func, select

from . import backup, feed
from .models import NodeState, Outbox, Peer, SyncCursor

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = getattr(record, "atlas_event", None)
        if data is None:
            # Exception messages and formatting arguments may contain remote URLs or secrets.
            # Keep the source and exception class, not their text or traceback.
            errors = [type(arg).__name__ for arg in (record.args if isinstance(record.args, tuple) else ())
                      if isinstance(arg, BaseException)]
            if record.exc_info and record.exc_info[0]:
                errors.append(record.exc_info[0].__name__)
            data = {"event": "application", "source": f"{record.module}.{record.funcName}", "errors": errors}
        return json.dumps({"level": record.levelname, **data}, separators=(",", ":"))


def configure_logging() -> None:
    for name in ("atlas", "uvicorn.error"):
        logger = logging.getLogger(name)
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
    # The replacement request events omit query strings, reset/verification tokens and client addresses.
    logging.getLogger("uvicorn.access").disabled = True


class Stats:
    def __init__(self):
        self.started = time.time()
        self.lock = threading.Lock()
        self.counts = Counter()
        self.sums = Counter()
        self.buckets = Counter()

    def observe(self, method, route, status, elapsed):
        key = (method, route, str(status))
        with self.lock:
            self.counts[key] += 1
            self.sums[key] += elapsed
            for bound in BUCKETS:
                if elapsed <= bound:
                    self.buckets[(*key, str(bound))] += 1

    def render(self, state, session):
        lines = ["# TYPE atlas_uptime_seconds gauge", f"atlas_uptime_seconds {time.time() - self.started:.3f}",
                 "# TYPE atlas_http_requests_total counter", "# TYPE atlas_http_request_duration_seconds histogram"]
        with self.lock:
            for (method, route, status), count in sorted(self.counts.items()):
                labels = f'method={json.dumps(method)},route={json.dumps(route)},status={json.dumps(status)}'
                lines.append(f"atlas_http_requests_total{{{labels}}} {count}")
                for bound in BUCKETS:
                    number = self.buckets[(method, route, status, str(bound))]
                    lines.append(f'atlas_http_request_duration_seconds_bucket{{{labels},le="{bound}"}} {number}')
                total = self.sums[(method, route, status)]
                lines.extend([f'atlas_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count}',
                              f"atlas_http_request_duration_seconds_count{{{labels}}} {count}",
                              f"atlas_http_request_duration_seconds_sum{{{labels}}} {total:.6f}"])
        lines.extend(["# TYPE atlas_feed_head gauge", f"atlas_feed_head {feed.head_seq(session)}",
                      "# TYPE atlas_outbox_messages gauge"])
        status = case((Outbox.sent_at.is_not(None), "sent"), (Outbox.error.is_not(None), "failed"), else_="pending")
        counts = dict(session.execute(select(status, func.count()).select_from(Outbox).group_by(status)).all())
        for name in ("pending", "failed", "sent"):
            lines.append(f'atlas_outbox_messages{{state="{name}"}} {counts.get(name, 0)}')
        cursors = {row.upstream: row.last_seq for row in session.scalars(select(SyncCursor))}
        heads = {row.key.removeprefix("sync_head:"): int(row.value)
                 for row in session.scalars(select(NodeState).where(NodeState.key.like("sync_head:%")))}
        lines.extend(["# TYPE atlas_peer_last_success_timestamp_seconds gauge", "# TYPE atlas_peer_error gauge",
                      "# TYPE atlas_peer_lag_entries gauge"])
        for peer in session.scalars(select(Peer).where(Peer.enabled.is_(True))):
            from datetime import UTC

            last = peer.last_ok_at
            timestamp = (last if last.tzinfo else last.replace(tzinfo=UTC)).timestamp() if last else 0
            lines.append(f'atlas_peer_last_success_timestamp_seconds{{peer_id="{peer.id}"}} {timestamp}')
            lines.append(f'atlas_peer_error{{peer_id="{peer.id}"}} {int(bool(peer.last_error))}')
            if peer.node_id in heads:
                lag = max(0, heads[peer.node_id] - cursors.get(peer.node_id, 0))
                lines.append(f'atlas_peer_lag_entries{{peer_id="{peer.id}",node_id="{peer.node_id}"}} {lag}')
        lines.append("# TYPE atlas_sync_cursor gauge")
        for origin, cursor in cursors.items():
            lines.append(f"atlas_sync_cursor{{node_id={json.dumps(origin)}}} {cursor}")
        lines.append("# TYPE atlas_sync_head gauge")
        for origin, head in heads.items():
            lines.append(f"atlas_sync_head{{node_id={json.dumps(origin)}}} {head}")
        # Compatibility heads per upstream feed (C1.7). Labels: trusted upstream node ids and fixed names only.
        from . import sync

        upstreams = sync.status(session, state.ctx)["upstreams"]
        lines.append("# TYPE atlas_sync_upstream_head gauge")
        for origin, info in upstreams.items():
            for head in ("observed", "downloaded", "verified", "applied"):
                lines.append(f'atlas_sync_upstream_head{{node_id={json.dumps(origin)},head="{head}"}} '
                             f'{info[head + "_head"]}')
        lines.extend(["# TYPE atlas_sync_parked_entries gauge", "# TYPE atlas_sync_quarantined_entries gauge",
                      "# TYPE atlas_sync_refused gauge", "# TYPE atlas_sync_rewound gauge",
                      "# TYPE atlas_sync_backlog_full gauge",
                      "# TYPE atlas_sync_in_sync gauge"])
        for origin, info in upstreams.items():
            label = f"node_id={json.dumps(origin)}"
            parked = info["parked"] or {}
            lines += [f"atlas_sync_parked_entries{{{label}}} {parked.get('count', 0)}",
                      f"atlas_sync_quarantined_entries{{{label}}} {info['quarantined']['count']}",
                      f"atlas_sync_refused{{{label}}} {int(bool(info['refused']))}",
                      f"atlas_sync_rewound{{{label}}} {int(bool(info['rewound']))}",
                      f"atlas_sync_backlog_full{{{label}}} {int(bool(parked.get('backlog_full')))}",
                      f"atlas_sync_in_sync{{{label}}} {int(info['in_sync'])}"]
        lines.append("# TYPE atlas_provider_failures_total counter")
        for provider, count in sorted(state.providers.failure_counts().items()):
            lines.append(f"atlas_provider_failures_total{{provider={json.dumps(provider)}}} {count}")
        archives = backup.latest(state.ctx.settings)
        last_backup = max((row["when"].timestamp() for row in archives), default=0)
        lines.extend(["# TYPE atlas_backup_last_success_timestamp_seconds gauge",
                      f"atlas_backup_last_success_timestamp_seconds {last_backup}",
                      "# TYPE atlas_backup_enabled gauge",
                      f"atlas_backup_enabled {int(bool(state.ctx.settings.backup_dir))}"])
        return "\n".join(lines) + "\n"


class RequestTelemetry:
    def __init__(self, app, state, stats, json_logs):
        self.app, self.state, self.stats, self.json_logs = app, state, stats, json_logs

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        supplied_id = next((v for k, v in scope["headers"] if k == b"x-request-id"), b"")
        try:
            request_id = supplied_id.decode("ascii")
        except UnicodeDecodeError:
            request_id = ""
        if not REQUEST_ID.fullmatch(request_id):
            request_id = uuid.uuid4().hex
        known_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
        method = scope["method"] if scope["method"] in known_methods else "OTHER"
        scope["atlas_request_id"] = request_id
        started, status = time.monotonic(), 500

        async def respond(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, respond)
        finally:
            elapsed = time.monotonic() - started
            route = getattr(scope.get("route"), "path", "unmatched")
            self.stats.observe(method, route, status, elapsed)
            if self.json_logs:
                logging.getLogger("atlas.access").info("request", extra={"atlas_event": {
                    "event": "http.request", "request_id": request_id, "node_id": self.state.ctx.identity.node_id,
                    "method": method, "route": route, "status": status, "duration_seconds": round(elapsed, 6)}})
