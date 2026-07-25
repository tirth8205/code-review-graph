"""dbt model SQL parser tests: {{ ref() }} / {{ source() }} extraction."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser

DBT_MODEL = b"""\
with

source as (
    select * from {{ ref('stg_orders') }}
),

customers as (
    select * from {{ ref('analytics_utils', 'dim_customers') }}
),

payments as (
    select * from {{ source('core_db', 'payments') }}
),

final as (
    select
        source.order_id,
        customers.customer_id,
        payments.amount
    from source
    left join customers on source.customer_id = customers.customer_id
    left join payments on source.order_id = payments.order_id
)

select * from final
"""


class TestDbtModelParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_bytes(
            Path("models/staging/fct_orders.sql"), DBT_MODEL,
        )

    def test_model_node_named_after_file_stem(self):
        models = [
            n for n in self.nodes
            if n.kind == "Class" and n.extra.get("sql_kind") == "dbt_model"
        ]
        assert len(models) == 1
        assert models[0].name == "fct_orders"
        assert models[0].language == "sql"

    def test_contains_edge(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target for e in contains}
        assert "models/staging/fct_orders.sql::fct_orders" in targets

    def test_ref_and_source_dependency_edges(self):
        imports = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert imports == {
            "stg_orders",            # ref('stg_orders')
            "dim_customers",         # ref('package', 'model') → model
            "core_db.payments",      # source() stays qualified
        }

    def test_cte_names_are_not_dependency_edges(self):
        # The FROM/JOIN regex pass must not run on dbt models: it would
        # record the CTE names as phantom IMPORTS_FROM targets.
        imports = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert not imports & {"source", "customers", "payments", "final"}

    def test_duplicate_refs_are_deduplicated(self):
        nodes, edges = self.parser.parse_bytes(
            Path("models/dupes.sql"),
            b"select * from {{ ref('stg_orders') }}\n"
            b"union all\n"
            b"select * from {{ ref('stg_orders') }}\n",
        )
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert [e.target for e in imports] == ["stg_orders"]

    def test_plain_sql_without_jinja_keeps_ddl_extraction(self):
        nodes, edges = self.parser.parse_bytes(
            Path("schema.sql"),
            b"CREATE TABLE users (id INT);\n"
            b"CREATE VIEW active_users AS SELECT id FROM users;\n",
        )
        kinds = {n.extra.get("sql_kind") for n in nodes if n.kind == "Class"}
        assert kinds == {"table", "view"}
        assert not any(
            n.extra.get("sql_kind") == "dbt_model" for n in nodes
        )


def test_full_build_links_dbt_models_by_ref(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "stg_orders.sql").write_text(
        "select * from {{ source('core_db', 'orders') }}\n",
        encoding="utf-8",
    )
    (models / "fct_orders.sql").write_text(
        "with orders as (\n"
        "    select * from {{ ref('stg_orders') }}\n"
        ")\n"
        "select * from orders\n",
        encoding="utf-8",
    )

    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    try:
        full_build(tmp_path, store)

        model_names = {n.name for n in store.get_nodes_by_kind(["Class"])}
        assert {"stg_orders", "fct_orders"} <= model_names

        fct_file = str(models / "fct_orders.sql")
        targets = {
            e.target_qualified
            for e in store.get_edges_by_source(fct_file)
            if e.kind == "IMPORTS_FROM"
        }
        assert "stg_orders" in targets
        assert "orders" not in targets  # CTE name must not leak in
    finally:
        store.close()
