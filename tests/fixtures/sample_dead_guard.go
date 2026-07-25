package main

// Fixture for testing Go dead-guard detection on CALLS edges.
//
// Go's if_statement shares the same tree-sitter node type as Python's,
// and Go's `false` literal shares the same node type.  The existing
// _eval_static_dead_cond already handles cond.type == "false", so Go
// dead guards are detected by the same code path as Python.
//
// Patterns tested:
//   if false { dead() }           -- consequence is dead
//   if false { } else { live() }  -- else branch is live

func live_helper() {}

func dead_false_call() {}

func live_in_else() {}

func dead_in_consequence() {}

func live_final_else() {}

func live_in_wrapped() {}

func caller() {
	live_helper() // live -- no guard

	if false {
		dead_false_call() // dead consequence
	}
}

func else_branch() {
	// Calls in the else branch of if false are live.
	if false {
		dead_in_consequence() // dead consequence
	} else {
		live_in_else() // live -- else branch
	}
}

func dead_wrapped_func() {
	// A whole function definition is NOT inside if false in Go
	// (Go forbids func declarations inside if blocks), so this
	// call stays live -- it is at module scope.
	live_in_wrapped() // live -- func def is at module scope, not guarded
}

func some_condition() bool {
	return true
}

func elif_chain() {
	// Go has no elif, but chained if-else-if achieves the same.
	// Only the if-false consequence is dead; the else branch is live.
	if false {
		dead_in_consequence() // dead
	} else {
		if some_condition() {
			live_final_else() // live
		}
	}
}

func live_in_if_true() {}

func true_guard() {
	// if true is NOT a dead guard -- the consequence is live.
	if true {
		live_in_if_true() // live -- true is not a dead guard
	}
}
