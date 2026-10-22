# /catalog-update - Analyze friction and update the catalog

Superseded by the `friction-analysis` skill, which also finds the friction the
catalog cannot fix (stale hints, word-split arguments, flag drift, unsafe fuzzy
matches) and renders a report.

1. Read `skills/friction-analysis/SKILL.md` and run its pipeline, passing any
   arguments given to this command (export path, CLI name, binary, catalog).
2. Show the user the proposed catalog changes and wait for their approval.
3. Apply the approved changes to the catalog with the skill's apply step
   (`--stages apply --apply-catalog <catalog path>`), then run the CLI's tests.
