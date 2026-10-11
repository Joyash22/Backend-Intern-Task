from decimal import Decimal

from services.data_source import ScanContext, transform_deal
from tests.conftest import hs_deal


def test_transform_types_and_metadata():
    row = transform_deal(hs_deal("9", "X", "25000", "closedwon", "2026-09-30T00:00:00Z"), ScanContext("t1", "s1"))
    assert row["id"] == "9" and row["amount"] == Decimal("25000.00")
    assert row["closedate"].year == 2026 and row["closedate"].tzinfo is not None
    assert row["hs_is_closed_won"] is True
    assert (row["_tenant_id"], row["_scan_id"]) == ("t1", "s1") and row["extracted_at"]


def test_transform_handles_nulls_and_epoch_millis():
    raw = {"id": 5, "properties": {"amount": "", "closedate": "1767225600000", "dealname": None}}
    row = transform_deal(raw, ScanContext("t", "s"))
    assert row["amount"] is None and row["closedate"].year == 2026 and row["dealname"] is None


def test_transform_bad_values_become_null():
    row = transform_deal({"id": "1", "properties": {"amount": "abc", "closedate": "garbage"}}, ScanContext("t", "s"))
    assert row["amount"] is None and row["closedate"] is None
