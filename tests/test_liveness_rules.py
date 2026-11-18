"""The rules that decide what counts as a use live in one catalog.

Dead code, the parser, the flow heuristics and rename must read the same rows; a
second copy is what let the same false positive return after being fixed.
"""

from __future__ import annotations

from code_review_graph import flows, liveness_rules, refactor


def test_consumers_read_the_shared_catalog():
    assert (
        refactor.NON_CODE_SUBJECT_LANGUAGES
        is liveness_rules.NON_CODE_SUBJECT_LANGUAGES
    )
    assert set(liveness_rules.PYTHON_HOOK_DECORATORS).issubset(
        set(flows._FRAMEWORK_DECORATOR_PATTERNS)
    )


def test_every_verdict_has_a_reason_code():
    for code in (
        "value_reference",
        "export_surface",
        "annotation_reference",
        "framework_hook",
        "non_code_subject",
        "ambiguous",
        "no_incoming_edges",
    ):
        assert code in liveness_rules.REASON_CODES


def test_literal_generics_are_values_not_types():
    assert liveness_rules.annotation_declares_values('Literal["open"]')
    assert liveness_rules.annotation_declares_values('typing.Literal["open"]')
    assert not liveness_rules.annotation_declares_values("Model")
    assert not liveness_rules.annotation_declares_values("Annotated[Model, Field()]")
