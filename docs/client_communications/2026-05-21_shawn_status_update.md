# Status Update Email for Shawn — paste-ready

**To:** shawn@clearfacilityservices.com
**From:** jt@ksquaredai.com
**Subject:** CFS launch status — on track for May 31, 2 quick asks

---

Hey Shawn,

Wanted to give you a real status update on where we are with the system before launch next week. Headline: **we're on track for May 31**, and a few things I need from you.

## Where we are

The full lead-gen system is built and live in production. To put it concretely:

- **2,500 Twin Cities accounts** were pulled from Apollo and run through ICP scoring this week. About **205 of them came out as strong fits (score ≥6)** for the kind of mid-market and velocity work CFS does best — that's the inbound funnel for Cohort 1.
- The **AI is generating personalized first-touch emails + 3 follow-ups** for each company right now (background job, finishes in about an hour). Each touch is written in your voice using publicly-available signals about the building — recent leases, expansions, vendor changes, hiring patterns, etc.
- The **reply-router is live** — when a prospect replies to one of our emails, an AI reads it, classifies the intent (interested / not now / wrong person / etc.), drafts a contextual response, and routes it to me for approval before sending. For interested prospects, the AI's response includes a link to a qualification form that ends in a booking on your Discovery Call calendar.
- **The first warmed mailboxes hit Day 10 of 22** today — they'll be fully warmed by launch.
- A **dashboard** is up showing live state — cohort scoring, pipeline progression, inbox replies, deliverability health. I'll send you the link separately.

The May 31 launch is the soft launch: real prospects, real emails, but I'm in the approval loop for every outbound reply for the first 2 weeks so we catch any edges before opening the gates fully.

## What I'd love your help with

These are the things that would meaningfully sharpen the system before we go live. None of them are blockers — I have placeholders in for all of them — but the more I can replace placeholders with your actual voice, the more the AI will sound like CFS rather than a generic vendor.

### 1. The "AI voice" doc (15 min, biggest leverage)

I've drafted a doc with **11 short questions** about how CFS positions itself — tagline, services, value props, pricing response, common objections (locked in contract, wants pricing first, no budget, etc.), and which credentials to mention in cold emails. I put my best-guess defaults in each spot so you're editing instead of writing from scratch.

Anything you don't confirm gets **filtered out of replies** by the system, so it's actively in your interest to skim through. The thinner the confirmed context, the more generic the AI sounds.

I'll share that as a Google Doc shortly. Just edit in place — I'll see your answers in real time.

### 2. Calendar ownership

For launch I've connected the Discovery Call calendar to **my** Google Calendar as a stand-in so the booking flow works end-to-end. Before your first real qualified prospect books, we should swap it to **your** calendar in GHL so you control availability and conflicts.

This is a 30-second OAuth flow in GHL Settings → Calendars → Discovery Call → Calendar Settings. I can walk you through it whenever you have a minute next week.

### 3. The 5th domain

The plan was 5 sending domains × 3 inboxes = 15 warmed mailboxes. We're at **4 domains live + warming**. If you've made progress on the 5th CFS domain purchase, let me know — when it's ready I'll get the 3 additional inboxes set up and added to the warmup pool. Not a launch blocker (we're sending fine with what we have), but more mailboxes = more daily volume headroom.

### 4. Pricing rule confirmation

I want to make sure the AI handles pricing questions the way you'd want it to. Current rule: **the system will never quote a dollar figure in a cold reply.** When a prospect asks "what does it cost," the AI's response is:

> "Pricing depends on square footage, frequency, and what kind of facility you're running — happy to put a number together after a quick 10-minute walkthrough so I can see the space."

If that's not how you'd prefer to handle it, send me the wording and I'll update.

## What happens next week

- **Mon–Wed (5/26–5/28):** I'm doing final test sends to my own inbox to verify the full flow end-to-end one more time
- **Thu–Fri (5/29–5/30):** I send you the AI voice doc to fill in (if you haven't already), and we do a final calendar-handoff if you're ready
- **Sat 5/31 morning:** Activate. First emails go out by mid-morning Central

You'll start seeing replies flow into the dashboard within 24-48 hours of activation, and I'll be hands-on for every approval through mid-June.

Let me know if any of this raises questions. Otherwise the AI voice doc is the biggest single thing you can do to make the system sharper — keep an eye out for the Google Doc link.

— JT

---

## Notes for JT (not part of the email)

This is a status-update email separate from the existing `2026-05-20_shawn_business_context_request.md` (the "fill out the 11 questions" doc). Send them in order:

1. **First — this email** — gives Shawn the headline + asks
2. **Then — the Google Doc** — links into the doc with the 11 questions

Once Shawn replies to either, replace any `JT_DRAFT_*` and `AWAITING_SHAWN_CONFIRM` strings in `reply-router/clients/clear_facility.json` with the confirmed text. The responder filter strips both prefixes from the AI's prompt, so until those get updated, the AI is leaning on safer-but-generic phrasing.

Tone calibration: this is intentionally **operator-to-operator**, not vendor-to-client. Shawn is technical and not into corporate boilerplate. If you want me to dial it down further into "buddy speak" or up into "more formal," tell me and I'll rewrite.
