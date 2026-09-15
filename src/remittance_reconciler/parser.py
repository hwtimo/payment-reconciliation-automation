"""Pure parser: remittance email HTML -> :class:`~remittance_reconciler.models.EftStatement`.

No I/O, clock or randomness. Structural invariants (V1-V9) make a misread statement fail
loudly instead of producing plausible-looking wrong numbers.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from collections.abc import Collection, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import EftRow, EftStatement, WarnCode

log = logging.getLogger(__name__)

__all__ = [
    "ParseError",
    "REQUIRED_COLUMNS",
    "INVOICE_RE",
    "INVOICE_RE_C",
    "AMOUNT_RE",
    "FP_FIELD_SEP",
    "FP_ROW_SEP",
    "parse_amount",
    "content_fingerprint",
    "parse_statement",
]


class ParseError(Exception):
    """A statement violated a parser invariant; ``code`` names the invariant (for example ``V4``)."""
    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# V1: each required column must appear exactly once, in any order.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "Date",
    "Invoice No.",
    "Claim Ref.",
    "Line ID",
    "Billed Amount",
    "Prior Paid",
    "Remaining",
    "Paid Amount",
)

# V6: the suffix is part of the identifier. -B01 and -B02 are different invoices.
INVOICE_RE = r"^\d+-[Bb]\d{2}$"

INVOICE_RE_C = re.compile(INVOICE_RE)

AMOUNT_RE = re.compile(r"^\d{1,3}(,\d{3})*\.\d{2}$|^\d+\.\d{2}$")

# G-22 separators: control characters that cannot occur inside values.
FP_FIELD_SEP = "\x1f"
FP_ROW_SEP = "\x1e"


def parse_amount(raw: str) -> Decimal:
    """Parse a remittance amount into a signed ``Decimal`` with exactly two decimals.

    Accepts a currency prefix, thousands separators and three negative notations
    (``-1.00``, ``1.00-``, ``(1.00)``). Anything else raises ``ParseError("AMOUNT_FORMAT")``.
    Stripping non-digit characters instead would silently turn a clawback into a payment.
    """
    if raw is None:
        raise ParseError("AMOUNT_FORMAT", "amount is None")
    s = str(raw).strip()
    if not s:
        raise ParseError("AMOUNT_FORMAT", "empty amount")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.endswith("-"):
        negative = True
        s = s[:-1].strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    for prefix in ("USD $", "USD$", "USD ", "$"):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
            break

    if not AMOUNT_RE.match(s):
        raise ParseError("AMOUNT_FORMAT", f"not a 2-decimal amount: {raw!r}")

    try:
        value = Decimal(s.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover
        raise ParseError("AMOUNT_FORMAT", f"undecodable amount: {raw!r}") from exc

    if value != value.quantize(Decimal("0.01")):
        raise ParseError("AMOUNT_FORMAT", f"amount is not 2dp: {raw!r}")

    return -value if negative else value


def content_fingerprint(
    vendor_no: str,
    payment_document_no: str,
    eft_date: date,
    deposit_amount: Decimal,
    rows: Sequence[EftRow],
) -> str:
    """SHA-256 over a canonical, row-order-independent serialization of a statement (G-22)."""
    def m(d: Decimal) -> str:
        return str(Decimal(d).quantize(Decimal("0.01")))

    header = FP_FIELD_SEP.join(
        (
            vendor_no.strip(),
            payment_document_no.strip(),
            eft_date.isoformat(),
            m(deposit_amount),
        )
    )
    body = FP_ROW_SEP.join(
        FP_FIELD_SEP.join(
            (r.invoice_no.strip(), m(r.gross), m(r.prev_paid), m(r.outstanding), m(r.net))
        )
        for r in sorted(rows, key=lambda r: r.invoice_no)
    )
    canonical = header + FP_ROW_SEP + body
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _statement_root(html: str):
    """Drop the forwarding attribution block and narrow to the quoted original message."""
    from lxml import html as lh

    doc = lh.fromstring(html)
    for attr in doc.xpath("//*[contains(@class,'gmail_attr')]"):
        attr.getparent().remove(attr)
    quotes = doc.xpath("//*[contains(@class,'gmail_quote')]")
    return quotes[0] if quotes else doc


def _eft_table(root):
    """Locate the remittance table by its required header names, never by position."""
    want = {c.lower() for c in REQUIRED_COLUMNS}
    for tb in root.xpath(".//table"):
        rows = tb.xpath(".//tr")
        if not rows:
            continue
        for r in rows[:3]:
            cells = [re.sub(r"\s+", " ", c.text_content()).strip() for c in r.xpath("./th|./td")]
            norm = {c.lower() for c in cells if c}
            if want.issubset(norm):
                return tb, r, cells
    return None, None, None


def _header_field(text: str, label: str, pattern: str) -> list[str]:
    """Find ``label`` followed by a value matching ``pattern`` in whitespace-normalized text."""
    return re.findall(rf"{re.escape(label)}[.\s]*[:#]?[.\s]*({pattern})", text)


def parse_statement(html: str, known_vendors: Collection[str]) -> EftStatement:
    """Parse and validate one remittance email.

    Hard invariants: V1 required columns exactly once, V2 columns bound by name, V4 deposit ==
    footer total == sum of row nets, V6 identifier format (a malformed row is isolated, never
    repaired), V7 no duplicate identifiers, V8 singleton header fields, V9 known vendor.
    V2b (extra columns) and V3 (per-row accounting mismatch) produce warnings.
    """
    root = _statement_root(html)
    warnings: list[WarnCode] = []

    tb, header_row, header_cells = _eft_table(root)
    if tb is None:
        raise ParseError("V1", "no table carrying the 8 required EFT columns")

    index: dict[str, int] = {}
    for i, name in enumerate(header_cells):
        key = name.strip()
        if not key:
            continue
        if key in REQUIRED_COLUMNS:
            if key in index:
                raise ParseError("V1", f"duplicate required column: {key!r}")
            index[key] = i
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        raise ParseError("V1", f"missing required column(s): {missing}")
    if len([c for c in header_cells if c.strip()]) > len(REQUIRED_COLUMNS):
        warnings.append(WarnCode.EXTRA_COLUMNS)

    text = re.sub(r"\s+", " ", root.text_content())
    vendors = _header_field(text, "Payee ID", r"\d+")
    deposits = _header_field(text, "Deposit Total", r"(?:USD\s*)?\$?\s?[\d,]+\.\d{2}")
    paydocs = _header_field(text, "Remittance No", r"\d+")
    for label, got in (("Payee ID", vendors), ("Deposit Total", deposits),
                       ("Remittance No.", paydocs)):
        if len(got) != 1:
            raise ParseError("V8", f"{label} appears {len(got)} times, expected exactly 1")

    dates = _header_field(text, "Date", r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s*\d{4}")
    if len(dates) != 1:
        raise ParseError("V8", f"statement Date appears {len(dates)} times, expected exactly 1")
    eft_date = _parse_eft_date(dates[0])

    vendor_no = vendors[0]
    if vendor_no not in set(known_vendors):
        raise ParseError(
            "V9", f"vendor_no {vendor_no!r} is not in known_vendors; add it to "
                  f"config.yaml if this clinic location is authorized")

    deposit_amount = parse_amount(deposits[0])

    rows: list[EftRow] = []
    malformed: list[str] = []
    footer_total: Decimal | None = None
    ncols = len(header_cells)
    for tr in tb.xpath(".//tr"):
        cells = tr.xpath("./td|./th")
        if tr is header_row:
            continue
        vals = [re.sub(r"\s+", " ", c.text_content()).strip() for c in cells]
        if any("total paid" in v.lower() for v in vals):
            amounts = [v for v in vals if re.search(r"\d\.\d{2}", v)]
            if amounts:
                footer_total = parse_amount(amounts[-1])
            continue
        if len(cells) != ncols or not any(vals):
            continue
        g = lambda name: vals[index[name]]
        inv = g("Invoice No.").strip()
        if not inv:
            continue
        identifier_ok = True
        if not INVOICE_RE_C.match(inv):
            identifier_ok = False
            malformed.append(inv)
        row = EftRow(
            invoice_no=inv,
            row_date=_parse_row_date(g("Date")),
            reference_no=g("Claim Ref."),
            document_no=g("Line ID"),
            gross=parse_amount(g("Billed Amount")),
            prev_paid=parse_amount(g("Prior Paid")),
            outstanding=parse_amount(g("Remaining")),
            net=parse_amount(g("Paid Amount")),
            identifier_ok=identifier_ok,
        )
        if row.gross - row.prev_paid - row.outstanding != row.net:
            if WarnCode.ACCOUNTING_EQ not in warnings:
                warnings.append(WarnCode.ACCOUNTING_EQ)
        rows.append(row)

    if not rows:
        raise ParseError("V1", "no data rows found in the EFT table")

    seen: dict[str, str] = {}
    for r in rows:
        k = r.invoice_no.upper()
        if k in seen:
            raise ParseError("V7", f"duplicate invoice identifier within the statement: {r.invoice_no!r}")
        seen[k] = r.invoice_no

    net_sum = sum((r.net for r in rows), Decimal("0.00"))
    if footer_total is None:
        raise ParseError("V4", "statement footer total not found")
    if not (deposit_amount == footer_total == net_sum):
        raise ParseError(
            "V4",
            f"triple-sum mismatch: deposit/footer/sum(net) disagree "
            f"({deposit_amount} / {footer_total} / {net_sum})",
        )

    if malformed:
        if len(malformed) == len(rows):
            raise ParseError(
                "V6", f"every invoice identifier fails the required format "
                      f"(first: {malformed[0]!r})")
        warnings.append(WarnCode.MALFORMED_ROW_IDENTIFIER)
        log.warning("statement %s: %d malformed row identifier(s) isolated: %s",
                    paydocs[0], len(malformed), malformed)

    fp = content_fingerprint(vendor_no, paydocs[0], eft_date, deposit_amount, rows)
    return EftStatement(
        vendor_no=vendor_no,
        payment_document_no=paydocs[0],
        eft_date=eft_date,
        deposit_amount=deposit_amount,
        rows=tuple(rows),
        content_fingerprint=fp,
        warnings=tuple(warnings),
    )


def _parse_eft_date(raw: str) -> date:
    """Statement date in ``Mon DD, YYYY`` or ``Month DD, YYYY`` form."""
    s = re.sub(r"\s+", " ", raw).replace(".", "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParseError("V8", f"unparseable statement date: {raw!r}")


def _parse_row_date(raw: str) -> date:
    """Row date in ``YYYY/MM/DD`` form."""
    s = raw.strip()
    try:
        return datetime.strptime(s, "%Y/%m/%d").date()
    except ValueError as exc:
        raise ParseError("V1", f"unparseable row date: {raw!r}") from exc
