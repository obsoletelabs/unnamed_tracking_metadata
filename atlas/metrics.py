"""Process metrics registry: bounded labels, Prometheus text rendering and optional multi-worker aggregation.

Every label value is checked against the family's allowlist (a fixed tuple) or its cardinality cap (a small number
of control-plane values such as configured provider names); anything else is folded into `other`. Nothing a
request, peer or provider sends can create a new series. Families are module-level, so instrumentation costs a
dictionary update under a lock and nothing when metrics are disabled (`ENABLED` stays False).

Multi-worker aggregation (`ATLAS_METRICS_DIR`): each process writes a JSON snapshot of its own counters and
histograms to that directory (atomically, every few seconds and on every scrape). A scrape sums every snapshot,
so any worker answers for all workers on that host and counters stay monotonic across worker restarts: a
snapshot whose process stopped is folded into `retired.json` instead of being dropped. Live gauges (requests in
flight) are summed only over processes whose snapshot is fresh.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Instrumentation is a no-op until `enable()` (the app does it when ATLAS_METRICS_ENABLED is on).
ENABLED = False
OTHER = "other"
_SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:/{}-]{1,96}\Z")
#: Seconds after which a worker snapshot without a heartbeat is treated as a stopped process.
STALE_AFTER = 120.0

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
SLOW_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900)
DB_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1, 5)
COUNT_BUCKETS = (0, 1, 5, 10, 25, 50, 100)
DELAY_BUCKETS = (0.01, 0.1, 0.5, 1, 5, 15, 60, 300)


def enable(on: bool = True) -> None:
    global ENABLED
    ENABLED = on


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


class Family:
    """One metric family. `labels` maps a label name to a tuple of allowed values, or to an int cap for
    control-plane values (the first `cap` distinct safe values are kept, later ones fold into `other`)."""

    def __init__(self, name: str, kind: str, help: str, labels: dict[str, tuple[str, ...] | int] | None = None,
                 buckets: tuple[float, ...] | None = None, merge: str = "sum", live: bool = False) -> None:
        assert kind in ("counter", "gauge", "histogram")
        assert "job" not in (labels or {}), "`job` is reserved by Prometheus for the scrape job"
        self.name, self.kind, self.help = name, kind, help
        self.labels = dict(labels or {})
        self.buckets = tuple(buckets or ()) if kind == "histogram" else ()
        self.merge, self.live = merge, live
        self.lock = threading.Lock()
        self.series: dict[tuple[str, ...], Any] = {}
        self._seen: dict[str, set[str]] = {k: set() for k, v in self.labels.items() if isinstance(v, int)}

    # ---- label handling ------------------------------------------------------------------------------------
    def _key(self, labels: dict[str, Any]) -> tuple[str, ...]:
        out = []
        for name, allowed in self.labels.items():
            value = labels.get(name)
            value = "" if value is None else str(value)
            if isinstance(allowed, tuple):
                out.append(value if value in allowed else OTHER)
                continue
            seen = self._seen[name]
            if value in seen:
                out.append(value)
            elif _SAFE_VALUE.fullmatch(value) and len(seen) < allowed:
                seen.add(value)
                out.append(value)
            else:
                out.append(OTHER)
        return tuple(out)

    def cardinality(self) -> int:
        """The most series this family can ever hold."""
        total = 1
        for allowed in self.labels.values():
            total *= (len(allowed) + 1) if isinstance(allowed, tuple) else allowed + 1
        return total

    # ---- recording -----------------------------------------------------------------------------------------
    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        if not ENABLED or amount < 0:
            return
        with self.lock:
            key = self._key(labels)
            self.series[key] = self.series.get(key, 0.0) + amount

    def set(self, value: float, **labels: Any) -> None:
        if not ENABLED:
            return
        with self.lock:
            self.series[self._key(labels)] = float(value)

    def add(self, amount: float, **labels: Any) -> None:
        """Gauge up/down (requests in flight)."""
        if not ENABLED:
            return
        with self.lock:
            key = self._key(labels)
            self.series[key] = max(0.0, self.series.get(key, 0.0) + amount)

    def observe(self, value: float, **labels: Any) -> None:
        if not ENABLED:
            return
        with self.lock:
            key = self._key(labels)
            row = self.series.get(key)
            if row is None:
                row = self.series[key] = [[0] * len(self.buckets), 0.0, 0]
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    row[0][i] += 1
            row[1] += value
            row[2] += 1

    def value(self, **labels: Any) -> Any:
        """Current value of one series (tests and the status page); 0 when it was never recorded."""
        with self.lock:
            got = self.series.get(self._key(labels))
        if got is None:
            return 0 if self.kind != "histogram" else [[0] * len(self.buckets), 0.0, 0]
        return got

    def reset(self) -> None:
        with self.lock:
            self.series.clear()
            for seen in self._seen.values():
                seen.clear()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            series = [[list(k), (v if self.kind != "histogram" else [list(v[0]), v[1], v[2]])]
                      for k, v in self.series.items()]
        return {"type": self.kind, "help": self.help, "labels": list(self.labels), "buckets": list(self.buckets),
                "merge": self.merge, "live": self.live, "series": series}


class Registry:
    def __init__(self) -> None:
        self.families: dict[str, Family] = {}

    def _add(self, family: Family) -> Family:
        existing = self.families.get(family.name)
        if existing is not None:
            return existing
        self.families[family.name] = family
        return family

    def counter(self, name: str, help: str, labels=None) -> Family:
        assert name.endswith("_total"), name
        return self._add(Family(name, "counter", help, labels))

    def gauge(self, name: str, help: str, labels=None, *, merge: str = "max", live: bool = False) -> Family:
        return self._add(Family(name, "gauge", help, labels, merge=merge, live=live))

    def histogram(self, name: str, help: str, labels=None, buckets: tuple[float, ...] = LATENCY_BUCKETS) -> Family:
        return self._add(Family(name, "histogram", help, labels, buckets))

    def snapshot(self) -> dict[str, Any]:
        return {name: f.snapshot() for name, f in self.families.items()}

    def reset(self) -> None:
        for f in self.families.values():
            f.reset()


REGISTRY = Registry()


# ---- multi-worker snapshots ---------------------------------------------------------------------------------

#: Unique per process start, so a reused PID never overwrites a stopped worker's counters.
PROCESS_TOKEN = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
RETIRED = "retired.json"
_LOCK = ".compact.lock"


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def write_snapshot(directory: str | Path, families: dict[str, Any], *, token: str = PROCESS_TOKEN,
                   now: float | None = None) -> None:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    _write_atomic(path / f"worker-{token}.json", {"token": token, "heartbeat": now or time.time(),
                                                  "families": families})


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # a half-written foreign file or a race with compaction: skipped this scrape


def merge(snapshots: Iterable[tuple[dict[str, Any], bool]]) -> dict[str, Any]:
    """Combine family snapshots. Each item is (families, is_live). Counters and histograms are summed over every
    snapshot; gauges only over live ones, using the family's merge rule (`sum`, `max` or `min`)."""
    out: dict[str, dict[str, Any]] = {}
    for families, live in snapshots:
        for name, fam in families.items():
            if fam["type"] == "gauge" and not live:
                continue
            target = out.setdefault(name, {**fam, "series": {}})
            if target["type"] != fam["type"] or target["labels"] != fam["labels"]:
                continue  # a worker from another release with a different shape: ignored, never mixed
            series = target["series"]
            for labels, value in fam["series"]:
                key = tuple(labels)
                if fam["type"] == "histogram":
                    if list(fam["buckets"]) != list(target["buckets"]):
                        continue
                    cur = series.get(key)
                    if cur is None:
                        series[key] = [list(value[0]), value[1], value[2]]
                    else:
                        cur[0] = [a + b for a, b in zip(cur[0], value[0], strict=True)]
                        cur[1] += value[1]
                        cur[2] += value[2]
                elif fam["type"] == "counter" or target.get("merge") == "sum":
                    series[key] = series.get(key, 0.0) + value
                elif target.get("merge") == "min":
                    series[key] = min(series.get(key, value), value)
                else:
                    series[key] = max(series.get(key, value), value)
    for fam in out.values():
        fam["series"] = [[list(k), v] for k, v in fam["series"].items()]
    return out


