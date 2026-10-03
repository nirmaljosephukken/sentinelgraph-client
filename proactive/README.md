# Proactive monitoring (beyond the 20 benchmark cases)

`python monitor.py --investigate 6` scans the whole exam period (Nov–Dec 2016) in TigerGraph without any alert, then investigates what it finds end to end with the same agent (trigger = analyst_request). Every case is written to the graph (`CASE-2016-PRO-xxx`).

## What the scans found
* **Device-sharing rings** (`device_ring_scan`): 57 specific device profiles used on 3+ cards with proxy/New-device indicators or high memory-model scores. The top 10:

| Device profile | Cards | Via proxy | Marked New | Mean memory-model score |
|---|---|---|---|---|
| `SM-G935F Build/NRD90M \| Android 7.0 \| chrome 62.0 for android \| 1920x1080` | 28 | 100% | 100% | 0.10 |
| `SM-A300H Build/LRX22G \|  \| chrome 65.0 for android \|` | 18 | 0% | 32% | 0.72 |
| `LG-D320 Build/KOT49I.V10a \|  \| chrome generic \|` | 22 | 0% | 0% | 0.63 |
| `MotoG3 Build/MPIS24.65-33.1-2-16 \|  \| chrome 66.0 for android \|` | 17 | 0% | 15% | 0.67 |
| `\|  \| edge 16.0 \|` | 11 | 17% | 25% | 0.67 |
| `FRD-L09 Build/HUAWEIFRD-L09 \| Android 7.0 \| chrome 66.0 for android \| 1920x1080` | 8 | 0% | 0% | 0.91 |
| `M4 SS4456 Build/LMY47V \|  \| chrome 66.0 for android \|` | 9 | 0% | 24% | 0.59 |
| `Windows \| other \| chrome 61.0 \| 1280x720` | 5 | 0% | 80% | 0.95 |
| `GT-I9060M Build/KTU84P \|  \| chrome 65.0 for android \|` | 9 | 0% | 45% | 0.52 |
| `Moto G (4) Build/NPJS25.93-14-8.1-4 \|  \| chrome generic \|` | 7 | 0% | 0% | 0.81 |

* **Threshold structuring** (`amount_band_scan`): 12 cards with ≥3 varied just-under-$500 online purchases within an hour: C05851-K1, C10990-K1, C00466-K1, C07297-K1, C01890-K1, C10751-K1, C03633-K1, C12641-K2, C02265-K1, C05423-K1, C05766-K1, C06881-K1. This is the same scheme as ALR-006.
* **WCC components** (`ring_components`, weakly connected components over the client↔device co-usage graph): 188 components with 4+ cards and at most 3 devices. The SM-G935F ring appears as one 28-card component.

## Investigations opened by the agent

| Case | Verdict | P(fraud) | Pattern | Connected cards | Shared device | SAR |
|---|---|---|---|---|---|---|
|  PRO-001 | fraud | 0.90 | undocumented | 19 | SM-G935F Build/NRD90M \| Android 7.0 \| chrome 62.0 for android \| 1920x1 | yes |
|  PRO-002 | fraud | 0.99 | card_not_present_new_device | 21 | SM-A300H Build/LRX22G \|  \| chrome 65.0 for android \| | yes |
|  PRO-003 | fraud | 0.99 | card_not_present_new_device | 32 | LG-D320 Build/KOT49I.V10a \|  \| chrome generic \| | yes |
|  PRO-004 | fraud | 0.99 | card_not_present_new_device | 9 | MotoG3 Build/MPIS24.65-33.1-2-16 \|  \| chrome 66.0 for android \| | yes |
|  PRO-005 | fraud | 0.97 | undocumented | 11 |  | yes |
|  PRO-006 | fraud | 0.98 | undocumented | 11 |  | yes |

Full answer files: [`cases/`](cases/). Raw scan output: [`alerts.json`](alerts.json).
