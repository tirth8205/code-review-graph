"""``tests_for`` must find Java and Ruby tests, and only the true ones (PR #1154).

Java: a test in ``src/test/java`` could not reach ``src/main/java``. The import
walk only climbs the caller's own directories, and a class in the same package
needs no import at all, so every call out of a test stayed a bare name.

Ruby: only JavaScript and TypeScript turned ``it "..." do`` into a Test node, so
spec-style tests were not tests at all. Their calls also named no receiver.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.flows import detect_entry_points
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.query import query_graph


def _build(root: Path, files: dict[str, str]) -> Path:
    root = root.resolve()
    (root / ".git").mkdir()
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        full_build(root, store)
        run_post_processing(store)
    return root


def _tests_for(root: Path, relative: str, symbol: str) -> list[str]:
    result = query_graph("tests_for", f"{root / relative}::{symbol}", str(root))
    assert result["status"] == "ok", result
    return [row["name"] for row in result["results"]]


@pytest.mark.parametrize(
    ("files", "wanted", "called_by_code"),
    [
        (
            {
                "lib/cart.rb": (
                    "class Cart\n"
                    "  def total\n    0\n  end\n"
                    "  def tax\n    0\n  end\n"
                    "  def checkout\n    tax()\n  end\n"
                    "end\n"
                ),
                "test/cart_test.rb": (
                    'it "totals" do\n  Cart.new.total\n  Cart.new.tax\nend\n'
                ),
            },
            "lib/cart.rb::Cart.total",
            "lib/cart.rb::Cart.tax",
        ),
        (
            {
                "src/main/java/a/Cart.java": (
                    "package a;\npublic class Cart {\n"
                    "  public int total() { return 0; }\n"
                    "  public int tax() { return 0; }\n"
                    "  public int checkout() { return tax(); }\n"
                    "}\n"
                ),
                "src/test/java/a/CartTests.java": (
                    "package a;\nimport org.junit.jupiter.api.Test;\n"
                    "class CartTests {\n  @Test\n  void totals() {\n"
                    "    Cart cart = new Cart();\n    cart.total();\n    cart.tax();\n"
                    "  }\n}\n"
                ),
            },
            "src/main/java/a/Cart.java::Cart.total",
            "src/main/java/a/Cart.java::Cart.tax",
        ),
    ],
    ids=["ruby", "java"],
)
def test_a_tested_method_stays_an_entry_point(tmp_path, files, wanted, called_by_code):
    root = _build(tmp_path, files)
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        entries = {node.qualified_name for node in detect_entry_points(store)}
    assert str(root / wanted) in entries
    # A caller in the code itself still means it is no root.
    assert str(root / called_by_code) not in entries


def test_suffix_convention_is_for_classes_only(tmp_path: Path):
    """Seen on gin: ``QueryTest`` is a struct for the binding tests, not a test
    of ``Context.Query``."""
    root = _build(tmp_path, {
        "context.go": (
            "package gin\n\ntype Context struct{}\n\n"
            'func (c *Context) Query() string { return "" }\n'
        ),
        "binding_test.go": "package gin\n\ntype QueryTest struct{}\n",
    })
    assert _tests_for(root, "context.go", "Context.Query") == []


JAVA_FILES = {
    "src/main/java/com/acme/shop/Cart.java": """\
package com.acme.shop;

public class Cart {
    public void add(String item) {}
    public int total() { return 1; }
    public int size() { return 1; }
    public int neverCalled() { return 1; }
    public static Cart empty() { return new Cart(); }
}
""",
    "src/main/java/com/acme/shop/Note.java": """\
package com.acme.shop;

public class Note {
    public int length() { return 1; }
}
""",
    "src/main/java/com/acme/pay/Gateway.java": """\
package com.acme.pay;

public class Gateway {
    public boolean charge(int amount) { return true; }
    public boolean refund() { return true; }
    public int isEmpty() { return 0; }
}
""",
    "src/main/java/com/acme/other/Cart.java": """\
package com.acme.other;

public class Cart {
    public int total() { return 2; }
}
""",
    "src/test/java/com/acme/shop/CartTests.java": """\
package com.acme.shop;

import java.util.List;
import org.junit.jupiter.api.Test;
import com.acme.pay.Gateway;

class CartTests {
    private Cart field;

    @Test
    void sums() {
        Cart cart = new Cart();
        cart.add("x");
        cart.total();
    }

    @Test
    void usesField() {
        this.field.size();
    }

    @Test
    void usesStatic() {
        Cart.empty();
    }

