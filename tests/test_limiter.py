import pytest

from consultant.llm.limiter import QuotaExhausted, RateLimiter, RequestTooLarge


class Clock:
    def __init__(self):
        self.t, self.slept = 1_000_000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(clock, **kw):
    return RateLimiter(clock=clock, sleep=clock.sleep, **kw)


def test_rpm_blocks_until_oldest_request_leaves_window():
    c = Clock()
    lim = make(c, rpm=3)
    for _ in range(3):
        lim.acquire(1)
    assert c.slept == []
    lim.acquire(1)
    assert c.slept == [60.0]


def test_tpm_waits_for_tokens_and_settle_returns_unused_estimate():
    c = Clock()
    lim = make(c, tpm=100)
    ev = lim.acquire(90)
    lim.settle(ev, 10)  # actual usage was far below the estimate
    lim.acquire(80)
    assert c.slept == []
    c.t += 5
    lim.acquire(50)  # 90 used in window -> wait until the first event (t0) expires
    assert c.slept == [55.0]


def test_daily_quota_exhaustion_raises_instead_of_sleeping_for_hours():
    c = Clock()
    lim = make(c, rpd=2, max_wait=180)
    lim.acquire(1)
    lim.acquire(1)
    with pytest.raises(QuotaExhausted):
        lim.acquire(1)
    c.t += 86400
    lim.acquire(1)  # window rolled over


def test_oversized_request_rejected():
    with pytest.raises(RequestTooLarge):
        make(Clock(), tpm=8000).acquire(9000)


def test_day_window_persists_across_restarts(tmp_path):
    c = Clock()
    path = tmp_path / "m.json"
    lim = make(c, tpd=100, state_path=path)
    lim.acquire(100)
    with pytest.raises(QuotaExhausted):
        make(c, tpd=100, state_path=path).acquire(1)


def test_otpm_spaces_calls_after_a_long_output():
    c = Clock()
    lim = make(c, otpm=1000)
    ev = lim.acquire(500, out=3000)  # admission only counts up to the cap
    lim.settle(ev, 2000, 1500)       # the reply itself was long
    c.t += 10
    lim.acquire(100, out=200)
    assert c.slept == [50.0]
