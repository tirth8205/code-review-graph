// Fixture for testing TypeScript/JavaScript dead-guard detection.
//
// Both TS and JS share the same tree-sitter if_statement node type.
// The condition is wrapped in parenthesized_expression, which must be
// unwrapped before checking for false/0 literals.

function live_helper(): void {}

function dead_false_call(): void {}

function dead_zero_call(): void {}

function live_in_else(): void {}

function dead_in_consequence(): void {}

function live_final_else(): void {}

function live_in_if_true(): void {}

function some_condition(): boolean {
    return true;
}

function caller(): void {
    live_helper(); // live -- no guard

    if (false) {
        dead_false_call(); // dead consequence
    }
}

function zero_guard(): void {
    if (0) {
        dead_zero_call(); // dead consequence -- 0 is falsy
    }
}

function else_branch(): void {
    if (false) {
        dead_in_consequence(); // dead consequence
    } else {
        live_in_else(); // live -- else branch
    }
}

function elif_chain(): void {
    if (false) {
        dead_in_consequence(); // dead
    } else if (some_condition()) {
        live_final_else(); // live
    }
}

function true_guard(): void {
    // if true is NOT a dead guard -- consequence is live.
    if (true) {
        live_in_if_true(); // live
    }
}
