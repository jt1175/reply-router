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

**🚨 LIVE TEST CAMPAIGN (sends at 8 AM Central):**

I built a SECOND Smartlead campaign — id `3369156` "CFS TEST — JT verification 2026-05-21" — and **activated it** with 3 leads (jt@ksquaredai.com, jtkolke11@gmail.com, jtkolke@att.net), each with hand-crafted realistic personalization referencing K Squared AI's growth situation. The test campaign has the same 4-touch sequence, all 15 mailboxes attached, and the same webhook registered (id=596730) so replies fire to reply-router. When the schedule kicks in at 08:00 Central, you should see 3 emails arrive across the 3 inboxes from 3 different sender personas (Smartlead rotates Sarah/Mike/Jessica based on mailbox health).

**What to check when the 3 emails arrive:**
1. Sender name in From: header matches a real persona (Sarah Jones / Mike Brooks / Jessica Martin), not lowercase or weird
2. Body intro reads naturally: "Quick intro — I'm Jessica with Clear Facility Services" (or Sarah/Mike) — no literal `{{sender_first_name}}` showing
3. Sign-off `— Jessica` (or Sarah/Mike) — same first name as the sender
4. Signature block at bottom matches the sending persona (full name + Clear Facility Services, Inc. + 7362 University Ave. NE Suite 310-5 + Fridley, MN 55432)
5. No literal `{{var}}` strings anywhere in subject or body
6. Each of the 3 leads should have *different* personalized_subject + personalized_line content (proves per-row merge mapping works)
7. **End-to-end demo:** reply to ONE of them with "I'm interested" — within ~60 seconds, Slack should ping you with the approval UI link (reply-router → Claude classify → draft response → Slack)

If anything renders wrong, pause campaign 3369156 in Smartlead UI before importing real Cohort 1 leads into 3368966.

**Endpoint smoke tests passed:**
- `GET /v1/health` → 200
- `GET /v1/clients/clear_facility/qualify/VYWFssBcsYaQQ0xEt9KJ?token=<valid>` → 200, form renders correctly with all 12 fields + CSRF
- Webhook `POST /v1/clients/clear_facility/replies` with empty JSON → 200 (defensive 200-on-malformed works for circuit-breaker safety)
- Reply-router booking flow live + tested via direct hit

**Audit findings (all green):**
- ✅ All 20 GHL custom field IDs in config confirmed live in CFS sub-account
- ✅ All stage_id references in clear_facility.json resolve correctly after pipeline refactor (pause_on_stage_ids, qualify/gray/reject stages, all 6 classification routings)
- ✅ All 5 sending domains have valid SPF / DKIM (google._domainkey selector) / DMARC (p=none, monitor-only — standard starter posture) / MX (smtp.google.com)
- ✅ Engine output column names match Smartlead's expected merge var names exactly (lowercase: personalized_subject, personalized_line, subject_2/3/4, line_2/3/4)
- ✅ Reply-router auto-create-skeleton-contact path live for unknown senders (jtkolke11@gmail.com + jtkolke@att.net will get skeleton contacts; jt@ksquaredai.com already exists in GHL)
- ✅ All required env vars present in Vercel production (CFS_GHL_API_KEY, CFS_SMARTLEAD_API_KEY, CFS_ROUTER_SECRET, CFS_SLACK_WEBHOOK_URL, ANTHROPIC_API_KEY)

**One hole I fixed (commit `7b6cb94` in intent-signal-engine):**
The `merge_apollo_contacts.py` script was writing only the engine-side column names (`contact_email`, `contact_first_name`, `contact_name`). Smartlead's CSV import only auto-maps `email` / `first_name` / `last_name` to recipient fields — engine names would have landed as opaque custom fields, meaning Smartlead wouldn't know where to send the emails. **Fixed:** merge script now also emits the 3 Smartlead-friendly aliases alongside the engine names. Your morning import flow now works without manual column mapping.

**Dashboard wired:** committed `c2b9909` in dashboard repo — `clients.ts` now has `campaign_ids: ["3368966"]` for CFS so the inbox/campaigns/funnel views all pull from the right campaign once it's activated.

