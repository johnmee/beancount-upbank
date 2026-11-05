"""Fixtures shared by the test modules."""

import datetime

import pytest

from beancount_upbank import client as upbank_client


@pytest.fixture
def pinned_today(monkeypatch):
    """Pin Sydney's "today" to a fixed date, so a run that crosses midnight can't flake.

    The date is not the real today, so a test passes only if the code under
    test asks ``sydney_today()`` rather than reading the clock itself.
    """
    day = datetime.date(2026, 9, 7)
    monkeypatch.setattr(upbank_client, "sydney_today", lambda: day)
    return day
