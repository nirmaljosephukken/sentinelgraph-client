# SentinelGraph: 3-minute demo video script

Target length 2:55 (hard limit 3:00). About 420 spoken words. Judging weight: technical merit and Gen AI
implementation (40%), so every section shows Gemini or Google Cloud doing real work.

## Before you press record (10 minutes)

1. Savanna console: workspace **Active**.
2. Warm up the cloud: on the live site, send one customer dispute and wait for **done** (this starts the worker
   and connects it to TigerGraph). Click **New chat** on Ask the agent. On Scenario lab, generate one world.
3. Browser full screen, zoom 90%. Tabs in this order:
   1. `https://sentinelgraph-539369061797.asia-south1.run.app/dispute`
   2. the same site, page **Live queue**
   3. the same site, page **Ask the agent**
   4. the same site, page **Scenario lab**
   5. Google Cloud console, **Cloud Run** service list (shows `sentinelgraph` and `sentinelgraph-worker`)
4. Notifications off. Recorder: OBS or Win+Alt+R. Mic on. Script on your phone.

---

## 0:00 to 0:15 · The problem (tab 1, page at the top)

> "A bank's fraud team gets thousands of alerts and complaints a day. Each one needs a decision the bank can defend to
> a customer and a regulator, and an AI that makes things up is worse than no AI at all."

## 0:15 to 0:30 · What SentinelGraph is (same tab)

> "This is SentinelGraph, a fraud investigator built with Google's Agent Development Kit and Gemini on Vertex AI,
> running on Cloud Run. Gemini reads, explains and chooses the tools. The decisions come from the bank's policy,
> written as code."

## 0:30 to 1:10 · A complaint in Hindi becomes a case (tab 1)

Customer **C07297**, example **Hindi** is already selected. Point at the message.

> "Here's a customer complaint in Hindi: a $482 purchase they say they never made."

Click **Read and send to the agent**. Point at step 1.

> "Gemini translates it and pulls out the claim and the amount. It never picks the transaction: code checks the amount
> is in the customer's own words, and the graph finds the payment."

Point at step 2, then step 3.

> "The alert goes onto a Pub/Sub stream. A private Cloud Run worker picks it up and investigates, and every stage is
> written to Firestore."

Wait for **done** on the status line.

> "About fifteen seconds later: fraud, ninety-nine percent. Block the card, which needs a team lead, and file a
> suspicious activity report, which needs a fraud manager."

## 1:10 to 1:30 · Why, and who approves (tab 2, Live queue)

Pick the Hindi alert under **Open an investigated alert**, scroll to the four cards, then to **Next best action**.

> "Every alert lands here, investigated. The agent shows what happened, what it found, how certain it is and what
> happens next, and every action has an approval route. Nothing that hurts a customer runs without a human."

## 1:30 to 2:05 · Ask the agent, with a guardrail (tab 3)

Type: *Pull up ALR-006. How are the connected cards linked, and is this a device-sharing ring?*

> "Analysts can ask the agent directly. Gemini decides which tools to call on TigerGraph."

Point at the answer as it appears.

> "Eleven other cards show the same pattern, but they're linked by amount and timing only. No shared device, so it
> isn't a ring. That sentence comes from the evidence, not from the model. A guardrail on every reply removes any
> claim or ID the graph doesn't support."

## 2:05 to 2:25 · Tested on data it has never seen (tab 4, Scenario lab)

Pick **Device ring**, set **Other cards involved** to 8, click **Generate world and investigate**.

> "Real alerts come from one dataset, so we test on new data. The lab builds a fresh synthetic bank, plants a pattern
> the agent can't see, and lets it investigate."

Point at **Agent concluded** and the green **matches what was planted** chip.

> "It found the ring and all eight cards. Across two hundred random worlds, fraud and harmless look-alikes, it matched
> every one."

## 2:25 to 2:45 · How it runs on Google Cloud (tab 5)

Show the two Cloud Run services.

> "Two Cloud Run services from one image: the console with the ADK agent, and a private worker behind Pub/Sub.
> Firestore holds the live queue, Secret Manager holds the graph credential, and every Gemini call goes through
> Vertex AI with a service account, so there's no API key in the cloud."

## 2:45 to 2:55 · Close (back to tab 1)

> "SentinelGraph: complaints in any language, investigated in seconds, decisions a bank can defend. The code and the
> live demo are linked below. Thanks for watching."

---

## If something goes wrong while recording

- **The alert stays "queued" for more than 30 seconds:** the worker is starting up. Keep talking over it; it is
  usually done within a minute. Warming up first (step 2 above) avoids this.
- **"TigerGraph unreachable" or empty pages:** Savanna went to sleep. Wake it in the Savanna console, wait a minute,
  reload. (The Scenario lab does not need Savanna.)
- **Running long:** cut the Live queue section (1:10 to 1:30) first, then shorten the Google Cloud section.
- **You stumble:** pause and repeat the sentence; trim it afterwards.
