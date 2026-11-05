# ADR 0005: Local service and operational boundaries

Status: adopted.

Use PostgreSQL, service-owned immutable files and a loopback-only FastAPI service with a plain JavaScript client. Viewer and operator tokens carry explicit dataset scopes. The runtime role cannot import sources or publish snapshots.

Retain published reports and snapshots. Use pointer compare-and-swap for conflicting publications and new interpretation generations for parser repairs. Source-use checks apply to historical reads and exports.

Bound request bodies, evidence, report documents, synchronous work and background jobs. Hold durable pins until the operation stops. Preserve committed success when only cleanup fails.

Backups combine an exported PostgreSQL snapshot with verified immutable object copies. Restore creates fresh targets and checks closure and retained report digests. Data backups exclude credentials. Off-machine copies, fresh-cluster provisioning and public workload capacity require separate operational configuration.

The deterministic fact renderer validates typed assertions against retained reports. It does not connect to an external generation service.
