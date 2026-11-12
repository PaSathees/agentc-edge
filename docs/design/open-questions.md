# Open questions from requirements

[Design overview](../design.md)


The six questions left open at the Specify gate, and how the design settles each.

| Question | Answer | Still to do |
|---|---|---|
| Which grid emission factor for Sri Lanka? | An **average** grid factor (location-based Scope 2), not the combined margin used for carbon-credit projects. Source order: the national Energy Balance from the Sri Lanka Sustainable Energy Authority, else Ember. Stored as a `factors` row with publisher, year and page; the design holds the method, not the number | Owner reads the value from the primary publication and enters it. Published figures disagree, so a secondary site is not enough |
| Block or time-of-use tariff? | **Both.** A tariff is a `factors` set: block (rate per monthly unit band) or time-of-use (rate per period). Under block, `calc` prices a saving at the marginal band of the projected month. Under time-of-use, the solver also gets a cost per hour, so moving use out of the peak counts | The home is on block. With block pricing and one grid factor, timing changes neither cost nor carbon here, so savings come from fewer on-hours, not from shifting |
| First monthly goal? | November's measured kgCO2e minus 10%, as December's goal. The owner can edit it any time. Until it is set, goal status shows "no goal" and no drift events are raised | Set in December, once November is complete |
| ntfy: public or self-hosted? | **Self-hosted on the hub.** For instant iOS delivery the hub forwards only a poll request (message id and topic) to ntfy.sh; the phone fetches the text from the hub over Tailscale. The web inbox stays the main channel; ntfy is a shortcut | Verify on the iPhone (A-06), including a reported iOS 26 sound issue |
| How do phone buttons authenticate? | Each button URL carries a single-use token for that one proposal, version and action, valid until expiry; the request must also arrive over Tailscale ([guardrails](guardrails.md)) | Covered by the integration tests |
| Electricity Maps for Sri Lanka? | It covers Sri Lanka (zone `LK`) with hourly history, behind a sign-up; live data is a paid API. **Not used in v1**: the flat factor is the method. Hourly history may later show which hours are cleaner, as context only | Revisit if the grid factor method changes |
