# Import Provenance

## my-farm-breeding-trial-management
- source_repo: https://github.com/GarretJohns50/my-farm-advisor-skills.git
- source_local_path: N/A
- source_ref: main
- source_commit: 45dcbf0a2b22eb50334c438211d81fe1ab876e1c
- source_status: clean remote ref
- source_path: my-farm-breeding-trial-management/
- destination_path: my-farm-breeding-trial-management/
- import_date: 2026-06-30
- exclusions: `.git/`; any unrelated scientific skills outside the imported breeding-trial-management subtree; remote flat-layout paths that would overwrite the grouped local example taxonomy
- local_modifications: Copied the local untracked skill tree as the structural base; added root `README.md` from the remote skill with grouped-path adjustments; backfilled remote-only `scripts/breeding_cli.py` and `examples/field-trial-placement/`; merged remote unified-CLI/tool-selection documentation into local `SKILL.md` while preserving local grouped examples and local `references/bms-api.md` + `references/breedbase-api.md`.
- update_procedure: Run `git ls-remote https://github.com/GarretJohns50/my-farm-advisor-skills.git refs/heads/main`, confirm the SHA, clone or fetch the repo, copy only `my-farm-breeding-trial-management/` into `my-farm-breeding-trial-management/`, rerun `./scripts/validate.sh`, and refresh the provenance fields in the same commit.
