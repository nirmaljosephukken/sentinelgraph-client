# Known fraud patterns (bank typologies)

These are the patterns the bank's analysts recognize. **They are not the only patterns in the data.** Noticing activity that fits none of them, describing it in your own words, and recommending a defensible action is scored.

**1. Card testing.** A stolen card number is checked before use: three or more tiny online authorizations, often under $5, then a larger purchase. Confirmed by the sequence itself. Policy R5.

**2. Card-not-present fraud.** The number is used online without the card. Amounts and products that don't fit the cardholder's history, often in a burst of two to four within 48 hours. On its own, one unusual online purchase is ambiguous: verify. Policy R1 to R4.

**3. Card-not-present fraud from a new device.** Same as above, with the identity record marking the device as `New` for this account, sometimes behind a proxy. Stronger than pattern 2, still not proof: people buy new phones.

**4. Out-of-region use.** Card-present purchases in a billing region the cardholder has no history in, while their normal activity continues at home. Several days of purchases in one new region is a trip, not a clone. Policy R2, R3.

**5. Account takeover.** Mixed-channel activity inconsistent with the cardholder, often with device and match-flag anomalies, pointing to stolen credentials rather than a stolen number.

## Things to know

- **A risk score is a reason to look.** Never a verdict.
- **Half the cases are legitimate.** Many look suspicious. An agent that blocks everything scores badly.
- **The known patterns are not the only ones.** Some activity in this data fits none of the five. Noticing it and describing it in your own words is scored.
- **Devices and regions connect people.** A device profile or a billing region shared across many cards in a short window is worth a look. Some cases can only be solved by asking what happened on *other* cards.
- **The V, C, D, M and numeric id columns are real model features with no names.** You may use them as signals. Say so in your evidence rather than pretending to know what V127 means.
- **Customer and analyst replies are not provided.** If your agent asks the customer or requests step-up authentication, simulate the response in your own system and record what you assumed in `evidence_requests`.

## Glossary

| Term | Meaning here |
|---|---|
| **Risk score** | A number from 0 to 1 the bank's model attached to every transaction. High means "look at this." It is often wrong in both directions. Never treat it as the answer |
| **Closed case** | An investigation the bank already finished, July to October. Either confirmed fraud or cleared as a false alarm. The only place the truth is written down |
| **Case pack** | The 20 alerts you investigate, all from November and December. Your exam |
| **Trigger** | Why an alert exists: the model scored it high, a customer complained, or an analyst asked |
| **Pattern** | The kind of fraud. Five are documented below. Some in the data are not |
| **Channel** | `in_person` (product code W, no device record) or `online` (all other product codes, device record present) |
| **Identity record** | The device and connection details Vesta captured for online transactions: device type and model, OS, browser, screen, proxy flag, and encoded ratings |
| **Billing region** | `addr1`: an anonymized code for where the card is billed. `addr2` is the country code; 87 is the home country |
| **Exposure** | Total dollars in the fraud episode you identified |
| **Case** (as a deliverable) | The bank's internal record of your investigation. Part 1 of your answer |
| **SAR** | Suspicious Activity Report. The regulatory filing a bank must make for confirmed or strongly suspected fraud above certain thresholds. Part 2 of your answer, only when the policy calls for it |
| **Next best action** | What the bank should do now, and who has to approve it. Part 3 of your answer |
| **Approval route** | `auto` the agent may act alone; `L1` a team lead must approve; `L2` a fraud manager must approve |
| **MCP** | Model Context Protocol. How your agent calls TigerGraph as a set of tools |
| **GraphRAG** | Retrieving evidence from the graph and text from documents, and giving both to the LLM to reason over, instead of raw data |
