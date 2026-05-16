# Smartlead API Endpoint Research

**Date of research:** 2026-05-16  
**Researcher:** Subagent (Task 2.4 Step 2)  
**Budget:** ~15 minutes / 3-5 min per URL

---

## URLs Investigated

1. `https://api.smartlead.ai/llms.txt`
2. `https://api.smartlead.ai/reference/get-messages` (and multiple other /reference/* slugs)
3. `https://api.smartlead.ai/reference/` (index)
4. `https://api.smartlead.ai/llms-full.txt`
5. Additional reference slugs: `get-all-email-messages-of-a-lead`, `get-email-messages`,
   `fetch-email-message-of-a-lead`, `update-lead-status`, `update-lead-status-in-a-campaign`,
   `patch-campaigns-campaign-id-leads-lead-id-status`, `get-lead-message-history`

---

## Findings: send_reply_in_thread

**Status: VERIFIED (pre-confirmed; research consistent)**

- **URL:** `POST https://server.smartlead.ai/api/v1/campaigns/{campaign_id}/reply-email-thread`
- **Auth:** `?api_key=YOUR_KEY`
- **Body:** `{ email_stats_id, email_body, reply_message_id }`
- **Source:** Confirmed via prior research (Task 2.4 Step 2 pre-context). The llms.txt and
  reference pages do not list this endpoint explicitly, but it is known to exist from prior
  Smartlead docs research session (2026-05-15).

**Conclusion:** VERIFIED — implemented with no guard flag.

---

## Findings: list_replies

**Research target:** An endpoint to list replies/messages received in a campaign since a given timestamp.

**What was found:**
- `llms.txt` lists the following campaigns endpoints: `GET /campaigns/`,
  `GET /campaigns/{id}`, `GET /campaigns/{id}/analytics`, `GET /analytics/overview`.
  **No messages or replies endpoint** is listed anywhere in the public llms.txt index.
- `llms-full.txt` lists detailed analytics endpoints but **no messages/replies/inbox endpoint**.
- Every `/reference/*` slug attempted (including `get-messages`, `get-email-messages`,
  `get-all-email-messages-of-a-lead`, `get-lead-message-history`) returned the generic
  campaigns page — indicating the Smartlead reference site does not publish these endpoints
  in public-accessible documentation.
- The `GET /campaigns/{id}/statistics` endpoint (from llms-full.txt) accepts `email_status`
  as a filter but returns aggregate statistics, not individual reply message objects.

**Conclusion: UNVERIFIED — not found in public docs. JT must contact Smartlead support.**

The implementation uses a tentative best-guess URL of  
`GET /api/v1/campaigns/{campaign_id}/messages?since={iso_ts}`  
with `_LIST_REPLIES_ENDPOINT_VERIFIED = False`. This method raises `RuntimeError` on call
until the flag is flipped.

---

## Findings: mark_unsubscribe

**Research target:** An endpoint to mark a lead as "unsubscribed" in a campaign.

**What was found:**
- `llms.txt` explicitly lists: `PATCH /campaigns/{id}/leads/{lead_id}/status`
- This endpoint **is present** in the public docs index.
- However, the `/reference/` site is broken — every attempt to fetch the reference page for
  this endpoint (tried slugs: `update-lead-status`, `alter-lead-status-in-a-campaign`,
  `update-lead-status-in-a-campaign`, `patch-campaigns-campaign-id-leads-lead-id-status`)
  returned the generic campaigns GET page, not the actual endpoint documentation.
- **Valid body parameters and accepted status values** (especially whether `"unsubscribed"`
  or `"UNSUBSCRIBED"` is a valid value) could not be confirmed from public docs.
- The `stop_lead_settings` field uses values like `REPLY_TO_AN_EMAIL`, `OPENED_EMAIL`,
  `CLICKED_LINK`, `NEVER` — these are campaign-level settings, not lead status values.
- Campaign status values (`ACTIVE`, `PAUSED`, `STOPPED`, `ARCHIVED`, `DRAFTED`) are
  confirmed but apply to campaigns, not leads.

**Conclusion: UNVERIFIED — URL shape is known, but request body / valid status values
are unconfirmed. JT must verify via Smartlead support or private docs.**

The implementation uses a tentative URL of  
`PATCH /api/v1/campaigns/{campaign_id}/leads/{lead_id}/status`  
with body `{ "status": "unsubscribed" }` and `_MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED = False`.
This method raises `RuntimeError` on call until the flag is flipped.

---

## Summary Table

| Endpoint              | URL Shape                                                      | Method | Confirmed? | Flag                              |
|-----------------------|----------------------------------------------------------------|--------|------------|-----------------------------------|
| send_reply_in_thread  | `/campaigns/{cid}/reply-email-thread`                          | POST   | YES        | N/A (no guard needed)             |
| list_replies          | `/campaigns/{cid}/messages` (TENTATIVE)                        | GET    | NO         | `_LIST_REPLIES_ENDPOINT_VERIFIED = False` |
| mark_unsubscribe      | `/campaigns/{cid}/leads/{lead_id}/status` (URL known; body ?) | PATCH  | NO         | `_MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED = False` |

---

## Action Required (JT)

1. **list_replies:** Contact Smartlead support or check private API docs for the correct
   endpoint to list incoming replies/messages for a campaign since a given timestamp.
   Once confirmed, update `_LIST_REPLIES_ENDPOINT_VERIFIED = True` in
   `reply_router/smartlead_client.py` and replace the tentative URL.

2. **mark_unsubscribe:** Verify the request body for `PATCH /campaigns/{id}/leads/{lead_id}/status`.
   Specifically: what is the accepted `status` value for unsubscribe? (`"unsubscribed"`,
   `"UNSUBSCRIBED"`, `"opted_out"`?) Once confirmed, update
   `_MARK_UNSUBSCRIBE_ENDPOINT_VERIFIED = True` and correct the body if needed.

3. **Gmail threading** (Step 1 of Task 2.4): Manual verification that `reply_message_id`
   correctly threads replies in Gmail. JT must complete this before Task 3.4 wires
   `send_reply_in_thread` into `responder.py`.
