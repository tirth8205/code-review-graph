# vhs tape: context_savings demo
# Render with: vhs diagrams/context-savings-demo.tape
# Output: diagrams/context-savings-demo.gif
#
# Demos the two CLI surfaces that expose the new estimated context_savings
# metric (v2.3.4+), and the --verify flag that cross-checks against tiktoken.
# Runs against the flask test repo at SHA a29f88ce6f2f.

Output diagrams/context-savings-demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 760
Set Theme "Dracula"
Set Padding 24
Set TypingSpeed 40ms
Set PlaybackSpeed 1.0

# --- silent setup ---
Hide
Type `cd evaluate/test_repos/flask`
Enter
Type `clear`
Enter
Show

Type `# code-review-graph — estimated context savings (v2.3.4)`
Enter
Sleep 700ms
Type `# flask @ a29f88ce — 4-file docs commit`
Enter
Sleep 700ms

Type `git diff --stat HEAD~1 | head -6`
Enter
Sleep 1800ms

Type `# Surface 1: detect-changes --brief`
Enter
Sleep 400ms
Type `uv run --project ../../.. code-review-graph detect-changes --brief 2>/dev/null`
Enter
Sleep 4000ms

Type `# Surface 2: update --brief (same brief summary after incremental update)`
Enter
Sleep 400ms
Type `uv run --project ../../.. code-review-graph update --brief 2>/dev/null`
Enter
Sleep 4500ms

Type `# Prove the estimate is real with --verify (cross-checks tiktoken cl100k_base)`
Enter
Sleep 400ms
Type `uv run --project ../../.. code-review-graph detect-changes --brief --verify 2>/dev/null`
Enter
Sleep 5500ms

Type `# Estimate within 1 percentage point of GPT-4 ground truth.`
Enter
Sleep 2000ms
