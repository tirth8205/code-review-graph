// Fixture for Rust dead-guard detection on CALLS edges.
//
// Rust's if is an if_expression, not an if_statement, and its false
// literal is a boolean_literal, not the bare false node Go and TS use.
// The else branch and if true stay live.

fn live_helper() {}

fn dead_false_call() {}

fn live_in_else() {}

fn dead_in_consequence() {}

fn live_final_else() {}

fn some_condition() -> bool {
    true
}

fn caller() {
    live_helper(); // live -- no guard

    if false {
        dead_false_call(); // dead consequence
    }
}

fn else_branch() {
    if false {
        dead_in_consequence(); // dead consequence
    } else {
        live_in_else(); // live -- else branch
    }
}

fn elif_chain() {
    if false {
        dead_in_consequence(); // dead
    } else if some_condition() {
        live_final_else(); // live
    }
}

fn live_in_if_true() {}

fn true_guard() {
    if true {
        live_in_if_true(); // live -- true is not a dead guard
    }
}