def _counters_only(families: dict[str, Any]) -> dict[str, Any]:
    return {n: f for n, f in families.items() if f["type"] != "gauge"}


def compact(directory: str | Path, *, now: float | None = None, stale_after: float = STALE_AFTER) -> int:
    """Fold snapshots of stopped workers into `retired.json`, so the directory does not grow with every restart
    and their counters keep counting. Crash-safe: deleting a folded worker file is retried next time if it
    failed before. `folded` keeps one entry per worker file (replaced, not summed, since a worker's own counters
    are cumulative since its process start): a worker that is only paused or stalled, not stopped, keeps its file
    and is left alone here, so `collect` reads its resumed, fresher counters instead of the stale fold. Returns
    the number of snapshots folded in this call."""
    path = Path(directory)
    now = now or time.time()
    lock = path / _LOCK
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if now - lock.stat().st_mtime > 300:  # a compactor that died holding the lock
                lock.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    except OSError:
        return 0
    try:
        os.close(fd)
        retired = _read(path / RETIRED) or {"folded": {}}
        folded: dict[str, Any] = dict(retired.get("folded", {}))
        stale = []
        for file in sorted(path.glob("worker-*.json")):
            data = _read(file)
            if data is None:
                continue  # a half-written or foreign file: leave it for a later pass, never fold blindly
            if now - float(data.get("heartbeat", 0)) > stale_after:
                stale.append((file, data))
        for file, data in stale:
            folded[file.name] = {"heartbeat": data.get("heartbeat", 0), "families": _counters_only(data["families"])}
        if len(folded) > 10_000:  # bounded: drop the longest-retired entries first
            for name in sorted(folded, key=lambda n: folded[n].get("heartbeat", 0))[: len(folded) - 10_000]:
                del folded[name]
        if stale or len(folded) != len(retired.get("folded", {})):
            _write_atomic(path / RETIRED, {"folded": folded})
        for file, _ in stale:
            file.unlink(missing_ok=True)
        return len(stale)
    finally:
        lock.unlink(missing_ok=True)


