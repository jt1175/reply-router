# reply-router

Inbound reply-routing service for K Squared AI lead-gen campaigns.

**Sibling to:** [intent-signal-engine](../intent-signal-engine)
**Design spec:** See `intent-signal-engine/docs/superpowers/specs/2026-05-15-reply-router-design.md` for the authoritative architecture, data flow, and decision rationale.

## Quick start (local dev)

```bash
python3.13 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in real values
make verify-configs   # validate clients/*.json
make verify           # unit + fixture tests
```

## Deploy

Push to `main`; Vercel auto-deploys. Manual smoke:
```bash
make verify-live   # hits sandbox APIs
```

See spec §9 for operational checklists.
