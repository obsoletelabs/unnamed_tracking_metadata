from atlas import main, media


class StopAfterWaits:
    def __init__(self, count, after_wait=None):
        self.count = count
        self.waits = []
        self.after_wait = after_wait

    def is_set(self):
        return len(self.waits) >= self.count

    def wait(self, delay):
        self.waits.append(delay)
        if self.after_wait:
            self.after_wait()


def test_media_worker_drains_full_batches_and_waits_when_idle(monkeypatch, maker, ctx):
    stop = StopAfterWaits(3)
    results = iter([ctx.settings.media_cache_batch, 1, 0])
    monkeypatch.setattr(media, "cache_pending", lambda *_, **__: next(results))

    main._cache_media(maker, ctx, stop)

    assert stop.waits[0] == 0
    assert all(0 < delay <= 5 for delay in stop.waits[1:])


def test_media_worker_recovers_after_failure_without_logging_secrets(monkeypatch, maker, ctx, caplog):
    stop = StopAfterWaits(2)
    attempts = []

    def cache(*_, **__):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("https://example.test/image?secret=private")
        return 0

    monkeypatch.setattr(media, "cache_pending", cache)
    main._cache_media(maker, ctx, stop)

    assert len(attempts) == 2 and all(0 < delay <= 5 for delay in stop.waits)
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text


def test_media_worker_respects_disabled_caching_and_live_setting_changes(monkeypatch, maker, ctx):
    ctx.settings = ctx.settings.model_copy(update={"media_cache_enabled": False})
    downloads = []

    def change_setting():
        ctx.settings = ctx.settings.model_copy(update={"media_cache_enabled": len(stop.waits) == 1})

    stop = StopAfterWaits(3, change_setting)
    monkeypatch.setattr(media, "cache_pending", lambda *_, **__: downloads.append(True) or 0)
    main._cache_media(maker, ctx, stop)

    assert downloads == [True] and all(0 < delay <= 5 for delay in stop.waits)


def test_housekeeping_does_not_run_image_downloads(monkeypatch, maker, ctx):
    downloads = []
    monkeypatch.setattr(media, "cache_pending", lambda *_: downloads.append(True))

    main._housekeeping(maker, ctx, StopAfterWaits(1))

    assert not downloads
