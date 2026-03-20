# Changelog

## 0.2.0 — 2026-03-20

### Added
- **Query Graph** command with 8 query patterns (callers_of, callees_of, imports_of, etc.)
- **Find Callees** command to trace all functions called by a target
- **Find Large Functions** command to identify oversized functions/classes
- **Compute Embeddings** command to generate vector embeddings
- **Watch Mode** command for continuous graph updates
- Cursor-aware resolution for blast radius and navigation commands
- Fuzzy fallback search when exact node matches fail
- SCM decorations for git-aware file status

### Changed
- Updated README with complete command table (13 commands)
- All 13 commands now documented

## 0.1.1 — 2026-03-17

### Fixed
- CLI path setting scoped to `machine` level (security fix)
- Secure nonce generation using `crypto.randomBytes()`

## 0.1.0 — 2026-03-17

Initial release.

- Code Graph tree view with file, class, function, type, and test nodes
- Interactive D3.js graph visualisation in a webview panel
- Blast radius analysis from cursor position
- Find callers and find tests commands
- Search across all graph nodes
- Review changes with git-aware impact analysis
- Auto-update graph on file save
- CLI auto-detection and guided installation
- Getting Started walkthrough
