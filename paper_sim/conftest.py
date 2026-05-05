"""Pytest config for paper_sim. Auto-marks async tests."""
import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "asyncio" not in item.keywords:
            # only mark coroutines
            import asyncio
            try:
                if asyncio.iscoroutinefunction(item.function):
                    item.add_marker(pytest.mark.asyncio)
            except AttributeError:
                pass
