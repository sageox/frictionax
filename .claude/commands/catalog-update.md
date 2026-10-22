# /catalog-update - Analyze friction and update the catalog

Superseded by the `friction-analysis` skill, which also finds the friction the
catalog cannot fix (stale hints, word-split arguments, flag drift, unsafe fuzzy
matches) and renders a report.

Read `skills/friction-analysis/SKILL.md` and follow it. Pass any arguments given
to this command (export path, CLI name, binary) through to the skill's pipeline.
