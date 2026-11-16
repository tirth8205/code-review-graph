"""One catalog of what counts as a use, a subject, and a suppression.

The same knowledge was being restated in the parser (annotation and value
references), the refactor engine (non-code subjects, type names), the flow
heuristics (framework decorators) and the inheritance index (C3 rules), so every
new construct — a Pydantic field, a PEP 695 alias, a Protocol member — had to be
taught to each of them.

This module owns the rules that are shared by more than one consumer. A language
or framework behaviour is a row here plus a fixture, not a new if-chain.

Consumers: ``parser`` (annotation semantics), ``refactor`` (subjects, type
names), ``flows`` (framework hooks), ``inheritance`` (override decorators).
"""

from __future__ import annotations

import re
from typing import Final

#: Languages whose documents an orchestrator, a provider or a query engine runs.
#: They have no call site to be missing, so "no incoming edge" says nothing
#: about whether they are dead: Ansible playbooks alone produced 740 of
#: algar-oud-mig's reports.
NON_CODE_SUBJECT_LANGUAGES: Final[frozenset[str]] = frozenset({
    "ansible", "yaml", "json", "toml", "hcl", "sql", "markdown", "md",
    "dockerfile", "ini", "xml", "csv",
})

#: Generic heads whose string arguments are values, never type names.
LITERAL_GENERICS: Final[tuple[str, ...]] = (
    "Literal[",
    "typing.Literal[",
    "typing_extensions.Literal[",
)

#: Decorators a Python runtime or framework invokes instead of a call site.
#: A member carrying one is live even with no caller in the graph.
PYTHON_HOOK_DECORATORS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^(?:[\w.]+\.)?(?:property|cached_property|computed_field)$"),
    re.compile(
        r"^(?:[\w.]+\.)?(?:field_validator|model_validator|field_serializer"
        r"|model_serializer|validator|root_validator)(?:\s*\(.*)?$",
        re.DOTALL,
    ),
    re.compile(r"^(?:[\w.]+\.)?(?:abstractmethod|overload)$"),
    re.compile(r"^(?:[\w.]+\.)?(?:override|Override|Override)$"),
)

#: Why a symbol was suppressed or judged. Every verdict carries one, so a
#: suppression is auditable instead of silent.
REASON_CODES: Final[dict[str, str]] = {
    "value_reference": "handed to a library as a value",
    "callback_argument": "passed as a callback argument",
    "export_surface": "published by a package export registry or __all__",
    "registry_map": "named in a string-to-symbol map",
    "annotation_reference": "named by a type annotation",
    "framework_hook": "invoked by the runtime or a framework",
    "visitor_dispatch": "invoked by a visitor dispatcher",
    "protocol_hook": "dunder or protocol method invoked by the runtime",
    "override_of_unknown_base": "overrides a base outside the graph",
    "non_code_subject": "a document language, not a code subject",
    "generated_projection": "generated output, never authored",
    "ambiguous": "resolution could not single out one target",
    "no_incoming_edges": "no caller, test, importer or reference",
}


def annotation_declares_values(text: str) -> bool:
    """Return whether an annotation is a ``Literal`` (its strings are values)."""
    return text.lstrip().startswith(LITERAL_GENERICS)
