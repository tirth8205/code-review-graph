from pathlib import Path

from code_review_graph.parser import CodeParser


def _calls_from(edges, source_suffix: str) -> list[str]:
    return [
        edge.target
        for edge in edges
        if edge.kind == "CALLS" and edge.source.endswith(source_suffix)
    ]


def test_python_imported_class_and_typed_receivers_resolve(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    service = pkg / "service.py"
    service.write_text(
        "class Service:\n    @classmethod\n    def build(cls): ...\n    def work(self): ...\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from pkg.service import Service\n\n"
        "def run(service: Service[list[str]]):\n"
        "    Service.build()\n"
        "    service.work()\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(consumer)

    targets = _calls_from(edges, "::run")
    assert targets.count(f"{service.resolve()}::Service.build") == 1
    assert targets.count(f"{service.resolve()}::Service.work") == 1
    assert "build" not in targets
    assert "work" not in targets


def test_python_typed_receiver_scope_does_not_leak(tmp_path: Path) -> None:
    source = tmp_path / "scopes.py"
    source.write_text(
        "class OuterService:\n"
        "    def work(self): ...\n\n"
        "class InnerService:\n"
        "    def work(self): ...\n\n"
        "def outer(value: OuterService):\n"
        "    value.work()\n"
        "    def inner(value: InnerService):\n"
        "        value.work()\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(source)

    assert f"{source.resolve()}::OuterService.work" in _calls_from(edges, "::outer")
    inner_targets = _calls_from(edges, "::inner")
    assert f"{source.resolve()}::InnerService.work" in inner_targets
    assert f"{source.resolve()}::OuterService.work" not in inner_targets


def test_unimported_cross_file_class_name_is_not_guessed(tmp_path: Path) -> None:
    other = tmp_path / "other.py"
    other.write_text(
        "class Service:\n    @classmethod\n    def build(cls): ...\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "def run():\n    Service.build()\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(consumer)

    assert _calls_from(edges, "::run") == ["build"]


def test_untyped_common_method_is_retained_but_not_guessed(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "class Service:\n    def update(self): ...\n\ndef run(obj):\n    obj.update()\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(source)

    # PR #337 deleted common method names globally. Preserve the uncertain
    # call instead, but do not bind it to Service without type/import evidence.
    assert _calls_from(edges, "::run") == ["update"]


def test_container_annotation_is_not_mistaken_for_element_type(
    tmp_path: Path,
) -> None:
    source = tmp_path / "container.py"
    source.write_text(
        "class Service:\n"
        "    def append(self): ...\n\n"
        "def run(values: list[Service]):\n"
        "    values.append()\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(source)

    assert _calls_from(edges, "::run") == ["append"]


def test_kotlin_generic_parameter_local_and_field_types_resolve(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    app = tmp_path / "app"
    pkg.mkdir()
    app.mkdir()
    service = pkg / "Service.kt"
    service.write_text(
        "package pkg\nclass Service<T> {\n  fun work() {}\n  fun done() {}\n  fun save() {}\n}\n",
        encoding="utf-8",
    )
    consumer = app / "Consumer.kt"
    consumer.write_text(
        "package app\n"
        "import pkg.Service\n"
        "class Consumer(private val field: Service<String>) {\n"
        "  fun run(param: Service<Int>) {\n"
        "    val local: Service<Long> = param\n"
        "    local.work()\n"
        "    param.done()\n"
        "    field.save()\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(consumer)

    targets = _calls_from(edges, "::Consumer.run")
    assert f"{service.resolve()}::Service.work" in targets
    assert f"{service.resolve()}::Service.done" in targets
    assert f"{service.resolve()}::Service.save" in targets


def test_java_generic_parameter_local_and_field_types_resolve(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    app = tmp_path / "app"
    pkg.mkdir()
    app.mkdir()
    service = pkg / "Service.java"
    service.write_text(
        "package pkg;\n"
        "class Service<T> {\n"
        "  void work() {}\n"
        "  void done() {}\n"
        "  void save() {}\n"
        "}\n",
        encoding="utf-8",
    )
    consumer = app / "Consumer.java"
    consumer.write_text(
        "package app;\n"
        "import pkg.Service;\n"
        "class Consumer {\n"
        "  private Service<String> field;\n"
        "  void run(Service<Integer> param) {\n"
        "    Service<Long> local = param;\n"
        "    local.work();\n"
        "    param.done();\n"
        "    field.save();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(consumer)

    targets = _calls_from(edges, "::Consumer.run")
    assert f"{service.resolve()}::Service.work" in targets
    assert f"{service.resolve()}::Service.done" in targets
    assert f"{service.resolve()}::Service.save" in targets


def test_typescript_generic_parameter_local_and_field_types_resolve(
    tmp_path: Path,
) -> None:
    service = tmp_path / "service.ts"
    service.write_text(
        "export class Service<T> {\n  work() {}\n  done() {}\n  save() {}\n}\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer.ts"
    consumer.write_text(
        "import { Service } from './service';\n"
        "class Consumer {\n"
        "  constructor(private field: Service<string>) {}\n"
        "  run(param: Service<number>) {\n"
        "    const local: Service<boolean> = param;\n"
        "    local.work();\n"
        "    param.done();\n"
        "    this.field.save();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(consumer)

    targets = _calls_from(edges, "::Consumer.run")
    assert f"{service.resolve()}::Service.work" in targets
    assert f"{service.resolve()}::Service.done" in targets
    assert f"{service.resolve()}::Service.save" in targets


def test_typescript_block_shadowing_restores_outer_type(tmp_path: Path) -> None:
    source = tmp_path / "scopes.ts"
    source.write_text(
        "class OuterService { work() {} }\n"
        "class InnerService { work() {} }\n"
        "function run(value: OuterService) {\n"
        "  value.work();\n"
        "  {\n"
        "    const value: InnerService = new InnerService();\n"
        "    value.work();\n"
        "  }\n"
        "  value.work();\n"
        "}\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(source)

    targets = _calls_from(edges, "::run")
    assert targets.count(f"{source.resolve()}::OuterService.work") == 2
    assert targets.count(f"{source.resolve()}::InnerService.work") == 1


def test_this_call_resolves_to_its_enclosing_class(tmp_path: Path) -> None:
    source = tmp_path / "this-call.ts"
    source.write_text(
        "class First { work() {} }\nclass Second {\n  work() {}\n  run() { this.work(); }\n}\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=tmp_path).parse_file(source)

    assert _calls_from(edges, "::Second.run") == [
        f"{source.resolve()}::Second.work",
    ]
