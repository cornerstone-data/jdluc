import concurrent.futures
import time

from jdluc.utils import (
    threadsafe_cache,
)


def test_threadsafe_cache_serial() -> None:
    @threadsafe_cache
    def now() -> float:
        time.sleep(0.2)
        return time.monotonic()

    result = now()
    assert now() == result
    assert now() == result
    assert now() == result
    assert now() == result


def test_threadsafe_cache_parallel() -> None:
    @threadsafe_cache
    def now() -> float:
        time.sleep(0.2)
        return time.monotonic()

    results: set[float] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures: list[concurrent.futures.Future[float]] = []
        for _ in range(25):
            futures.append(executor.submit(now))
        for future in concurrent.futures.as_completed(futures):
            results.add(future.result())
    assert len(results) == 1
