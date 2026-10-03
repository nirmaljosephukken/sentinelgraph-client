# Regulatory guidance digest (for GraphRAG grounding)

Short, paraphrased digests of the public regulatory documents listed in the dataset README. Each
section names its source so the agent can cite it. These are summaries written for this project,
not quotations; consult the linked originals for authoritative text.

## FinCEN SAR Narrative Guidance: the five essential elements
Source: FinCEN, "Guidance on Preparing a Complete & Sufficient Suspicious Activity Report Narrative"
(https://www.fincen.gov/system/files/shared/sarnarrcompletguidfinal_112003.pdf) and SAR Narrative Guidance
(https://www.fincen.gov/system/files/shared/sar_guidance_narrative.pdf).
A SAR narrative must stand on its own and answer: WHO is conducting the suspicious activity (subjects,
accounts, cards, devices and identifiers); WHAT instruments or mechanisms were used (card-not-present
purchases, card-present purchases, authorisations); WHEN it occurred (first and last dates, time pattern);
WHERE it took place (channel, billing region, country, online vs in person); WHY the institution thinks it is
suspicious (deviation from the customer's known profile, red flags, typology match); and HOW it was carried
out (modus operandi: testing, structuring, shared device, account takeover). Narratives should be concise,
chronological, avoid unexplained jargon and internal codes, and state the total dollar amount involved.

## FinCEN SAR FAQs (October 2025)
Source: https://www.fincen.gov/system/files/2025-10/SAR-FAQs-October-2025.pdf
Institutions file when they know, suspect or have reason to suspect a transaction involves funds from illegal
activity or is designed to evade reporting requirements, subject to dollar thresholds. Structuring - breaking
activity into amounts designed to stay under a reporting or authorisation threshold - is itself reportable.
Continuing activity should be reviewed and reported on a periodic cycle. A SAR is supported by retained
documentation; the filing itself does not require proof of a crime.

## FinCEN SAR Supporting Documentation (FIN-2007-G003)
Source: https://www.fincen.gov/system/files/shared/fin-2007-g003.pdf
Supporting documentation (transaction records, device logs, customer statements, investigation notes) must
be identified in the narrative and retained for five years; it is provided to law enforcement on request.
This is why the agent writes every case, its evidence and its decisions into the graph as an audit record.

## FinCEN Advisory on Account Takeover Activity (FIN-2011-A016)
Source: https://www.fincen.gov/resources/advisories/fincen-advisory-fin-2011-a016
Account takeover occurs when a criminal obtains a customer's credentials and uses them to initiate
transactions. Red flags: access from new or unrecognised devices or IP addresses, use of anonymising proxies,
changes to contact details, activity inconsistent with the customer's history across channels, and multiple
accounts accessed from the same device. Institutions are asked to reference account takeover in SAR
narratives and to describe the device and access indicators.

## FinCEN Advisory on Imposter Scams and Money Mule Schemes (2020)
Source: https://www.fincen.gov/system/files/advisory/2020-07-07/Advisory_%20Imposter_and_Money_Mule_COVID_19_508_FINAL.pdf
Money mules move proceeds for fraud networks, often across many accounts controlled or recruited by one
group. Red flags include many unrelated customers sharing a device, email, or address; rapid movement of
similar amounts; and activity inconsistent with the customer's profile. Coordinated activity across customers
warrants a SAR even when each individual amount is small.

## FinCEN Identity-Related Suspicious Activity (2021 Financial Trend Analysis)
Source: https://www.fincen.gov/system/files/shared/FTA_Identity_Final508.pdf
Identity-related SARs dominate reporting. The most common typologies are impersonation, account takeover
and use of compromised credentials, frequently detected through device and online-access indicators
(new devices, proxies, mismatched identity attributes). Shared identifiers across customers are a primary
signal of organised identity fraud.

## SAR Activity Review: Trends, Tips and Issues
Source: https://www.fincen.gov/sites/default/files/sar_report/sar_tti_19.pdf
Law enforcement values SARs that link subjects across filings (shared devices, emails, accounts). Filers
should name every connected account or identifier they found, because these links let investigators
aggregate small, individually unremarkable reports into a case.

## FATF - Illicit Financial Flows from Cyber-Enabled Fraud (2023)
Source: https://www.fatf-gafi.org/content/dam/fatf-gafi/reports/Illicit-financial-flows-cyber-enabled-fraud.pdf.coredownload.inline.pdf
Cyber-enabled fraud is organised, transnational and fast: proceeds are dispersed within hours. Recommended
practice: real-time detection, network (graph) analytics to connect victims and mule accounts, public-private
information sharing, and early intervention (holds, step-up authentication) before funds leave.

## FATF - Money Laundering Using New Payment Methods
Source: https://www.fatf-gafi.org/en/publications/Methodsandtrends/Reportonnewpaymentmethods.html
Card-not-present and online payment channels allow non-face-to-face use of stolen card data; risk increases
with anonymity (proxies, disposable emails), high velocity and cross-border use.

## FATF - Professional Money Laundering
Source: https://www.fatf-gafi.org/en/publications/Methodsandtrends/Professional-money-laundering.html
Professional networks reuse infrastructure (devices, accounts, identities) across many clients. Reuse of the
same technical infrastructure across unrelated customers is a strong indicator of a coordinated operation.

## FFIEC BSA/AML Manual - Red Flags (Appendix F) and Suspicious Activity Reporting
Source: https://bsaaml.ffiec.gov/manual/Appendices/07 and https://bsaaml.ffiec.gov/manual/AssessingComplianceWithBSARegulatoryRequirements/04
Red flags include transactions structured to avoid thresholds, activity inconsistent with the customer's
business or history, many accounts sharing a common identifier, and unexplained high-velocity activity. A
SAR decision must be documented, including decisions not to file. Banks must maintain an audit trail of
alerts, investigation steps, decisions and approvals.

## OFAC SDN list
Source: https://www.treasury.gov/ofac/downloads/sdnlist.pdf
Screening against the Specially Designated Nationals list applies to named parties. This dataset is
anonymised and contains no names, so screening is not applicable; the agent records "not applicable" rather
than implying a screen was performed.
