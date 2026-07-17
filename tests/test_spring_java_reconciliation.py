from pathlib import Path

from code_review_graph.parser import CodeParser, EdgeInfo


def _parse_java(source: str) -> tuple[list, list[EdgeInfo]]:
    return CodeParser().parse_bytes(Path("SpringReconciliation.java"), source.encode())


def _injected_fields(source: str, class_name: str) -> dict[str, str]:
    _, edges = _parse_java(source)
    return {
        edge.extra["field_name"]: edge.extra["injection_type"]
        for edge in edges
        if edge.kind == "INJECTS"
        and class_name in edge.source
        and "field_name" in edge.extra
    }


def test_required_args_constructor_matches_lombok_field_selection() -> None:
    fields = _injected_fields(
        """
        import lombok.NonNull;
        import lombok.RequiredArgsConstructor;

        @RequiredArgsConstructor
        class RequiredService {
            private final Repository requiredFinal;
            private final Repository initializedFinal = new Repository();
            @NonNull private Client requiredNonNull;
            @NonNull private Client initializedNonNull = new Client();
            private final Repository first, initializedSecond = new Repository();
            private static final Repository SHARED = new Repository();
            private String ordinary;
        }
        """,
        "RequiredService",
    )

    assert fields == {
        "requiredFinal": "constructor_lombok",
        "requiredNonNull": "constructor_lombok",
        "first": "constructor_lombok",
    }


def test_all_args_constructor_emits_one_edge_per_non_static_declarator() -> None:
    fields = _injected_fields(
        """
        import lombok.AllArgsConstructor;

        @AllArgsConstructor
        class AllService {
            private Repository primary, secondary;
            private final Client initialized = new Client();
            private static Repository shared;
        }
        """,
        "AllService",
    )

    assert fields == {
        "primary": "constructor_lombok_all",
        "secondary": "constructor_lombok_all",
        "initialized": "constructor_lombok_all",
    }


def test_explicit_field_injection_emits_each_declared_field() -> None:
    fields = _injected_fields(
        """
        import org.springframework.beans.factory.annotation.Autowired;

        class ExplicitService {
            @Autowired private Repository primary, secondary;
        }
        """,
        "ExplicitService",
    )

    assert fields == {
        "primary": "field",
        "secondary": "field",
    }
