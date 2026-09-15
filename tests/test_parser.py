"""Remittance parser invariants (V1-V9) and amount-notation handling."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from conftest import (
    DEPOSIT_AMOUNT,
    EFT_DATE,
    PAYMENT_DOCUMENT_NO,
    ROW_DATE,
    VENDOR_NO,
    load_fixture,
    make_eft_row,
)
from remittance_reconciler.models import EftRow, EftStatement, WarnCode
from remittance_reconciler.parser import (
    INVOICE_RE,
    REQUIRED_COLUMNS,
    ParseError,
    content_fingerprint,
    parse_amount,
    parse_statement,
)


KNOWN_VENDORS: tuple[str, ...] = (VENDOR_NO,)

NORMAL_NETS: dict[str, Decimal] = {
    "300137-B01": Decimal("118.40"),
    "300925-B01": Decimal("64.00"),
    "300248-B01": Decimal("118.40"),
    "300583-B01": Decimal("118.40"),
    "300761-B01": Decimal("64.00"),
    "300094-B01": Decimal("64.00"),
    "300319-B01": Decimal("118.40"),
}

CREDIT_INVOICE = "300412-B01"
CREDIT_NET = Decimal("-118.40")
CREDIT_DEPOSIT = Decimal("547.20")

NEGATIVE_NOTATION_FIXTURES = ("credit_memo", "trailing_minus", "paren_negative")


def parse(name: str, known_vendors=KNOWN_VENDORS) -> EftStatement:
    return parse_statement(load_fixture(name), known_vendors)


def expect_reject(name: str, code: str, known_vendors=KNOWN_VENDORS) -> ParseError:
    with pytest.raises(ParseError) as exc:
        parse(name, known_vendors)
    assert exc.value.code == code, (
        f"{name}: expected ParseError(code={code!r}), got {exc.value.code!r}"
    )
    return exc.value


def row_of(st: EftStatement, invoice_no: str) -> EftRow:
    hits = [r for r in st.rows if r.invoice_no == invoice_no]
    assert len(hits) == 1, (
        f"{invoice_no!r} appears {len(hits)}x in {[r.invoice_no for r in st.rows]}"
    )
    return hits[0]


def net_map(st: EftStatement) -> dict[str, Decimal]:
    return {r.invoice_no: r.net for r in st.rows}


def total_net(st: EftStatement) -> Decimal:
    return sum((r.net for r in st.rows), Decimal("0.00"))


VALID_AMOUNTS = [
    ("118.40", Decimal("118.40")),
    ("64.00", Decimal("64.00")),
    ("0.00", Decimal("0.00")),
    ("$665.60", Decimal("665.60")),
    ("$1,234.56", Decimal("1234.56")),
    ("-118.40", Decimal("-118.40")),
    ("118.40-", Decimal("-118.40")),
    ("(118.40)", Decimal("-118.40")),
]


class TestParseAmount:
    @pytest.mark.parametrize("raw,expected", VALID_AMOUNTS)
    def test_accepts_contract_notations(self, raw: str, expected: Decimal) -> None:
        assert parse_amount(raw) == expected

    def test_plain_two_decimals(self) -> None:
        assert parse_amount("118.40") == Decimal("118.40")

    def test_dollar_sign_and_thousands_separator(self) -> None:
        assert parse_amount("$1,234.56") == Decimal("1234.56")

    def test_leading_minus_preserves_sign(self) -> None:
        assert parse_amount("-118.40") == Decimal("-118.40")
        assert parse_amount("-118.40") < 0

    def test_trailing_minus_is_negative(self) -> None:
        assert parse_amount("118.40-") == Decimal("-118.40")

    def test_parenthesised_is_negative(self) -> None:
        assert parse_amount("(118.40)") == Decimal("-118.40")

    def test_three_negative_notations_agree(self) -> None:
        values = {parse_amount(s) for s in ("-118.40", "118.40-", "(118.40)")}
        assert values == {Decimal("-118.40")}

    @pytest.mark.parametrize("raw", ["118.4", "100", "", "abc", "1.234,56"])
    def test_rejects_bad_format(self, raw: str) -> None:
        with pytest.raises(ParseError) as exc:
            parse_amount(raw)
        assert exc.value.code == "AMOUNT_FORMAT"

    def test_reject_carries_amount_format_code(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_amount("118.4")
        assert exc.value.code == "AMOUNT_FORMAT"

    @pytest.mark.parametrize("raw,expected", VALID_AMOUNTS)
    def test_result_is_exactly_two_decimal_places(
        self, raw: str, expected: Decimal
    ) -> None:
        got = parse_amount(raw)
        assert got == got.quantize(Decimal("0.01"))

    def test_returns_decimal_never_float(self) -> None:
        got = parse_amount("118.40")
        assert isinstance(got, Decimal)
        assert not isinstance(got, float)


class TestStatementHeader:
    def test_normal_parses(self) -> None:
        st = parse("normal_7rows")
        assert isinstance(st, EftStatement)

    def test_vendor_no_is_the_expected_value(self) -> None:
        st = parse("normal_7rows")
        assert st.vendor_no == VENDOR_NO == "8100001"

    def test_payment_document_no_keeps_leading_zeros(self) -> None:
        st = parse("normal_7rows")
        assert st.payment_document_no == PAYMENT_DOCUMENT_NO == "004000000101"
        assert st.payment_document_no.startswith("00")

    def test_deposit_amount_is_decimal(self) -> None:
        st = parse("normal_7rows")
        assert st.deposit_amount == DEPOSIT_AMOUNT == Decimal("665.60")
        assert isinstance(st.deposit_amount, Decimal)

    def test_seven_rows_with_full_identifiers(self) -> None:
        st = parse("normal_7rows")
        assert len(st.rows) == 7
        assert net_map(st) == NORMAL_NETS

    def test_row_fields_are_bound_to_the_right_columns(self) -> None:
        st = parse("normal_7rows")
        first = row_of(st, "300137-B01")
        assert first.reference_no == "90-05500755"
        assert first.document_no == "055000220022"
        assert first.gross == Decimal("118.40")
        assert first.prev_paid == Decimal("0.00")
        assert first.outstanding == Decimal("0.00")
        assert first.net == Decimal("118.40")

    def test_all_amounts_are_two_place_decimals(self) -> None:
        st = parse("normal_7rows")
        amounts = [st.deposit_amount]
        for r in st.rows:
            amounts += [r.gross, r.prev_paid, r.outstanding, r.net]
        for amount in amounts:
            assert isinstance(amount, Decimal)
            assert not isinstance(amount, float)
            assert amount == amount.quantize(Decimal("0.01"))

    def test_clean_statement_has_no_warnings(self) -> None:
        st = parse("normal_7rows")
        assert st.warnings == ()

    def test_rows_is_an_immutable_tuple(self) -> None:
        st = parse("normal_7rows")
        assert isinstance(st.rows, tuple)
        with pytest.raises(Exception):
            st.rows = ()  # type: ignore[misc]


class TestDateDiscipline:
    def test_eft_date_is_the_header_date(self) -> None:
        st = parse("normal_7rows")
        assert st.eft_date == date(2037, 5, 15)
        assert st.eft_date == EFT_DATE

    def test_every_row_date_is_the_table_date(self) -> None:
        st = parse("normal_7rows")
        assert {r.row_date for r in st.rows} == {date(2037, 5, 8)}
        assert {r.row_date for r in st.rows} == {ROW_DATE}

    def test_eft_date_and_row_date_are_distinct(self) -> None:
        st = parse("normal_7rows")
        assert st.eft_date != st.rows[0].row_date
        assert (st.eft_date - st.rows[0].row_date).days == 7

    def test_dates_are_date_objects_not_strings(self) -> None:
        st = parse("normal_7rows")
        assert isinstance(st.eft_date, date)
        assert all(isinstance(r.row_date, date) for r in st.rows)


class TestColumnInvariants:
    def test_missing_required_column_is_rejected(self) -> None:
        expect_reject("missing_column", "V1")

    def test_duplicate_required_column_is_rejected(self) -> None:
        expect_reject("duplicate_column", "V1")

    def test_reordered_columns_pass(self) -> None:
        st = parse("reordered_columns")
        assert len(st.rows) == 7

    def test_reordered_columns_bind_values_by_name(self) -> None:
        reordered = parse("reordered_columns")
        normal = parse("normal_7rows")
        assert net_map(reordered) == net_map(normal)
        assert {r.invoice_no: r for r in reordered.rows} == {
            r.invoice_no: r for r in normal.rows
        }

    def test_extra_column_passes_with_warning(self) -> None:
        st = parse("extra_column")
        assert WarnCode.EXTRA_COLUMNS in st.warnings
        assert len(st.rows) == 7

    def test_extra_column_does_not_disturb_row_values(self) -> None:
        assert net_map(parse("extra_column")) == net_map(parse("normal_7rows"))

    def test_required_columns_constant_matches_the_expected_header(self) -> None:
        parse("normal_7rows")
        assert set(REQUIRED_COLUMNS) == {
            "Date",
            "Invoice No.",
            "Claim Ref.",
            "Line ID",
            "Billed Amount",
            "Prior Paid",
            "Remaining",
            "Paid Amount",
        }
        assert len(REQUIRED_COLUMNS) == len(set(REQUIRED_COLUMNS)) == 8


class TestTotalsInvariant:
    def test_deposit_equals_sum_of_net(self) -> None:
        st = parse("normal_7rows")
        assert total_net(st) == st.deposit_amount == Decimal("665.60")

    def test_sum_mismatch_is_rejected(self) -> None:
        expect_reject("sum_mismatch", "V4")

    def test_footer_total_is_the_third_leg(self) -> None:
        html = load_fixture("normal_7rows").replace(
            "<td>$665.60</td>", "<td>$999.99</td>"
        )
        assert html.count("$999.99") == 1, "the fixture's footer cell format changed"
        with pytest.raises(ParseError) as exc:
            parse_statement(html, KNOWN_VENDORS)
        assert exc.value.code == "V4"

    def test_deposit_leg_is_checked(self) -> None:
        html = load_fixture("normal_7rows").replace(
            "USD $665.60", "USD $999.99"
        )
        assert html.count("USD $999.99") == 1, "the fixture's Deposit Total format changed"
        with pytest.raises(ParseError) as exc:
            parse_statement(html, KNOWN_VENDORS)
        assert exc.value.code == "V4"


class TestIdentifierInvariants:
    def test_identifier_without_hyphen_is_isolated_not_repaired(self) -> None:
        from remittance_reconciler.models import WarnCode as _W
        try:
            st = parse("bad_invoice_id")
        except ParseError as exc:
            assert exc.code == "V6"
            return
        bad = [r for r in st.rows if not r.identifier_ok]
        assert bad, "the malformed row was not isolated"
        assert _W.MALFORMED_ROW_IDENTIFIER in st.warnings
        assert not re.match(INVOICE_RE, bad[0].invoice_no)

    def test_every_identifier_matches_invoice_re(self) -> None:
        st = parse("normal_7rows")
        for r in st.rows:
            assert re.match(INVOICE_RE, r.invoice_no), r.invoice_no

    def test_duplicate_identifier_is_rejected(self) -> None:
        expect_reject("dup_invoice", "V7")

    def test_split_suffix_b01_and_b02_coexist(self) -> None:
        st = parse("split_suffix")
        assert len(st.rows) == 7
        b01 = row_of(st, "300248-B01")
        b02 = row_of(st, "300248-B02")
        assert b01.invoice_no != b02.invoice_no
        assert b01.invoice_no == "300248-B01"
        assert b02.invoice_no == "300248-B02"

    def test_split_suffix_rows_are_independent(self) -> None:
        st = parse("split_suffix")
        assert sorted(r.invoice_no for r in st.rows) == [
            "300094-B01",
            "300137-B01",
            "300248-B01",
            "300248-B02",
            "300583-B01",
            "300761-B01",
            "300925-B01",
        ]

    def test_identifiers_are_never_reduced_to_base_number(self) -> None:
        st = parse("split_suffix")
        identifiers = {r.invoice_no for r in st.rows}
        assert "300248" not in identifiers
        assert all("-B" in i for i in identifiers)


class TestSingletonInvariant:
    def test_quoted_reply_duplicating_payment_document_is_rejected(self) -> None:
        expect_reject("quoted_duplicate", "V8")

    def test_tail_vendor_number_does_not_trip_v8(self) -> None:
        st = parse("normal_7rows")
        assert "Payee ID reference" in load_fixture("normal_7rows")
        assert st.vendor_no == VENDOR_NO

    def test_column_headers_do_not_trip_the_deposit_singleton(self) -> None:
        st = parse("normal_7rows")
        assert st.deposit_amount == Decimal("665.60")


class TestVendorGate:
    def test_unknown_vendor_is_rejected(self) -> None:
        expect_reject("unknown_vendor", "V9")

    def test_known_vendor_list_is_a_hard_gate(self) -> None:
        expect_reject("normal_7rows", "V9", known_vendors=())

    def test_known_vendor_passes(self) -> None:
        st = parse("normal_7rows", known_vendors=("8100001", "9999999"))
        assert st.vendor_no == "8100001"


class TestAccountingEquation:
    def test_accounting_eq_violation_still_parses(self) -> None:
        st = parse("accounting_eq_violation")
        assert len(st.rows) == 7
        assert st.deposit_amount == Decimal("665.60")

    def test_accounting_eq_violation_records_warning(self) -> None:
        st = parse("accounting_eq_violation")
        assert WarnCode.ACCOUNTING_EQ in st.warnings

    def test_clean_statement_has_no_accounting_eq_warning(self) -> None:
        st = parse("normal_7rows")
        assert WarnCode.ACCOUNTING_EQ not in st.warnings


class TestAmountNotationsInStatement:
    def test_comma_amount_parses(self) -> None:
        st = parse("comma_amount")
        assert row_of(st, "300319-B01").net == Decimal("1234.56")
        assert st.deposit_amount == Decimal("1781.76")
        assert total_net(st) == st.deposit_amount

    def test_one_decimal_is_rejected(self) -> None:
        expect_reject("one_decimal", "AMOUNT_FORMAT")

    @pytest.mark.parametrize("name", NEGATIVE_NOTATION_FIXTURES)
    def test_credit_row_parses_as_negative(self, name: str) -> None:
        st = parse(name)
        credit = row_of(st, CREDIT_INVOICE)
        assert credit.net == CREDIT_NET
        assert credit.net < 0
        assert credit.gross == CREDIT_NET

    @pytest.mark.parametrize("name", NEGATIVE_NOTATION_FIXTURES)
    def test_credit_statement_satisfies_v4(self, name: str) -> None:
        st = parse(name)
        assert len(st.rows) == 8
        assert st.deposit_amount == CREDIT_DEPOSIT
        assert total_net(st) == CREDIT_DEPOSIT

    def test_all_negative_notations_agree(self) -> None:
        nets = {name: row_of(parse(name), CREDIT_INVOICE).net
                for name in NEGATIVE_NOTATION_FIXTURES}
        assert set(nets.values()) == {CREDIT_NET}, nets

    def test_positive_rows_survive_alongside_a_credit(self) -> None:
        st = parse("credit_memo")
        for invoice_no, expected in NORMAL_NETS.items():
            assert row_of(st, invoice_no).net == expected


def fp(rows, *, vendor_no=VENDOR_NO, payment_document_no=PAYMENT_DOCUMENT_NO,
       eft_date=EFT_DATE, deposit_amount=DEPOSIT_AMOUNT) -> str:
    return content_fingerprint(
        vendor_no, payment_document_no, eft_date, deposit_amount, rows
    )


def fp_rows() -> list[EftRow]:
    return [
        make_eft_row(invoice_no="300137-B01"),
        make_eft_row(invoice_no="300925-B01",
                     gross=Decimal("64.00"), net=Decimal("64.00")),
        make_eft_row(invoice_no="300248-B01"),
    ]


class TestContentFingerprint:
    def test_is_sha256_hex(self) -> None:
        got = fp(fp_rows())
        assert isinstance(got, str)
        assert re.fullmatch(r"[0-9a-f]{64}", got), got

    def test_same_content_same_fingerprint(self) -> None:
        assert fp(fp_rows()) == fp(fp_rows())

    def test_row_order_does_not_change_fingerprint(self) -> None:
        rows = fp_rows()
        assert fp(rows) == fp(list(reversed(rows)))
        assert fp(rows) == fp([rows[1], rows[2], rows[0]])

    def test_amount_scale_does_not_change_fingerprint(self) -> None:
        base = [make_eft_row(net=Decimal("64.00"), gross=Decimal("64.00"))]
        same = [make_eft_row(net=Decimal("64.0"), gross=Decimal("64.0"))]
        assert fp(base) == fp(same)
        assert fp(base, deposit_amount=Decimal("665.60")) == fp(
            base, deposit_amount=Decimal("665.6")
        )

    def test_changed_amount_changes_fingerprint(self) -> None:
        base = fp_rows()
        bumped = [make_eft_row(invoice_no="300137-B01", net=Decimal("64.00")),
                  *base[1:]]
        assert fp(base) != fp(bumped)

    def test_changed_identifier_changes_fingerprint(self) -> None:
        base = fp_rows()
        swapped = [*base[:2], make_eft_row(invoice_no="300248-B02")]
        assert fp(base) != fp(swapped)

    def test_changed_deposit_changes_fingerprint(self) -> None:
        rows = fp_rows()
        assert fp(rows) != fp(rows, deposit_amount=Decimal("729.60"))

    def test_changed_eft_date_changes_fingerprint(self) -> None:
        rows = fp_rows()
        assert fp(rows) != fp(rows, eft_date=date(2037, 5, 16))

    def test_statement_fingerprint_matches_recomputation(self) -> None:
        st = parse("normal_7rows")
        assert st.content_fingerprint == content_fingerprint(
            st.vendor_no,
            st.payment_document_no,
            st.eft_date,
            st.deposit_amount,
            st.rows,
        )

    def test_normal_and_v2_share_the_financial_key(self) -> None:
        normal = parse("normal_7rows")
        v2 = parse("statement_v2_8rows")
        assert (normal.vendor_no, normal.payment_document_no) == (
            v2.vendor_no,
            v2.payment_document_no,
        )
        assert len(normal.rows) == 7
        assert len(v2.rows) == 8
        assert v2.deposit_amount == Decimal("729.60")

    def test_normal_and_v2_have_different_fingerprints(self) -> None:
        assert (
            parse("normal_7rows").content_fingerprint
            != parse("statement_v2_8rows").content_fingerprint
        )

    def test_column_layout_does_not_change_fingerprint(self) -> None:
        normal = parse("normal_7rows").content_fingerprint
        assert parse("reordered_columns").content_fingerprint == normal
        assert parse("extra_column").content_fingerprint == normal
