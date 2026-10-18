from __future__ import annotations

import sys
from pathlib import Path

import pytest

from open_data_mcp.fakeportal import FakePortal, build_offline_service
from open_data_mcp.service import OpenDataService

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

REQ = ("syn-service-requests-2024", "syn-res-requests-csv")
BUD = ("syn-regional-budget", "syn-res-budget-datastore")


@pytest.fixture
def offline() -> tuple[OpenDataService, FakePortal]:
    return build_offline_service()


@pytest.fixture
def service(offline: tuple[OpenDataService, FakePortal]) -> OpenDataService:
    return offline[0]


@pytest.fixture
def portal(offline: tuple[OpenDataService, FakePortal]) -> FakePortal:
    return offline[1]
