"""HTML renderers for the qualification booking flow.

Pure functions: take dicts of data → return HTML strings. No I/O.
Mirrors the inline-f-string + html.escape pattern from api.index._render_form.
"""
from __future__ import annotations

import html
from typing import Any


BASE_STYLES = """
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 2em auto; padding: 1em; color: #222; line-height: 1.5; }
  h1 { font-size: 1.4em; margin-bottom: 0.2em; }
  h2 { font-size: 1.1em; margin-top: 1.8em; }
  .meta { color: #666; font-size: 0.9em; margin-bottom: 1.5em; }
  .field { margin: 0.9em 0; }
  label { display: block; font-weight: 600; margin-bottom: 0.3em; }
  input[type=text], input[type=tel], input[type=number], select, textarea {
    width: 100%; padding: 0.55em; box-sizing: border-box; font-size: 1em;
    border: 1px solid #ccc; border-radius: 4px;
  }
  textarea { min-height: 4em; }
  .radio-group label { font-weight: normal; display: flex; align-items: center; padding: 0.3em 0; }
  .radio-group input { margin-right: 0.6em; }
  .helptext { font-size: 0.85em; color: #777; margin-top: 0.2em; }
  button.primary { background: #0a6; color: white; padding: 0.7em 1.4em; border: none; border-radius: 4px; font-size: 1em; cursor: pointer; }
  button.primary:hover { background: #084; }
  button.slot { display: block; width: 100%; text-align: left; background: white; border: 1px solid #0a6; color: #0a6; padding: 0.7em; margin: 0.4em 0; border-radius: 4px; font-size: 1em; cursor: pointer; }
  button.slot:hover { background: #f0fcf6; }
  .terminal { padding: 1.5em; background: #f7f7f7; border-radius: 8px; margin-top: 1em; }
  .terminal.success { background: #f0fcf6; border-left: 4px solid #0a6; }
  .terminal.neutral { background: #fafafa; border-left: 4px solid #888; }
  .context { background: #f9f9fb; padding: 0.8em 1em; border-radius: 4px; font-size: 0.92em; }
</style>
"""


def _e(value: Any) -> str:
    """Escape a value (None-safe) for HTML insertion."""
    if value is None:
        return ""
    return html.escape(str(value))


def render_form(
    contact: dict,
    token: str,
    csrf: str,
    form_issued_at_unix: int,
    company_display_name: str,
    action_path: str,
) -> str:
    """Render the qualification form. POSTs to action_path with all fields + CSRF."""
    fn = _e(contact.get("firstName") or "there")
    co = _e(contact.get("companyName") or "your company")
    em = _e(contact.get("email") or "")
    return f"""<!doctype html>
<html><head><title>{_e(company_display_name)} — Quick Qualification</title>
{BASE_STYLES}
</head><body>
<h1>Quick qualification for {_e(company_display_name)}</h1>
<p class="meta">Hi {fn}, takes about 60 seconds. We use this to make sure we're a fit before booking a walkthrough.</p>

<div class="context">
  <strong>Contact:</strong> {fn} at {co}<br>
  <strong>Email:</strong> {em}
</div>

<form method="POST" action="{_e(action_path)}">
  <input type="hidden" name="csrf" value="{_e(csrf)}">
  <input type="hidden" name="form_issued_at_unix" value="{form_issued_at_unix}">
  <input type="hidden" name="token" value="{_e(token)}">

  <div class="field">
    <label for="building_size_sqft">Facility size (square feet)</label>
    <input type="number" id="building_size_sqft" name="building_size_sqft" min="0" step="100" placeholder="e.g. 25000" required>
  </div>

  <div class="field">
    <label>Building type</label>
    <div class="radio-group">
      <label><input type="radio" name="building_type" value="office" required> Office</label>
      <label><input type="radio" name="building_type" value="warehouse"> Warehouse / industrial</label>
      <label><input type="radio" name="building_type" value="logistics"> Logistics / distribution</label>
      <label><input type="radio" name="building_type" value="multi_tenant"> Multi-tenant commercial</label>
      <label><input type="radio" name="building_type" value="medical"> Medical practitioner office</label>
      <label><input type="radio" name="building_type" value="manufacturing"> Manufacturing</label>
      <label><input type="radio" name="building_type" value="retail"> Retail</label>
      <label><input type="radio" name="building_type" value="other"> Other</label>
    </div>
  </div>

  <div class="field">
    <label>Current cleaning situation</label>
    <div class="radio-group">
      <label><input type="radio" name="current_vendor_status" value="have_vendor_happy" required> Have a vendor and happy with them</label>
      <label><input type="radio" name="current_vendor_status" value="have_vendor_evaluating"> Have a vendor but evaluating alternatives</label>
      <label><input type="radio" name="current_vendor_status" value="no_vendor"> Looking for a vendor (no current provider)</label>
      <label><input type="radio" name="current_vendor_status" value="inhouse"> In-house cleaning today, considering outsourcing</label>
    </div>
  </div>

  <div class="field">
    <label>When are you looking to make a decision?</label>
    <div class="radio-group">
      <label><input type="radio" name="decision_timeline" value="this_month" required> This month</label>
      <label><input type="radio" name="decision_timeline" value="next_3_months"> Next 3 months</label>
      <label><input type="radio" name="decision_timeline" value="this_year"> Sometime this year</label>
      <label><input type="radio" name="decision_timeline" value="not_set"> No firm timeline</label>
    </div>
  </div>

  <div class="field">
    <label>Rough monthly cleaning budget</label>
    <p class="helptext">Helps us prioritize scheduling — not a quote.</p>
    <div class="radio-group">
      <label><input type="radio" name="monthly_budget_range" value="under_500" required> Under $500/mo</label>
      <label><input type="radio" name="monthly_budget_range" value="500_to_2k"> $500 – $2,000/mo</label>
      <label><input type="radio" name="monthly_budget_range" value="2k_to_5k"> $2,000 – $5,000/mo</label>
      <label><input type="radio" name="monthly_budget_range" value="5k_to_15k"> $5,000 – $15,000/mo</label>
      <label><input type="radio" name="monthly_budget_range" value="15k_plus"> $15,000+/mo</label>
      <label><input type="radio" name="monthly_budget_range" value="not_disclosed"> Prefer not to say yet</label>
    </div>
  </div>

  <div class="field">
    <label for="best_phone">Best phone number</label>
    <input type="tel" id="best_phone" name="best_phone" placeholder="(555) 555-5555">
  </div>

  <div class="field">
    <label for="additional_context">Anything else we should know? (optional)</label>
    <textarea id="additional_context" name="additional_context" placeholder="e.g. specific concerns, current pain points, timing constraints"></textarea>
  </div>

  <button type="submit" class="primary">Continue</button>
</form>
</body></html>"""


