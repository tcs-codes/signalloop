You are tuning a company search so it returns a workable target list.

## Current company filters (index: filter)

{{filters}}

## Result

The search matched **{{count}}** companies. The target range is {{target_min}}–{{target_max}}.
Direction needed: **{{direction}}**.

## Changes already tried (do not repeat them)

{{history}}

## Allowed fields

{{field_guide}}

## Task

Propose exactly ONE change that moves the count toward the target range:
- To loosen: remove the most restrictive filter, widen a numeric bound, or add values to an `in` list.
- To tighten: add one filter (for example 6-month headcount growth `=>` 10, a recent
  `funding.last_fundraise_date`), narrow a numeric bound, or remove values from an `in` list.

Return:
- `direction`: "{{direction}}"
- `action`: "add", "remove" or "replace"
- `index`: the filter index to remove or replace (omit for "add")
- `condition`: the new filter for "add" / "replace" (omit for "remove"), using the allowed
  fields and operators `in`, `not_in`, `=`, `>`, `<`, `=>`, `=<`, `between`
- `reason`: one sentence
