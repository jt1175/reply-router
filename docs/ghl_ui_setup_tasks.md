# GHL Manual UI Tasks (JT)

Two things the API token can't do (scope-blocked, would need a Marketplace App OAuth install to expand). For now, do these in the GHL UI. ~30 min total.

---

## 1. Pipeline Refactor (15 min)

Current pipeline has 8 stages; we want 9 with clearer flow. The script tried via API and got `401: The token is not authorized for this scope`.

### What to change

**Pipeline:** `CFS Lead Gen Pipeline`

Navigate: **Sub-Account → Opportunities → Pipeline view → edit (gear icon top-right)**

Current state:

| Pos | Current Name              | Action       |
|-----|---------------------------|--------------|
| 0   | New Reply                 | RENAME to "Replied" |
| 1   | Qualified - Velocity      | **DELETE** (deal type now tracked via custom field) |
| 2   | Qualified - Mid-Market    | **DELETE** (deal type now tracked via custom field) |
| 3   | Walkthrough Scheduled     | KEEP (config references this id) |
| 4   | Proposal / RFP Sent       | RENAME to "Proposal Sent" |
| 5   | Closed Won                | KEEP |
| 6   | Closed Lost               | KEEP |
| 7   | Nurture                   | KEEP (gray-zone qualifier routes here) |

Then **ADD 3 new stages** in this order:

- "Outreach" — position 0 (very top)
- "Call Scheduled" — between Replied and Walkthrough Scheduled
- "Walkthrough Done" — between Walkthrough Scheduled and Proposal Sent

### Final order should look like:

```
0. Outreach              (NEW)
1. Replied               (renamed from "New Reply")
2. Call Scheduled        (NEW)
3. Walkthrough Scheduled
4. Walkthrough Done      (NEW)
5. Proposal Sent         (renamed from "Proposal / RFP Sent")
6. Nurture
7. Closed Won
8. Closed Lost
```

### After saving — back-fill the new stage IDs

Reply-router code references 4 stage IDs in `clients/clear_facility.json`. The existing 3 ID references should still work (we preserved the stages they point at). But for completeness, after saving the pipeline:

1. Open the pipeline editor → hover each new stage → click "Copy ID" (or look in URL/element inspector)
2. No config changes needed unless you want me to wire automations to the new stages — for now the 4 referenced stages stayed intact:
   - `qualify_pipeline_stage_id` → Walkthrough Scheduled ✓
   - `gray_zone_pipeline_stage_id` → Nurture ✓
   - `reject_pipeline_stage_id` → Closed Lost ✓
   - `pause_on_stage_ids` → [Closed Won, Closed Lost] ✓

### Optional follow-up

If you want a sync automation that auto-moves leads to "Outreach" when they're imported, that needs a GHL workflow (not API-doable). One-time setup, ~10 min.

---

## 2. Email Templates (15 min)

Token also lacks template-write scope. The endpoint `POST /locations/{id}/templates` returns `401: The token is not authorized for this scope`. Create these 4 in the UI.

Navigate: **Sub-Account → Marketing → Emails → Templates → "+ New Template"**

For each template below, choose **"Plain Email"** (not drag-drop builder — keeps the source clean), name it as listed, paste the subject + body verbatim.

### Template 1: Walkthrough Confirmation

**Name:** `walkthrough_confirmation`
**Subject:** `Confirmed: walkthrough at {{contact.company_name}} — {{appointment.start_time}}`

```
Hi {{contact.first_name}},

Confirming our walkthrough at {{contact.company_name}} on {{appointment.start_time}}.

I'll arrive a few minutes early — typical walkthrough takes about 30 minutes
depending on building size. I'll be looking at:

- Square footage and floor plate layout
- Floor types (hard floor / carpet split)
- Restroom count and fixtures
- Trash flow and waste removal pattern
- Anything specific you'd like me to evaluate

Reply here with any access notes (parking, building entry, who to ask for).
I'll send a written proposal within 48 hours of the walkthrough.

— Shawn

Clear Facility Services
{{custom_values.shawn_phone}}
clearfacilityservices.com
```

### Template 2: Day-Of Walkthrough Reminder

**Name:** `walkthrough_day_of_reminder`
**Subject:** `Today: walkthrough at {{contact.company_name}}`

```
Hi {{contact.first_name}},

Quick reminder we're walking the building today at {{appointment.start_time}}.

If anything changed on your end, reply or text {{custom_values.shawn_phone}}.

See you soon.

— Shawn
```

### Template 3: Post-Walkthrough Proposal

**Name:** `proposal_send`
**Subject:** `Cleaning proposal for {{contact.company_name}}`

```
Hi {{contact.first_name}},

Thanks for the walkthrough yesterday — building looked great and you were
generous with your time.

Proposal attached. Quick highlights:

- Frequency: [fill in]
- Scope: [fill in — link to scope checklist]
- Start date: [fill in]
- Pricing: [fill in — locked for 12 months]

We typically do a 30-day "shakedown" where we calibrate routines to your
team's actual patterns. After that we lock into the steady-state cadence.

Happy to walk through this on a quick call or just take it at your pace.
Whichever works.

— Shawn

Clear Facility Services
clearfacilityservices.com
```

### Template 4: Polite Decline

**Name:** `polite_decline`
**Subject:** `Re: {{contact.company_name}}`

```
Hi {{contact.first_name}},

Appreciate you taking a look. After reviewing what you shared, we're not
going to be the right fit for what you're trying to do right now.

If anything changes — whether that's a different facility, a different
scope, or 6 months from now when your current setup hits a wall — feel
free to come back. No hard feelings.

— Shawn

Clear Facility Services
```

### Custom value to create before using templates

The templates above reference `{{custom_values.shawn_phone}}`. If that doesn't already exist:

Navigate: **Sub-Account → Settings → Custom Values → "+ Add Custom Value"**
- Name: `Shawn Phone`
- Value: (Shawn's actual phone, or your Google Voice stand-in)

---

## After Both Are Done

Let me know and I'll:
- Update `LAUNCH_RUNBOOK.md` with the new pipeline stage IDs (just the cosmetic listing — config IDs unchanged)
- Verify a test contact flows cleanly through Outreach → Replied → Call Scheduled in the API
- (Optional) Add a Workflow trigger to auto-tag contacts as `state_in_outreach` when they hit the Outreach stage

---

## Why The Token Lacks These Scopes

The current GHL API key is a **Private Integration Token (PIT)** with these scopes:
- `contacts.write`, `contacts.readonly` ✓
- `opportunities.readonly` (write requires re-auth) — needed for opportunities POST, currently works for create
- `tags.write` ✓ (just confirmed — provisioned 27 tags tonight)
- `custom-fields.write` ✓ (provisioned 9 tonight)
- `pipelines.write` ✗ — **blocked**
- `templates.write` ✗ — **blocked**

To unblock these via API, we'd need to install a Marketplace App with the broader scope list (`pipelines.write` + `email-templates.write`), which requires an OAuth install flow — beyond tonight's scope. UI execution is faster for one-time setup work like this anyway.
