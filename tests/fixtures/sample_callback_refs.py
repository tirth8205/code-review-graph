"""Fixture for issue #363: function references in callback positions.

Each `*_callback` function is passed as a bare-identifier argument to
another call. They are never invoked with parens, so without REFERENCES
edge tracking they would be flagged as dead code.
"""
from concurrent.futures import ThreadPoolExecutor


def executor_callback():
    return "submitted"


def filter_callback(item):
    return item > 0


def map_callback(item):
    return item * 2


def trigger_executor():
    with ThreadPoolExecutor() as executor:
        future = executor.submit(executor_callback)
        return future


def trigger_filter():
    items = [1, -2, 3, -4]
    return list(filter(filter_callback, items))


def trigger_map():
    items = [1, 2, 3]
    return list(map(map_callback, items))
