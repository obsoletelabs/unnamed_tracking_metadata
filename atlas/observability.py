"""Opt-in, bounded request telemetry. Never label metrics or logs with user input."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from collections import Counter
from datetime import timedelta

from sqlalchemy import case, func, select
from starlette.requests import ClientDisconnect

from . import backup, feed, instruments, metrics, readiness
from .models import NodeState, Outbox, Peer, SyncCursor

BUCKETS = metrics.LATENCY_BUCKETS
# Cardinality budget for the `route` label: matched route templates are finite (code, not input), but a
# dynamically mounted router must never be able to grow the series set without bound.
MAX_ROUTE_LABELS = 512
KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})
# Mutually exclusive classes for requests that did not succeed; every class is always exported.
ERROR_CLASSES = ("client_error", "server_error", "unhandled_exception", "client_disconnect")
# Status recorded when the client went away before any response was started (nginx convention). Without
# it a cancelled request would be counted as a server 500 and trip the error-rate alert.
STATUS_CLIENT_DISCONNECT = 499
COLLECTORS = ("readiness", "feed", "upgrade", "outbox", "peers", "sync_upstreams", "providers", "backup", "database",
              "catalog", "media_storage")
#: Collectors whose result is cached for `ATLAS_METRICS_CACHE_SECONDS` (aggregate scans and a directory walk):
#: a scrape never runs them more than once per period per process.
CACHED = ("catalog", "media_storage")
#: Response kinds for `atlas_http_response_bytes_total`, from the response content type.
RESPONSE_KINDS = ("html", "json", "image", "text", "other")


def response_kind(content_type: bytes) -> str:
    value = content_type.split(b";", 1)[0].strip().lower()
    if value == b"text/html":
        return "html"
    if value == b"application/json" or value.endswith(b"+json"):
        return "json"
    if value.startswith(b"image/"):
        return "image"
    if value.startswith(b"text/"):
        return "text"
    return "other"
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
        self.routes: set[str] = set()
        self.errors = Counter()
        self.bytes = Counter()
        self.in_flight = 0
        self.settings = None  # set by the app: metrics_dir and metrics_cache_seconds
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._cache_locks = {name: threading.Lock() for name in CACHED}

    def begin(self):
        with self.lock:
            self.in_flight += 1

    def end(self):
        with self.lock:
            self.in_flight = max(0, self.in_flight - 1)

    def _route(self, route):
        if route not in self.routes:
            if len(self.routes) >= MAX_ROUTE_LABELS:
                return "other"
            self.routes.add(route)
        return route

    def observe(self, method, route, status, elapsed, error_class=None, sent_bytes=0, kind="other"):
        status = status if isinstance(status, int) and 100 <= status <= 599 else 0
        kind = kind if kind in RESPONSE_KINDS else "other"
        with self.lock:
            route = self._route(route)
            if sent_bytes:
                self.bytes[(route, kind)] += sent_bytes
            key = (method, route, str(status))
            self.counts[key] += 1
            self.sums[key] += elapsed
            for bound in BUCKETS:
                if elapsed <= bound:
                    self.buckets[(*key, str(bound))] += 1
            if error_class is None and status >= 400:
                error_class = "server_error" if status >= 500 else "client_error"
            if error_class:
                self.errors[error_class] += 1

    def families(self) -> dict:
        """This process's HTTP series in the shared snapshot format (`atlas.metrics`), plus every registry family."""
        labels = ["method", "route", "status"]
        with self.lock:
            requests = [[list(k), v] for k, v in self.counts.items()]
            durations = [[list(k), [[self.buckets[(*k, str(b))] for b in BUCKETS], self.sums[k], n]]
                         for k, n in self.counts.items()]
            errors = [[[name], self.errors[name]] for name in ERROR_CLASSES]
            sent = [[list(k), v] for k, v in self.bytes.items()]
            in_flight = self.in_flight
        out = metrics.REGISTRY.snapshot()
        out.update({
            "atlas_http_requests_total": {
                "type": "counter", "labels": labels, "buckets": [], "merge": "sum", "live": False, "series": requests,
                "help": "Requests by bounded method, matched route template and status (499: client left first)"},
            "atlas_http_request_duration_seconds": {
                "type": "histogram", "labels": labels, "buckets": list(BUCKETS), "merge": "sum", "live": False,
                "series": durations, "help": "Request duration in seconds"},
            "atlas_http_request_errors_total": {
                "type": "counter", "labels": ["class"], "buckets": [], "merge": "sum", "live": False,
                "series": errors, "help": "Requests that did not succeed, one class per request"},
            "atlas_http_response_bytes_total": {
                "type": "counter", "labels": ["route", "kind"], "buckets": [], "merge": "sum", "live": False,
                "series": sent, "help": "Response body bytes sent, by route template and response kind"},
            "atlas_http_requests_in_flight": {
                "type": "gauge", "labels": [], "buckets": [], "merge": "sum", "live": True,
                "series": [[[], in_flight]], "help": "Requests running now in this process (or live workers)"},
        })
        return out

    def flush(self) -> None:
        """Write this process's snapshot for the other workers (multi-worker mode only)."""
        directory = getattr(self.settings, "metrics_dir", None)
        if directory:
            metrics.write_snapshot(directory, self.families())

    def _process_lines(self) -> list[str]:
        directory = getattr(self.settings, "metrics_dir", None)
        if not directory:
            return metrics.render(metrics.merge([(self.families(), True)]))
        self.flush()
        metrics.compact(directory)
        lines = metrics.render(metrics.collect(directory))
        return lines + ["# HELP atlas_metrics_workers Worker snapshots with a fresh heartbeat in ATLAS_METRICS_DIR",
                        "# TYPE atlas_metrics_workers gauge", f"atlas_metrics_workers {metrics.workers(directory)}"]

    def _readiness(self, state, session):
        result = readiness.check(session, state.ctx.settings)
        lines = ["# TYPE atlas_ready gauge", f"atlas_ready {int(result.serving)}",
                 "# TYPE atlas_ready_check gauge"]
        lines += [f'atlas_ready_check{{check="database"}} {int(result.database)}',
                  f'atlas_ready_check{{check="schema"}} {int(result.schema != "behind")}',
                  f'atlas_ready_check{{check="media"}} {int(result.media)}',
                  "# TYPE atlas_schema_state gauge"]
        lines += [f'atlas_schema_state{{state="{name}"}} {int(result.schema == name)}'
                  for name in ("current", "behind", "unknown")]
        return lines

    def _feed(self, state, session):
        return ["# TYPE atlas_feed_head gauge", f"atlas_feed_head {feed.head_seq(session)}"]

    def _upgrade(self, state, session):
        """C16 upgrade journal (§6.3, Z2): one gauge per stage, 1 while it is the first still-pending stage for
        this node (so a scrape can alert on "stuck on introduce_missing for N minutes"), plus whether the whole
        journal is done and how many introduce-batch entries are left in the current stage. Absent stage names
        (`atlas.upgrade.STAGES`) never appear with value 0 only because this process has not run them yet: every
        name is always emitted, one way or the other."""
        from . import upgrade

        info = upgrade.status(session)
        lines = ["# TYPE atlas_upgrade_complete gauge", f"atlas_upgrade_complete {int(info is None or info['complete'])}"]
        lines.append("# TYPE atlas_upgrade_stage gauge")
        current = info["stage"] if info else None
        for name in upgrade.STAGES:
            lines.append(f'atlas_upgrade_stage{{stage="{name}"}} {int(name == current)}')
        lines.append("# TYPE atlas_upgrade_stage_left gauge")
        lines.append(f"atlas_upgrade_stage_left {(info or {}).get('left') or 0}")
        lines.append("# TYPE atlas_upgrade_skipped_scope_ceiling gauge")
        lines.append(f"atlas_upgrade_skipped_scope_ceiling {(info or {}).get('skipped_scope_ceiling') or 0}")
        return lines

    def _outbox(self, state, session):
        lines = ["# TYPE atlas_outbox_messages gauge"]
        status = case((Outbox.sent_at.is_not(None), "sent"), (Outbox.error.is_not(None), "failed"), else_="pending")
        counts = dict(session.execute(select(status, func.count()).select_from(Outbox).group_by(status)).all())
        for name in ("pending", "failed", "sent"):
            lines.append(f'atlas_outbox_messages{{state="{name}"}} {counts.get(name, 0)}')
        # Absent when nothing is waiting (an empty queue has no age); failed rows still due for a retry count.
        oldest = session.scalar(select(func.min(Outbox.created_at)).where(Outbox.sent_at.is_(None)))
        if oldest is not None:
            from datetime import UTC

            when = (oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)).timestamp()
            lines += ["# TYPE atlas_mail_oldest_pending_timestamp_seconds gauge",
                      f"atlas_mail_oldest_pending_timestamp_seconds {when:.0f}"]
        return lines

    def _peers(self, state, session):
        cursors = {row.upstream: row.last_seq for row in session.scalars(select(SyncCursor))}
        heads = {row.key.removeprefix("sync_head:"): int(row.value)
                 for row in session.scalars(select(NodeState).where(NodeState.key.like("sync_head:%")))}
        lines = ["# TYPE atlas_peer_last_success_timestamp_seconds gauge", "# TYPE atlas_peer_error gauge",
                 "# TYPE atlas_peer_lag_entries gauge"]
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
        return lines

    def _sync_upstreams(self, state, session):
        # Compatibility heads per upstream feed (C1.7). Labels: trusted upstream node ids and fixed names only.
        from . import sync

        full = sync.status(session, state.ctx)
        upstreams = full["upstreams"]
        lines = ["# TYPE atlas_sync_upstream_head gauge"]
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
        # C13 §7.7: entries waiting for a record, per upstream feed, and one status per peer (fixed names only)
        lines.append("# TYPE atlas_sync_waiting_entries gauge")
        for origin, info in upstreams.items():
            lines.append(f"atlas_sync_waiting_entries{{node_id={json.dumps(origin)}}} "
                         f"{(info.get('waiting') or {}).get('count', 0)}")
        lines.append("# TYPE atlas_sync_waiting_records gauge")  # distinct records the entries wait for
        for origin, info in upstreams.items():
            lines.append(f"atlas_sync_waiting_records{{node_id={json.dumps(origin)}}} "
                         f"{(info.get('waiting') or {}).get('records', 0)}")
        lines.append("# TYPE atlas_sync_open_failures gauge")  # wait + quarantine, against sync_max_open_failures
        for origin, info in upstreams.items():
            lines.append(f"atlas_sync_open_failures{{node_id={json.dumps(origin)}}} "
                         f"{(info.get('waiting') or {}).get('count', 0) + info['quarantined']['count']}")
        lines.append("# TYPE atlas_sync_woken_total counter")  # records whose arrival applied waiting entries
        for (origin, trigger), count in sorted(sync.woken_counts(session).items()):
            if trigger in sync.WOKEN_TRIGGERS:
                lines.append(f"atlas_sync_woken_total{{node_id={json.dumps(origin)},trigger=\"{trigger}\"}} {count}")
        lines.append("# TYPE atlas_peer_status gauge")
        for peer in full["peers"]:
            for name in sync.PEER_STATUSES:
                lines.append(f'atlas_peer_status{{peer_id="{peer["id"]}",status="{name}"}} '
                             f'{int(peer["status"]["code"] == name)}')
        if state.ctx.settings.role == "authority":
            records = feed.records_summary(session)
            lines += ["# TYPE atlas_feed_records gauge",
                      f'atlas_feed_records{{state="introduced"}} {records["introduced"]}',
                      f'atlas_feed_records{{state="not_federated"}} {records["not_federated"]}',
                      f'atlas_feed_records{{state="missing"}} {feed.unintroduced_count(session, "missing")}']
        return lines

    def _providers(self, state, session):
        lines = ["# TYPE atlas_provider_failures_total counter"]
        for provider, count in sorted(state.providers.failure_counts().items()):
            lines.append(f"atlas_provider_failures_total{{provider={json.dumps(provider)}}} {count}")
        return lines

    def _backup(self, state, session):
        archives = backup.latest(state.ctx.settings)
        last_backup = max((row["when"].timestamp() for row in archives), default=0)
        verified = backup.recorded(session, backup.VERIFIED)
        drilled = backup.recorded(session, backup.DRILL)
        return ["# TYPE atlas_backup_last_success_timestamp_seconds gauge",
                f"atlas_backup_last_success_timestamp_seconds {last_backup}",
                "# TYPE atlas_backup_enabled gauge",
                f"atlas_backup_enabled {int(bool(state.ctx.settings.backup_dir))}",
                # node_state evidence, shared by every worker; zero means never recorded
                "# TYPE atlas_backup_last_verified_timestamp_seconds gauge",
                f"atlas_backup_last_verified_timestamp_seconds {verified or 0:.0f}",
                "# TYPE atlas_backup_last_restore_drill_timestamp_seconds gauge",
                f"atlas_backup_last_restore_drill_timestamp_seconds {drilled or 0:.0f}"]

    def _database(self, state, session):
        """Connection pool occupancy (#74); absent where the pool has no such numbers."""
        from . import db

        pool = db.pool_status(session.get_bind())
        if pool is None:
            return []
        lines = ["# TYPE atlas_database_pool_connections gauge"]
        lines += [f'atlas_database_pool_connections{{state="{name}"}} {pool[name]}'
                  for name in ("checked_out", "idle", "overflow")]
        return lines + ["# TYPE atlas_database_pool_size gauge", f"atlas_database_pool_size {pool['size']}"]

    def _catalog(self, state, session):
        from . import catalog_metrics

        return catalog_metrics.lines(session)

    def _media_storage(self, state, session):
        from . import media

        usage = media.storage_usage(state.ctx.settings)
        lines = ["# TYPE atlas_media_storage_present gauge", f"atlas_media_storage_present {int(usage['present'])}"]
        if not usage["present"]:
            return lines  # no directory: usage unknown, not zero
        lines += ["# TYPE atlas_media_storage_bytes gauge", f"atlas_media_storage_bytes {usage['bytes']}",
                  "# TYPE atlas_media_storage_files gauge", f"atlas_media_storage_files {usage['files']}",
                  "# TYPE atlas_media_storage_scan_truncated gauge",
                  f"atlas_media_storage_scan_truncated {int(usage['truncated'])}"]
        stats = media.stats(session)
        lines.append("# TYPE atlas_media_artwork gauge")
        lines += [f'atlas_media_artwork{{state="{k}"}} {stats[k]}' for k in ("total", "cached", "failing")]
        return lines

    def _group(self, state, session):
        """Owner-group health (CONTRACT-C10 §8.3). Only rendered while this node is in a group; it discloses the
        member node ids to whoever may scrape /metrics (docs/guide/node-groups.md)."""
        from . import groups

        group = groups.load(session)
        now = groups.utcnow()
        members = list(session.scalars(select(groups.GroupMember).where(groups.GroupMember.group_id == group.group_id)))
        lines = ["# TYPE atlas_group_member gauge"]
        for status in ("active", "expiring", "expired", "revoked", "left", "fork"):
            lines.append(f'atlas_group_member{{status="{status}"}} {sum(m.status == status for m in members)}')
        lines.append("# TYPE atlas_group_cert_expiry_seconds gauge")
        lines += [f'atlas_group_cert_expiry_seconds{{node="{m.node_id}"}} '
                  f'{(groups.aware(m.not_after) - now).total_seconds():.0f}' for m in members[:32]]
        lines.append("# TYPE atlas_group_crl_seq gauge")
        lines.append(f'atlas_group_crl_seq{{issuer="root"}} {groups.crl_seqs(session, group.group_id).get("root", 0)}')
        lines.append("# TYPE atlas_group_last_sync_age_seconds gauge")
        lines += [f'atlas_group_last_sync_age_seconds{{node="{m.node_id}"}} '
                  f'{(now - groups.aware(m.last_seen_at)).total_seconds():.0f}' for m in members[:32] if m.last_seen_at]
        views = list(session.scalars(select(groups.GroupView).where(groups.GroupView.group_id == group.group_id)))
        lines.append("# TYPE atlas_group_view_age_seconds gauge")
        lines += [f'atlas_group_view_age_seconds{{issuer="{v.node_id}"}} '
                  f'{(now - groups.aware(v.issued_at)).total_seconds():.0f}' for v in views[:32]]
        since = now - timedelta(days=30)
        lines.append("# TYPE atlas_group_enrolments gauge")
        rows = list(session.scalars(select(groups.GroupEnrolment.state).where(
            groups.GroupEnrolment.side == "inviter", groups.GroupEnrolment.created_at >= since)))
        for result, states in (("issued", ("issued", "active")), ("burned", ("burned",)), ("expired", ("expired",)),
                               ("aborted", ("aborted",))):
            lines.append(f'atlas_group_enrolments{{result="{result}"}} {sum(r in states for r in rows)}')
        from .models import AuditEntry

        day = now - timedelta(days=1)
        lines.append("# TYPE atlas_group_signins gauge")  # A12 (§8.3): sign-ins through the group, last 24 h
        for result, action in (("ok", "group_signin"), ("refused", "group_signin_refused")):
            count = session.scalar(select(func.count()).select_from(AuditEntry).where(
                AuditEntry.action == action, AuditEntry.created_at >= day)) or 0
            lines.append(f'atlas_group_signins{{result="{result}"}} {count}')
        lines += ["# TYPE atlas_group_resyncing gauge", f"atlas_group_resyncing {int(group.status == 'resyncing')}"]
        lines += self._channel(session, group, now, [m.node_id for m in members[:32]])
        return lines

    @staticmethod
    def _channel(session, group, now, pinned: list[str]) -> list[str]:
        """Private channel health (§8.3): counts and closed reason labels only, never a field, value or account."""
        from . import group_channel
        from .models import AuditEntry, GroupChange, GroupStream, ProfileConflict

        day = now - timedelta(days=1)
        lines = ["# TYPE atlas_group_stream_lag gauge"]
        lag: dict[str, int] = {}
        for s in session.scalars(select(GroupStream).where(GroupStream.group_id == group.group_id,
                                                           GroupStream.advertised_head.isnot(None))):
            if s.origin in pinned:
                lag[s.origin] = max(lag.get(s.origin, 0), max(0, (s.advertised_head or 0) - s.hwm))
        lines += [f'atlas_group_stream_lag{{origin="{o}"}} {v}' for o, v in sorted(lag.items())]
        rows = session.execute(select(GroupChange.disposition, GroupChange.reason, func.count()).where(
            GroupChange.group_id == group.group_id, GroupChange.received_at >= day, GroupChange.received_from.isnot(None)
        ).group_by(GroupChange.disposition, GroupChange.reason)).all()
        lines.append("# TYPE atlas_group_entries gauge")
        for disposition in ("applied", "noop", "parked", "held", "elided", "rejected"):
            lines.append(f'atlas_group_entries{{disposition="{disposition}"}} '
                         f'{sum(n for d, _, n in rows if d == disposition)}')
        # Refusals that stop a stream without storing the entry are only in the audit log.
        unstored: Counter[str] = Counter()
        for action, detail in session.execute(select(AuditEntry.action, AuditEntry.detail).where(
                AuditEntry.action.in_(("group_peer_suspect", "group_stream_fork")), AuditEntry.created_at >= day)
                .limit(10_000)):
            unstored["fork" if action == "group_stream_fork" else str((detail or {}).get("reason"))] += 1
        lines.append("# TYPE atlas_group_rejected gauge")
        for reason in group_channel.METRIC_REASONS:
            stored = sum(n for d, r, n in rows if d == "rejected" and r == reason)
            lines.append(f'atlas_group_rejected{{reason="{reason}"}} {stored + unstored[reason]}')
        open_n = session.scalar(select(func.count()).select_from(ProfileConflict).where(
            ProfileConflict.status == "open")) or 0
        lines += ["# TYPE atlas_profile_conflicts_open gauge", f"atlas_profile_conflicts_open {open_n}"]
        # group_change keeps every entry (new members and snapshots need contiguous streams): watch its growth
        total = session.scalar(select(func.count()).select_from(GroupChange).where(
            GroupChange.group_id == group.group_id)) or 0
        lines += ["# TYPE atlas_group_change_rows gauge", f"atlas_group_change_rows {total}"]
        return lines

    def _collectors(self, session) -> tuple[str, ...]:
        try:
            from . import groups

            return (*COLLECTORS, "group") if groups.active(session) is not None else COLLECTORS
        except Exception:
            session.rollback()
            return COLLECTORS

    def _run(self, name, state, session):
        """One collector, through the cache for the expensive ones. Returns (lines, age_seconds or None)."""
        ttl = getattr(self.settings, "metrics_cache_seconds", 300) if self.settings is not None else 300
        if name not in CACHED:
            return self._timed(name, state, session), None
        cached = self._cache.get(name)
        now = time.monotonic()
        if cached is not None and now - cached[0] < ttl:
            return cached[1], now - cached[0]
        lock = self._cache_locks[name]
        if not lock.acquire(blocking=False):  # another scrape is refreshing it: serve the previous result
            if cached is not None:
                return cached[1], now - cached[0]
            raise RuntimeError("refresh in progress")
        try:
            lines = self._timed(name, state, session)
            self._cache[name] = (time.monotonic(), lines)
            return lines, 0.0
        finally:
            lock.release()

    def _timed(self, name, state, session):
        started, outcome = time.monotonic(), "error"
        try:
            lines = getattr(self, f"_{name}")(state, session)
            outcome = "ok"
            return lines
        finally:
            instruments.collector_refresh.observe(time.monotonic() - started, collector=name, outcome=outcome)

    def render(self, state, session):
        """One scrape. Each collector is isolated: a failure drops only that collector's series (never
        reported as zero) and shows as `atlas_collector_success{collector=...} 0`. Collector names are the
        fixed set in COLLECTORS; nothing from the failure (message, query, path) reaches the output."""
        lines = ["# TYPE atlas_uptime_seconds gauge", f"atlas_uptime_seconds {time.time() - self.started:.3f}"]
        status, timing, ages = {}, {}, {}
        collectors = self._collectors(session)
        collected: list[str] = []
        for name in collectors:
            started = time.monotonic()
            try:
                got, age = self._run(name, state, session)
                collected += got
                status[name] = 1
                if age is not None:
                    ages[name] = age
            except Exception as exc:
                status[name] = 0
                try:
                    session.rollback()  # a failed statement leaves PostgreSQL's transaction aborted
                except Exception:
                    pass
                logging.getLogger("atlas.metrics").warning("metrics collector failed", extra={"atlas_event": {
                    "event": "metrics.collector_failed", "collector": name, "error_type": type(exc).__name__}})
            timing[name] = time.monotonic() - started
        lines += self._process_lines()  # after the collectors, so their refresh timings are in this scrape
        lines += collected
        lines.append("# TYPE atlas_collector_success gauge")
        lines += [f'atlas_collector_success{{collector="{name}"}} {status[name]}' for name in collectors]
        lines.append("# TYPE atlas_collector_duration_seconds gauge")
        lines += [f'atlas_collector_duration_seconds{{collector="{name}"}} {timing[name]:.6f}' for name in collectors]
        lines.append("# TYPE atlas_collector_cache_age_seconds gauge")
        lines += [f'atlas_collector_cache_age_seconds{{collector="{name}"}} {age:.3f}' for name, age in ages.items()]
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
        method = scope["method"] if scope["method"] in KNOWN_METHODS else "OTHER"
        scope["atlas_request_id"] = request_id
        started, status = time.monotonic(), 500
        started_response = completed = disconnected = False
        error_class = None
        sent, kind = 0, "other"

        async def listen():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not completed:
                disconnected = True
            return message

        async def respond(message):
            nonlocal status, started_response, completed, sent, kind
            if message["type"] == "http.response.start":
                status, started_response = message["status"], True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                kind = response_kind(next((v for k, v in headers if k.lower() == b"content-type"), b""))
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode())]}
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
                if not message.get("more_body"):
                    completed = True
            await send(message)

        self.stats.begin()
        try:
            await self.app(scope, listen, respond)
        except asyncio.CancelledError:
            # The server cancelled the task because the client went away (or it is shutting down): not a
            # server failure, so it must not count as a 500. Re-raised so cancellation still propagates.
            error_class = "client_disconnect"
            raise
        except Exception as exc:
            # Starlette raises ClientDisconnect when the client hangs up mid-body (an upload): not a server fault.
            error_class = ("client_disconnect" if disconnected or isinstance(exc, ClientDisconnect)
                           else "unhandled_exception")
            raise
        finally:
            self.stats.end()
            elapsed = time.monotonic() - started
            if error_class is None and disconnected and not completed:
                error_class = "client_disconnect"
            if error_class == "client_disconnect" and not started_response:
                status = STATUS_CLIENT_DISCONNECT
            route = getattr(scope.get("route"), "path", "unmatched")
            self.stats.observe(method, route, status, elapsed, error_class, sent, kind)
            if self.json_logs:
                logging.getLogger("atlas.access").info("request", extra={"atlas_event": {
                    "event": "http.request", "request_id": request_id, "node_id": self.state.ctx.identity.node_id,
                    "method": method, "route": route, "status": status, "duration_seconds": round(elapsed, 6)}})
