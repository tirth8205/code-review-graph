# Security / Data / Ops Axis — Sub-Agent Brief

Review the diff through the lens of security, persistence, operational safety, and failure behavior. This axis answers:
**does the change preserve trust boundaries, data integrity, and production operability?**

You receive this brief alongside `shared-rules.md`, which contains diff scope discipline, finding sizing, output
format, and boundaries. Follow both documents.

## Standards First

Use the matched standards supplied by the orchestrator as the source of truth. In Zazz repos, those standards are
selected from `<DOCS_ROOT>/standards/index.yaml` by changed path and activity.

Security/data/ops-relevant standards commonly include auth/authz, HTTP errors, data-layer behavior, stored procedures,
migrations, CI/deploy, logging, observability, secrets, and operational runbooks. If no matching standard exists,
review with general engineering judgment and state the standards gap as residual risk.

## Review Focus

Look for realistic defects in changed code:

- missing or weakened authentication, authorization, permission checks, tenant boundaries, or object ownership checks
- secrets, credentials, tokens, connection strings, private keys, or sensitive values committed to source, tests, docs,
  logs, snapshots, or examples
- injection risks in SQL, shell commands, templates, file paths, URLs, redirects, object storage keys, or dynamic imports
- unsafe persistence behavior: missing transactions, non-idempotent retries, destructive migrations, rollback hazards,
  concurrency races, or schema/data incompatibility
- weak error handling: swallowed failures, broad catches that hide root cause, internals leaked to clients, missing
  return-code handling, or inconsistent error envelopes
- operational blind spots: missing logging for failure paths, noisy logs that leak data, missing metrics around new
  background work, or changes that make incidents harder to diagnose
- deployment and CI hazards: workflow permission expansion, unpinned or overly broad credentials, generated artifacts
  out of sync, cache poisoning, or environment-only behavior

Size by practical blast radius. A changed admin-only path can still be a `[rock]` when it bypasses authorization,
corrupts data, or leaks sensitive information.

## Evidence

For each finding, cite the changed file and line, name the matched standard when one governs the issue, and explain the
runtime path that makes the risk real. Prefer concrete failure examples over hypothetical language.

## Data Contract Seams

When reviewing persistence changes, trace contracts across the SQL, data wrapper, service layer, and tests. Pay special
attention to seams where a local layer can look valid while the composed behavior is broken:

- stored procedure return codes and `@ErrorMessage` output values;
- result-set column names and order consumed by Python wrappers;
- tSQLt temp-table schemas used with `INSERT ... EXEC`;
- service-layer exception translation and whether wrapper results are consumed or discarded;
- seed grants, permission constants, and route decorators that must match exactly.

Escalate cross-seam drift to at least `[rock]` when it can produce data corruption, unclassifiable return codes,
mis-mapped rows, broken HTTP contracts, or false-positive tests.

If the diff does not touch a security/data/ops-sensitive surface, say "No findings in the reviewed security/data/ops
surface" and list any residual risk, such as standards not available or checks not run.
