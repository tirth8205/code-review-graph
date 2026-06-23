"""Regression coverage for review-context advisory contract signals."""

from code_review_graph.review_projection import project_for_review


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base_config(repo) -> None:
    _write(
        repo / "pyproject.toml",
        """
[tool.code-review-graph.contract_shapes]
sql_path_globs = ["db/**/*.sql"]
wrapper_path_globs = ["src/data/**/*.py"]
test_path_globs = ["db/tests/**/*.sql"]

[tool.code-review-graph.http_contracts]
route_path_globs = ["src/http/**/*.py"]
test_path_globs = ["tests/http/**/*.py"]

[tool.code-review-graph.review_policies.service_integration]
service_path_globs = ["src/svc/**/*.py"]
test_path_globs = ["tests/**/*.py"]
db_test_markers = ["pytest.mark.db"]
real_dependency_patterns = ["(qb2_[A-Za-z_]+)"]

[[tool.code-review-graph.synthetic_edges.rules]]
kind = "CONTRACT_WRAPPER"
source_path_globs = ["db/**/*.sql"]
source_name_regex = "(qb2_[A-Za-z_]+)"
target_path_globs = ["src/data/**/*.py"]
target_content_regex = "{source_name}"
reason = "sproc wrapper naming convention"
""",
    )


def test_sql_wrapper_and_tsqlt_shape_mismatch_is_advisory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _base_config(repo)
    _write(
        repo / "db/qb2_InsertServiceCharge.sql",
        """
create procedure dbo.qb2_InsertServiceCharge
as
begin
    select ServiceChargeID, MovementType, Amount, CurrencyCode;
end
""",
    )
    _write(
        repo / "src/data/service_charge.py",
        """
from typing import TypedDict

SPROC = "qb2_InsertServiceCharge"

class InsertServiceChargeRow(TypedDict):
    ServiceChargeID: int
    Amount: int
    CurrencyCode: str
    Status: str
    CreatedAt: str
    ErrorMessage: str
    MovementType: str
""",
    )
    _write(
        repo / "db/tests/test_service_charge.sql",
        """
exec dbo.qb2_InsertServiceCharge;
create table #actualResults (
    ServiceChargeID int,
    Amount int,
    CurrencyCode varchar(10),
    Status varchar(10),
    CreatedAt datetime,
    ErrorMessage varchar(200),
    MovementType varchar(20)
);
""",
    )

    result = project_for_review(
        _analysis(),
        repo_root=repo,
        changed_files=["db/qb2_InsertServiceCharge.sql"],
        base="main",
        max_tokens=None,
    )

    mismatch = result["contract_shape_mismatches"][0]
    assert mismatch["contract"] == "qb2_InsertServiceCharge"
    assert mismatch["first_differing_index"] == 1
    assert mismatch["sql_columns"][1] == "MovementType"
    assert mismatch["wrapper_columns"][1] == "Amount"
    assert result["synthetic_edges"][0]["kind"] == "CONTRACT_WRAPPER"


def test_service_db_policy_gap_ignores_mock_only_tests(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _base_config(repo)
    _write(
        repo / "src/svc/service_charge.py",
        """
from src.data.service_charge import qb2_UpdateServiceCharge

def update_service_charge():
    return qb2_UpdateServiceCharge()
""",
    )
    _write(
        repo / "tests/test_service_charge.py",
        """
def test_update_service_charge_uses_mock(mocker):
    mocker.patch("src.data.service_charge.qb2_UpdateServiceCharge")
""",
    )

    result = project_for_review(
        _analysis(file_path="src/svc/service_charge.py", name="update_service_charge"),
        repo_root=repo,
        changed_files=["src/svc/service_charge.py"],
        base="main",
        max_tokens=None,
    )

    gap = result["policy_gaps"][0]
    assert gap["kind"] == "SERVICE_INTEGRATION_GAP"
    assert "qb2_UpdateServiceCharge" in gap["reason"]
    assert "update_service_charge" in gap["reason"]


def test_changed_route_status_missing_from_docs_is_advisory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _base_config(repo)
    _write(
        repo / "src/http/routes.py",
        """
from http import HTTPStatus
from apiflask import abort

@bp.doc(responses={200: "ok"})
def create_service_charge():
    if bad:
        abort(HTTPStatus.CONFLICT)
    return {}, HTTPStatus.CREATED
""",
    )
    _write(
        repo / "tests/http/test_routes.py",
        "def test_ok(client):\n    assert client.get('/x').status_code == 200\n",
    )

    result = project_for_review(
        _analysis(file_path="src/http/routes.py", name="create_service_charge"),
        repo_root=repo,
        changed_files=["src/http/routes.py"],
        base="main",
        max_tokens=None,
    )

    mismatch = result["http_contract_mismatches"][0]
    assert "201" in mismatch["missing_documented_responses"]
    assert "409" in mismatch["missing_documented_responses"]
    assert "201" in mismatch["missing_status_tests"]


def test_ambiguous_text_stays_reconciliation_evidence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _base_config(repo)
    _write(repo / "notes/spec.md", "This should probably reconcile with source behavior.")

    result = project_for_review(
        _analysis(),
        repo_root=repo,
        changed_files=["notes/spec.md"],
        base="main",
        max_tokens=None,
    )

    assert result["advisory_reconciliation"][0]["kind"] == "AMBIGUOUS_TEXT_RECONCILIATION"
    assert "policy_gaps" not in result
    assert "contract_shape_mismatches" not in result
    assert "http_contract_mismatches" not in result


def test_review_context_keeps_existing_keys_and_adds_standards(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _base_config(repo)
    _write(
        repo / ".zazz/standards/index.yaml",
        """
standards:
  - file: service-layer.md
    applies_to:
      paths:
        - src/svc/
      activities:
        - reviewing code structure, module cohesion, duplicated logic, or redundant computation
    purpose: Service-layer review rules.
""",
    )
    _write(repo / ".zazz/standards/service-layer.md", "# Service\n")
    result = project_for_review(
        _analysis(file_path="src/svc/service_charge.py", name="update_service_charge"),
        repo_root=repo,
        changed_files=["src/svc/service_charge.py"],
        base="main",
        max_tokens=800,
    )

    assert "test_gaps" in result
    assert "review_priorities" in result
    assert result["matched_standards"][0]["file"].endswith("service-layer.md")
    assert result["budget"]["max_tokens"] == 800


def test_advisory_rows_are_budget_truncated(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    changed = []
    for index in range(30):
        rel = f"notes/spec_{index}.md"
        changed.append(rel)
        _write(repo / rel, "This is ambiguous and should probably be reconciled.")

    result = project_for_review(
        {
            "summary": "summary",
            "risk_score": 0.0,
            "changed_functions": [],
            "review_priorities": [],
            "test_gaps": [],
            "affected_flows": [],
        },
        repo_root=repo,
        changed_files=changed,
        base="main",
        max_tokens=500,
    )

    assert result["budget"]["truncated"] is True
    assert result["budget"]["omitted"]["advisory_reconciliation"] > 0
    assert result["budget"]["estimated_tokens"] <= 500


def _analysis(file_path: str = "src/app.py", name: str = "changed_func"):
    row = {
        "name": name,
        "qualified_name": f"{file_path}::{name}",
        "kind": "Function",
        "file_path": file_path,
        "line_start": 1,
        "line_end": 4,
        "risk_score": 0.4,
    }
    return {
        "summary": "summary",
        "risk_score": 0.4,
        "changed_functions": [row],
        "review_priorities": [row],
        "test_gaps": [row],
        "affected_flows": [],
    }
