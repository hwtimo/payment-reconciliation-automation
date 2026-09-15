"""Sales-report CSV parsing, amount tolerance and exclusion of patient columns."""

from __future__ import annotations

from decimal import Decimal

import pytest

from remittance_reconciler.portal_csv import (
    NEVER_EXTRACT_COLUMNS,
    PortalCsvError,
    parse_portal_amount,
    parse_sales_csv,
)
from remittance_reconciler.parser import ParseError, parse_amount

EXPORT_HEADER = (
    "Location, Service Date, Invoice Date, Patient ID, Patient, Service,"
    " Provider, Payer, Invoice #, Category, Notes, Status,"
    " Subtotal, Total, Collected, Balance, Tax"
)


def _csv(*rows: str) -> str:
    return EXPORT_HEADER + "\n" + "\n".join(rows) + "\n"


def test_headers_are_stripped_before_binding():
    rows = parse_sales_csv(
        _csv("Clinic,2037-07-07,2037-07-07,G,P,I,S,ACME Plan A,290012-B01,Cat,,unpaid,86.0,86.0,0.0,86.0,0.0")
    )
    assert len(rows) == 1
    assert rows[0].invoice_no == "290012-B01"
    assert rows[0].balance == Decimal("86.00")


def test_phi_columns_are_never_extracted():
    rows = parse_sales_csv(
        _csv(
            "Clinic,2037-07-07,2037-07-07,GUID-XYZ,Pat Doe,Service 60,Staff A,"
            "ACME Direct,290013-B01,Cat,,unpaid,131.65,131.65,0.0,131.65,0.0"
        )
    )
    blob = repr(rows)
    for leaked in ("GUID-XYZ", "Pat Doe", "Service 60", "Staff A"):
        assert leaked not in blob, f"PHI leaked into parsed output: {leaked}"
    assert NEVER_EXTRACT_COLUMNS == {"Patient", "Patient ID", "Service", "Provider"}


def test_leading_hash_is_stripped_from_identifier():
    rows = parse_sales_csv(
        _csv("Clinic,2037-07-07,2037-07-07,G,P,I,S,ACME Claims,#290014-B01,Cat,,unpaid,50.0,50.0,0.0,50.0,0.0")
    )
    assert rows[0].invoice_no == "290014-B01"


def test_suffix_alphabet_is_wider_than_B():
    rows = parse_sales_csv(
        _csv("Clinic,2037-07-07,2037-07-07,G,P,I,S,Patient,290813-S01,Cat,,unpaid,60.0,60.0,0.0,60.0,0.0")
    )
    assert rows[0].invoice_no == "290813-S01"
    assert rows[0].payer == "Patient"


def test_missing_required_column_is_rejected():
    bad = EXPORT_HEADER.replace(", Balance", ", NotBalance") + "\nx\n"
    with pytest.raises(PortalCsvError) as exc:
        parse_sales_csv(bad)
    assert exc.value.code == "CSV_MISSING_COLUMN"


@pytest.mark.parametrize(
    "raw,expected",
    [("86.0", "86.00"), ("131.65", "131.65"), ("100", "100.00"), ("1,234.5", "1234.50")],
)
def test_portal_amounts_allow_one_decimal(raw: str, expected: str):
    assert parse_portal_amount(raw) == Decimal(expected)


def test_eft_parser_still_rejects_one_decimal():
    with pytest.raises(ParseError):
        parse_amount("86.0")


def test_the_two_parsers_agree_numerically():
    assert parse_portal_amount("86.0") == parse_amount("86.00")


def test_portal_amount_preserves_sign():
    assert parse_portal_amount("-118.40") == Decimal("-118.40")
    assert parse_portal_amount("(118.40)") == Decimal("-118.40")
    assert parse_portal_amount("118.40-") == Decimal("-118.40")
