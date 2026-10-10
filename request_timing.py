"""Per-request stage timings for /discussion, logged as one line per request.

Only fixed stage names, durations, counts, the status and the worker pid are logged —
never titles, IDs, URLs or upstream content (see the privacy policy's logging limits).
"""
from contextlib import contextmanager
from time import perf_counter
import logging
import os

from flask import g, has_request_context

logger = logging.getLogger('aninex.timing')
# Gunicorn/Flask default levels can hide INFO records. Configure only this logger.
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def start():
    g.timing_start = perf_counter()
    g.timing_stages = {}
    g.timing_logged = False


def record(stage, seconds):
    if not has_request_context() or getattr(g, 'timing_stages', None) is None:
        return
    total, count = g.timing_stages.get(stage, (0.0, 0))
    g.timing_stages[stage] = (total + seconds, count + 1)


@contextmanager
def timed(stage):
    """Time one outbound call (or group of calls) under a fixed stage name."""
    began = perf_counter()
    try:
        yield
    finally:
        record(stage, perf_counter() - began)


def finish(status):
    """Log the request's timing line once. Returns the line for tests."""
    if getattr(g, 'timing_start', None) is None or g.timing_logged:
        return None
    g.timing_logged = True
    total_ms = round((perf_counter() - g.timing_start) * 1000)
    stages = ' '.join(f'{stage}={round(total * 1000)}ms/{count}'
                      for stage, (total, count) in sorted(g.timing_stages.items()))
    # Time not attributed to a stage is our own work (parsing, JSON, Flask).
    other_ms = total_ms - round(sum(total for total, _ in g.timing_stages.values()) * 1000)
    line = (f'discussion_timing status={status} total_ms={total_ms} '
            f'{stages + " " if stages else ""}other_ms={max(other_ms, 0)} pid={os.getpid()}')
    logger.info(line)
    return line
