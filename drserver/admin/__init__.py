"""Admin panel — HTTP server with web dashboard for server management.

Ported from the C# DR_Server's dr_admin.py + dashboard.html admin console.
Runs an HTTP server on a configurable port (default 8080) and serves a
browser-based admin panel.

Unlike the C# version (which communicated via SQLite bridge tables), this
admin panel runs in-process and calls server internals directly.
"""