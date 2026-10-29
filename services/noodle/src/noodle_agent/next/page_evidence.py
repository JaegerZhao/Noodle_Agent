"""Prepare page-grounded evidence once, with independent per-note dependencies."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from .visual_context import page_contexts


class PageEvidence:
    def __init__(self, generation, units):
        self.g = generation
        self.pool = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("NOODLE_NEXT_VISION_CONCURRENCY", "3"))), thread_name_prefix="page-evidence")
        self.units = {}
        for unit in units:
            self.units.setdefault(unit.page, []).append(unit)
        self.futures = {}
        self.lock = Lock()
        self.provider_blocked = None

    def __enter__(self):
        from .visual_context import needed_pages
        from .visual_policy import asset_needs_vision, native_text
        all_units = [u for values in self.units.values() for u in values]
        needed = set(needed_pages(all_units, True))
        needed.update(page for page, values in self.units.items()
                      if any(asset_needs_vision(u, native_text(values)) for u in values))
        from .cost_policy import enabled
        if enabled():
            from .visual_context import native_page_complete
            skipped = {page for page in needed if native_page_complete(self.units[page])}
            needed.difference_update(skipped)
            if skipped:
                self.g.progress('visual-native-reused', {'pages': sorted(skipped), 'policy': 'native-complete-v2'})
        self.needed = needed
        return self

    def prefetch(self, units):
        with self.lock:
            for page in sorted({u.page for u in units} & self.needed):
                if page not in self.futures:
                    self.futures[page] = self.pool.submit(self._prepare, page)

    def _prepare(self, page):
        from .resilience import transient_provider_failure

        for attempt in range(3):
            with self.lock:
                if self.provider_blocked is not None:
                    raise self.provider_blocked
            self.g.progress("visual-page-started", {"page": page,'attempt':attempt+1})
            try:
                result = page_contexts(self.g.store, self.g.journal, self.g.parsing.vision,
                                       self.g.lease.input["revision_id"], self.units[page],
                                       source_aligned=True, selected_pages=[page])
            except Exception as exc:
                from pydantic import ValidationError

                from .planning import QualityFailure
                from .provider_errors import provider_issue
                if provider_issue({'detail':str(exc)}):
                    with self.lock:
                        self.provider_blocked = exc
                    raise
                if isinstance(exc, (ValidationError, ValueError, TypeError)):
                    # A malformed page response is local to its dependent
                    # Atomics. Do not stop unrelated chapters or publish them
                    # with invented/missing visual evidence.
                    raise QualityFailure(f'Visual evidence response invalid on page {page}: {exc}') from exc
                if attempt==2 or not transient_provider_failure(exc):
                    raise
                self.g.progress('visual-retry',{'page':page,'attempt':attempt+1,'error_code':type(exc).__name__,'backoff_seconds':2**(attempt+1)})
                time.sleep(2**(attempt+1))
                continue
            self.g.progress("visual-page-ready", {"page": page})
            return result

    def for_units(self, units):
        self.prefetch(units)
        return [context for page in sorted({u.page for u in units}) if page in self.futures
                for context in self.futures[page].result()]

    def __exit__(self, *args):
        self.pool.shutdown(wait=True, cancel_futures=True)
