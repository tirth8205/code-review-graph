/* Fixture for testing C/C++ dead-guard detection on CALLS edges.
 *
 * #if 0 / #elif 0 blocks are dead code -- calls inside them should be
 * omitted, even when a function definition sits inside the block.
 * #else and #elif branches of #if 0 are live -- their calls are kept.
 */

extern void live_helper(void);
extern void dead_in_if0(void);
extern void live_in_else(void);
extern void dead_in_elifblock(void);
extern void live_in_elif(void);
extern void dead_in_wrapped(void);
extern void live_in_if1(void);
extern void dead_in_elif0(void);

/* #if 0 wrapping a whole function: the preprocessor removes the
 * function entirely, so the call inside it is dead too. */
#if 0
void dead_wrapped_func(void) {
    dead_in_wrapped();   /* dead -- function is inside #if 0 */
}
#endif

void caller(void) {
    live_helper();       /* live -- no guard */

#if 0
    dead_in_if0();       /* dead -- inside #if 0 */
#else
    live_in_else();      /* live -- #else of #if 0 */
#endif

#if 0
    dead_in_elifblock(); /* dead -- inside #if 0 (elif form) */
#elif 1
    live_in_elif();      /* live -- #elif of #if 0 (regression guard) */
#endif

#if 1
    live_in_if1();       /* live -- #if 1 is taken */
#elif 0
    dead_in_elif0();     /* dead -- inside #elif 0 */
#endif
}
