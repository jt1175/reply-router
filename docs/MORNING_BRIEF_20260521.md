# Morning Brief — 2026-05-21 (CFS launch prep)

JT — here's what I got done overnight + what's left for you. Read top-down, knock through in order.

---

## ✅ What I finished overnight

**Smartlead campaign 3368966 ("CFS Cohort 1 — Cold Outreach") is fully built but NOT activated.** What's wired:

- **Schedule:** Mon–Fri, 08:00–17:00 Central, 12-min gap between sends, max 75 new leads/day
- **Settings:** stop-on-reply, plain-text mode, open-tracking OFF (cold inbox hygiene), 40% follow-up percentage
- **Mailboxes:** all 15 of your warmed mailboxes attached (3 personas × 5 domains, all at 100% warmup)
- **Sequence:** 4 touches with delays 0d / +3d / +7d / +14d. Bodies use the 8 engine merge vars **plus** Smartlead built-ins `{{sender_first_name}}` and `{{signature}}` so each persona signs their own email (Sarah / Mike / Jessica), not Shawn. Shawn surfaces only at the calendar-pick step after qualification. (First upload had hardcoded "— Shawn" + manual footer; corrected at ~2:30 AM, see `docs/smartlead_sequence_templates.md`.)
- **Webhook:** id=596722 registered → `reply-router.vercel.app/v1/clients/clear_facility/replies` with all 8 reply categories
- **reply-router config:** `campaign_ids: ["3368966"]` wired + committed + pushed (commit `32fc452`)
- **reply-router booking_link:** flipped off `PLACEHOLDER` → qualify endpoint (commit `102a105`). Safe: `interested`/`info_request`/`objection` still gated by `auto_send: false` in routing config, so still go to approval UI. Only `unsubscribe` + `out_of_office` auto-send, which are safe templated responses.

**GHL custom field audit:** all 20 field IDs in `clients/clear_facility.json` confirmed live in your CFS sub-account. No drift.

**Endpoint smoke tests passed:**
- `GET /v1/health` → 200
- `GET /v1/clients/clear_facility/qualify/<contact_id>?token=invalid` → 403 with branded error page (CSRF working)

**Top-scorers report (partial):** 8 winners ≥7 from first 277/500 rows. See `reports/cohort_1_top_winners_20260521.csv` and `.md`. The remaining ~223 rows are still scoring at ~32 rows/hr — should finish by ~9 AM.

---

## ⚠️ What I couldn't finish (you do this in the morning)

### 1. GHL pipeline drag-reorder (20 sec) — you must do this in the UI

The new stages I created sit at the bottom. Reorder to final target:

| Position | Stage Name (drag-target order) |
|---|---|
| 0 | Outreach |
| 1 | **Replied** |
| 2 | **Call Scheduled** ← drag from current pos 7 |
| 3 | Walkthrough Scheduled |
| 4 | **Walkthrough Done** ← drag from current pos 8 |
| 5 | Proposal Sent |
| 6 | Nurture |
| 7 | Closed Won |
| 8 | Closed Lost |

URL: `https://app.gohighlevel.com/v2/location/fMmu5p7WaIabhwXYkdDn/opportunities/pipeline/WaNo1BZftVUmpieCPweb?tab=stages`

I couldn't programmatically drag — GHL's drag library uses real pointer events that JS `DragEvent` dispatches don't trigger. The Chromium window is still open with your GHL session; you can do this in 20 sec.

### 2. GHL workflow webhook for stage-change sync (5 min) — you must do this in the UI

I tried automating this via Playwright but GHL's onboarding modals (phone-number setup, AI Builder upsell) kept hijacking the page and the workflow builder's deep-virtualized DOM resisted button-finding. So:

**Steps:**
1. Go to `https://app.gohighlevel.com/v2/location/fMmu5p7WaIabhwXYkdDn/automation/workflows`
2. Click **Create Workflow** → **Start from scratch**
3. Name it: `reply-router stage-change sync`
4. Add Trigger: **Opportunity Status Changed** (some versions call it "Pipeline Stage Changed")
   - Pipeline: **CFS Cohort 1** (or whatever your main pipeline is)
   - In Stage: leave blank (we filter server-side)
   - From Stage: leave blank
5. Add Action: **Webhook**
   - Method: **POST**
   - URL: `https://reply-router.vercel.app/v1/clients/clear_facility/ghl-stage-change?secret=<paste CFS_ROUTER_SECRET from your .env>`
   - Headers: leave default (Content-Type: application/json)
   - Body (JSON):
     ```json
     {
       "contactId": "{{contact.id}}",
       "opportunityId": "{{opportunity.id}}",
       "currentStage": "{{opportunity.pipeline_stage_id}}",
       "previousStage": "{{opportunity.previous_pipeline_stage_id}}",
       "locationId": "{{location.id}}"
     }
     ```
