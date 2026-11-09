// A method declared under `if false` is still callable: Rust impl blocks are
// non-local, so `S::run()` below reaches the method body. Its callee must stay
// in the graph even though the declaration sits in a dead branch.

struct S;

fn live_helper() {}

fn main() {
    if false {
        impl S {
            fn run() {
                live_helper();
            }
        }
    }
    S::run();
}