    @Test
    void charges() {
        Gateway gateway = new Gateway();
        gateway.charge(1);
    }

    @Test
    void touchesOnlyLibraries(List<String> items, String name) {
        items.size();
        items.isEmpty();
        name.length();
    }
}
""",
}


@pytest.fixture
def java_repo(tmp_path: Path) -> Path:
    return _build(tmp_path, JAVA_FILES)


CART = "src/main/java/com/acme/shop/Cart.java"
GATEWAY = "src/main/java/com/acme/pay/Gateway.java"
OTHER_CART = "src/main/java/com/acme/other/Cart.java"
NOTE = "src/main/java/com/acme/shop/Note.java"


class TestJava:
    def test_import_reaches_main_from_test(self, java_repo: Path):
        with GraphStore(java_repo / ".code-review-graph" / "graph.db") as store:
            imports = {
                edge.target_qualified
                for edge in store.get_edges_by_source(
                    str(java_repo / "src/test/java/com/acme/shop/CartTests.java"),
                )
                if edge.kind == "IMPORTS_FROM"
            }
        assert str(java_repo / GATEWAY) in imports
        # A library stays what it was: nothing in the repository defines it.
        assert "java.util.List" in imports

    def test_imported_class_is_linked(self, java_repo: Path):
        assert _tests_for(java_repo, GATEWAY, "Gateway.charge") == ["charges"]

    def test_same_package_class_is_linked_with_no_import(self, java_repo: Path):
        assert "sums" in _tests_for(java_repo, CART, "Cart.add")
        assert "sums" in _tests_for(java_repo, CART, "Cart.total")
        assert "sums" in _tests_for(java_repo, CART, "Cart")

    def test_typed_field_is_linked(self, java_repo: Path):
        assert "usesField" in _tests_for(java_repo, CART, "Cart.size")

    def test_static_call_is_linked(self, java_repo: Path):
        assert "usesStatic" in _tests_for(java_repo, CART, "Cart.empty")

    def test_var_takes_the_type_of_its_constructor(self, tmp_path: Path):
        files = dict(JAVA_FILES)
        files["src/test/java/com/acme/shop/VarTests.java"] = """\
package com.acme.shop;

import org.junit.jupiter.api.Test;

class VarTests {
    @Test
    void usesVar() {
        var cart = new Cart();
        cart.neverCalled();
    }
}
"""
        root = _build(tmp_path, files)
        assert _tests_for(root, CART, "Cart.neverCalled") == ["usesVar"]

    def test_uncalled_method_has_no_test(self, java_repo: Path):
        assert _tests_for(java_repo, CART, "Cart.neverCalled") == []
        assert _tests_for(java_repo, GATEWAY, "Gateway.refund") == []

    def test_same_method_name_on_a_library_type_is_not_a_link(self, java_repo: Path):
        # items.size() and name.length() share a name with methods in this
        # very package, and must not be credited to them.
        assert "touchesOnlyLibraries" not in _tests_for(java_repo, CART, "Cart.size")
        assert _tests_for(java_repo, NOTE, "Note.length") == []
        # Nor to a method of a class the test imports for another reason.
        assert _tests_for(java_repo, GATEWAY, "Gateway.isEmpty") == []

    def test_same_class_name_in_another_package_is_not_a_link(self, java_repo: Path):
        assert _tests_for(java_repo, OTHER_CART, "Cart.total") == []

    def test_main_cannot_see_test_sources(self, tmp_path: Path):
        files = dict(JAVA_FILES)
        files["src/main/java/com/acme/shop/Oops.java"] = """\
package com.acme.shop;

import com.acme.shop.CartTests;

public class Oops {
    public void run() {
        CartTests tests = new CartTests();
        tests.sums();
    }
}
"""
        root = _build(tmp_path, files)
        with GraphStore(root / ".code-review-graph" / "graph.db") as store:
            imports = {
                edge.target_qualified
                for edge in store.get_edges_by_source(
                    str(root / "src/main/java/com/acme/shop/Oops.java"),
                )
                if edge.kind == "IMPORTS_FROM"
            }
        assert imports == {"com.acme.shop.CartTests"}

    def test_gradle_extra_source_set_reaches_main(self, tmp_path: Path):
        files = dict(JAVA_FILES)
        files["src/integrationTest/java/com/acme/pay/GatewayIT.java"] = """\
package com.acme.pay;

import org.junit.jupiter.api.Test;

