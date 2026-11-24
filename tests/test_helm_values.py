"""A chart's default values are dependencies of the templates that read them."""

from pathlib import Path

import pytest

from code_review_graph.forget import forget_files
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update

_CHART = "apiVersion: v2\nname: test-chart\nversion: 0.1.0\n"


def _write(repo: Path, name: str, source: str) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _imports(store: GraphStore, source: Path) -> list[tuple[str, int]]:
    return [
        (edge.target_qualified, edge.line)
        for edge in store.get_edges_by_source(source.as_posix())
        if edge.kind == "IMPORTS_FROM"
    ]


def test_values_impact_stays_with_the_owning_chart(tmp_path):
    for chart in ("site", "site/charts/worker"):
        _write(tmp_path, f"{chart}/Chart.yaml", _CHART)
        _write(tmp_path, f"{chart}/values.yaml", "replicas: 2\n")
    parent = _write(tmp_path, "site/templates/workload.yaml", "replicas: {{ .Values.replicas }}\n")
    child = _write(tmp_path, "site/charts/worker/templates/jobs/workload.yml", (
        "kind: Job\nreplicas: {{- $.Values.replicas }}\n"
    ))

    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)

        parent_values = (tmp_path / "site/values.yaml").as_posix()
        child_values = (tmp_path / "site/charts/worker/values.yaml").as_posix()
        assert _imports(store, parent) == [(parent_values, 1)]
        assert _imports(store, child) == [(child_values, 2)]
        impacted = store.get_impact_radius([child_values])["impacted_files"]
        assert child.as_posix() in impacted
        assert parent.as_posix() not in impacted


@pytest.mark.parametrize("text", [
    "plain: .Values.replicaCount\n",
    'a: {{ printf ".Values.replicaCount }}" }}\n',
    'a: {{/* .Values.replicaCount }} */}}\n',
    "a: {{ $context.Values.replicaCount }}\n",
    "a: {{ .ValuesExtra.replicaCount }}\n",
    "a: {{ .Values.replicaCount\n",
])
def test_other_text_does_not_create_a_helm_values_dependency(tmp_path, text):
    _write(tmp_path, "Chart.yaml", _CHART)
    _write(tmp_path, "values.yaml", "replicas: 2\n")
    template = _write(tmp_path, "templates/workload.yaml", text)
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        assert _imports(store, template) == []


def test_adding_and_removing_chart_metadata_recomputes_values_links(tmp_path):
    values = _write(tmp_path, "values.yaml", "replicas: 2\n")
    template = _write(tmp_path, "templates/workload.yaml", "replicas: {{ .Values.replicas }}\n")
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        assert _imports(store, template) == []

        chart = _write(tmp_path, "Chart.yaml", _CHART)
        incremental_update(tmp_path, store, changed_files=["Chart.yaml"])
        assert _imports(store, template) == [(values.as_posix(), 1)]

        chart.unlink()
        incremental_update(tmp_path, store, changed_files=["Chart.yaml"])
        assert _imports(store, template) == []


def test_forgetting_chart_metadata_removes_the_derived_edge(tmp_path):
    chart = _write(tmp_path, "Chart.yaml", _CHART)
    values = _write(tmp_path, "values.yaml", "replicas: 2\n")
    template = _write(tmp_path, "templates/workload.yaml", "replicas: {{ .Values.replicas }}\n")
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        assert _imports(store, template) == [(values.as_posix(), 1)]
        forget_files(store, tmp_path, [chart.as_posix()])
        assert _imports(store, template) == []
