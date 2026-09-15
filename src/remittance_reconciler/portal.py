"""Playwright adapter for the practice-management portal.

Invoice data comes from the sales-report CSV export. The page itself is used only for
navigation links, detail validation and the single write action. UI targeting is strict:
a semantic trigger, a positively confirmed open menu, and an exact ``menuitem`` match inside
that container (count must be exactly 1). No coordinates, positional selectors, keyboard
selection or partial text matches. Credentials come from the macOS Keychain; the session lives
in a dedicated persistent browser profile.

In this public version, selector strings and URLs are generic placeholders.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .portal_csv import parse_portal_amount
from .models import InvoiceDetail

log = logging.getLogger(__name__)
from .reconcile import canonical_invoice_key as normalize_key
from .reconcile import normalize_invoice_no as normalize

if TYPE_CHECKING:
    from playwright.sync_api import Locator

__all__ = [
    "PortalError",
    "IdentityMismatch",
    "MenuError",
    "PayTarget",
    "PortalSession",
    "DETAIL_PANE",
    "EXPORT_TRIGGER",
    "ACTION_TRIGGER_XPATH",
    "ACTION_MENU_XPATH",
    "INVOICE_HREF_RE",
    "RECORD_PAYMENT",
]


# DOM contract (generic placeholders in this public version).
DETAIL_PANE = "#invoice-detail-pane"

EXPORT_TRIGGER = "div.toolbar-button.export-menu.dropdown-toggle"

ACTION_MENU_XPATH = ".//ul[@role='menu'][.//*[normalize-space(text())='Record Payment']]"
ACTION_TRIGGER_XPATH = ACTION_MENU_XPATH + "/preceding-sibling::button[@aria-haspopup='true'][1]"

INVOICE_HREF_RE = re.compile(r"#invoices/(\d+)")

RECORD_PAYMENT = "Record Payment"


KEYCHAIN_SERVICE = "remittance-reconciler-portal"

LOGIN_SUCCESS_NAV = 'a[href="/app#reports"]'


CHALLENGE_IFRAME_SELECTORS: tuple[str, ...] = (
    'iframe[src*="recaptcha"][src*="bframe"]',
    'iframe[src*="hcaptcha"][src*="challenge"]',
    'iframe[src*="challenges.cloudflare.com"]',
)

CHALLENGE_CONTAINER_SELECTORS: tuple[str, ...] = (
    ".login-challenge",
    ".cf-turnstile",
    "#challenge-form",
)

CHALLENGE_INPUT_SELECTORS: tuple[str, ...] = (
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[name*="mfa" i]',
    'input[name*="two_factor" i]',
    'input[name*="twofactor" i]',
    'input[id*="otp" i]',
    'input[name*="verification" i]',
    'input[id*="verification_code" i]',
)

INTERACTIVE_CHALLENGE_TEXT: tuple[str, ...] = (
    "i'm not a robot",
    "i am not a robot",
    "verify you are human",
    "verify you're human",
    "are you a robot",
    "enter the code we sent",
    "confirm it's you",
)

LOGIN_FORM_SELECTOR = "input[type=password]"


_VISIBLE_BOX_JS = """(els) => els.map((el) => {
  const r = el.getBoundingClientRect();
  const cs = window.getComputedStyle(el);
  const fixed = cs.position === 'fixed';
  const attached = fixed || el.offsetParent !== null;
  const visible = attached
    && cs.display !== 'none'
    && cs.visibility !== 'hidden'
    && cs.opacity !== '0'
    && r.width > 0 && r.height > 0;
  return {visible: visible, w: Math.round(r.width), h: Math.round(r.height)};
}).filter((b) => b.visible)"""

AUTH_STATE_TIMEOUT_MS = 30000
AUTH_STATE_POLL_MS = 250


class PortalError(Exception):
    """Base class for portal automation errors."""


class LoginRequired(PortalError):
    """An interactive challenge needs a person. The run stops before any financial action."""
    def __init__(self, marker: str) -> None:
        super().__init__(f"interactive verification required: {marker}")
        self.marker = marker


class LoginError(PortalError):
    """Authentication failed without an interactive challenge (for example, a missing Keychain item)."""


def _headless_safe_user_agent(playwright) -> str | None:
    """The headless Chromium user agent with the ``Headless`` token removed, or ``None``."""
    probe = None
    try:
        probe = playwright.chromium.launch(headless=True)
        ua = probe.new_page().evaluate("navigator.userAgent")
    except Exception:  # noqa: BLE001
        return None
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:  # noqa: BLE001
                pass
    return ua.replace("HeadlessChrome", "Chrome") if "HeadlessChrome" in ua else None


def launch_persistent_session(playwright, profile_dir: "Path | str",
                              *, headless: bool = False,
                              base_url: str = "https://portal.example.com",
                              success_signals: tuple[str, ...] = ()):
    """Open the dedicated persistent profile (0700) and return ``(context, PortalSession)``."""
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)

    opts: dict = {
        "user_data_dir": str(profile),
        "headless": headless,
        "viewport": {"width": 1600, "height": 1400},
    }
    ua = _headless_safe_user_agent(playwright) if headless else None
    if ua:
        opts["user_agent"] = ua
        log.info("headless: presenting the standard Chrome UA to the portal")

    context = playwright.chromium.launch_persistent_context(**opts)
    page = context.pages[0] if context.pages else context.new_page()
    return context, PortalSession(page, base_url=base_url,
                                success_signals=tuple(success_signals))


def keychain_credentials(service: str = KEYCHAIN_SERVICE) -> tuple[str, str]:
    """Read the portal username and password from a macOS Keychain generic-password item."""
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-g"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise LoginError(
            f"no Keychain item for service {service!r}; the operator must add it"
        ) from exc

    acct = re.search(r'"acct"<blob>="([^"]*)"', out.stdout)
    pw = re.search(r'^password: "(.*)"$', out.stderr, re.M)
    if not acct or not pw:
        raise LoginError(f"Keychain item {service!r} is missing an account or password")
    return acct.group(1), pw.group(1)


class IdentityMismatch(PortalError):
    """The detail pane showed a different invoice than the one requested."""


class MenuError(PortalError):
    """The action menu could not be resolved to exactly one enabled target. No claim is created."""


class PayTarget:
    """A resolved, validated menu item, together with the invoice and amount it belongs to."""
    __slots__ = ("invoice_no", "locator", "menu", "expected_amount")

    def __init__(self, invoice_no: str, locator: "Locator | Any",
                 menu: "Locator | Any" = None, expected_amount: object = None) -> None:
        self.invoice_no = invoice_no
        self.locator = locator
        self.menu = menu
        self.expected_amount = expected_amount


class PortalSession:
    """Session-level operations against the portal."""
    def __init__(self, page: "Any", base_url: str = "https://portal.example.com",
                 success_signals: tuple[str, ...] = ()) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.success_signals = tuple(success_signals)


    def detail_pane(self) -> "Any":
        return self.page.locator(DETAIL_PANE)

    def action_menu(self) -> "Any":
        return self.detail_pane().locator("xpath=" + ACTION_MENU_XPATH)

    def action_trigger(self) -> "Any":
        return self.detail_pane().locator("xpath=" + ACTION_TRIGGER_XPATH)

    @staticmethod
    def _menu_is_open(trigger: "Any") -> bool:
        return (trigger.get_attribute("aria-expanded") or "").lower() == "true"

    def _visible_boxes(self, selector: str) -> list[dict[str, Any]]:
        try:
            boxes = self.page.locator(selector).evaluate_all(_VISIBLE_BOX_JS)
        except Exception:  # noqa: BLE001
            return []
        return list(boxes or [])

    def observe_login_state(self, timeout_ms: int = AUTH_STATE_TIMEOUT_MS,
                            poll_ms: int = AUTH_STATE_POLL_MS) -> str:
        """Classify the page as ``authenticated``, ``logged-out`` or ``unknown`` from visible evidence only."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while True:
            authed = bool(self._visible_boxes(LOGIN_SUCCESS_NAV))
            form = bool(self._visible_boxes(LOGIN_FORM_SELECTOR))
            if authed and not form:
                return "authenticated"
            if form and not authed:
                return "logged-out"
            if time.monotonic() >= deadline:
                if authed and form:
                    log.warning("both the portal admin nav and a login form are visible")
                return "unknown"
            try:
                self.page.wait_for_timeout(poll_ms)
            except Exception:  # noqa: BLE001
                return "unknown"

    def is_authenticated(self, timeout_ms: int = AUTH_STATE_TIMEOUT_MS) -> bool:
        return self.observe_login_state(timeout_ms=timeout_ms) == "authenticated"

    def detect_interactive_challenge(self) -> str | None:
        """Describe a *visible* challenge (iframe, container, one-time-code input or text), or return ``None``."""
        for sel in CHALLENGE_IFRAME_SELECTORS:
            for box in self._visible_boxes(sel):
                return f"visible challenge iframe {sel} ({box['w']}x{box['h']})"
        for sel in CHALLENGE_CONTAINER_SELECTORS:
            for box in self._visible_boxes(sel):
                return f"visible challenge container {sel} ({box['w']}x{box['h']})"
        for sel in CHALLENGE_INPUT_SELECTORS:
            for box in self._visible_boxes(sel):
                return f"visible verification input {sel} ({box['w']}x{box['h']})"
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            return None
        for phrase in INTERACTIVE_CHALLENGE_TEXT:
            if phrase in body:
                return f"text {phrase!r}"
        return None

    def ensure_authenticated(self, service: str = KEYCHAIN_SERVICE,
                             timeout_ms: int = 30000,
                             state_timeout_ms: int = AUTH_STATE_TIMEOUT_MS) -> str:
        """Reuse the saved session, or log in exactly once with Keychain credentials.

        Raises :class:`LoginRequired` whenever a challenge is visible or the login state is ambiguous.
        """
        try:
            self.page.goto(f"{self.base_url}/app", wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise LoginError(f"could not reach the portal admin URL: {exc}") from exc

        state = self.observe_login_state(timeout_ms=state_timeout_ms)
        if state == "authenticated":
            log.info("reusing the existing authenticated portal session")
            return "reused"

        marker = self.detect_interactive_challenge()
        if marker is not None:
            raise LoginRequired(marker)

        if state != "logged-out":
            raise LoginRequired(
                "could not positively determine the portal login state "
                f"({state}: neither the admin nav nor a login form became visible)"
            )

        log.info("the portal session has expired; attempting a single Keychain login")
        try:
            self.login(service=service, timeout_ms=timeout_ms)
        except LoginError:
            marker = self.detect_interactive_challenge()
            if marker is not None:
                raise LoginRequired(marker) from None
            raise

        marker = self.detect_interactive_challenge()
        if marker is not None:
            raise LoginRequired(marker)
        if self.observe_login_state(timeout_ms=state_timeout_ms) != "authenticated":
            raise LoginRequired(
                "the Keychain login completed but the authenticated portal admin "
                "context never became visible"
            )
        log.info("performed a fresh Keychain-backed portal login")
        return "logged-in"

    def login(self, service: str = KEYCHAIN_SERVICE, timeout_ms: int = 30000) -> None:
        """One native login attempt with Keychain credentials. No retries."""
        username, password = keychain_credentials(service)

        self.page.goto(f"{self.base_url}/app", wait_until="domcontentloaded")

        pw = self.page.locator("input[type=password]")
        if pw.count() == 0:
            self._assert_admin_context(timeout_ms)
            return

        user_field = self.page.locator(
            "input[type=email], input[name*='email' i], input[name*='user' i], "
            "input[type=text]"
        ).first
        if user_field.count() == 0:
            raise LoginError("login form present but no username field was found")

        user_field.fill(username)
        pw.first.fill(password)
        del password

        submit = self.page.get_by_role("button", name=re.compile(r"^\s*Sign In\s*$", re.I))
        if submit.count() == 0:
            submit = self.page.locator("button[type=submit]")
        if submit.count() != 1:
            raise LoginError(f"expected exactly one Sign In control, found {submit.count()}")
        submit.first.click()

        try:
            self.page.wait_for_selector(LOGIN_SUCCESS_NAV, timeout=timeout_ms)
        except Exception as exc:
            if self.page.locator("input[type=password]").count():
                raise LoginError("login rejected: still on the sign-in form") from exc
            raise LoginError("login did not reach the portal admin context") from exc

        self._assert_admin_context(timeout_ms)

    def _assert_admin_context(self, timeout_ms: int = 30000) -> None:
        self.page.wait_for_selector(LOGIN_SUCCESS_NAV, timeout=timeout_ms)
        host = re.sub(r"^https?://", "", self.base_url).rstrip("/")
        if host not in self.page.url:
            raise LoginError("authenticated session is not on the expected portal host")

    def export_outstanding_csv(
        self, start: date, end: date, *, all_invoice_states: bool = False,
        scratch_dir: "Path | None" = None,
    ) -> Path:
        """Export the sales report for a date window to an owner-only CSV file in ``scratch_dir``."""
        self._open_sales_report(start, end, all_invoice_states=all_invoice_states)

        trigger = self.page.locator(EXPORT_TRIGGER)
        if trigger.count() != 1:
            raise MenuError(f"export trigger resolved {trigger.count()} elements, expected 1")
        if not self._menu_is_open(trigger):
            trigger.click()
        self.page.wait_for_function(
            "sel => { const e = document.querySelector(sel);"
            " return e && e.getAttribute('aria-expanded') === 'true'; }",
            arg=EXPORT_TRIGGER, timeout=15000,
        )

        menu = trigger.locator("xpath=following-sibling::ul[@role='menu']")
        if menu.count() != 1:
            raise MenuError(f"export menu container resolved {menu.count()}, expected 1")

        target = menu.get_by_role("menuitem").filter(has_text=re.compile(r"^Export CSV$"))
        n = target.count()
        if n != 1:
            raise MenuError(f"'Export CSV' resolved {n} targets, expected exactly 1")

        scratch = Path(scratch_dir or (Path.cwd() / "scratch"))
        scratch.mkdir(parents=True, exist_ok=True)

        ctx = self.page.context
        before = set(ctx.pages)
        target.click()
        preview = None
        for _ in range(120):
            self.page.wait_for_timeout(500)
            new = [p for p in ctx.pages if p not in before and "/downloads/" in p.url]
            if new:
                preview = new[0]
                break
        if preview is None:
            raise PortalError("export preview tab did not appear")

        link = preview.get_by_role("link", name=re.compile(r"Download File"))
        if link.count() != 1:
            preview.close()
            raise PortalError(f"download control resolved {link.count()}, expected 1")
        try:
            with preview.expect_download(timeout=180000) as dl:
                link.first.click()
            path = scratch / f"sales_{start:%Y%m%d}_{end:%Y%m%d}.csv"
            dl.value.save_as(str(path))
            path.chmod(0o600)
        finally:
            preview.close()
        return path


    def _open_sales_report(self, start: date, end: date, *, all_invoice_states: bool = False) -> None:
        """Load the sales report with all locations, the invoice-state filter and the date range applied."""
        target = f"{self.base_url}/app#reports/invoices"
        if self.page.url.rstrip("/") == target.rstrip("/"):
            self.page.reload(wait_until="domcontentloaded")
        else:
            self.page.goto(target, wait_until="domcontentloaded")
        self.page.wait_for_selector('[data-testid="report-filters"]', timeout=30000)

        self._select_all_locations()
        self._select_invoice_state("All Statuses" if all_invoice_states else "Outstanding Only")
        self._set_date_range(start, end)
        self._wait_for_settled_count()

    def _select_all_locations(self) -> None:
        bar = self.page.locator('[data-testid="report-filters"]')
        trig = bar.locator('[data-testid^="filter-toggle-"]').first
        if (trig.get_attribute("data-testid") or "").endswith("All Locations"):
            return
        trig.click()
        self.page.wait_for_selector(
            '[data-testid="filter-options-menu"]:visible', timeout=15000
        )
        self.page.locator(
            '[data-testid="filter-options-menu"]:visible [data-testid="filter-select-all"]'
        ).first.click()
        self.page.keyboard.press("Escape")

    def _select_invoice_state(self, label: str) -> None:
        bar = self.page.locator('[data-testid="report-filters"]')
        trig = bar.locator(
            '[data-testid="filter-toggle-All Statuses"], '
            '[data-testid="filter-toggle-Outstanding Only"], '
            '[data-testid="filter-toggle-Paid"], [data-testid="filter-toggle-Unpaid"]'
        ).first
        if (trig.get_attribute("data-testid") or "") == f"filter-toggle-{label}":
            return
        trig.click()
        self.page.wait_for_selector(
            '[data-testid="filter-options-menu"]:visible', timeout=15000
        )

        if label == "All Statuses":
            menu = self.page.locator('[data-testid="filter-options-menu"]:visible').first
            menu.locator('[data-testid="filter-select-all"]').first.click()
        else:
            opt = self.page.get_by_role("button", name=f"Show only {label}", exact=True)
            opt.first.wait_for(state="visible", timeout=15000)
            opt.first.click()
        self.page.keyboard.press("Escape")

    @staticmethod
    def _day_aria(d: date) -> str:
        n = d.day
        suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"Choose {d:%A}, {d:%B} {n}{suffix}, {d:%Y}"

    def _goto_month(self, d: date, max_steps: int = 36) -> None:
        want = (d.year, d.month)
        for _ in range(max_steps):
            shown = [t.strip() for t in self.page.locator(
                ".react-datepicker__current-month").all_inner_texts() if t.strip()]
            if not shown:
                return
            months = []
            for t in shown:
                try:
                    m = datetime.strptime(t, "%B %Y")
                except ValueError:
                    continue
                months.append((m.year, m.month))
            if not months or want in months:
                return
            direction = "previous" if want < min(months) else "next"
            nav = self.page.locator(f".react-datepicker__navigation--{direction}")
            if nav.count() == 0 or not nav.first.is_visible():
                return
            nav.first.click()
            self.page.wait_for_timeout(200)

    def _set_date_range(self, start: date, end: date) -> None:
        """Pick the start and end dates deterministically, navigating months as needed."""
        btn = self.page.locator('[data-testid="date-range-button"]')

        def is_open() -> bool:
            return self.page.locator(".react-datepicker").count() > 0

        for _ in range(3):
            if is_open():
                break
            btn.click()
            self.page.wait_for_timeout(600)
        if not is_open():
            raise PortalError("date range picker did not open")

        try:
            self.page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:  # noqa: BLE001
            pass

        for d in (start, end):
            target = self.page.locator(
                f'.react-datepicker__day[aria-label="{self._day_aria(d)}"]'
                ':not(.react-datepicker__day--outside-month)'
            )
            if target.count() == 0:
                self._goto_month(d)
                target = self.page.locator(
                    f'.react-datepicker__day[aria-label="{self._day_aria(d)}"]'
                    ':not(.react-datepicker__day--outside-month)'
                )
            if target.count() == 0:
                raise PortalError(f"date picker: could not find {d.isoformat()}")
            cell = target.first
            cell.wait_for(state="visible", timeout=15000)
            try:
                cell.scroll_into_view_if_needed(timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            cell.click()
            self.page.wait_for_timeout(300)

        self.page.keyboard.press("Escape")

    def _wait_for_settled_count(self, stable_reads: int = 3, interval_ms: int = 700) -> int:
        """Wait until the on-screen invoice count is identical across consecutive reads."""
        seen: list[int] = []
        for _ in range(60):
            n = self.visible_invoice_count()
            rendered = self.page.locator("table tbody tr").count()
            consistent = n is not None and ((n == 0) == (rendered == 0))
            if consistent:
                seen.append(n)
                if len(seen) >= stable_reads and len(set(seen[-stable_reads:])) == 1:
                    return seen[-1]
            else:
                seen.clear()
            self.page.wait_for_timeout(interval_ms)
        raise PortalError("on-screen invoice count never settled")

    def visible_invoice_count(self) -> int | None:
        """The report's total invoice count, or ``None`` if it cannot be read unambiguously."""
        vals = self.page.evaluate(
            """() => [...document.querySelectorAll('*')]
                 .filter(e => e.children.length === 0 && /Showing results/i.test(e.textContent||''))
                 .map(e => e.textContent.trim())"""
        )
        nums = set()
        for v in vals:
            m = re.search(r"of\s+([\d,]+)", v)
            if m:
                nums.add(int(m.group(1).replace(",", "")))
        return nums.pop() if len(nums) == 1 else None

    def collect_hrefs(self, targets: set[str], start: date | None = None,
                      end: date | None = None, max_pages: int = 200) -> dict[str, str]:
        """Collect detail links for the target identifiers, paging through the report as needed."""
        found: dict[str, str] = {}
        want = {normalize_key(x): normalize(x) for x in targets}
        remaining = set(want)
        if not remaining:
            return found

        for _ in range(max_pages):
            pairs = self.page.evaluate(
                """() => [...document.querySelectorAll('table tbody a')]
                     .map(a => [ (a.innerText||'').trim(), a.getAttribute('href')||'' ])"""
            )
            for text, href in pairs:
                if not INVOICE_HREF_RE.search(href):
                    continue
                ident = normalize(text)
                key = normalize_key(ident)
                if key in remaining:
                    found[want[key]] = href
                    remaining.discard(key)
            if not remaining:
                break
            more = self.page.get_by_role("button", name=re.compile(r"^Load More$"))
            if more.count() == 0 or not more.first.is_enabled():
                break
            before = len(pairs)
            more.first.click()
            try:
                self.page.wait_for_function(
                    "n => document.querySelectorAll('table tbody a').length > n",
                    arg=before, timeout=15000,
                )
            except Exception:
                break
        return found

    def open_invoice(self, href: str, expected_invoice_no: str | None = None,
                     timeout_ms: int = 20000) -> InvoiceDetail:
        """Open a detail pane, wait for the expected identifier, and read the total and status badges."""
        expected = expected_invoice_no and normalize(expected_invoice_no)
        self.page.goto(f"{self.base_url}/app{href}" if href.startswith("#") else href,
                       wait_until="domcontentloaded")
        pane = self.detail_pane()
        h1 = pane.locator("h1")
        if expected:
            try:
                h1.filter(
                    has_text=re.compile(rf"^Invoice {re.escape(expected)}$", re.IGNORECASE)
                ).wait_for(state="visible", timeout=timeout_ms)
            except Exception as exc:
                shown = ""
                try:
                    shown = normalize(h1.first.inner_text().removeprefix("Invoice "))
                except Exception:
                    pass
                raise IdentityMismatch(
                    f"detail pane did not show {expected!r} (showing {shown!r})"
                ) from exc
        else:
            h1.first.wait_for(state="visible", timeout=timeout_ms)

        ident = normalize(h1.first.inner_text().removeprefix("Invoice "))

        total_row = pane.locator("div.row").filter(has_text=re.compile(r"^Total")).first
        total_txt = total_row.locator(".col-xs-3.align-right").inner_text()

        status_row = pane.locator("div.row").filter(has_text=re.compile(r"^Status")).first
        badges = status_row.locator("span.label")
        labels = [badges.nth(i).inner_text().strip() for i in range(badges.count())]

        pay_states = {"Paid", "Unpaid", "Partially Paid"}
        payment = next((x for x in labels if x in pay_states), "")
        submission = next((x for x in labels if x not in pay_states), "")

        m = INVOICE_HREF_RE.search(href) or INVOICE_HREF_RE.search(self.page.url)
        return InvoiceDetail(
            invoice_no=ident,
            total=parse_portal_amount(total_txt),
            payment_status=payment,
            submission_status=submission,
            portal_invoice_id=m.group(1) if m else "",
        )

    def resolve_record_payment(self, detail: InvoiceDetail) -> PayTarget:
        """Resolve the exact, visible, enabled write action inside the open menu, or raise :class:`MenuError`."""
        pane = self.detail_pane()
        trigger = pane.locator("xpath=" + ACTION_TRIGGER_XPATH)

        n_trig = trigger.count()
        if n_trig == 0:
            raise MenuError("action dropdown trigger not found")
        if n_trig > 1:
            raise MenuError(f"action dropdown trigger is ambiguous: {n_trig} matches")

        if (trigger.get_attribute("aria-expanded") or "").lower() != "true":
            trigger.click()
        try:
            self.page.wait_for_function(
                """el => el && el.getAttribute('aria-expanded') === 'true'""",
                arg=trigger.element_handle(), timeout=10000,
            )
        except Exception as exc:
            raise MenuError("action menu did not open") from exc

        menu = pane.locator("xpath=" + ACTION_MENU_XPATH)
        n_menu = menu.count()
        if n_menu == 0:
            raise MenuError("opened action menu container not found")
        visible = [i for i in range(n_menu) if menu.nth(i).is_visible()] if n_menu > 1 else None
        if n_menu > 1:
            if len(visible or []) != 1:
                raise MenuError(f"ambiguous open menu scope: {n_menu} containers")
            menu = menu.nth(visible[0])
        if not menu.first.is_visible():
            raise MenuError("action menu container is not visible")

        target = menu.get_by_role("menuitem", name=RECORD_PAYMENT, exact=True)
        n = target.count()
        if n == 0:
            raise MenuError(f"exact {RECORD_PAYMENT!r} target not present in the open menu")
        if n > 1:
            raise MenuError(f"exact {RECORD_PAYMENT!r} resolved {n} targets, expected 1")

        el = target.first
        try:
            if not el.is_visible():
                raise MenuError(f"{RECORD_PAYMENT!r} target is not visible")
            if not el.is_enabled():
                raise MenuError(f"{RECORD_PAYMENT!r} target is disabled")
        except MenuError:
            raise
        except Exception as exc:
            raise MenuError(f"{RECORD_PAYMENT!r} target went stale during validation") from exc

        return PayTarget(detail.invoice_no, locator=el, menu=menu,
                         expected_amount=detail.total)

    def click_record_payment(self, target: PayTarget) -> str:
        """Click once and return the verified success signal (empty string if unconfirmed)."""
        target.locator.click()
        return self._observe_success_signal(
            target.invoice_no, expected_amount=target.expected_amount
        )

    _OBSERVE_JS = r"""
      const OBSERVE = ([expected, amount]) => {
        const pane = document.querySelector('#invoice-detail-pane');
        if (!pane) return null;
        const norm = t => (t || '').replace(/\s+/g, ' ').trim();
        const bare = t => (t || '').replace(/,/g, '');
        const h1 = pane.querySelector('h1');
        const statusRow = [...pane.querySelectorAll('div.row')]
          .map(e => norm(e.innerText)).find(t => /^Status /.test(t));
        const status = statusRow ? statusRow.replace(/^Status /, '') : null;
        const parts = status ? status.split('/').map(x => x.trim()) : [];
        const rows = [...pane.querySelectorAll('table tr')].map(e => norm(e.innerText));
        const want = bare('$' + amount);
        const matching = rows.filter(t => /Receipt #\d+/.test(t) && bare(t).includes(want));
        const labels = [...pane.querySelectorAll('span.label')].map(e => norm(e.innerText));
        const obs = {
          invoice:     h1 ? norm(h1.innerText) : null,
          status:      status,
          payment:     parts.length === 2 ? parts[0] : null,
          submission:  parts.length === 2 ? parts[1] : null,
          labels:      labels,
          paymentRow:  matching[0] || null,
          paymentRowCount: matching.length,
          anyPaymentRow:   rows.some(t => /Receipt #\d+/.test(t)),
        };
        obs.ready = (
          obs.invoice === 'Invoice ' + expected &&
          obs.payment === 'Paid' &&
          obs.submission === 'Settled' &&
          labels.includes('Paid') && labels.includes('Settled') &&
          obs.paymentRowCount >= 1
        );
        return obs;
      };
    """

    _SUCCESS_PROBE = _OBSERVE_JS + """
      (args) => { const o = OBSERVE(args); return (o && o.ready) ? o : null; }
    """

    _DIAG_PROBE = _OBSERVE_JS + """
      (args) => OBSERVE(args)
    """

    _REQUIRED_PAYMENT_STATUS = "Paid"
    _REQUIRED_SUBMISSION_STATUS = "Settled"

    def _read_success_state(self, expected_invoice_no: str, expected_amount: str,
                            timeout_ms: int) -> "dict | None":
        arg = [normalize(expected_invoice_no), expected_amount]
        try:
            handle = self.page.wait_for_function(
                self._SUCCESS_PROBE, arg=arg, timeout=timeout_ms
            )
            return handle.json_value()
        except Exception:
            try:
                diag = self.page.evaluate(self._DIAG_PROBE, arg)
                log.warning("G-5: success state not reached in %dms; observed %r",
                            timeout_ms, diag)
            except Exception:
                log.warning("G-5: success state not reached in %dms;"
                            " diagnostic read also failed", timeout_ms)
            return None

    def _judge(self, obs: "dict | None", expected_invoice_no: str) -> str:
        """Accept an observation only if every element of the success contract holds."""
        if not obs:
            return ""
        want_h1 = f"Invoice {normalize(expected_invoice_no)}"
        if obs.get("invoice") != want_h1:
            log.warning("G-5: pane shows %r, expected %r", obs.get("invoice"), want_h1)
            return ""
        if obs.get("payment") != self._REQUIRED_PAYMENT_STATUS:
            log.warning("G-5: payment status is %r, expected 'Paid'", obs.get("payment"))
            return ""
        if obs.get("submission") != self._REQUIRED_SUBMISSION_STATUS:
            log.warning("G-5: submission status is %r, expected 'Settled'",
                        obs.get("submission"))
            return ""
        labels = set(obs.get("labels") or ())
        if not {self._REQUIRED_PAYMENT_STATUS, self._REQUIRED_SUBMISSION_STATUS} <= labels:
            log.warning("G-5: status labels %r do not corroborate Paid/Settled",
                        sorted(labels))
            return ""
        n = int(obs.get("paymentRowCount") or 0)
        if n == 0:
            log.warning("G-5: no receipt row for the expected amount"
                        " (any payment row present: %s)", bool(obs.get("anyPaymentRow")))
            return ""
        if n > 1:
            log.error("G-5: %d payments match the expected amount — possible DUPLICATE", n)
            return ""
        status = obs.get("status") or ""
        if status not in self.success_signals:
            log.warning("G-5: status %r is not in post_click_success_signals", status)
            return ""
        return status

    def _observe_success_signal(self, expected_invoice_no: str = "",
                                expected_amount: object = None,
                                timeout_ms: int = 15000) -> str:
        """G-5: confirm a write from persisted state, never from UI transitions.

        Requires the expected identifier, Paid/Settled status, exactly one payment row for the expected
        amount, and the same state after re-fetching the invoice from the server.
        """
        if not self.success_signals:
            return ""
        if not expected_invoice_no or expected_amount is None:
            return ""
        amount = str(expected_amount)

        first = self._judge(
            self._read_success_state(expected_invoice_no, amount, timeout_ms),
            expected_invoice_no,
        )
        if not first:
            return ""

        url = self.page.url
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            want = f"Invoice {normalize(expected_invoice_no)}"
            self.page.locator(f"{DETAIL_PANE} h1").filter(
                has_text=re.compile(rf"^{re.escape(want)}$", re.IGNORECASE)
            ).wait_for(state="visible", timeout=max(timeout_ms, 30000))
        except Exception:
            log.warning("G-5: could not re-open %s from the server;"
                        " cannot confirm persistence", expected_invoice_no)
            return ""
        second = self._judge(
            self._read_success_state(expected_invoice_no, amount,
                                     max(timeout_ms, 30000)),
            expected_invoice_no,
        )
        if second != first:
            log.warning("G-5: state did not survive reload (%r -> %r)", first, second)
            return ""
        return second
