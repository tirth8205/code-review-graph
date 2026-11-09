"""``Class.method`` query targets must find Ruby methods in a namespace (PR #933)."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools.query import query_graph

SOURCE = """\
module Billing
  class Base
    def settle
      1
    end
  end

  module Adapters
    class Paypal
      def charge
        audit(1)
      end

      def audit(amount)
        amount
      end
    end
  end
end

class Alpha::Same
  def run; end
end

class Beta::Same
  def run; end
end
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "billing.rb").write_text(SOURCE, encoding="utf-8")
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
    return tmp_path


@pytest.mark.parametrize(
    "target",
    ["Paypal.audit", "Adapters.Paypal.audit", "Billing.Adapters.Paypal.audit"],
)
def test_any_tail_of_the_namespace_path_finds_the_method(repo: Path, target: str) -> None:
    result = query_graph("callers_of", target, str(repo))

    assert result["status"] == "ok", result.get("summary")
    assert result["target"].endswith("::Billing.Adapters.Paypal.audit")
    assert [row["name"] for row in result["results"]] == ["charge"]


def test_two_classes_with_one_name_are_reported_as_ambiguous(repo: Path) -> None:
    result = query_graph("callers_of", "Same.run", str(repo))

    assert result["status"] == "ambiguous"
    assert result["candidate_count"] == 2
    assert {c["qualified_name"].rsplit("::", 1)[-1] for c in result["candidates"]} == {
        "Alpha.Same.run",
        "Beta.Same.run",
    }


@pytest.mark.parametrize("target", ["Other.audit", "Base.missing"])
def test_a_wrong_class_never_falls_back_to_the_method_name(repo: Path, target: str) -> None:
    result = query_graph("callers_of", target, str(repo))

    assert result["status"] == "not_found", result