def collect(directory: str | Path, *, now: float | None = None, stale_after: float = STALE_AFTER) -> dict[str, Any]:
    path = Path(directory)
    now = now or time.time()
    retired = _read(path / RETIRED) or {"folded": {}}
    folded = retired.get("folded", {})
    items: list[tuple[dict[str, Any], bool]] = []
    live_names: set[str] = set()
    for file in sorted(path.glob("worker-*.json")):
        data = _read(file)
        if data is None:
            continue
        live_names.add(file.name)
        items.append((data.get("families", {}), now - float(data.get("heartbeat", 0)) <= stale_after))
    for name, entry in folded.items():
        if name in live_names:
            continue  # the worker resumed and wrote a fresh file: its own snapshot already covers this folding
        items.append((entry.get("families", {}), False))
    return merge(items)


def workers(directory: str | Path, *, now: float | None = None, stale_after: float = STALE_AFTER) -> int:
    now = now or time.time()
    live = 0
    for file in Path(directory).glob("worker-*.json"):
        data = _read(file)
        if data and now - float(data.get("heartbeat", 0)) <= stale_after:
            live += 1
    return live


# ---- rendering ----------------------------------------------------------------------------------------------

def render(families: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for name in sorted(families):
        fam = families[name]
        lines.append(f"# HELP {name} {fam['help']}")
        lines.append(f"# TYPE {name} {fam['type']}")
        names = fam["labels"]
        for labels, value in sorted(fam["series"], key=lambda s: s[0]):
            pairs = [f'{k}="{_escape(v)}"' for k, v in zip(names, labels, strict=True)]
            text = ",".join(pairs)
            if fam["type"] != "histogram":
                lines.append(f"{name}{{{text}}} {_num(value)}" if text else f"{name} {_num(value)}")
                continue
            counts, total, count = value
            prefix = text + "," if text else ""
            for bound, n in zip(fam["buckets"], counts, strict=True):
                lines.append(f'{name}_bucket{{{prefix}le="{bound}"}} {n}')
            lines.append(f'{name}_bucket{{{prefix}le="+Inf"}} {count}')
            suffix = f"{{{text}}}" if text else ""
            lines.append(f"{name}_sum{suffix} {total:.6f}")
            lines.append(f"{name}_count{suffix} {count}")
    return lines


class Timer:
    """`with Timer() as t: ...; t.elapsed` (monotonic seconds)."""

    def __enter__(self) -> Timer:
        self.started = time.monotonic()
        self.elapsed = 0.0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.elapsed = time.monotonic() - self.started
