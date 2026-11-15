"""Sandboxed analysis: allowed analyses produce correct results; escapes and abuse are refused.

Correct results are checked against values computed here directly from the
data, so a sandbox that returned plausible-looking wrong numbers would fail.
The escape list covers the standard CPython sandbox breakouts; each must be
refused by static validation before any code runs.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from salesops_agent.data import CLOSED_STAGES, CRMData
from salesops_agent.sandbox import SandboxConfig, SandboxViolation, approved_data, run_analysis, spawn_worker, validate_code

ROOT = Path(__file__).resolve().parents[1]
CFG = SandboxConfig.load(ROOT / "config" / "sandbox.yaml")
CRM = CRMData.load(ROOT / "data")

# ------------------------------------------------------------------ allowed analyses


def test_pipeline_by_stage_matches_data() -> None:
    code = (
        "totals = {}\n"
        "for o in opportunities:\n"
        "    if o['stage'] not in ('Closed Won', 'Closed Lost'):\n"
        "        totals[o['stage']] = totals.get(o['stage'], 0) + o['amount_usd']\n"
        "result = totals\n"
    )
    expected: dict[str, int] = {}
    for o in CRM.opportunities:
        if o["stage"] not in CLOSED_STAGES:
            expected[o["stage"]] = expected.get(o["stage"], 0) + o["amount_usd"]
    out = run_analysis(code, CRM, CFG)
    assert out == {"status": "ok", "result": expected}


def test_count_open_opportunities() -> None:
    out = run_analysis("result = len([o for o in opportunities if o['stage'] not in ('Closed Won','Closed Lost')])", CRM, CFG)
    assert out["result"] == sum(o["stage"] not in CLOSED_STAGES for o in CRM.opportunities)


def test_average_deal_size_and_ticket_severity() -> None:
    code = (
        "won = [o['amount_usd'] for o in opportunities if o['stage'] == 'Closed Won']\n"
        "sev = {}\n"
        "for t in support_tickets:\n"
        "    sev[t['severity']] = sev.get(t['severity'], 0) + 1\n"
        "result = {'avg_won': round(sum(won) / len(won), 2), 'severity': sev}\n"
    )
    won = [o["amount_usd"] for o in CRM.opportunities if o["stage"] == "Closed Won"]
    out = run_analysis(code, CRM, CFG)
    assert out["result"]["avg_won"] == round(sum(won) / len(won), 2)
    assert sum(out["result"]["severity"].values()) == len(CRM.support_tickets)


def test_health_vs_renewal_handles_missing_health_scores() -> None:
    code = (
        "rows = [a for a in accounts if a['health_score'] is not None and a['health_score'] < 50]\n"
        "result = sorted([[a['name'], a['contract_end']] for a in rows], key=lambda r: r[1])\n"
    )
    out = run_analysis(code, CRM, CFG)
    assert out["status"] == "ok"
    assert len(out["result"]) == sum(1 for a in CRM.accounts if a["health_score"] is not None and a["health_score"] < 50)

# ---------------------------------------------------------------- refused statically

ESCAPES = [
    "import os\nresult = os.listdir('.')",
    "from subprocess import run",
    "result = __import__('os').environ",
    "result = open('data/internal_hr_data.json').read()",
    "result = eval('1+1')",
    "result = exec('x=1')",
    "result = ().__class__.__base__.__subclasses__()",
    "result = [].__class__",
    "result = (x for x in [1]).gi_frame",
    "result = '{0.__class__}'.format(1)",
    "result = getattr(accounts, 'copy')",
    "result = globals()",
    "def f():\n    return 1\nresult = f()",
    "class A:\n    pass",
    "with open('x') as f:\n    pass",
    "try:\n    x = 1\nexcept Exception:\n    pass",
    "while True:\n    pass",
    "result = internal_hr_data",
    "result = hr",
    "global accounts",
    "del accounts",
    "result = b'bytes'",
    "x = " + "1+" * 3000 + "1",  # over the code-size limit
]


@pytest.mark.parametrize("code", ESCAPES, ids=range(len(ESCAPES)))
def test_escape_is_refused_before_running(code: str) -> None:
    with pytest.raises(SandboxViolation):
        validate_code(code, CFG)
    assert run_analysis(code, CRM, CFG)["status"] == "blocked"


def test_restricted_data_is_never_passed_in() -> None:
    data = approved_data(CRM, CFG)
    assert set(data) == set(CFG.datasets)
    assert not {"hr", "financial", "internal_hr_data", "internal_financial_data"} & set(data)
    assert all("email" not in c for c in data["contacts"])  # projected out

# --------------------------------------------------------------------- runtime limits


def test_cpu_bound_loop_times_out() -> None:
    cfg = dataclasses.replace(CFG, timeout_seconds=1.0, cpu_seconds=1)
    out = run_analysis("total = 0\nfor i in range(1000000000000):\n    total += i\nresult = total", CRM, cfg)
    assert out["status"] == "error" and ("timed out" in out["error"] or "resource limit" in out["error"])


def test_oversized_result_is_refused() -> None:
    out = run_analysis("result = [a for a in accounts for x in range(50)]", CRM, CFG)
    assert out["status"] == "error" and "limit" in out["error"]


def test_missing_result_variable_is_reported() -> None:
    out = run_analysis("total = 1", CRM, CFG)
    assert out["status"] == "error" and "result" in out["error"]


def test_second_layer_child_sees_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth: even if static validation were bypassed, the child has no API key to read."""
    monkeypatch.setenv("VOCAREUM_API_KEY", "voc-secret-should-not-leak")
    request = {
        "code": "result = sorted(__import__('os').environ)",  # bypasses validate_code on purpose
        "data": {},
        "builtins": ["sorted", "__import__"],
        "limits": {"cpu_seconds": 5, "memory_mb": 512, "max_output_chars": 20000},
    }
    out = spawn_worker(request, 5)
    assert out["status"] == "ok"
    assert "VOCAREUM_API_KEY" not in out["result"]


