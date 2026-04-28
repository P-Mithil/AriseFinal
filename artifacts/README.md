## Artifacts Folder

Use this folder for local archival if you want to move heavy generated/debug files out of repo root.

Recommended subfolders:

- `artifacts/debug/` - copied strict/debug json/txt snapshots
- `artifacts/logs/` - copied ad-hoc run logs (`*.log`, `gen_out*.txt`, etc.)
- `artifacts/exports/` - copied generated Excel/CSV output bundles for handover

This project currently keeps operational outputs in `DATA/OUTPUT`, `DATA/DEBUG`, and `DATA/EDITED OUTPUT`.
The `artifacts/` folder is an optional clean organization layer for maintainers.
