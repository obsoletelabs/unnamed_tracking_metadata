"""Metric families (#69-#76): bounded labels, documented names, cardinality budget and multi-worker aggregation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from atlas import instruments, metrics
from atlas.metrics import OTHER, REGISTRY, Family

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "guide" / "observability.md").read_text(encoding="utf-8")
#: Worst case for one family, histogram buckets included. The biggest today is a few hundred series.
FAMILY_BUDGET = 2_500
#: Worst case for every process-recorded family together (before route/peer labels, which have their own budgets).
TOTAL_BUDGET = 15_000
FORBIDDEN_LABELS = {"job", "user", "user_id", "account", "subject", "client_id", "email", "ip", "token_value", "url",
                    "path", "query", "title", "error", "message", "exception", "instance"}


@pytest.fixture
def recording():
    was = metrics.ENABLED
    metrics.enable()
    REGISTRY.reset()
    yield
    REGISTRY.reset()
    metrics.enable(was)


def worst_case(family: Family) -> int:
    per_series = len(family.buckets) + 3 if family.kind == "histogram" else 1  # buckets, +Inf, _sum, _count
    return family.cardinality() * per_series


def test_every_family_is_documented_typed_and_named_by_convention():
    assert len(REGISTRY.families) >= 30
    for name, family in REGISTRY.families.items():
        assert re.fullmatch(r"atlas_[a-z0-9_]+", name), name
        assert f"`{name}`" in DOC, f"{name} is not documented in docs/guide/observability.md"
        if family.kind == "counter":
            assert name.endswith("_total"), name
        else:
            assert not name.endswith("_total"), name
        if family.kind == "histogram":
            assert list(family.buckets) == sorted(family.buckets) and family.buckets, name
        assert family.help and "\n" not in family.help


def test_collector_series_are_documented_too():
    """Series rendered at scrape time (not in the registry) are named in the guide as well."""
    sources = "".join((ROOT / "atlas" / f).read_text(encoding="utf-8")
                      for f in ("observability.py", "catalog_metrics.py"))
    names = set(re.findall(r"# TYPE (atlas_[a-z0-9_]+)", sources))
    assert {"atlas_catalog_field_coverage", "atlas_media_storage_bytes", "atlas_backup_last_verified_timestamp_seconds",
            "atlas_mail_oldest_pending_timestamp_seconds", "atlas_database_pool_connections"} <= names
    groups = (ROOT / "docs" / "guide" / "node-groups.md").read_text(encoding="utf-8")  # owner-group series live there
    missing = sorted(n for n in names if f"`{n}`" not in DOC + groups)
    assert not missing, missing


def test_no_family_uses_a_reserved_or_identifying_label():
    for name, family in REGISTRY.families.items():
        assert not set(family.labels) & FORBIDDEN_LABELS, (name, family.labels)
    with pytest.raises(AssertionError):
        Family("atlas_x_total", "counter", "x", {"job": ("a",)})


def test_cardinality_is_bounded_per_family_and_in_total():
    total = 0
    for name, family in REGISTRY.families.items():
        for label, allowed in family.labels.items():
            if isinstance(allowed, int):  # only configured control-plane values may be open-ended, and capped
                assert label == "provider" and allowed == instruments.PROVIDER_CAP, (name, label)
        cost = worst_case(family)
        assert cost <= FAMILY_BUDGET, (name, cost)
        total += cost
    assert total <= TOTAL_BUDGET, total


def test_values_outside_the_allowlist_fold_into_other_and_never_create_series(recording):
    fam = instruments.background_ops
    hostile = ["../../etc/passwd", "user@example.com", "https://evil.example/?q=1", "x" * 500, "", None, "admin\n"]
    for value in hostile:
        fam.inc(operation=value, outcome=value)
    keys = {tuple(k) for k, _ in fam.snapshot()["series"]}
    assert keys == {(OTHER, OTHER)}
    assert fam.value(operation="nope", outcome="nope") == len(hostile)


def test_the_provider_label_is_capped_and_rejects_unsafe_values(recording):
    fam = instruments.provider_cache
    for i in range(instruments.PROVIDER_CAP + 10):
        fam.inc(provider=f"provider{i}", result="hit")
    fam.inc(provider="has space", result="hit")
    fam.inc(provider='quote"inject', result="hit")
    providers = {k[0] for k, _ in fam.snapshot()["series"]}
    assert len(providers - {OTHER}) == instruments.PROVIDER_CAP and OTHER in providers
    assert fam.value(provider=OTHER, result="hit") == 12


def test_nothing_is_recorded_while_metrics_are_disabled():
    was = metrics.ENABLED
    metrics.enable(False)
    try:
        REGISTRY.reset()
        instruments.background_ops.inc(operation="backup", outcome="ok")
        instruments.background_duration.observe(1.0, operation="backup")
        assert REGISTRY.snapshot()["atlas_background_operations_total"]["series"] == []
    finally:
        metrics.enable(was)


def test_rendered_text_is_valid_exposition_with_cumulative_buckets(recording):
    instruments.search_duration.observe(0.003, mode="title", outcome="ok")
    instruments.search_duration.observe(0.2, mode="title", outcome="ok")
    instruments.search_duration.observe(50, mode="title", outcome="ok")
    lines = metrics.render(metrics.merge([(REGISTRY.snapshot(), True)]))
    text = "\n".join(lines)
    assert "# HELP atlas_search_duration_seconds " in text and "# TYPE atlas_search_duration_seconds histogram" in text
    buckets = [int(line.rsplit(" ", 1)[1]) for line in lines if line.startswith("atlas_search_duration_seconds_bucket")]
    assert buckets == sorted(buckets) and buckets[0] == 1 and buckets[-1] == 3  # +Inf is the count
    assert 'atlas_search_duration_seconds_count{mode="title",outcome="ok"} 3' in lines
    for line in lines:
        assert line.startswith("#") or re.fullmatch(r'atlas_[a-z0-9_]+(\{[a-z_]+="[^"\n]*"(,[a-z_]+="[^"\n]*")*\})? \S+',
                                                    line), line


def test_threads_recording_at_once_lose_nothing(recording):
    def work():
        for _ in range(5000):
            instruments.db_transactions.inc(outcome="commit")
            instruments.db_query.observe(0.001, operation="select", outcome="ok")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert instruments.db_transactions.value(outcome="commit") == 40_000
    assert instruments.db_query.value(operation="select", outcome="ok")[2] == 40_000


# ---- multi-worker aggregation (ATLAS_METRICS_DIR) ------------------------------------------------------------

def family(kind, series, *, labels=("outcome",), buckets=(), merge="sum", live=False):
    return {"type": kind, "help": "h", "labels": list(labels), "buckets": list(buckets), "merge": merge, "live": live,
            "series": series}


def test_counters_sum_across_workers_and_gauges_only_over_live_ones(tmp_path):
    now = 1_000_000.0
    a = {"atlas_x_total": family("counter", [[["ok"], 3]]),
         "atlas_in_flight": family("gauge", [[[], 2]], labels=(), live=True)}
    b = {"atlas_x_total": family("counter", [[["ok"], 4], [["error"], 1]]),
         "atlas_in_flight": family("gauge", [[[], 5]], labels=(), live=True)}
    metrics.write_snapshot(tmp_path, a, token="a", now=now)
    metrics.write_snapshot(tmp_path, b, token="b", now=now - 500)  # stopped long ago
    got = metrics.collect(tmp_path, now=now)
    assert dict((tuple(k), v) for k, v in got["atlas_x_total"]["series"]) == {("ok",): 7, ("error",): 1}
    assert got["atlas_in_flight"]["series"] == [[[], 2]]  # the stopped worker's gauge is not a live request
    assert metrics.workers(tmp_path, now=now) == 1


def test_a_stopped_worker_is_folded_once_so_counters_stay_monotonic(tmp_path):
    now = 2_000_000.0
    metrics.write_snapshot(tmp_path, {"atlas_x_total": family("counter", [[["ok"], 10]])}, token="old", now=now - 500)
    metrics.write_snapshot(tmp_path, {"atlas_x_total": family("counter", [[["ok"], 1]])}, token="new", now=now)
    before = metrics.collect(tmp_path, now=now)["atlas_x_total"]["series"]
    assert metrics.compact(tmp_path, now=now) == 1
    assert not (tmp_path / "worker-old.json").exists() and (tmp_path / metrics.RETIRED).exists()
    assert metrics.compact(tmp_path, now=now) == 0  # folded once, never twice
    after = metrics.collect(tmp_path, now=now)["atlas_x_total"]["series"]
    assert before == after == [[["ok"], 11]]
    # the restarted worker keeps counting from zero; the total never goes down
    metrics.write_snapshot(tmp_path, {"atlas_x_total": family("counter", [[["ok"], 2]])}, token="new", now=now + 1)
    assert metrics.collect(tmp_path, now=now + 1)["atlas_x_total"]["series"] == [[["ok"], 12]]


def test_a_snapshot_left_behind_by_a_crashed_compaction_is_not_counted_twice(tmp_path):
    now = 3_000_000.0
    metrics.write_snapshot(tmp_path, {"atlas_x_total": family("counter", [[["ok"], 5]])}, token="old", now=now - 500)
    metrics.compact(tmp_path, now=now)
    # simulate the unlink having failed: the folded file is back
    metrics.write_snapshot(tmp_path, {"atlas_x_total": family("counter", [[["ok"], 5]])}, token="old", now=now - 500)
    assert metrics.collect(tmp_path, now=now)["atlas_x_total"]["series"] == [[["ok"], 5]]


def test_half_written_and_foreign_shaped_snapshots_are_skipped(tmp_path):
    now = 4_000_000.0
    metrics.write_snapshot(tmp_path, {"atlas_x_total": family("counter", [[["ok"], 1]])}, token="a", now=now)
    (tmp_path / "worker-broken.json").write_text('{"token": "broken", "heartbeat": ', encoding="utf-8")
    other_shape = {"atlas_x_total": family("counter", [[["ok", "x"], 9]], labels=("outcome", "extra"))}
    metrics.write_snapshot(tmp_path, other_shape, token="b", now=now)
    got = metrics.collect(tmp_path, now=now)
    assert got["atlas_x_total"]["series"] == [[["ok"], 1]]  # a release with another label set is never mixed in


WORKER = """
import sys
from atlas import instruments, metrics
metrics.enable()
for _ in range(int(sys.argv[2])):
    instruments.background_ops.inc(operation="sync_round", outcome="ok")
    instruments.background_duration.observe(0.2, operation="sync_round")
metrics.write_snapshot(sys.argv[1], metrics.REGISTRY.snapshot())
"""


def test_separate_worker_processes_add_up_exactly(tmp_path):
    """Real processes (as `uvicorn --workers N` would start), each writing its own snapshot into the shared dir."""
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(tmp_path), str(n)], cwd=ROOT)
             for n in (100, 250, 7)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0]
    assert len(list(tmp_path.glob("worker-*.json"))) == 3  # one file per process start, never a shared one
    got = metrics.collect(tmp_path)
    ops = {tuple(k): v for k, v in got["atlas_background_operations_total"]["series"]}
    assert ops == {("sync_round", "ok"): 357}
    [(_, (_, total, count))] = got["atlas_background_operation_duration_seconds"]["series"]
    assert count == 357 and total == pytest.approx(357 * 0.2)
    text = "\n".join(metrics.render(got))
    assert 'atlas_background_operations_total{operation="sync_round",outcome="ok"} 357' in text
    snapshot = json.loads(next(tmp_path.glob("worker-*.json")).read_text(encoding="utf-8"))
    assert set(snapshot) == {"token", "heartbeat", "families"}  # names, bounded labels and numbers only