class GatewayIT {
    @Test
    void refunds() {
        Gateway gateway = new Gateway();
        gateway.refund();
    }
}
"""
        root = _build(tmp_path, files)
        assert _tests_for(root, GATEWAY, "Gateway.refund") == ["refunds"]

    def test_test_class_is_found_by_its_suffix(self, java_repo: Path):
        result = query_graph("tests_for", f"{java_repo / CART}::Cart", str(java_repo))
        named = [
            row for row in result["results"]
            if row.get("inferred_by") == "naming_convention"
        ]
        assert [row["name"] for row in named] == ["CartTests"]


RUBY_CART = """\
module Shop
  class Cart
    def initialize(items = [])
      @items = items
    end

    def self.build
      new
    end

    def add(item)
      @items << item
    end

    def total
      @items.size
    end

    def tax
      0
    end

    def never_called
      0
    end
  end

  class Other
    def total
      0
    end

    def add(item)
      item
    end
  end
end
"""

MINITEST_SPEC = """\
require_relative "helper"

class LocalThing
  def initialize(value)
    @value = value
  end

  def add(item)
    item
  end
end

describe Shop::Cart do
  before do
    @cart = Shop::Cart.new
  end

  it "adds" do
    @cart.add(1)
  end

  describe "totals" do
    it "totals" do
      cart = Shop::Cart.new
      assert_equal 0, cart.total
    end
  end

  it "builds" do
    Shop::Cart.build
  end

  it "chains" do
    Shop::Cart.new.tax
  end

  it "uses the local class" do
    LocalThing.new(1).add(2)
  end

  it "loses the type on reassignment" do
    thing = Shop::Cart.new
    thing = make_thing
    thing.never_called
  end
end
"""

RSPEC_FILE = """\
RSpec.describe Shop::Cart do
  let(:cart) { described_class.new }

  subject(:other) { Shop::Other.new }

  it "adds through let" do
    cart.add(1)
  end

  it "totals through subject" do
    other.total
  end

  specify "and through described_class" do
    described_class.build
  end
end
"""

ACTIVE_SUPPORT_FILE = """\
class CartTest < ActiveSupport::TestCase
  setup do
    @cart = Shop::Cart.new
  end

  test "adds" do
    @cart.add(1)
  end
end
"""


def _ruby_repo(tmp_path: Path, relative: str, text: str) -> Path:
    return _build(tmp_path, {"lib/shop/cart.rb": RUBY_CART, relative: text})


class TestRuby:
    def test_blocks_become_test_nodes(self, tmp_path: Path):
        root = _ruby_repo(tmp_path, "test/cart_test.rb", MINITEST_SPEC)
        with GraphStore(root / ".code-review-graph" / "graph.db") as store:
            names = {
                node.name
                for node in store.get_nodes_by_file(str(root / "test/cart_test.rb"))
                if node.kind == "Test"
            }
        # ``::`` splits a qualified name from its file, so it cannot stay.
        assert any(name.startswith("describe:Shop.Cart@") for name in names)
        assert any(name.startswith("it:adds@") for name in names)
        assert any(name.startswith("before@") for name in names)

    def test_spec_call_finds_its_method(self, tmp_path: Path):
        root = _ruby_repo(tmp_path, "test/cart_test.rb", MINITEST_SPEC)
        cart = "lib/shop/cart.rb"
        assert any(n.startswith("it:adds@") for n in _tests_for(root, cart, "Shop.Cart.add"))
        assert any(n.startswith("it:totals@") for n in _tests_for(root, cart, "Shop.Cart.total"))
        assert any(n.startswith("it:builds@") for n in _tests_for(root, cart, "Shop.Cart.build"))
        assert any(n.startswith("it:chains@") for n in _tests_for(root, cart, "Shop.Cart.tax"))
        assert any(
            n.startswith("before@") for n in _tests_for(root, cart, "Shop.Cart.initialize")
        )

    def test_no_false_links(self, tmp_path: Path):
        root = _ruby_repo(tmp_path, "test/cart_test.rb", MINITEST_SPEC)
        cart = "lib/shop/cart.rb"
        assert _tests_for(root, cart, "Shop.Other.total") == []
        assert _tests_for(root, cart, "Shop.Other.add") == []
        # The reassigned variable no longer holds a Cart.
        assert _tests_for(root, cart, "Shop.Cart.never_called") == []

    def test_unknown_receiver_does_not_match_a_required_file(self, tmp_path: Path):
        root = _build(tmp_path, {
            "lib/shop.rb": "module Shop\n  def self.size\n    0\n  end\nend\n",
            "test/shop_test.rb": """\
require "shop"

it "measures a list" do
  list = make_list
  list.size
