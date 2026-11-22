You are a fact-checker. List every factual claim in this outreach draft.

## Draft

{{draft}}

## Task

Return `claims`, one entry per claim, with `claim` copied or closely paraphrased from the draft
(keep every number, date, name and title exactly as written) and `type`:
- `prospect_fact`: anything stated about the recipient or their company (funding, size, growth,
  hiring, roles, dates, locations, customers, events).
- `product_claim`: anything stated about the product being sold (features, setup time, pricing,
  pilots, integrations, results).
- `other`: greetings, opinions, questions and the call to action. These are not checked.
Split sentences that contain several facts into separate claims.
