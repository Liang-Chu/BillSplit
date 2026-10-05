"""OCR slots are held by jobs until they really finish (or are cancelled while queued)."""

import threading
import time

import pytest

from app.errors import APIError


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def make_engine(monkeypatch):
    from app.config import get_settings

    engines = []

    def _make(concurrency=1, queue=0, timeout=0.05):
        monkeypatch.setenv("OCR_ENABLED", "true")
        monkeypatch.setenv("OCR_MAX_CONCURRENCY", str(concurrency))
        monkeypatch.setenv("OCR_MAX_QUEUE", str(queue))
        monkeypatch.setenv("OCR_TIMEOUT_SECONDS", str(timeout))
        get_settings.cache_clear()
        from app.services.ocr import OCREngine

        e = OCREngine()
        engines.append(e)
        return e

    yield _make
    for e in engines:
        e._executor.shutdown(wait=True, cancel_futures=True)
    get_settings.cache_clear()


def gated_job(engine):
    """Replace _run with a job that blocks until `gate` is set; `running` is set while it runs."""
    gate, running = threading.Event(), threading.Event()

    def _run(data, mime):
        running.set()
        gate.wait(5)
        running.clear()
        return {"merchant": None}

    engine._run = _run
    return gate, running


def code_of(fn):
    try:
        fn()
        return "ok"
    except APIError as e:
        return e.code


def test_timed_out_running_job_keeps_its_slot(make_engine):
    e = make_engine(concurrency=1, queue=0, timeout=0.02)
    gate, running = gated_job(e)
    assert code_of(lambda: e.read(b"x", "image/png")) == "ocr_failed"  # caller timed out
    assert running.is_set() and e.pending == 1  # job still running, slot still held
    assert code_of(lambda: e.read(b"x", "image/png")) == "ocr_busy"  # rejected, not queued
    gate.set()
    assert wait_until(lambda: e.pending == 0)  # released when the job really ends
    e._run = lambda d, m: {"merchant": "ok"}
    assert e.read(b"x", "image/png") == {"merchant": "ok"}
    assert e.pending == 0


def test_queued_job_cancelled_on_timeout_releases_slot(make_engine):
    e = make_engine(concurrency=1, queue=1, timeout=0.05)
    gate, running = gated_job(e)
    first = threading.Thread(target=lambda: code_of(lambda: e.read(b"x", "image/png")))
    first.start()
    assert wait_until(running.is_set)
    # second job waits in the executor queue, times out, is cancelled → its slot comes back at once
    assert code_of(lambda: e.read(b"x", "image/png")) == "ocr_failed"
    assert e.pending == 1  # only the running job
    first.join()
    assert e.pending == 1  # first caller timed out too, but its job is still running
    gate.set()
    assert wait_until(lambda: e.pending == 0)


def test_success_and_failure_release_exactly_once(make_engine):
    e = make_engine(concurrency=1, queue=0, timeout=1)
    e._run = lambda d, m: {"merchant": "x"}
    for _ in range(3):
        assert e.read(b"x", "image/png") == {"merchant": "x"}
    assert e.pending == 0

    def boom(d, m):
        raise RuntimeError("engine crashed")

    e._run = boom
    for _ in range(3):
        assert code_of(lambda: e.read(b"x", "image/png")) == "ocr_failed"
    assert wait_until(lambda: e.pending == 0) and e.pending == 0


def test_submit_failure_returns_slot(make_engine, monkeypatch):
    e = make_engine(concurrency=1, queue=0, timeout=1)

    def broken_submit(*a, **k):
        raise RuntimeError("executor is shut down")

    monkeypatch.setattr(e._executor, "submit", broken_submit)
    assert code_of(lambda: e.read(b"x", "image/png")) == "ocr_failed"
    assert e.pending == 0


def test_counter_never_goes_negative(make_engine):
    e = make_engine()
    e._release()
    assert e.pending == 0
