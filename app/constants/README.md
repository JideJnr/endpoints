# constants

## Purpose

This domain is the future home for shared constants — magic strings, numeric thresholds, and enum-like values that are currently scattered across multiple modules. Centralising them here eliminates duplication and makes global threshold changes a single-file edit.

## Member Modules

| Module | Responsibility |
|---|---|
| *(none yet)* | Constants will be extracted from existing modules as part of ongoing refactoring |

## Dependency Direction

**Depends on:** nothing — this sits at the bottom of the DAG alongside `config`

**Depended on by:** all domains (once constants are extracted and imports are updated)

## Notes

Until the extraction work is complete, constants remain inline in their originating modules. When extracting, prefer Python `enum.Enum` for categorical values and module-level `Final` typed variables for numeric thresholds. Do not put any logic or functions in this domain — pure data only.
