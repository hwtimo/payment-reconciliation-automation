"""Post-click success-signal contract (G-5): only positive, server-persisted evidence confirms a write."""

from __future__ import annotations

import pytest

from remittance_reconciler.portal import PortalSession

SIGNAL = "Paid / Settled"
INVOICE = "290007-B01"


class FakePage:
    def __init__(self, *, h1: str | None, status: str | None,
                 payment_history: list[str], labels: list[str] | None = None,
                 raise_timeout: bool = False, reload_error: bool = False,
                 after_reload: dict | None = None,
                 pane_never_renders: bool = False):
        self.h1, self.status = h1, status
        self.payment_history = payment_history
        self._labels = labels
        self.raise_timeout = raise_timeout
        self.reload_error = reload_error
        self.after_reload = after_reload
        self.pane_never_renders = pane_never_renders
        self.calls = 0
        self.reloads = 0
        self.pane_waits = 0


    def _state(self):
        if self.reloads and self.after_reload is not None:
            return self.after_reload
        return dict(h1=self.h1, status=self.status,
                    payment_history=self.payment_history, labels=self._labels)

    def _observe(self, arg) -> dict:
        expected, amount = arg
        st = self._state()
        status = st["status"]
        parts = [x.strip() for x in status.split("/")] if status else []
        labels = st["labels"]
        if labels is None:
            labels = list(parts)
        want = f"${amount}".replace(",", "")
        matching = [t for t in st["payment_history"]
                    if "Receipt #" in t and want in t.replace(",", "")]
        obs = {
            "invoice": st["h1"],
            "status": status,
            "payment": parts[0] if len(parts) == 2 else None,
            "submission": parts[1] if len(parts) == 2 else None,
            "labels": labels,
            "paymentRow": matching[0] if matching else None,
            "paymentRowCount": len(matching),
            "anyPaymentRow": any("Receipt #" in t for t in st["payment_history"]),
        }
        obs["ready"] = (
            obs["invoice"] == f"Invoice {expected}"
            and obs["payment"] == "Paid"
            and obs["submission"] == "Settled"
            and "Paid" in labels and "Settled" in labels
            and obs["paymentRowCount"] >= 1
        )
        return obs

    def wait_for_function(self, _js, arg=None, timeout=None):
        self.calls += 1
        if self.raise_timeout:
            raise TimeoutError("probe timed out")
        obs = self._observe(arg)
        if not obs["ready"]:
            raise TimeoutError("probe never became ready")
        return _Handle(obs)

    def evaluate(self, _js, arg=None):
        return self._observe(arg)


    url = "https://x/app#invoices/1"

    def goto(self, _url, **_kw):
        self.reloads += 1
        if self.reload_error:
            raise RuntimeError("navigation failed")

    def locator(self, _sel):
        page = self

        class _L:
            def filter(self, **_k): return self
            def wait_for(self, **_k):
                page.pane_waits += 1
                if page.pane_never_renders:
                    raise TimeoutError("detail pane did not re-render")
        return _L()


class _Handle:
    def __init__(self, value): self._v = value
    def json_value(self): return self._v


def session(page, signals=(SIGNAL,)) -> PortalSession:
    return PortalSession(page, success_signals=tuple(signals))


AMOUNT = "131.65"

PRE = dict(h1=f"Invoice {INVOICE}", status="Unpaid / Submitted",
           payment_history=["Posted Receipt Details Amount",
                            "No Receipts"])
POST = dict(h1=f"Invoice {INVOICE}", status="Paid / Settled",
            payment_history=["Posted Receipt Details Amount",
                             "Jul 18, 2037 Jul 18, 2037 Receipt #700177 ACME Direct, Check $131.65"])


def observe(page, invoice=INVOICE, amount=AMOUNT, signals=(SIGNAL,)) -> str:
    return session(page, signals)._observe_success_signal(invoice, expected_amount=amount)


def test_observed_post_click_state_is_a_success_signal() -> None:
    assert observe(FakePage(**POST)) == SIGNAL


def test_observed_pre_click_state_is_not_a_success_signal() -> None:
    assert observe(FakePage(**PRE)) == ""


def test_timeout_is_not_a_success_signal() -> None:
    assert observe(FakePage(**POST, raise_timeout=True)) == ""


def test_status_paid_without_payment_history_is_not_enough() -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status="Paid / Settled",
                    payment_history=["No Receipts"])
    assert observe(page) == ""


@pytest.mark.parametrize("status", [
    "Partially Paid / Settled",
    "Unpaid / Settled",
    "Paid / Submitted",
    "Paid",
    "",
])
def test_contradictory_status_is_never_success(status: str) -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status=status,
                    payment_history=POST["payment_history"])
    assert observe(page) == ""


def test_signal_read_from_a_different_invoice_is_rejected() -> None:
    page = FakePage(h1="Invoice 290024-B01", status="Paid / Settled",
                    payment_history=POST["payment_history"])
    assert observe(page) == ""


def test_empty_success_signals_config_can_never_confirm() -> None:
    page = FakePage(**POST)
    assert observe(page, signals=()) == ""
    assert page.calls == 0, "when observation is disabled the browser must not be touched"


def test_missing_expected_invoice_no_never_observes() -> None:
    page = FakePage(**POST)
    assert observe(page, invoice="") == ""
    assert page.calls == 0


