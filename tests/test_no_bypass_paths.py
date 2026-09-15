"""Structural tests: nothing can claim or click outside the single write primitive."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "remittance_reconciler"
TOOLS = Path(__file__).resolve().parent.parent / "tools"
PY_FILES = sorted(SRC.glob("*.py"))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_docstrings(node: ast.AST) -> ast.AST:
    for n in ast.walk(node):
        if not isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            continue
        body = getattr(n, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            del body[0]
    return node


def _code_only(src: str) -> str:
    return ast.unparse(_strip_docstrings(ast.parse(src)))


def _function_code(src: str, name: str) -> str:
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.unparse(_strip_docstrings(fn))


def test_only_database_module_writes_to_write_claims() -> None:
    pattern = re.compile(r"(INSERT\s+INTO|UPDATE)\s+write_claims", re.I)
    offenders = [
        p.name
        for p in list(PY_FILES) + sorted(TOOLS.glob("*.py"))
        if p.name != "database.py" and pattern.search(_code_only(_read(p)))
    ]
    assert offenders == [], f"modules writing write_claims directly: {offenders}"


def test_insert_claim_signature_requires_provenance() -> None:
    tree = ast.parse(_read(SRC / "database.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "insert_claim"
    )
    args = [a.arg for a in fn.args.args]
    assert args == ["self", "work", "prov"], f"insert_claim signature changed: {args}"


def test_only_one_implementation_can_cross_the_click_boundary() -> None:
    callers = set()
    for p in PY_FILES:
        if p.name == "portal.py":
            continue
        tree = ast.parse(_read(p))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "click_record_payment"):
                    callers.add(f"{p.name}:{node.name}")
    assert callers == {"writepath.py:execute_one_authorized_work"}, callers


def test_the_primitive_verifies_before_claiming() -> None:
    src = _read(SRC / "writepath.py")
    body = _function_code(src, "execute_one_authorized_work")

    i_valid = body.index("validate_authorized_work(portal, cfg, work, prov)")
    i_resolve = body.index("resolve_record_payment(")
    i_claim = body.index("db.insert_claim(work, prov)")
    i_click = body.index("click_record_payment(")
    assert i_valid < i_resolve < i_claim < i_click, "the write-boundary order changed"


def test_validation_never_writes_to_the_database() -> None:
    body = _function_code(_read(SRC / "writepath.py"), "validate_authorized_work")
    for banned in ("insert_claim", "update_work", "set_claim_outcome",
                   "set_statement_state", "insert_work_rows", "bump_attempt",
                   "conn.execute", "commit"):
        assert banned not in body, f"validate_smoke_target calls {banned}"
    tree = ast.parse(_read(SRC / "writepath.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "validate_authorized_work")
    assert "db" not in [a.arg for a in fn.args.args], (
        "validate_authorized_work receives db, which would make writes possible"
    )


def test_execute_statement_verifies_provenance_before_claiming() -> None:
    src = _read(SRC / "main.py")
    body = _function_code(src, "execute_statement")

    i_elig = body.index("write_eligibility(")
    i_exec = body.index("execute_one_authorized_work(")
    assert i_elig < i_exec, "calls the financial primitive before provenance verification"
    assert "insert_claim" not in body, "a second claim implementation appeared"
    assert "click_record_payment" not in body, "a second click implementation appeared"

    elig = _function_code(src, "write_eligibility")
    i_prov = elig.index("verify_provenance(db, work)")
    i_state = elig.index("work.state is not WorkState.DETAIL_VALIDATED")
    assert i_prov < i_state, "the provenance check moved after the state check"


def test_candidate_selection_never_touches_portal() -> None:
    body = _function_code(_read(SRC / "database.py"), "authorized_write_candidates")
    for forbidden in ("portal", "Portal", "playwright", "page.", "csv"):
        assert forbidden not in body, f"the candidate query references {forbidden!r}"
    assert "invoice_work" in body and "emails" in body


def test_candidate_tool_is_read_only() -> None:
    src = _code_only(_read(TOOLS / "select_candidate.py"))
    for forbidden in ("insert_claim", "click_record_payment", "resolve_record_payment",
                      "PortalSession", "sync_playwright"):
        assert forbidden not in src, f"the candidate lookup tool uses {forbidden!r}"


@pytest.mark.parametrize("col", ["raw_eft_invoice_no", "eft_net_provenance"])
def test_provenance_columns_are_absent_from_the_update_whitelist(col: str) -> None:
    from remittance_reconciler.database import Database

    assert col not in Database._WORK_COLUMNS


def test_no_module_backfills_provenance_from_invoice_no() -> None:
    pattern = re.compile(
        r"raw_eft_invoice_no\s*=\s*invoice_no|SET\s+raw_eft_invoice_no\s*=\s*invoice_no",
        re.I,
    )
    for p in list(PY_FILES) + sorted(TOOLS.glob("*.py")):
        assert not pattern.search(_code_only(_read(p))), \
            f"{p.name}: fabricates provenance"


def test_smoke_runner_requires_two_explicit_flags() -> None:
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    runner = root / "tools" / "run_smoke.py"
    assert runner.is_file()

    def run(*flags):
        return subprocess.run(
            [sys.executable, str(runner), "--work-id", "1", *flags],
            capture_output=True, text=True, cwd=root, timeout=120,
        )

    r = run()
    assert r.returncode == 2 and "rehearse" in (r.stdout + r.stderr)

    r = run("--arm")
    assert r.returncode == 2 and "i-authorize-one-financial-click" in r.stdout

    r = run("--arm", "--i-authorize-one-financial-click")
    assert r.returncode == 2 and "--cap" in r.stdout

    r = run("--a", "--i")
    assert r.returncode == 2
    out = r.stdout + r.stderr
    assert "REHEARSAL" not in out and "ARMED" not in out

    r = run("--rehearse", "--arm")
    assert r.returncode == 2 and "not allowed with" in (r.stdout + r.stderr)


def test_smoke_runner_never_writes_the_config_file() -> None:
    src = _code_only(_read(TOOLS / "run_smoke.py"))
    for forbidden in ("write_text", "safe_dump", "yaml.dump", "open("):
        assert forbidden not in src, f"the smoke runner uses {forbidden!r}"
    assert "replace(" in src, "config must only be overridden in memory"


def test_daily_entrypoint_cannot_perform_the_smoke() -> None:
    src = _function_code(_read(SRC / "main.py"), "main")
    assert "portal=None" in src, "the daily entrypoint creates a portal session"


def test_smoke_runner_cross_check_is_operator_supplied() -> None:
    src = _code_only(_read(TOOLS / "run_smoke.py"))
    assert "smoke_expect_invoice_no=work.invoice_no" not in src, (
        "the cross-check value is derived from the target row itself"
    )
    assert "args.expect_invoice" in src


def test_smoke_runner_requires_the_operator_to_type_the_amount() -> None:
    src = _code_only(_read(TOOLS / "run_smoke.py"))
    assert "args.cap" in src
    assert "cap != total" in src, "does not require cap to equal the targets' total exactly"


def test_smoke_runner_always_runs_the_isolation_report() -> None:
    src = _read(TOOLS / "run_smoke.py")
    assert "except BaseException" in src, "no broad exception handler"
    i_try = src.index("lock = acquire_lock")
    i_diff = src.index("changed = {i for i in")
    i_except = src.index("except BaseException")
    assert i_try < i_except < i_diff, "the isolation diff does not follow the exception path"


def test_unverified_click_does_not_exit_zero() -> None:
    src = _code_only(_read(TOOLS / "run_smoke.py")).replace('"', "'")
    assert "'UNKNOWN_OUTCOME' in outcomes" in src
    assert "return 3" in src
    assert "outcomes <= {'REHEARSAL_OK', 'CONFIRMED'}" in src


def test_armed_path_does_not_export_the_window_twice() -> None:
    src = _read(TOOLS / "run_smoke.py")
    body = _function_code(src, "main")
    i_exec = body.index("execute_smoke_batch(")
    i_val = body.index("validate_smoke_target(")
    assert i_exec < i_val, (
        "the armed branch calls validate_smoke_target first (double export)"
    )
