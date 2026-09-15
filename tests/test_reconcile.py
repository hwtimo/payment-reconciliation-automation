"""Classification rules, terminal states, date windows and statement guards."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import (
    DEPOSIT_AMOUNT,
    EFT_DATE,
    PORTAL_INVOICE_DATE,
    ROW_DATE,
    make_eft_row,
    make_portal,
)

from remittance_reconciler.models import Verdict, WarnCode, WorkState
from remittance_reconciler.reconcile import (
    PAYER_PREFIX,
    classify,
    is_terminal,
    normalize_invoice_no,
    sales_report_window,
    statement_amount_guard,
    statement_is_complete,
)

SOFT_WARNINGS = frozenset(
    {
        WarnCode.OUTSTANDING_NONZERO,
        WarnCode.GROSS_VS_TOTAL,
        WarnCode.PREVPAID_VS_COLLECTED,
    }
)


def test_normalize_strips_whitespace_and_leading_hash():
    assert normalize_invoice_no(" #300248-B01 ") == "300248-B01"


@pytest.mark.parametrize(
    "raw",
    [
        "300248-B01",
        " 300248-B01",
        "300248-B01 ",
        "#300248-B01",
        " #300248-B01 ",
        "\t#300248-B01\n",
    ],
)
def test_normalize_all_spellings_collapse_to_same_identifier(raw):
    assert normalize_invoice_no(raw) == "300248-B01"


def test_normalize_never_strips_suffix():
    b01 = normalize_invoice_no(" #300248-B01 ")
    b02 = normalize_invoice_no(" #300248-B02 ")

    assert b01 != b02
    assert b01 == "300248-B01"
    assert b02 == "300248-B02"

    assert b01.endswith("-B01")
    assert b02.endswith("-B02")
    assert b01 != "300248"
    assert b02 != "300248"


def test_normalize_does_not_uppercase():
    assert normalize_invoice_no("300248-b01") == "300248-b01"


def test_normalize_does_not_zero_pad():
    assert normalize_invoice_no("300248-B1") == "300248-B1"
    assert normalize_invoice_no("860-B01") == "860-B01"


def test_normalize_keeps_hyphen_and_inner_characters():
    result = normalize_invoice_no("300248-B01")
    assert "-" in result
    assert result != "300248B01"


def test_normalize_is_idempotent():
    for raw in (" #300248-B01 ", "300248-B02", "\t300248-B1\n"):
        once = normalize_invoice_no(raw)
        assert normalize_invoice_no(once) == once


def test_payer_prefix_constant():
    assert PAYER_PREFIX == "ACME"

    row = make_eft_row()
    assert classify(row, make_portal(payer=f"{PAYER_PREFIX} Claims")).verdict is Verdict.APPROVE_OK
    assert classify(row, make_portal(payer="Other Insurance Co")).verdict is Verdict.PAYER_MISMATCH


def test_classify_all_four_hard_gates_pass_approves():
    row = make_eft_row()
    portal = make_portal()

    assert classify(row, portal).verdict is Verdict.APPROVE_OK


def test_classify_clean_match_has_no_warnings():
    decision = classify(make_eft_row(), make_portal())

    assert decision.verdict is Verdict.APPROVE_OK
    assert decision.warnings == ()


def test_classify_is_deterministic():
    row, portal = make_eft_row(), make_portal()
    assert classify(row, portal) == classify(row, portal)


def test_classify_identifier_mismatch_never_approves():
    row = make_eft_row(invoice_no="300248-B01")
    portal = make_portal(invoice_no="290006-B01")

    assert classify(row, portal).verdict is not Verdict.APPROVE_OK


def test_classify_suffix_mismatch_never_approves():
    row = make_eft_row(invoice_no="300248-B01")
    portal = make_portal(invoice_no="300248-B02")

    assert classify(row, portal).verdict is not Verdict.APPROVE_OK


def test_classify_tolerates_hash_and_whitespace_in_identifier():
    row = make_eft_row(invoice_no="300248-B01")
    portal = make_portal(invoice_no=" #300248-B01 ")

    assert classify(row, portal).verdict is Verdict.APPROVE_OK


def test_classify_amount_mismatch():
    row = make_eft_row(net=Decimal("118.40"))
    portal = make_portal(balance=Decimal("64.00"))

    assert classify(row, portal).verdict is Verdict.AMOUNT_MISMATCH


def test_classify_one_cent_difference_is_a_mismatch():
    row = make_eft_row(net=Decimal("118.40"))
    portal = make_portal(balance=Decimal("118.41"))

    assert classify(row, portal).verdict is Verdict.AMOUNT_MISMATCH


def test_regression_partial_payment_must_not_approve():
    row = make_eft_row(net=Decimal("118.40"))
    portal = make_portal(
        total=Decimal("118.40"),
        collected=Decimal("50.00"),
        balance=Decimal("68.40"),
    )

    assert classify(row, portal).verdict is Verdict.AMOUNT_MISMATCH


def test_regression_short_payment_must_not_approve():
    row = make_eft_row(
        gross=Decimal("118.40"),
        prev_paid=Decimal("0.00"),
        outstanding=Decimal("54.40"),
        net=Decimal("64.00"),
    )
    portal = make_portal(
        balance=Decimal("118.40"),
        total=Decimal("118.40"),
        collected=Decimal("0.00"),
    )

    assert classify(row, portal).verdict is Verdict.AMOUNT_MISMATCH


def test_regression_outstanding_nonzero_must_still_approve():
    row = make_eft_row(
        gross=Decimal("118.40"),
        prev_paid=Decimal("0.00"),
        outstanding=Decimal("54.40"),
        net=Decimal("64.00"),
    )
    portal = make_portal(
        balance=Decimal("64.00"),
        total=Decimal("118.40"),
        collected=Decimal("0.00"),
    )

    decision = classify(row, portal)

    assert decision.verdict is Verdict.APPROVE_OK
    assert WarnCode.OUTSTANDING_NONZERO in decision.warnings


@pytest.mark.parametrize("payer", ["ACME", "ACME Claims", "ACME Plan A", "ACME Plan B"])
def test_classify_payer_names_pass(payer):
    assert classify(make_eft_row(), make_portal(payer=payer)).verdict is Verdict.APPROVE_OK


def test_classify_other_payer_is_rejected():
    row = make_eft_row()
    portal = make_portal(payer="Other Insurance Co")

    assert classify(row, portal).verdict is Verdict.PAYER_MISMATCH


@pytest.mark.parametrize(
    "payer",
    [
        "Other Insurance Co",
        "Other Benefits Co",
        "Other Life Co",
        "Other Health Co",
        "",
    ],
)
def test_classify_other_insurers_all_rejected(payer):
    assert classify(make_eft_row(), make_portal(payer=payer)).verdict is Verdict.PAYER_MISMATCH


def test_classify_payer_must_be_prefix_not_substring():
    portal = make_portal(payer="Non-ACME Direct Bill")

    assert classify(make_eft_row(), portal).verdict is Verdict.PAYER_MISMATCH


def test_classify_portal_none_is_not_found():
    assert classify(make_eft_row(), None).verdict is Verdict.NOT_FOUND


@pytest.mark.parametrize("net", [Decimal("-118.40"), Decimal("0.00")])
def test_classify_non_positive_net_is_credit_memo(net):
    row = make_eft_row(net=net)
    portal = make_portal(balance=net)

    assert classify(row, portal).verdict is Verdict.CREDIT_MEMO


def test_classify_negative_gross_is_credit_memo():
    row = make_eft_row(gross=Decimal("-118.40"), net=Decimal("118.40"))
    portal = make_portal(balance=Decimal("118.40"))

    assert classify(row, portal).verdict is Verdict.CREDIT_MEMO


def test_classify_credit_memo_never_approves():
    row = make_eft_row(net=Decimal("-118.40"), gross=Decimal("-118.40"))
    portal = make_portal(balance=Decimal("-118.40"), payer="ACME Claims")

    assert classify(row, portal).verdict is not Verdict.APPROVE_OK


def test_classify_decimal_scale_difference_is_still_equal():
    row = make_eft_row(net=Decimal("118.40"))
    portal = make_portal(balance=Decimal("118.4"))

    assert classify(row, portal).verdict is Verdict.APPROVE_OK


def test_classify_is_not_float_based():
    row = make_eft_row(net=Decimal("118.40"))
    portal = make_portal(balance=Decimal("118.400000000000001"))

    assert classify(row, portal).verdict is Verdict.AMOUNT_MISMATCH


def test_soft_warning_gross_vs_total_does_not_block():
    row = make_eft_row(gross=Decimal("118.40"), net=Decimal("118.40"))
    portal = make_portal(total=Decimal("90.00"), balance=Decimal("118.40"))

    decision = classify(row, portal)

    assert decision.verdict is Verdict.APPROVE_OK
    assert WarnCode.GROSS_VS_TOTAL in decision.warnings


def test_soft_warning_prevpaid_vs_collected_does_not_block():
    row = make_eft_row(prev_paid=Decimal("25.00"), net=Decimal("118.40"))
    portal = make_portal(collected=Decimal("0.00"), balance=Decimal("118.40"))

    decision = classify(row, portal)

    assert decision.verdict is Verdict.APPROVE_OK
    assert WarnCode.PREVPAID_VS_COLLECTED in decision.warnings


def test_all_soft_warnings_at_once_still_approves():
    row = make_eft_row(
        gross=Decimal("118.40"),
        prev_paid=Decimal("25.00"),
        outstanding=Decimal("54.40"),
        net=Decimal("64.00"),
    )
    portal = make_portal(
        balance=Decimal("64.00"),
        total=Decimal("90.00"),
        collected=Decimal("0.00"),
    )

    decision = classify(row, portal)

    assert decision.verdict is Verdict.APPROVE_OK
    assert SOFT_WARNINGS.issubset(set(decision.warnings))


def test_soft_warnings_are_never_blocking_codes():
    row = make_eft_row(outstanding=Decimal("54.40"))
    decision = classify(row, make_portal())

    assert decision.verdict is Verdict.APPROVE_OK
    assert set(decision.warnings) <= SOFT_WARNINGS


@pytest.mark.parametrize(
    "state",
    [WorkState.CONFIRMED, WorkState.NO_ACTION, WorkState.MANUAL_REVIEW],
)
def test_terminal_states(state):
    assert is_terminal(state) is True


@pytest.mark.parametrize("state", [WorkState.RECONCILED, WorkState.PENDING_WRITE])
def test_non_terminal_states(state):
    assert is_terminal(state) is False


def test_is_terminal_covers_every_work_state():
    for state in WorkState:
        assert isinstance(is_terminal(state), bool)


def test_regression_confirmed_plus_manual_review_is_complete():
    states = [WorkState.CONFIRMED] * 18 + [WorkState.MANUAL_REVIEW] * 2

    assert statement_is_complete(states) is True


def test_regression_one_reconciled_row_blocks_completion():
    states = [WorkState.CONFIRMED] * 19 + [WorkState.RECONCILED]

    assert statement_is_complete(states) is False


def test_pending_write_blocks_completion():
    states = [WorkState.CONFIRMED] * 5 + [WorkState.PENDING_WRITE]

    assert statement_is_complete(states) is False


@pytest.mark.parametrize(
    "states, expected",
    [
        ([WorkState.CONFIRMED] * 7, True),
        ([WorkState.NO_ACTION] * 3, True),
        ([WorkState.MANUAL_REVIEW] * 4, True),
        ([WorkState.CONFIRMED, WorkState.NO_ACTION, WorkState.MANUAL_REVIEW], True),
        ([WorkState.RECONCILED], False),
        ([WorkState.PENDING_WRITE], False),
        ([WorkState.MANUAL_REVIEW, WorkState.PENDING_WRITE], False),
    ],
)
def test_statement_is_complete_table(states, expected):
    assert statement_is_complete(states) is expected


def test_statement_is_complete_agrees_with_is_terminal():
    for state in WorkState:
        assert statement_is_complete([state]) is is_terminal(state)


def test_statement_is_complete_accepts_an_iterator():
    done = [WorkState.CONFIRMED] * 18 + [WorkState.MANUAL_REVIEW] * 2
    assert statement_is_complete(s for s in done) is True

    not_done = [WorkState.CONFIRMED] * 19 + [WorkState.RECONCILED]
    assert statement_is_complete(s for s in not_done) is False


def _seven_statement_rows():
    return [
        make_eft_row(invoice_no=f"31540{i}-B01", net=net, gross=net)
        for i, net in enumerate(
            [Decimal("118.40")] * 4 + [Decimal("64.00")] * 3
        )
    ]


def test_window_single_row_date_no_buffer():
    rows = _seven_statement_rows()

    assert sales_report_window(rows, 0) == (date(2037, 5, 7), date(2037, 5, 7))


def test_window_default_buffer_is_zero():
    rows = _seven_statement_rows()

    assert sales_report_window(rows) == sales_report_window(rows, 0)


def test_window_with_buffer_three():
    rows = _seven_statement_rows()

    assert sales_report_window(rows, 3) == (date(2037, 5, 4), date(2037, 5, 10))


def test_regression_window_uses_row_date_not_eft_date():
    rows = _seven_statement_rows()
    start, end = sales_report_window(rows, 0)

    wrong = EFT_DATE - timedelta(days=1)

    assert (start, end) == (PORTAL_INVOICE_DATE, PORTAL_INVOICE_DATE)
    assert start == ROW_DATE - timedelta(days=1)
    assert start != wrong
    assert end != wrong
    assert not (start <= wrong <= end)


def test_window_spans_min_and_max_row_date():
    rows = [
        make_eft_row(invoice_no="300248-B01", row_date=date(2037, 5, 8)),
        make_eft_row(invoice_no="290001-B01", row_date=date(2037, 5, 10)),
        make_eft_row(invoice_no="290002-B01", row_date=date(2037, 5, 9)),
    ]

    assert sales_report_window(rows, 0) == (date(2037, 5, 7), date(2037, 5, 9))


def test_window_spans_min_and_max_row_date_with_buffer():
    rows = [
        make_eft_row(invoice_no="300248-B01", row_date=date(2037, 5, 8)),
        make_eft_row(invoice_no="290001-B01", row_date=date(2037, 5, 10)),
    ]

    assert sales_report_window(rows, 3) == (date(2037, 5, 4), date(2037, 5, 12))


def test_window_crosses_month_boundary():
    rows = [make_eft_row(row_date=date(2037, 5, 1))]

    assert sales_report_window(rows, 0) == (date(2037, 4, 30), date(2037, 4, 30))
    assert sales_report_window(rows, 3) == (date(2037, 4, 27), date(2037, 5, 3))


def test_window_crosses_year_boundary():
    rows = [make_eft_row(row_date=date(2038, 1, 1))]

    assert sales_report_window(rows, 0) == (date(2037, 12, 31), date(2037, 12, 31))


def test_window_start_never_after_end():
    for buffer_days in (0, 1, 3, 10):
        start, end = sales_report_window(_seven_statement_rows(), buffer_days)
        assert start <= end


def test_window_ignores_row_order():
    rows = [
        make_eft_row(invoice_no="300248-B01", row_date=date(2037, 5, 10)),
        make_eft_row(invoice_no="290001-B01", row_date=date(2037, 5, 8)),
    ]

    assert sales_report_window(rows, 0) == sales_report_window(list(reversed(rows)), 0)


def test_guard_sample_statement_sums_exactly_to_deposit():
    approved = [Decimal("118.40")] * 4 + [Decimal("64.00")] * 3

    assert sum(approved) == DEPOSIT_AMOUNT
    assert statement_amount_guard(approved, DEPOSIT_AMOUNT) is True


def test_guard_under_deposit_passes():
    approved = [Decimal("118.40"), Decimal("64.00")]

    assert statement_amount_guard(approved, DEPOSIT_AMOUNT) is True


def test_guard_over_deposit_fails():
    approved = [Decimal("118.40")] * 4 + [Decimal("64.00")] * 3 + [Decimal("0.01")]

    assert statement_amount_guard(approved, DEPOSIT_AMOUNT) is False


def test_guard_single_amount_over_deposit_fails():
    assert statement_amount_guard([Decimal("700.00")], Decimal("665.60")) is False


def test_guard_empty_approved_passes():
    assert statement_amount_guard([], DEPOSIT_AMOUNT) is True


def test_regression_guard_is_not_float_based():
    approved = [Decimal("0.10"), Decimal("0.20")]

    assert statement_amount_guard(approved, Decimal("0.30")) is True


def test_regression_guard_is_per_statement_not_per_run():
    assert statement_amount_guard([Decimal("700.00")], Decimal("700.00")) is True
    assert statement_amount_guard([Decimal("900.00")], Decimal("900.00")) is True
    assert statement_amount_guard([Decimal("500.00")], Decimal("500.00")) is True

    run_total = [Decimal("700.00"), Decimal("900.00"), Decimal("500.00")]
    assert statement_amount_guard(run_total, Decimal("900.00")) is False


def test_v1_partial_payment_is_manual_review_even_when_amounts_match():
    row = make_eft_row(net=Decimal("50.00"))
    portal = make_portal(balance=Decimal("50.00"), collected=Decimal("47.00"))

    decision = classify(row, portal)

    assert decision.verdict is Verdict.MANUAL_REVIEW
    assert "partially paid" in decision.reason


def test_v1_partial_payment_does_not_mask_a_genuine_amount_mismatch():
    row = make_eft_row(net=Decimal("118.40"))
    portal = make_portal(
        total=Decimal("118.40"), collected=Decimal("50.00"), balance=Decimal("68.40")
    )

    assert classify(row, portal).verdict is Verdict.AMOUNT_MISMATCH


def test_v1_collected_zero_still_approves():
    row = make_eft_row(net=Decimal("118.40"))
    portal = make_portal(balance=Decimal("118.40"), collected=Decimal("0.00"))

    assert classify(row, portal).verdict is Verdict.APPROVE_OK


def test_v1_sibling_patient_invoice_never_considered():
    row = make_eft_row(invoice_no="300248-B01", net=Decimal("118.40"))
    acme = make_portal(
        invoice_no="300248-B01", balance=Decimal("118.40"), collected=Decimal("0.00")
    )
    _sibling = make_portal(
        invoice_no="300248-S01", balance=Decimal("40.00"), collected=Decimal("10.00"),
        payer="Patient",
    )

    assert classify(row, acme).verdict is Verdict.APPROVE_OK
