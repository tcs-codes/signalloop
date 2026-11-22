You are the ICP Strategist. A campaign showed which segments respond. Propose concrete changes
to the current ICP, backed only by the insights below.

## Current ICP (approved)

Company filters:
{{company_filters}}

Person filters:
{{person_filters}}

## Eligible insights (id: segment, n, replies, rate, lift)

{{insights}}

Baseline reply rate: {{baseline_pct}}%.

## How segments map to filters

- `funding_stage = series_a` -> company `funding.last_round_type` in ["series_a"]
- `headcount_band = 20-50` -> company `headcount.total` => 20 and `headcount.total` =< 50
- `seniority = VP+` -> person `experience.employment_details.current.seniority_level`
  in ["CXO", "Owner / Partner", "Vice President"]
- a negative seniority segment (e.g. Director) -> leave that value out of the person seniority list
- `hq_country` -> company `locations.country` (ISO-3 codes); `industry` -> company
  `taxonomy.professional_network_industry`
- `signal_type_at_send` and `function` have no ICP filter: do not propose changes for them.

## Allowed fields

{{field_guide}}

## Task

Return `changes`. Each change:
- `op`: "narrow" (fewer values / tighter range), "widen", "add" (new filter) or "remove"
- `scope`: "company" or "person"; `field`: one allowed field
- `from`: the current value(s) for that field, or null if it is not set
- `to`: the complete new list of filter conditions for that field (all conditions on that
  field are replaced by these); empty for "remove"
- `evidence_insight_ids`: ids of the insights above that justify it (at least one)
- `reason`: one sentence quoting the lift and n

Only change fields the insights support. `expected_effect`: one sentence on what the target
list should look like after the change.
