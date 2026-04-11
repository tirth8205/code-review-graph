"""Fixture: module-level import followed by attribute access.

Pain point: `import json; json.dumps()` does NOT resolve because the parser
only tracks `from X import Y` in import_map, not `import X`.
"""

import json
import os.path


def serialize(data):
    return json.dumps(data)


def get_size(path):
    return os.path.getsize(path)
