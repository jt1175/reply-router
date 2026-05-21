# Smartlead 4-Touch Sequence — Email Body Templates (CFS)

Drop these into the Smartlead campaign sequence builder. The engine populates the engine-side merge vars per-row when you import the scored cohort CSV; Smartlead's built-in vars populate from the lead row and the sending mailbox.

## Sender model

Each prospect's email is sent from one of 15 warmed mailboxes belonging to 3 SDR personas (Sarah Jones / Mike Brooks / Jessica Martin) across 5 sister domains. The email body is signed by the **sending persona**, not Shawn. Shawn's name surfaces only at the calendar-pick step after the prospect qualifies. This is the standard SDR → AE handoff pattern.

## Merge Variables Used

**Engine-populated (per row, varies per prospect):**
- `{{personalized_subject}}` — Touch 1 subject
- `{{personalized_line}}` — Touch 1 personalized opener (1-2 sentences referencing a specific signal: lease activity, hiring, news, contractor change, etc.)
- `{{subject_2}}` / `{{line_2}}` — Touch 2 (+3 days)
- `{{subject_3}}` / `{{line_3}}` — Touch 3 (+7 days)
- `{{subject_4}}` / `{{line_4}}` — Touch 4 (+14 days)

**Smartlead built-in (lead):**
- `{{first_name}}` — contact first name
- `{{company_name}}` — company name

**Smartlead built-in (sender — per-mailbox):**
- `{{sender_first_name}}` — first word of the sending mailbox's `from_name` (Sarah / Mike / Jessica)
- `{{signature}}` — the per-mailbox HTML signature (full name + company + address)

## Cadence

| Touch | Delay | Purpose                            |
|-------|-------|------------------------------------|
| 1     | Day 0 | Cold open — specific signal hook   |
| 2     | +3d   | Light ping ("did this land?")      |
| 3     | +7d   | Value-add angle (walkthrough offer)|
| 4     | +14d  | Breakup — gracious, door-open      |

Total sequence: **24 days end-to-end.**

---

## Touch 1 — Day 0

**Subject:** `{{personalized_subject}}`

**Body:**

```
Hi {{first_name}},

{{personalized_line}}

Quick intro — I'm {{sender_first_name}} with Clear Facility Services. We're a
Twin Cities owner-operated commercial cleaning crew. We focus on consistency
and responsiveness — the things facility managers actually feel day to day.

Worth a quick 10-minute call to see if there's a fit? Happy to walk through
what we do and you can tell me whether it's even close to what you're
looking for. No pressure either way.

— {{sender_first_name}}
{{signature}}
```

---

## Touch 2 — +3 Days (Light Ping)

**Subject:** `{{subject_2}}`

**Body:**

```
Hi {{first_name}},

{{line_2}}

If now's not the right week, just say so — I'll get out of your inbox.

— {{sender_first_name}}
{{signature}}
```

---

## Touch 3 — +7 Days (Value-Add)

**Subject:** `{{subject_3}}`

**Body:**

```
{{first_name}},

{{line_3}}

If a walkthrough would be useful, we can usually fit one in within the
week — typically mornings or after-hours so we don't disrupt your team.
Happy to send our standard scope checklist ahead of time too, so you
know exactly what we'd be looking at.

— {{sender_first_name}}
{{signature}}
```

---

## Touch 4 — +14 Days (Breakup)

**Subject:** `{{subject_4}}`

**Body:**

```
{{first_name}},

{{line_4}}

I'll close the loop on my end and stop emailing. If timing shifts down
the road and you want to take another look, my door's open — just
reply to this thread.

Wishing {{company_name}} the best.

— {{sender_first_name}}
{{signature}}
```

---

## Boilerplate Footer

The `{{signature}}` merge var pulls the per-mailbox signature HTML from Smartlead settings. Each of the 15 mailboxes already has a signature block configured (full name, company, address). No manual footer in the body.

Unsubscribe link is auto-appended by Smartlead's `unsubscribe_text` campaign setting (configured at campaign creation time).

---

## Smartlead Campaign Setup Steps

1. Create campaign in Smartlead UI: "CFS Cohort 1 — Cold Outreach"
2. Sequence builder → add 4 emails with delays: 0d, +3d, +7d, +14d
3. Paste each subject and body from above
4. Save and verify Smartlead recognizes all 8 engine merge vars + the built-in sender vars
5. Import the personalized CSV (`*_personalized.csv` from engine) — Smartlead maps columns to merge vars automatically by name
6. **Test-send each touch to your own inbox using ALL 3 personas** — confirm the signature/from-name rotates correctly and `{{sender_first_name}}` renders as expected
7. Activate campaign

## Verification Checklist

Before activating, send yourself a test send from at least one mailbox of each persona (Sarah / Mike / Jessica) and confirm:

- [ ] Touch 1 subject + body render correctly with the engine-personalized opener
- [ ] No literal `{{var}}` strings visible (means a merge var didn't map)
- [ ] Signature block at bottom shows the SENDING persona's name + company + address, not a different persona
- [ ] `{{sender_first_name}}` in body matches the sending mailbox's display name (e.g., "Sarah" / "Mike" / "Jessica")
- [ ] Unsubscribe link present (auto-appended)
- [ ] All 4 touches queued in the lead's Smartlead activity view

**Watch out:** Smartlead displays warmup with a number while sequence builder uses ¼ Jessica mailboxes were initially set with lowercase `from_name`. If `{{sender_first_name}}` renders `jessica` lowercase, fix the display name in Settings → Email Accounts → click mailbox → update Display Name to title case.

## After Activation

Once the first send fires:
- Hour 1: spot-check 3-5 actual sent emails in Smartlead's "Sent" tab
- Day 1: check reply rate (no open-tracking on cold outreach by design)
- Day 3-4: first replies arrive → reply-router AI handles them
- Day 4-5: Touch 2 should auto-fire on non-replies