6. **Save** → **Publish**

This is **NOT a launch blocker** — first launch can ship without it. It only matters when you start manually moving opps to "Closed Won/Lost" and want Smartlead sequences to auto-pause. Even without it, the system works.

### 3. Fix 3 Jessica Martin mailbox display names (90 sec) — Smartlead UI

Smartlead's API requires a password to update OAuth-connected mailboxes, so I couldn't patch these programmatically. In Smartlead → Settings → Email Accounts, click each of these and update **Display Name** from `jessica martin` → `Jessica Martin`:

- `jessica.martin@clearfacilitymn.com` (id 18607413)
- `jessica.martin@getclearfacilityservices.com` (id 18606893)
- `jessica.martin@tryclearfacilityservices.com` (id 18606777)

Otherwise `{{sender_first_name}}` will render lowercase `jessica` for those 3 mailboxes' sends.

### 4. Apollo Contacts export → merge → personalize → import → activate (~45 min)

This is the only path-to-live activity.

**A. Apollo export (5 min):**
Open Apollo. From the 8 winners (and optionally the 10 bubble-score-6 companies if you want to lean wider), do a Contacts export. Filter by company name. Take one decision-maker per company (Director of Facilities / Office Manager / VP Operations / similar). Save as:
```
data/clear_facility/cohort_1_contacts_apollo_raw.csv
```

