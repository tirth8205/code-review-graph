# Fixture for testing REFERENCES edge extraction in Python map dispatch patterns.


def handle_create(data):
    print("create", data)


def handle_update(data):
    print("update", data)


def handle_delete(data):
    print("delete", data)


def validate_input(data):
    return data is not None


def process_data(data):
    return data


def format_output(data):
    return str(data)


# Pattern 1: Dict with function values
handlers = {
    "create": handle_create,
    "update": handle_update,
    "delete": handle_delete,
}

# Pattern 2: List of function references (pipeline)
pipeline = [validate_input, process_data, format_output]


# Pattern 3: Assignment to dict key
dynamic_handlers = {}
dynamic_handlers["format"] = format_output


def dispatch(action):
    handler = handlers.get(action)
    if handler:
        handler({})
