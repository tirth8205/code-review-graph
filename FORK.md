# Fork integration

`main` mirrors upstream. Merge the upstream tip into a dedicated candidate
based on `dc-use`, and merge that candidate through a reviewed PR. Never
rewrite either integration branch. The package in this candidate is
`2.4.0+dc.2`; a host-tools pin may refer only to the published release SHA.
The usage guide, feature history, and upstream Action pins refer to the
upstream base release. The fork package version belongs in its package
metadata and bug-report form. Publish fork assets from a matching tag on
`dc-use` (for example, `v2.4.0-dc.2` for `2.4.0+dc.2`), following the
provenance checks in `release-assets.yml`.

Upstream owns graph storage, schema migration, worktree and shared HTTP
serving, the watcher lifecycle, and documentary skill generation. The fork
retains its host-facing watch manifest, repository daemon settings, bundle
store and prune surface, caller-specified review bounds, lean MCP tool
selection, and release assets. Generation is `make gen`; lock upgrades are
`make upg`; Ruff, Pyrefly, and upstream mypy run through `make lint`,
`make types`, and `make mypy` respectively.
