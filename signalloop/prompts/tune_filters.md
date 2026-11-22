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

Filters **{{locked}}** are the ICP the user approved.

Propose exactly ONE change that moves the count toward the target range:
- To loosen: remove or widen the most restrictive filter, or add values to an `in` list.
- To tighten: NEVER change, remove or override the approved filters ({{locked}}), and do not
  add a filter on a field they already use. Instead add one new filter on another field, for
  example 6-month headcount growth `=>` 10, a recent `funding.last_fundraise_date`, or a
  minimum `funding.last_round_amount_usd`. You may also tighten a filter you added earlier
  (an index after the approved ones).

Return:
- `direction`: "{{direction}}"
- `action`: "add", "remove" or "replace"
- `index`: the filter index to remove or replace (omit for "add")
- `condition`: the new filter for "add" / "replace" (omit for "remove"), using the allowed
  fields and operators `in`, `not_in`, `=`, `>`, `<`, `=>`, `=<`, `between`
- `reason`: one sentence