@pytest.mark.parametrize("page_kw,label", [
    (dict(**POST, raise_timeout=True), "timeout"),
    (dict(h1=f"Invoice {INVOICE}", status="Unpaid / Submitted",
          payment_history=["No Receipts"]), "signal absent"),
    (dict(h1=f"Invoice {INVOICE}", status="Partially Paid / Settled",
          payment_history=POST["payment_history"]), "contradictory signal"),
])
def test_unconfirmed_signal_never_triggers_a_second_observation(page_kw, label) -> None:
    page = FakePage(**page_kw)
    assert observe(page) == ""
    assert page.calls == 1, f"{label}: observed {page.calls} times"


def test_record_payment_remains_available_after_payment() -> None:
    observed_menu_after_payment = [
        "View Invoice", "Mark as Submitted", "Mark as Draft", "Reopen Claim",
        "Reject Claim", "Record Payment", "Record Payment Plan...",
    ]
    assert observed_menu_after_payment.count("Record Payment") == 1

    from remittance_reconciler.main import _g18_reject_reason
    from remittance_reconciler.models import InvoiceDetail, WorkRow
    from decimal import Decimal as D

    work = WorkRow(id=1, statement_id=1, invoice_no=INVOICE, eft_net=D("131.65"))
    paid = InvoiceDetail(invoice_no=INVOICE, total=D("131.65"),
                         payment_status="Paid", submission_status="Settled",
                         portal_invoice_id="4300001")
    assert _g18_reject_reason(work, paid) == "ALREADY_PAID"


def test_success_requires_a_server_backed_refresh() -> None:
    page = FakePage(**POST)
    assert observe(page) == SIGNAL
    assert page.reloads == 1, "did not confirm server state via a reload"
    assert page.calls == 2, "must observe once before and once after the reload"


def test_state_that_does_not_survive_reload_is_not_success() -> None:
    page = FakePage(**POST, after_reload=dict(
        h1=f"Invoice {INVOICE}", status="Unpaid / Submitted",
        payment_history=["No Receipts"], labels=None))
    assert observe(page) == ""


def test_reload_failure_is_unknown_not_success() -> None:
    page = FakePage(**POST, reload_error=True)
    assert observe(page) == ""


def test_payment_history_must_match_the_expected_amount() -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status="Paid / Settled",
                    payment_history=["Jul 18, 2037 Receipt #700177 ACME Direct $99.99"])
    assert observe(page) == ""


def test_preexisting_payment_row_does_not_satisfy_the_contract() -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status="Paid / Settled",
                    payment_history=["Jun 17, 2037 Receipt #111111 ACME Direct $50.00"])
    assert observe(page) == ""
    ok = FakePage(h1=f"Invoice {INVOICE}", status="Paid / Settled",
                  payment_history=["Jun 17, 2037 Receipt #111111 ACME Direct $50.00",
                                   "Jul 18, 2037 Receipt #700177 ACME Direct $131.65"])
    assert observe(ok) == SIGNAL


def test_thousands_separator_in_the_amount_still_matches() -> None:
    page = FakePage(h1="Invoice 290028-B01", status="Paid / Settled",
                    payment_history=["Jul 18, 2037 Receipt #9 ACME Direct $1,234.56"])
    assert observe(page, invoice="290028-B01", amount="1234.56") == SIGNAL


def test_reordered_status_is_rejected() -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status="Settled / Paid",
                    payment_history=POST["payment_history"])
    assert observe(page) == ""


def test_status_labels_must_corroborate() -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status="Paid / Settled",
                    payment_history=POST["payment_history"],
                    labels=["Unpaid", "Submitted"])
    assert observe(page) == ""


def test_missing_expected_amount_never_observes() -> None:
    page = FakePage(**POST)
    assert session(page)._observe_success_signal(INVOICE, expected_amount=None) == ""
    assert page.calls == 0 and page.reloads == 0


def test_probe_returns_null_until_ready() -> None:
    probe = PortalSession._SUCCESS_PROBE
    assert "o.ready" in probe and "null" in probe, "the wait probe has no ready gate"
    diag = PortalSession._DIAG_PROBE
    assert "OBSERVE(args)" in diag


def test_state_not_yet_updated_is_not_success() -> None:
    page = FakePage(**PRE)
    assert observe(page) == ""


def test_timeout_diagnostic_read_is_never_judged_as_success() -> None:
    page = FakePage(**POST, raise_timeout=True)
    assert observe(page) == ""


def test_two_matching_payments_is_never_success() -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status="Paid / Settled",
                    payment_history=[
                        "Jul 18, 2037 Receipt #700177 ACME Direct $131.65",
                        "Jul 18, 2037 Receipt #700178 ACME Direct $131.65",
                    ])
    assert observe(page) == ""


def test_exactly_one_matching_payment_is_success() -> None:
    page = FakePage(h1=f"Invoice {INVOICE}", status="Paid / Settled",
                    payment_history=[
                        "Jun 17, 2037 Receipt #111111 ACME Direct $50.00",
                        "Jul 18, 2037 Receipt #700177 ACME Direct $131.65",
                    ])
    assert observe(page) == SIGNAL


def test_server_refetch_waits_for_the_expected_invoice_to_reappear() -> None:
    page = FakePage(**POST)
    assert observe(page) == SIGNAL
    assert page.reloads == 1
    assert page.pane_waits == 1, "did not wait for the expected invoice after re-fetching"


def test_invoice_that_never_reappears_is_unknown_not_success() -> None:
    page = FakePage(**POST, pane_never_renders=True)
    assert observe(page) == ""


def test_confirmation_refetches_from_the_server_not_a_reload() -> None:
    import ast
    import inspect

    from remittance_reconciler.portal import PortalSession

    src = ast.unparse(ast.parse(
        inspect.getsource(PortalSession._observe_success_signal).lstrip()))
    assert ".reload(" not in src, "still confirms server state with reload"
    assert "goto(" in src, "does not re-fetch from the server"
