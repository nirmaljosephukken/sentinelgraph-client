# Lessons mined from the bank's closed cases (Jul-Oct 2016)

Derived by the agent's memory builder from closed_cases_history.csv (5,565 cases). These are
statistical facts about the bank's own history, used to set priors and to recognise recurring schemes.

## Who raises an alert matters
- Every confirmed-fraud case (4,665) began with the cardholder reporting unrecognised activity.
- Every cleared case (900) began with a model alert (risk score 0.8-0.95) that the cardholder confirmed:
  716 confirmed travel to the billing region, 158 confirmed a purchase from a new phone, 26 confirmed an
  unusual but intended purchase. A high risk score on its own has historically been a false alarm.
- Consequence for the agent: a customer denial is strong evidence; a risk score alone is weak evidence and
  policy R1 (verify before blocking) applies.

## Reporting practice
- 397 of 4,665 confirmed cases had a SAR filed. 99% of filed SARs had exposure above $1,000 or a
  connected-card link; no confirmed case above $1,000 went unreported (largest unreported exposure: $999.95).
- All nine `undocumented` cases were reported.

## Undocumented scheme 1: shared-device ring
Cases CC-2649, CC-2971, CC-2985, CC-3035 (Aug-Sep 2016). Online purchases from one device profile
(SAMSUNG SM-G935F, Android 7.0, Chrome for Android, 1920x1080) behind an anonymous proxy, marked New on each
of 20+ unrelated cards, amounts $35-$260. Every case lists the other cards as connected cards.

## Undocumented scheme 2: threshold structuring
Cases CC-3748, CC-3841, CC-3907, CC-4086, CC-4124 (Sep 2016). Four online purchases within about forty
minutes, each just under $500 (roughly $455-$499), totalling about $1,900 per card. Amounts appear chosen to
stay under a $500 authorisation threshold. Different cards, same modus operandi, same time-of-hour start.

## Card testing
16 cases. Long runs of small online authorisations on one card followed by larger purchases, often spread
over days. Exposure $110-$4,900; reported when above $1,000.

## Out-of-region use
955 cases. Usually one or two card-present purchases in a region the cardholder never used, while home
activity continues. Cleared counterpart: several days of purchases in one new region = a trip.