**Top-scorers report (regen'd at ~03:00):** **11 winners ≥7** from 318/500 rows (3 score-8, 8 score-7) + **11 bubble at score 6**. See `reports/cohort_1_top_winners_20260521.csv` and `.md`. Scoring still continues — final cohort 1 winner count likely lands at 17–20 by 9 AM.

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

### 3. Apollo Contacts export → merge → personalize → import → activate (~45 min)

This is the only path-to-live activity.

**A. Apollo export (5 min):**
Open Apollo. From the **11 winners** (and optionally the **11 bubble-score-6 companies** if you want to lean wider — and scoring is still running so the final winner pool will likely be 17–20), do a Contacts export. Filter by company name. Take one decision-maker per company (Director of Facilities / Office Manager / VP Operations / similar). Save as:
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
4. **Critical:** verify Smartlead recognizes:
   - Primary fields auto-mapped: `email`, `first_name`, `last_name`, `company_name` (the merge script writes these aliases now — fixed in commit `7b6cb94`)
   - 8 custom-field merge vars: `personalized_subject`, `personalized_line`, `subject_2`, `line_2`, `subject_3`, `line_3`, `subject_4`, `line_4`
   - If any show as unrecognized, the CSV column header doesn't match (case-sensitive)

**E. Test-send before activation (5 min):**
1. Add yourself to the campaign as a test lead
2. Smartlead: send test of touch #1 from **at least one Sarah, one Mike, and one Jessica mailbox** (sequentially) to confirm persona rotation works
3. Confirm: subject + body render correctly, no literal `{{var}}` strings visible, `{{sender_first_name}}` renders title-case in the body ("I'm Jessica with..." not "I'm jessica with..."), `{{signature}}` block at bottom shows that persona's name + address
4. **If `{{sender_first_name}}` renders lowercase for Jessica** (was set wrong on 3 of her 5 mailboxes per legacy API data, but the UI Name column shows title-case — verify at send-time which one is actually rendered): fix the Display Name in Smartlead UI → Settings → Email Accounts → click mailbox → update Name field

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

**Score 7 (8 companies):**
- SPS Commerce — Minneapolis — enterprise — 200K sqft, 15-yr lease renewal Oct 2025, $8M lobby renovation
- Winthrop & Weinstine, P.A. — Minneapolis — mid_market — 107K sqft in Capella Tower, post-renovation
- HistoSonics, Inc. — Plymouth — mid_market
- St. Croix Hospice — Mendota Heights — mid_market
- Crossroads Properties — Oakdale — mid_market
- StuartCo — Bloomington — mid_market
- Larkin Hoffman — Minneapolis — mid_market
- Cities Management, Inc. — Minneapolis — mid_market

**Bubble (score 6, 11 companies):** Mulcahy Co, Viking Engineering, Moss & Barnett P.A., Circuit Check Inc., Harland Medical Systems, Clearfield, CVRx, Resolution Medical, Vibrant Technologies, Monteris Medical, Chandler Industries. In the CSV. Include or hold based on your read. Note: 4 of these are medical-practitioner offices (Harland, CVRx, Resolution Medical, Monteris) — Shawn's ICP allows these (not hospitals, not dentists).

Full reasoning + first-touch personalized subject/opener per company in `cohort_1_top_winners_20260521.md`.

---

## 🔧 Operational state for reference

| Component | State | URL/ID |
|---|---|---|
| reply-router latest commit | `7f49082` (auto-deployed to Vercel) | `https://reply-router.vercel.app` |
| intent-signal-engine latest | `7b6cb94` (merge-script alias fix) | — |
| dashboard latest | `c2b9909` (campaign_ids wired) | `https://ksquared-dashboard.vercel.app/?secret=<value>` |
| Smartlead PROD campaign | created, **not activated** | id=`3368966` |
| Smartlead TEST campaign | **ACTIVE — sends 8 AM Central** | id=`3369156`, 3 JT leads |
| Smartlead webhook (prod) | registered, 8 categories | id=`596722` |
| Smartlead webhook (test) | registered, 8 categories | id=`596730` |
| Smartlead mailboxes attached | 15 / 15 warmed (both campaigns) | — |
| GHL pipeline | refactored (9 stages), needs 20-sec drag-reorder | `pipeline/WaNo1BZftVUmpieCPweb` |
| GHL custom fields | all 20 live + IDs match config | — |
| GHL workflow webhook | NOT created (you do this) | — |
| GHL contact for jt@ksquaredai.com | exists | id=`VYWFssBcsYaQQ0xEt9KJ` |
| DNS for all 5 sending domains | SPF/DKIM/DMARC/MX all green | — |
| Scoring | 318/500 done, ~182 in flight ETA ~9 AM | PID 52218 still running |
| Browser controller | still running w/ your GHL session | `/tmp/gh_browser.py` PID 60425 |

---

## 🎯 Order of operations when you wake up

1. **Check your 3 test inboxes** (~5 min) — at 8 AM Central the TEST campaign 3369156 should fire 3 emails. Verify rendering matches the checklist above. **Reply "I'm interested" to one of them** to test the end-to-end demo flow (Smartlead → webhook → reply-router → Slack approval).
2. **First coffee:** drag-reorder the 2 GHL pipeline stages (20 sec)
3. **Second coffee:** create the GHL stage-change workflow webhook (5 min)
4. **Pull Apollo contacts** for the 11 winners (5 min)
5. **Run the 2 commands** (merge_apollo_contacts.py → orchestrator --resume) (~15 min including the personalize run)
6. **Import + verify column mapping + activate** the production Smartlead campaign 3368966 (10 min)
7. **Send Shawn the business_context confirmation email** (5 min — draft already written)
8. Watch the first replies start flowing 24–48 hrs later → reply-router handles them

Total morning work: ~50–70 min start-to-launch including test inbox review.

---

## 🧯 Failure modes / what to watch

- **If Smartlead complains about merge vars not recognized on import:** check the CSV header row — Smartlead is case-sensitive and won't auto-map `Subject_2` to `{{subject_2}}`. Use the lowercase versions exactly.
- **If the test-send shows literal `{{personalized_line}}`:** the merge var wasn't populated — that row's `personalized_line` is empty. Re-run personalize for that row.
- **If campaign-activate throws a "needs warmed accounts" error:** double-check that all 15 mailboxes attached are at 100% warmup. They were verified at 100% tonight.
- **If reply-router stops receiving webhooks:** check Vercel logs for circuit-breaker (4 consecutive 5xx pauses Smartlead delivery). Reconciler at `/api/reconcile` runs nightly 7 AM UTC and Phase 2 replays missed replies as backstop.

---

Built tonight: Smartlead campaign + sequence + webhook, reply-router config + booking_link flip, custom-field audit, partial top-scorers report, this brief.

Sleep well. 🌙