Required columns (Apollo's defaults match): First Name, Last Name, Email, Title, Company.

**B. Merge contacts into scored data (1 min):**
```bash
cd /Users/selene/Documents/Code/KSquared/intent-signal-engine
python scripts/merge_apollo_contacts.py \
  data/clear_facility/reports/cohort_1_top_winners_20260521.csv \
  data/clear_facility/cohort_1_contacts_apollo_raw.csv \
  data/clear_facility/cohort_1_winners_with_contacts_20260521.csv
```

**C. Personalize (~10 min for ~20 rows):**
```bash
python orchestrator.py \
  --config config/clear_facility.json \
  --input data/clear_facility/cohort_1_winners_with_contacts_20260521.csv \
  --output data/clear_facility/cohort_1_winners_personalized_20260521.csv \
  --resume --skip-signals --skip-scoring
```

The `--skip-signals --skip-scoring` flags skip the expensive Perplexity/Google/Claude scoring (already done) and just run touch-1 personalization + 3-touch sequence generation on the rows that now have contact info.

**D. Import to Smartlead campaign 3368966 (5 min):**
1. Open campaign `CFS Cohort 1 — Cold Outreach` in Smartlead UI
2. Leads tab → Import CSV
3. Upload `cohort_1_winners_personalized_20260521.csv`
4. **Critical:** verify Smartlead recognizes all 8 merge vars: `personalized_subject`, `personalized_line`, `subject_2`, `line_2`, `subject_3`, `line_3`, `subject_4`, `line_4`. If any show as unrecognized, the CSV column header doesn't match (case-sensitive).

**E. Test-send before activation (5 min):**
1. Add yourself to the campaign as a test lead
2. Smartlead: send test of touch #1 from **at least one Sarah, one Mike, and one Jessica mailbox** (sequentially) to confirm persona rotation works
3. Confirm: subject + body render correctly, no literal `{{var}}` strings visible, `{{sender_first_name}}` matches the sending mailbox, `{{signature}}` block at bottom shows that persona's name + address

**F. Activate.**

---

## ❓ What I need you to ask Shawn (Shawn-loop work this week)

These are real things to confirm. None block the cohort 1 launch, but all must land before week 2.

### A. business_context confirmation (highest priority)

Email already drafted at `docs/client_communications/2026-05-20_shawn_email_draft.md`. Send it. Specifically ask Shawn to confirm or replace:

1. **Phone** + **Address** (currently `JT_DRAFT_2026-05-20_PHONE_TBD_FROM_SHAWN` / `_ADDRESS_TBD_FROM_SHAWN`). Without these the AI responder can't accurately surface "we're at X" when prospects ask. Replace those values in `clients/clear_facility.json`.

2. **Credential mentions** — all 4 are marked `JT_DRAFT_AWAITING_SHAWN_CONFIRM` (issa-cert, insurance, background-checks, local owner-operator). The responder **strips any field still starting with `AWAITING_`** from the prompt, so until Shawn confirms each, the AI won't surface any credentials at all. Each item Shawn confirms → drop the `AWAITING_` prefix in the JSON.

3. **Tagline** (current draft: "Twin Cities owner-operated commercial cleaning — built for facility managers who care about consistency") + **value props** + **objection responses** — all `JT_DRAFT_` for him to confirm.

4. **Pricing rule:** confirm the system NEVER quotes a `$` figure even when asked. Current response: "Pricing depends on square footage, frequency, and facility type — happy to put a number together after a 10-min walkthrough."

### B. Calendar integration

The Discovery Call calendar (id `4vsUpwgKhxY9XyMixhYc`) is currently connected to **your** Google Calendar as a stand-in. **Before Shawn books his first qualified prospect**, swap calendar.owner to Shawn's Google Calendar OAuth in GHL Settings → Calendars → Discovery Call → Calendar Settings.

Not a launch blocker — you can stand in for the first 1–2 calls if needed.

### C. 5th domain inboxes

Per the config note (`clear_facility.json:60`): "5th CFS domain in flight as of 2026-05-15, add 3 inboxes when purchased." If Shawn's bought the 5th domain, add 3 more mailboxes in Smartlead and attach to campaign 3368966 (bumps from 15 → 18 inboxes).

---

## 📊 Top scorers cheat sheet (the 8 winners)

The Apollo export should hit these companies (in this order — score 8 trio first):

**Score 8 (must-Apollo):**
- **Polar Semiconductor** — Bloomington — enterprise — $525M cleanroom expansion ✨
- **Gamer Packaging, Inc.** — Minneapolis — mid_market — 20,192 sqft new lease, H2 2026 move-in ✨
- **Hempel Real Estate** — Minneapolis — mid_market — managing LaSalle Plaza, 60K sqft recent lease activity, open Facilities Manager position ✨

**Score 7:**
- SPS Commerce — Minneapolis — enterprise — 200K sqft, 15-yr lease renewal Oct 2025, $8M lobby renovation
- Winthrop & Weinstine, P.A. — Minneapolis — mid_market — 107K sqft in Capella Tower, post-renovation
- HistoSonics, Inc. — Plymouth — mid_market
- St. Croix Hospice — Mendota Heights — mid_market
- Crossroads Properties — Oakdale — mid_market

**Bubble (score 6, 10 companies):** in the CSV. Include or hold based on your read.

Full reasoning + first-touch personalized subject/opener per company in `cohort_1_top_winners_20260521.md`.

---

## 🔧 Operational state for reference

| Component | State | URL/ID |
|---|---|---|
| reply-router latest commit | `102a105` (auto-deployed to Vercel) | `https://reply-router.vercel.app` |
| Smartlead campaign | created, **not activated** | id=`3368966` |
| Smartlead webhook | registered, 8 categories | id=`596722` |
| Smartlead mailboxes attached | 15 / 15 warmed | — |
| GHL pipeline | refactored (9 stages), needs 20-sec drag-reorder | `pipeline/WaNo1BZftVUmpieCPweb` |
| GHL custom fields | all 20 live + IDs match config | — |
| GHL workflow webhook | NOT created (you do this) | — |
| Scoring | 277/500 done, ~223 in flight ETA ~9 AM | PID 52218 still running |
| Dashboard | deployed, gated by `DASHBOARD_SECRET` | `https://ksquared-dashboard.vercel.app/?secret=<value>` |
| Browser controller | still running w/ your GHL session | `/tmp/gh_browser.py` PID 60425 |

---

## 🎯 Order of operations when you wake up

1. **First coffee:** drag-reorder the 2 GHL pipeline stages (20 sec)
2. **Second coffee:** create the GHL stage-change workflow webhook (5 min)
3. **Fix the 3 Jessica mailbox display names** in Smartlead UI (90 sec)
4. **Pull Apollo contacts** for the 8 winners (5 min)
5. **Run the 2 commands** (merge_apollo_contacts.py → orchestrator --resume) (~15 min including the personalize run)
6. **Import + multi-persona test-send + activate** the Smartlead campaign (10 min)
7. **Send Shawn the business_context confirmation email** (5 min — draft already written)
8. Watch the first replies start flowing 24–48 hrs later → reply-router handles them

Total morning work: ~45–60 min start-to-launch.

---

## 🧯 Failure modes / what to watch

- **If Smartlead complains about merge vars not recognized on import:** check the CSV header row — Smartlead is case-sensitive and won't auto-map `Subject_2` to `{{subject_2}}`. Use the lowercase versions exactly.
- **If the test-send shows literal `{{personalized_line}}`:** the merge var wasn't populated — that row's `personalized_line` is empty. Re-run personalize for that row.
- **If campaign-activate throws a "needs warmed accounts" error:** double-check that all 15 mailboxes attached are at 100% warmup. They were verified at 100% tonight.
- **If reply-router stops receiving webhooks:** check Vercel logs for circuit-breaker (4 consecutive 5xx pauses Smartlead delivery). Reconciler at `/api/reconcile` runs nightly 7 AM UTC and Phase 2 replays missed replies as backstop.

---

Built tonight: Smartlead campaign + sequence + webhook, reply-router config + booking_link flip, custom-field audit, partial top-scorers report, this brief.

Sleep well. 🌙
