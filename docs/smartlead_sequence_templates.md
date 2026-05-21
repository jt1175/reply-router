# Smartlead 4-Touch Sequence — Email Body Templates (CFS)

Drop these into the Smartlead campaign sequence builder. The engine populates the merge vars per-row when you import the scored cohort CSV.

## Merge Variables Used

**Engine-populated (per row, varies per prospect):**
- `{{personalized_subject}}` — Touch 1 subject
- `{{personalized_line}}` — Touch 1 personalized opener (1-2 sentences referencing a specific signal: lease activity, hiring, news, contractor change, etc.)
- `{{subject_2}}` / `{{line_2}}` — Touch 2 (Day 3 ping)
- `{{subject_3}}` / `{{line_3}}` — Touch 3 (Day 10 value-add)
- `{{subject_4}}` / `{{line_4}}` — Touch 4 (Day 24 breakup)

**Smartlead built-in:**
- `{{first_name}}` — contact first name
- `{{company_name}}` — company name
- `{{email}}` — recipient email (used in unsubscribe link)

## Cadence

| Touch | Delay | Purpose                            |
|-------|-------|------------------------------------|
| 1     | Day 0 | Cold open — specific signal hook   |
| 2     | +3d   | Light ping ("did this land?")      |
| 3     | +7d   | Value-add angle (building-type tip)|
| 4     | +14d  | Breakup — gracious, door-open      |

Total sequence: **24 days end-to-end.**

---

## Touch 1 — Day 0

**Subject:** `{{personalized_subject}}`

**Body:**

```
Hi {{first_name}},

{{personalized_line}}

A quick intro — I'm Shawn at Clear Facility Services. We're a Twin Cities
owner-operated commercial cleaning crew. We focus on consistency and
responsiveness — the things facility managers actually feel day to day.

Worth a 10-minute call to see if there's a fit? I can walk through what
we do and you can tell me whether it's even close to what you're looking
for. No pressure either way.

— Shawn

Clear Facility Services
clearfacilityservices.com
```

---

## Touch 2 — Day 3 (Light Ping)

**Subject:** `{{subject_2}}`

**Body:**

```
Hi {{first_name}},

{{line_2}}

If now's not the right week, just say so — I'll get out of your inbox.

— Shawn
```

---

## Touch 3 — Day 10 (Value-Add)

**Subject:** `{{subject_3}}`

**Body:**

```
{{first_name}},

{{line_3}}

If a walkthrough would be useful, I can usually fit one in within the
week — typically mornings or after-hours so we don't disrupt your team.
Happy to send our standard scope checklist ahead of time too, so you
know exactly what we'd be looking at.

— Shawn

Clear Facility Services
clearfacilityservices.com
```

---

## Touch 4 — Day 24 (Breakup)

**Subject:** `{{subject_4}}`

**Body:**

```
{{first_name}},

{{line_4}}

I'll close the loop on my end and stop emailing. If timing shifts down
the road and you want to take another look, my door's open — just
reply to this thread.

Wishing {{company_name}} the best.

— Shawn

Clear Facility Services
```

---

## Boilerplate Footer (auto-appended by Smartlead)

Smartlead appends unsubscribe + sender block automatically based on
mailbox config. Verify these are configured in the campaign settings:

- Sender name: `Shawn Trythall`
- Reply-to: `shawn@clearfacilityservices.com`
- Unsubscribe link: enabled (Smartlead defaults are fine)

---

## Smartlead Campaign Setup Steps

1. Create campaign in Smartlead UI: "CFS Cohort 1 — Cold Outreach"
2. Sequence builder → add 4 emails with delays: 0d, 3d, 7d, 14d
   (Smartlead delays are between emails, not from start — so 3, 7, 14)
3. Paste each subject and body from above
4. Save and verify Smartlead recognizes all 8 custom merge vars
   (Smartlead lists unrecognized vars when you preview)
5. Import the personalized CSV (`*_personalized.csv` from engine) — Smartlead
   maps columns to merge vars automatically by name
6. Test-send to your own inbox to verify rendering before activating
7. Activate campaign

## Verification Checklist

Before activating, send yourself a test row from the campaign and confirm:

- [ ] Touch 1 subject + body render correctly (your name, company, opener)
- [ ] No literal `{{var}}` strings visible (means a merge var didn't map)
- [ ] Footer shows sender block + unsubscribe
- [ ] All 4 touches are queued in the lead's Smartlead activity view

## After Activation

Once the first send fires:
- Hour 1: spot-check 3-5 actual sent emails in Smartlead's "Sent" tab
- Day 1: check open rate (should land 30-50%+ on cold)
- Day 3-4: first replies arrive → reply-router AI handles them
- Day 4-5: Touch 2 should auto-fire on non-replies
