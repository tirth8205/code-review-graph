"""Mini AST parser for MRR eval fixture."""


def parse_tree(source):
    """Parse source text and return an AST root."""
    tokens = tokenize(source)
    root = build_root(tokens)
    return root


def walk_nodes(tree, visitor):
    stack = [tree]
    while stack:
        node = stack.pop()
        visitor(node)
        stack.extend(node.children)


def extract_functions(tree):
    found = []
    walk_nodes(tree, lambda n: found.append(n) if n.kind == "function" else None)
    return found


def tokenize(source):
    return source.split()


def build_root(tokens):
    class N:
        kind = "root"
        children = []
    return N()