end
""",
        })
        assert _tests_for(root, "lib/shop.rb", "Shop.size") == []

    def test_plain_call_still_matches_a_required_file(self, tmp_path: Path):
        root = _build(tmp_path, {
            "lib/helpers.rb": "def build_cart(size)\n  size\nend\n",
            "test/helpers_test.rb": """\
require "helpers"

it "builds" do
  build_cart(1)
end
""",
        })
        assert any(
            n.startswith("it:builds@") for n in _tests_for(root, "lib/helpers.rb", "build_cart")
        )

    def test_chain_over_several_lines_is_linked(self, tmp_path: Path):
        root = _ruby_repo(tmp_path, "test/cart_test.rb", """\
it "chains" do
  Shop::Cart
    .new
    .tax
end
""")
        assert any(
            n.startswith("it:chains@")
            for n in _tests_for(root, "lib/shop/cart.rb", "Shop.Cart.tax")
        )

    def test_free_text_in_a_test_name_is_not_a_naming_link(self, tmp_path: Path):
        root = _build(tmp_path, {
            "lib/out.rb": "module Out\n  def self.json\n    0\n  end\nend\n",
            "lib/job.rb": "module Job\n  def self.run\n    0\n  end\nend\n",
            "test/out_test.rb": 'it "tests json output" do\n  1\nend\n',
            "test/job_test.rb": 'it "runs" do\n  1\nend\n',
        })
        assert _tests_for(root, "lib/out.rb", "Out.json") == []
        # The file itself can still speak for a block inside it.
        result = query_graph("tests_for", f"{root / 'lib/job.rb'}::Job", str(root))
        assert any(
            row["name"].startswith("it:runs@") and row["inferred_by"] == "naming_convention"
            for row in result["results"]
        )

    def test_constructor_in_the_test_file_is_not_the_one_tested(self, tmp_path: Path):
        root = _ruby_repo(tmp_path, "test/cart_test.rb", MINITEST_SPEC)
        local = _tests_for(root, "test/cart_test.rb", "LocalThing.initialize")
        assert any(n.startswith("it:uses the local class@") for n in local)
        assert not any(n.startswith("before@") for n in local)
        assert not any(
            n.startswith("it:uses the local class@")
            for n in _tests_for(root, "lib/shop/cart.rb", "Shop.Cart.add")
        )

    def test_rspec_let_subject_and_described_class(self, tmp_path: Path):
        root = _ruby_repo(tmp_path, "spec/cart_spec.rb", RSPEC_FILE)
        cart = "lib/shop/cart.rb"
        assert any(
            n.startswith("it:adds through let@") for n in _tests_for(root, cart, "Shop.Cart.add")
        )
        assert any(
            n.startswith("it:totals through subject@")
            for n in _tests_for(root, cart, "Shop.Other.total")
        )
        assert any(
            n.startswith("specify:and through described_class@")
            for n in _tests_for(root, cart, "Shop.Cart.build")
        )

    def test_active_support_style(self, tmp_path: Path):
        root = _ruby_repo(tmp_path, "test/cart_test.rb", ACTIVE_SUPPORT_FILE)
        assert any(
            n.startswith("test:adds@")
            for n in _tests_for(root, "lib/shop/cart.rb", "Shop.Cart.add")
        )
        assert any(
            n.startswith("setup@")
            for n in _tests_for(root, "lib/shop/cart.rb", "Shop.Cart.initialize")
        )

    @pytest.mark.parametrize(
        "source",
        [
            'it "looks like a test" do\n  run\nend\n',
            'describe "looks like a spec" do\n  run\nend\n',
        ],
    )
    def test_runner_names_outside_test_files_are_ordinary_calls(self, tmp_path, source):
        path = tmp_path / "lib" / "dsl.rb"
        nodes, _ = CodeParser().parse_bytes(path, source.encode())
        assert [node for node in nodes if node.kind == "Test"] == []

    @pytest.mark.parametrize(
        "source",
        [
            # Ruby 3.4's implicit block parameter.
            "[1].map { it + 1 }\n",
            # A method of the same name on some other object.
            'robot.it "x" do\n  run\nend\n',
            "config.test do\n  run\nend\n",
            # A helper that takes no block is not a test.
            'test "x"\n',
        ],
    )
    def test_not_every_it_is_a_test(self, tmp_path, source):
        path = tmp_path / "test" / "thing_test.rb"
        nodes, _ = CodeParser().parse_bytes(path, source.encode())
        assert [node for node in nodes if node.kind == "Test"] == []
