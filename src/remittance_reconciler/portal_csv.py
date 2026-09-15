"""Pure parser for the portal's sales-report CSV export.

Deliberately separate from the email parser: the export uses one-decimal amounts, so the strict
two-decimal email rule would reject valid rows. Patient-identifying columns are never read.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

from .models import PortalInvoice

__all__ = [
    "PortalCsvError",
    "REQUIRED_CSV_COLUMNS",
    "NEVER_EXTRACT_COLUMNS",
    "parse_portal_amount",
    "parse_sales_csv",
]


class PortalCsvError(Exception):
    """The CSV export does not have the expected structure."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


REQUIRED_CSV_COLUMNS: tuple[str, ...] = (
    "Invoice #",
    "Balance",
    "Payer",
    "Status",
    "Total",
    "Collected",
    "Location",
)

NEVER_EXTRACT_COLUMNS: frozenset[str] = frozenset(
    {"Patient", "Patient ID", "Service", "Provider"}
)

_PORTAL_AMOUNT_RE = re.compile(r"^\d{1,3}(,\d{3})*(\.\d+)?$|^\d+(\.\d+)?$")


def parse_portal_amount(raw: str) -> Decimal:
    """Parse an export amount (one or two decimals) into a signed two-decimal ``Decimal``."""
    if raw is None:
        raise PortalCsvError("AMOUNT_FORMAT", "amount is None")
    s = str(raw).strip()
    if not s:
        raise PortalCsvError("AMOUNT_FORMAT", "empty amount")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.endswith("-"):
        negative, s = True, s[:-1].strip()
    if s.startswith("-"):
        negative, s = True, s[1:].strip()
    for prefix in ("USD $", "USD$", "USD ", "$"):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
            break

    if not _PORTAL_AMOUNT_RE.match(s):
        raise PortalCsvError("AMOUNT_FORMAT", f"not a portal amount: {raw!r}")
    try:
        value = Decimal(s.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover
        raise PortalCsvError("AMOUNT_FORMAT", f"undecodable amount: {raw!r}") from exc

    value = value.quantize(Decimal("0.01"))
    return -value if negative else value


def _normalize_headers(raw_headers: Iterable[str]) -> dict[str, int]:
    """Bind column names to indexes after stripping whitespace; reject duplicates and missing columns."""
    mapping: dict[str, int] = {}
    for i, h in enumerate(raw_headers):
        name = (h or "").strip()
        if not name:
            continue
        if name in mapping:
            raise PortalCsvError("CSV_DUP_COLUMN", f"duplicate column: {name!r}")
        mapping[name] = i
    missing = [c for c in REQUIRED_CSV_COLUMNS if c not in mapping]
    if missing:
        raise PortalCsvError("CSV_MISSING_COLUMN", f"missing column(s): {missing}")
    return mapping


def parse_sales_csv(text: str) -> list[PortalInvoice]:
    """Parse the export into :class:`PortalInvoice` rows, skipping patient-identifying columns entirely."""
    reader = csv.reader(io.StringIO(text))
    try:
        raw_headers = next(reader)
    except StopIteration:
        raise PortalCsvError("CSV_EMPTY", "csv has no header row") from None

    if raw_headers and raw_headers[0].startswith("﻿"):
        raw_headers[0] = raw_headers[0].lstrip("﻿")

    idx = _normalize_headers(raw_headers)
    width = len(raw_headers)
    out: list[PortalInvoice] = []

    for row in reader:
        if not any((c or "").strip() for c in row):
            continue
        if len(row) < width:
            row = row + [""] * (width - len(row))

        def cell(name: str) -> str:
            return (row[idx[name]] or "").strip()

        invoice_no = cell("Invoice #").lstrip("#").strip()
        if not invoice_no:
            continue

        out.append(
            PortalInvoice(
                invoice_no=invoice_no,
                balance=parse_portal_amount(cell("Balance")),
                payer=cell("Payer"),
                status=cell("Status"),
                total=parse_portal_amount(cell("Total")),
                collected=parse_portal_amount(cell("Collected")),
                location=cell("Location"),
            )
        )
    return out
