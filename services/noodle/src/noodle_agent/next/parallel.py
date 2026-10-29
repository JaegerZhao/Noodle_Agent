"""Bounded in-flight work; a failed item cannot leave an entire book draining silently."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def checked_parallel(function, items, workers):
    indexed = iter(enumerate(items))
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = {}

    def submit_one():
        try:
            index, item = next(indexed)
        except StopIteration:
            return False
        pending[pool.submit(function, item)] = index
        return True

    try:
        for _ in range(workers):
            if not submit_one():
                break
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            # Detect errors before queuing replacement work, even if multiple results arrived.
            for future in finished:
                if future.exception() is not None:
                    future.result()
            for future in finished:
                index = pending.pop(future)
                yield index, future.result()
                submit_one()
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


class LimitedVision:
    """One generation shares the same visual request slots across all writers."""

    def __init__(self, provider, limit=2):
        from threading import BoundedSemaphore

        self.provider = provider
        self.slots = BoundedSemaphore(limit)

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def analyze(self, *args, **kwargs):
        with self.slots:
            return self.provider.analyze(*args, **kwargs)

    def analyze_batch(self, *args, **kwargs):
        with self.slots:
            return self.provider.analyze_batch(*args, **kwargs)
