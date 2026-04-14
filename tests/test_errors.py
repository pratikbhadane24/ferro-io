"""Phase 4 tests: error propagation."""
import ferro_io


def test_run_fallible_all_ok():
    rt = ferro_io.AsyncRuntime()
    out = rt.run_fallible([1, 2, 3])
    assert out == [(True, 1), (True, 4), (True, 9)]


def test_run_fallible_partial_failure():
    rt = ferro_io.AsyncRuntime()
    out = rt.run_fallible([1, 2, 3, 4], fail_on=[2, 4])
    assert out[0] == (True, 1)
    assert out[2] == (True, 9)
    # Panicked tasks report ok=False with a descriptive message.
    panics = [r for r in out if not r[0]]
    assert len(panics) == 2
    for ok, msg in panics:
        assert ok is False
        assert "panicked" in msg
        assert "forced failure" in msg


def test_async_sleep_value_error():
    import pytest
    # Validation runs synchronously before the awaitable is returned,
    # so the ValueError surfaces immediately.
    with pytest.raises(ValueError):
        ferro_io.async_sleep(-1.0)
    with pytest.raises(ValueError):
        ferro_io.async_sleep(float("nan"))