def render_slot_picker(
    contact: dict,
    token: str,
    csrf: str,
    form_issued_at_unix: int,
    company_display_name: str,
    booking_action_path: str,
    free_slots: list[dict],
) -> str:
    """Render the slot-picker page after a 'qualify' decision.

    free_slots is a list of dicts: {"start_iso": "...", "label": "Mon May 26, 10:00 AM CT"}
    """
    fn = _e(contact.get("firstName") or "there")
    co = _e(contact.get("companyName") or "your company")

    if not free_slots:
        slots_html = (
            '<p>No open slots in the next two weeks — the team will reach out '
            'directly to schedule. You\'ll hear from us within one business day.</p>'
        )
    else:
        slot_buttons = "\n".join(
            f'''<form method="POST" action="{_e(booking_action_path)}" style="margin:0">
  <input type="hidden" name="csrf" value="{_e(csrf)}">
  <input type="hidden" name="form_issued_at_unix" value="{form_issued_at_unix}">
  <input type="hidden" name="token" value="{_e(token)}">
  <input type="hidden" name="selected_slot_iso" value="{_e(slot['start_iso'])}">
  <button type="submit" class="slot">{_e(slot['label'])}</button>
</form>'''
            for slot in free_slots
        )
        slots_html = slot_buttons

    return f"""<!doctype html>
<html><head><title>{_e(company_display_name)} — Pick a walkthrough time</title>
{BASE_STYLES}
</head><body>
<h1>You're a fit — let's get on the calendar</h1>
<p class="meta">{fn}, thanks for the context on {co}. Here are the next open 30-minute walkthrough slots.</p>
<h2>Pick a time</h2>
{slots_html}
</body></html>"""


def render_gray_zone(
    contact: dict,
    company_display_name: str,
    follow_up_blurb: str | None = None,
) -> str:
    fn = _e(contact.get("firstName") or "there")
    blurb = _e(
        follow_up_blurb
        or "Thanks for the info — based on what you shared we want to take a closer look before booking time. Someone from the team will reach out within one business day."
    )
    return f"""<!doctype html>
<html><head><title>{_e(company_display_name)} — Thanks</title>
{BASE_STYLES}
</head><body>
<h1>Thanks, {fn}</h1>
<div class="terminal neutral">
  <p>{blurb}</p>
</div>
</body></html>"""


def render_reject(
    contact: dict,
    company_display_name: str,
    reject_blurb: str | None = None,
) -> str:
    fn = _e(contact.get("firstName") or "there")
    blurb = _e(
        reject_blurb
        or "Thanks for considering us. Based on what you shared, we're not the right fit right now — usually because the facility type or scope is outside what we currently serve. If anything shifts (different building, expanded scope, vendor change), feel free to reach back out."
    )
    return f"""<!doctype html>
<html><head><title>{_e(company_display_name)} — Thanks</title>
{BASE_STYLES}
</head><body>
<h1>Thanks for reaching out, {fn}</h1>
<div class="terminal neutral">
  <p>{blurb}</p>
</div>
</body></html>"""


def render_confirmation(
    contact: dict,
    appointment_label: str,
    company_display_name: str,
    company_phone: str | None = None,
) -> str:
    fn = _e(contact.get("firstName") or "there")
    phone_html = (
        f'<p>Need to reschedule? Reply to the original email or call <strong>{_e(company_phone)}</strong>.</p>'
        if company_phone
        else '<p>Need to reschedule? Reply to the original email.</p>'
    )
    return f"""<!doctype html>
<html><head><title>{_e(company_display_name)} — Walkthrough confirmed</title>
{BASE_STYLES}
</head><body>
<h1>✓ Walkthrough confirmed</h1>
<div class="terminal success">
  <p><strong>{fn}, you're booked for:</strong></p>
  <p style="font-size: 1.15em;">{_e(appointment_label)}</p>
  <p>You'll get a calendar invite by email shortly. Plan on ~30 minutes — we'll walk the space, ask a few questions, and put a written proposal together within 48 hours of the visit.</p>
</div>
{phone_html}
</body></html>"""


def render_error(
    company_display_name: str,
    error_blurb: str = "Something went wrong on our end. Please reply to the original email and we'll sort it out.",
) -> str:
    return f"""<!doctype html>
<html><head><title>{_e(company_display_name)} — Error</title>
{BASE_STYLES}
</head><body>
<h1>Hmm, something went wrong</h1>
<div class="terminal neutral">
  <p>{_e(error_blurb)}</p>
</div>
</body></html>"""
