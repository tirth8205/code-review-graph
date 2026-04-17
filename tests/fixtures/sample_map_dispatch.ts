// Fixture for testing REFERENCES edge extraction in map dispatch patterns.

function handleCreate(data: any): void {
    console.log("create", data);
}

function handleUpdate(data: any): void {
    console.log("update", data);
}

function handleDelete(data: any): void {
    console.log("delete", data);
}

function validateInput(data: any): boolean {
    return data != null;
}

function processData(data: any): any {
    return data;
}

function formatOutput(data: any): string {
    return JSON.stringify(data);
}

// Pattern 1: Object literal with function values (Record<string, Handler>)
const handlers: Record<string, (data: any) => void> = {
    create: handleCreate,
    update: handleUpdate,
    delete: handleDelete,
};

// Pattern 2: Shorthand property references
const shorthandMap = { validateInput, processData };

// Pattern 3: Property assignment to map
const dynamicHandlers: Record<string, Function> = {};
dynamicHandlers['format'] = formatOutput;

// Pattern 4: Array of function references (pipeline)
const pipeline = [validateInput, processData, formatOutput];

// Pattern 5: Function passed as callback argument
function register(fn: Function): void {
    // registration logic
}

function dispatch(action: string): void {
    const handler = handlers[action];
    if (handler) {
        register(handleCreate);
        handler({});
    }
}