def test_second_layer_cannot_write_files(tmp_path: Path) -> None:
    """RLIMIT_FSIZE=0: a write that slipped past validation still cannot create file content."""
    target = tmp_path / "out.txt"
    request = {
        "code": f"f = open({str(target)!r}, 'w')\nf.write('x' * 10)\nf.close()\nresult = 'wrote'",
        "data": {}, "builtins": ["open"],
        "limits": {"cpu_seconds": 5, "memory_mb": 512, "max_output_chars": 20000},
    }
    out = spawn_worker(request, 5)
    assert out["status"] == "error" or not target.exists() or target.stat().st_size == 0


def test_exponent_operator_is_refused() -> None:
    """`**` is off the allowlist: 10**10**10 is a one-line denial of service."""
    with pytest.raises(SandboxViolation, match="Pow"):
        validate_code("result = 10**10**10", CFG)


# ------------------------------------------------------------------ actionable refusals

#: The exact code a live run submitted three times before abandoning the tool.
JOINED_ON_ONE_LINE = (
    "result = {'Discovery': 0, 'Qualification': 0, 'Proposal': 0, 'Negotiation': 0, 'Stalled': 0, 'total': 0}; "
    "for opportunity in opportunities: if opportunity['stage'] in result: "
    "result[opportunity['stage']] += opportunity['amount_usd']; result['total'] += opportunity['amount_usd'];"
)


def test_a_block_joined_with_semicolons_gets_an_actionable_message() -> None:
    from salesops_agent.sandbox import ONE_STATEMENT_PER_LINE

    with pytest.raises(SandboxViolation) as caught:
        validate_code(JOINED_ON_ONE_LINE, CFG)
    message = str(caught.value)
    assert message.startswith("syntax error: invalid syntax (line 1): result = {")
    assert ONE_STATEMENT_PER_LINE in message


def test_the_suggested_fix_runs_and_answers_correctly() -> None:
    """The example in the message must itself pass validation and give the right answer."""
    from salesops_agent.sandbox import ONE_STATEMENT_PER_LINE

    example = ONE_STATEMENT_PER_LINE.split("For example:\n", 1)[1]
    out = run_analysis(example, CRM, CFG)
    expected: dict[str, int] = {}
    for t in CRM.support_tickets:
        if t["status"] != "resolved":
            expected[t["severity"]] = expected.get(t["severity"], 0) + 1
    assert out["status"] == "ok" and out["result"] == expected


def test_other_syntax_errors_quote_the_line_without_the_joined_block_hint() -> None:
    """Negative control: the hint is for the one-line block mistake only."""
    from salesops_agent.sandbox import ONE_STATEMENT_PER_LINE

    with pytest.raises(SandboxViolation) as caught:
        validate_code("totals = (1 +\nresult = totals", CFG)
    assert "syntax error" in str(caught.value) and ONE_STATEMENT_PER_LINE not in str(caught.value)


def test_the_same_analysis_written_one_statement_per_line_passes() -> None:
    code = (
        "result = {'Discovery': 0, 'Qualification': 0, 'Proposal': 0, 'Negotiation': 0, 'Stalled': 0, 'total': 0}\n"
        "for opportunity in opportunities:\n"
        "    if opportunity['stage'] in result:\n"
        "        result[opportunity['stage']] += opportunity['amount_usd']\n"
        "        result['total'] += opportunity['amount_usd']\n"
    )
    out = run_analysis(code, CRM, CFG)
    assert out["status"] == "ok" and out["result"]["total"] == 26_525_000
