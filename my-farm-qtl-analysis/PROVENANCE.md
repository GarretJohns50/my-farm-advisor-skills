# Import Provenance

## my-farm-qtl-analysis
- source_repo: https://github.com/GarretJohns50/my-farm-advisor-skills.git
- source_local_path: N/A
- source_ref: main
- source_commit: 45dcbf0a2b22eb50334c438211d81fe1ab876e1c
- source_status: clean remote ref
- source_path: my-farm-qtl-analysis/
- destination_path: my-farm-qtl-analysis/
- import_date: 2026-06-30
- exclusions: `.git/`; generated `examples/**/output/` artifacts; generated `scripts/output/` artifacts; no remote flattening of the local grouped example taxonomy; no unrelated files outside the imported qtl-analysis subtree snapshot
- local_modifications: Imported the local grouped example layout as the structural base, backfilled remote-only `README.md` and `scripts/qtl_cli.py`, merged richer remote SKILL sections without changing example-first behavior, normalized local-source path assumptions to `my-farm-qtl-analysis/`, preserved `scripts/verify_gpu_hpc.py` and `VISUALIZATION_SUMMARY.md`, and excluded generated outputs after asset audit.
- update_procedure: Run `git ls-remote https://github.com/GarretJohns50/my-farm-advisor-skills.git refs/heads/main`, confirm the SHA, clone or fetch the repo, copy only `my-farm-qtl-analysis/` into `my-farm-qtl-analysis/`, rerun `./scripts/validate.sh`, and refresh the provenance fields in the same commit.
