# CFS Launch Runbook — May 31, 2026

State as of 2026-05-21. The system is production-ready end-to-end except for the items in **Pre-Launch Checklist** below.

## What was built (2026-05-20 → 2026-05-21 build night)

### Two repos in play
- **intent-signal-engine** (`jt1175/intent-signal-engine`) — config-driven enrichment + ICP scoring + 4-touch sequence generation. Outputs Smartlead-ready CSV.
- **reply-router** (`jt1175/reply-router`) — FastAPI service on Vercel. Receives Smartlead webhooks, handles replies via Claude, drives the qualification booking flow, syncs both directions with GHL.

### Engine modules
- `modules/perplexity_signals.py` — 5 Perplexity signal types per row (job postings, lease activity, company news, vendor changes, contractor interest)
- `modules/google_places_signals.py` — Google Reviews extraction
- `modules/claude_scoring.py` — 1-10 ICP scoring + first-touch personalization (subject + opener)
- `modules/sequence_engine.py` — 3 additional touches (#2/3/4) with deal-type variants (velocity / mid_market / enterprise)
- `scripts/normalize_apollo_export.py` — Apollo Accounts CSV → engine input schema

### Reply-router endpoints (all live + verified)
```
GET  /v1/health                                            — deploy probe
POST /v1/clients/{client}/replies                          — Smartlead webhook (dispatches REPLY / OPEN / CLICK / BOUNCE / UNSUBSCRIBE / unknown)
GET  /v1/clients/{client}/approvals/{token}                — shadow-mode approval UI for drafts
POST /v1/clients/{client}/approvals/{token}/send|/discard  — approve or reject draft reply
GET  /v1/clients/{client}/qualify/{contact_id}             — qualification form
POST /v1/clients/{client}/qualify/{contact_id}             — form submission → Claude routing
POST /v1/clients/{client}/qualify/{contact_id}/book        — slot pick → GHL appointment create
POST /v1/clients/{client}/ghl-stage-change                 — GHL workflow → Smartlead pause sync
POST /api/reconcile                                        — nightly cron (7 AM UTC) — 4 phases
```

### Bidirectional sync — both directions live
- **GHL → Smartlead**: opportunity stage change → look up Smartlead lead by email → pause sequence (when stage in `pause_on_stage_ids` = Closed Won + Closed Lost)
- **Smartlead → GHL**: real-time webhooks (OPEN/CLICK/BOUNCE/UNSUBSCRIBE) update GHL custom-field counters. Nightly reconciler Phase 4 polls Smartlead stats as a backstop for missed webhook deliveries (circuit-breaker scenarios).

### Safety architecture
- **Shadow mode**: `business_context.booking_link` contains `PLACEHOLDER` → AI generates drafts but they go to the approval UI, not the prospect. Forced for `interested` / `info_request` / `objection` classifications until JT flips the link to a real URL.
- **Confidence gate**: 4 of 6 classifications require human approval (`auto_send: false` in classification_actions). Only `unsubscribe` and `wrong_person` auto-send.
- **AWAITING_SHAWN_CONFIRM filter**: any `business_context` value flagged as awaiting Shawn confirmation is stripped from Claude's prompt at runtime. Defense-in-depth: prompt rule + missing data both prevent unverified-claim leakage.
- **Pricing rule**: contextual responder is forbidden from quoting any `$` figure. Hard rule in the prompt.
- **Circuit-breaker safety**: ALL Smartlead webhook paths return 200 even on malformed payloads. Smartlead pauses delivery after 4 consecutive 5xx — never trip it.

### Tests
- reply-router: **265 passed, 2 skipped**
- intent-signal-engine: **11 passed** (sequence engine + helpers)

---

## Pre-Launch Checklist

Order top-to-bottom; each item is required before the May 31 launch.

### Code-side (all done — verify deploy state)
- [x] All endpoints deployed at commit `c3c51ed` (or later)
- [x] GHL custom fields provisioned (12 total — 3 reply tracking + 3 qualification + 6 metrics)
- [x] Pipeline stages mapped (qualify → Walkthrough Scheduled, gray → Nurture, reject → Closed Lost)
- [x] Calendar `4vsUpwgKhxY9XyMixhYc` (Discovery Call) connected to Google Calendar
- [x] Calendar API live-verified (free-slots returning correctly)
- [x] Stage-change endpoint live-verified
- [x] Event handlers live-verified (OPEN/CLICK working)
- [x] Defensive 200-on-malformed verified in prod

### Config-side
- [x] `business_context` populated with JT_DRAFT values + real website + Shawn email
- [ ] **Shawn confirms business_context values** — email at `docs/client_communications/2026-05-20_shawn_business_context_request.md`
  - Replace JT_DRAFT_ prefixes with confirmed text
  - Replace AWAITING_SHAWN_CONFIRM in credentials with confirmed text (or remove if not accurate)
  - Fill `phone` + `address` (currently placeholders)
- [ ] **Smartlead production campaign created** with 4-touch sequence using merge vars: `{{personalized_subject}}`, `{{personalized_line}}`, `{{subject_2..4}}`, `{{line_2..4}}`
- [ ] **`clients/clear_facility.json` updated** with real campaign ID (currently `TBD_SMARTLEAD_CAMPAIGN_ID_PRIMARY`)
- [ ] **`business_context.booking_link` flipped** from `PLACEHOLDER` → `https://reply-router.vercel.app/v1/clients/clear_facility/qualify/{contact_id}?token={token}` — this unlocks shadow_send → real_send

### GHL-side
- [ ] **Workflow webhook created**: trigger on "Opportunity Stage Changed" → HTTP POST to `https://reply-router.vercel.app/v1/clients/clear_facility/ghl-stage-change?secret=<CFS_ROUTER_SECRET>` with payload `{contactId, currentStage}`
- [x] Discovery Call calendar created (calendar_id discovered + wired)
- [x] All custom fields created (script-provisioned)
- [ ] **Verify pipeline stage names match** the GHL UI (some older code comments use old names like "Manual Review" but the stage is actually named "Nurture" in the UI — this is fine, the IDs are canonical)

### Smartlead-side
- [ ] **Re-register webhook** at `https://reply-router.vercel.app/v1/clients/clear_facility/replies?secret=<CFS_ROUTER_SECRET>` with all 8 categories enabled (Interested, Out Of Office, Not Interested, Information Request, Meeting Request, Wrong Person, Do Not Contact, Sender Originated Bounce). Required for OPEN/CLICK/BOUNCE/UNSUBSCRIBE events to fire.
- [ ] Production campaign configured (see Config-side above)
- [ ] Warmup hits Day 22 on May 31 (currently Day 10/22 → on track)
- [ ] **5th domain inboxes added if purchased** (per `_pending_domains` note — would bump from 12 → 15 sending inboxes)

### Cohort 1 (in flight)
- [x] Apollo 2.5k accounts exported + normalized
- [x] Scoring run kicked off (500 rows, started 22:28 May 20, ~5 hr ETA)
- [ ] Review scored output — pick score-≥7 winners (~25% expected pass rate, ~125 winners from 500)
- [ ] Apollo export contacts for ONLY those winner companies (one decision-maker per company)
- [ ] Merge contacts into scored CSV
- [ ] Re-run engine with `--resume` and personalization enabled (touches #1-4 generated with contact_first_name)
- [ ] Import final CSV to Smartlead production campaign
- [ ] Decide whether to score the remaining 2k Apollo rows (cohort 2/3/4) or hold

---

## End-to-End Demo Flow (verification on launch day)

Use this to validate the full pipeline before opening the funnel to real prospects.

1. **Cold send**: Smartlead sends touch #1 to a test prospect (e.g. JT's personal email at a test company).
2. **Open tracking**: open the email → GHL `email_open_count` increments within 60 sec.
3. **Click tracking**: click any link → GHL `email_click_count` increments.
4. **Interested reply**: reply "I'm interested" → reply-router classifies as `interested` → drafts a response → posts to approval UI in Slack (because `auto_send: false` for `interested`).
5. **Approve draft**: open approval link in Slack → click Send → response goes out with embedded booking link.
6. **Qualification form**: prospect clicks booking link → form renders → submit with sweet-spot answers (mid-market, no current vendor, this-month timeline, $5k-15k budget).
7. **Slot pick**: form routes to qualify → calendar slots show → pick one → GHL appointment created on Shawn's Google Calendar.
8. **Stage progression**: GHL opportunity moves to "Walkthrough Scheduled" stage automatically.
9. **Stage-change sync**: manually move opportunity to "Closed Lost" in GHL → workflow webhook fires → Smartlead lead's sequence pauses (no more follow-ups).

If all 9 steps work, the system is launch-ready.

---

## Operational notes

### Where things live
- Reply router prod URL: `https://reply-router.vercel.app`
- Engine repo: `/Users/selene/Documents/Code/KSquared/intent-signal-engine`
- Reply-router repo: `/Users/selene/Documents/Code/KSquared/reply-router`
- Per-client data: `data/<client_id>/` in engine repo (gitignored — ephemeral)
- Per-client config: `config/<client_id>.json` (engine) + `clients/<client_id>.json` (reply-router)
- Logs: `intent-signal-engine/logs/` (gitignored)

### Crons
- `/api/reconcile` — daily 7 AM UTC (2 AM Central). Runs 4 phases: stuck-lock cleanup, missed-reply replay, expired-token cleanup, Smartlead-stats sync to GHL.

### Failure modes + recovery
- **Smartlead webhook circuit-breaker tripped**: reply-router was 5xx-ing. Now defensive — all paths return 200. If it trips anyway (e.g. Vercel down), reconciler Phase 2 replays missed replies on next cron tick. Phase 4 backfills missed metrics.
- **GHL contact lookup fails for a reply**: contact gets auto-created from the reply data (skeleton contact). Tagged `auto_created_from_reply`.
- **Claude API errors during classification**: response marked `unknown` confidence → routes to human approval (never auto-sends).
- **Cron didn't fire**: check Vercel Functions logs. Manually trigger via `curl -X POST -H "Authorization: Bearer $VERCEL_CRON_SECRET" https://reply-router.vercel.app/api/reconcile`.
- **Apollo CSV column mismatch**: re-run `scripts/normalize_apollo_export.py` — it has explicit column mapping that fails loudly on missing required columns.
- **Scoring run crashed mid-cohort**: `--resume` flag picks up where it left off based on `(company_name, address)` dedupe key. No rescoring.

### Adding a new client
1. Create `config/<client_id>.json` in engine (copy `clear_facility.json` template)
2. Create `clients/<client_id>.json` in reply-router (copy `clear_facility.json` template; set TBD placeholders to real values as available)
3. Create GHL sub-account, get `sub_account_id` + `pipeline_id`
4. Run `scripts/provision_qualification_setup.py` (idempotent — creates the 9 custom fields if absent, discovers calendar ID, patches config)
5. Set Vercel env vars: `<CLIENT_ID>_GHL_API_KEY`, `<CLIENT_ID>_SMARTLEAD_API_KEY`, `<CLIENT_ID>_SLACK_WEBHOOK_URL`, `<CLIENT_ID>_ROUTER_SECRET`
6. Drop `<client_id>/cohort_1_<date>_apollo_raw.csv` in engine data dir
7. `python scripts/normalize_apollo_export.py` → `python orchestrator.py --config config/<client_id>.json --input <input> --output <output>`

---

## Open follow-ups (post-launch backlog)

- **Smartlead `list_replies` endpoint** still gated (`_LIST_REPLIES_ENDPOINT_VERIFIED = False`). Reconciler Phase 2 raises immediately, so we have NO real reply-replay backstop today. The webhook is the only path. Worth investigating tomorrow whether Smartlead has a campaign-wide message-history endpoint or if we need per-lead polling.
- **A/B subject variants** on touches #2-4. Smartlead can do native `{a|b}` syntax — could leverage for sequence touches.
- **Sequence calendar offset** — currently +3 / +7 / +14 days from prior touch. Worth A/B testing later.
- **Weekly Signal Scanner** (BRD module, deferred from build night). Cron job that re-enriches watchlist accounts and alerts on new signals.
- **Multi-tenant Next.js dashboard** (BRD module, deferred). Multi-week build, separate project.
- **Pre-call brief generator** (BRD module, JT deferred). Email + Slack + GHL note 24h before a booked call.
- **business_context.email surfacing** — currently set to `shawn@clearfacilityservices.com`. Responder doesn't actively use this in prompts today, but worth deciding whether to surface in responses or hide it.

---

## Git history (build night)

### intent-signal-engine
```
e45f8e1 feat(scripts): add normalize_apollo_export
7f9fc31 refactor(data): per-client folder structure
5e8dbcc chore(deps): pytest deps for sequence engine
66f5a05 test(sequence-engine): 11-test pytest suite
f796128 config(cfs): sequence_prompt + deal_type_variants
da23f80 feat(orchestrator): wire generate_sequence
1d8ea7a feat(sequence-engine): 4-touch module + deal-type variants
```

### reply-router
```
c3c51ed fix(replies): also catch downstream process_reply crashes
d9d045b fix(replies): defensive 200-on-malformed; never 5xx
678f231 feat(reconciler): Phase 4 — Smartlead stats backstop
d0b0d12 feat(responder): wire value_props + objections + credentials
718333a config(cfs): JT_DRAFT business_context + Shawn brief
7947971 feat(sync): Smartlead non-reply events → GHL fields (3b)
d8f31a3 feat(sync): GHL stage-change → Smartlead pause (3a)
781a1af feat(qualify): add booking flow
cc3c76a chore: ignore uv.lock
```
