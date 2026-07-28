---
name: lifeos
description: "Run Signal Deck Daily OS check-ins safely."
version: 1.0.0
author: Domenico
license: MIT
platforms: [linux, macos, windows]
---

# Life OS Daily Check-in Skill

Use this skill only for the existing Daily OS/Life OS `daily.checkin`
operation. It does not write to Notion or Obsidian directly, create a scheduler,
or retain a second Daily Log. Signal Deck is the operator UI over the existing
backend, which remains the only Registry-gated Notion writer.

## When to Use

Use it when Hermes must prepare, apply, replay, or roll back the shared Daily
Log operation through Signal Deck. Do not use it for Studio, Psychologist, or
Fitness behavior: those future overlays remain configuration shells over this
same operation contract.

## Prerequisites

- Use the same idempotency key when coordinating with Signal Deck. Never invent
  a replacement key after a failed request; inspect the returned receipt first.
- The owner provides an existing Signal Deck session cookie and CSRF token in an
  owner-only JSON file. Do not place either in prompts, command history, source
  control, or receipts.
- The Signal Deck origin must use HTTPS. The helper refuses HTTP before loading
  or sending session credentials; there is no loopback exception in this
  contract.
- Read the returned preview before applying. Hermes cannot approve its own
  mutation: applying requires the owner to explicitly say `APPLICA`.
- The helper records this caller as `caller_type=agent`,
  `agent_runtime=hermes`, and no overlay. Signal Deck is instead an
  `operator_ui`; an ordinary ChatGPT Project is a future `agent_runtime=chatgpt`
  handoff, not an alternate database or writer.
- Live application remains manual when fresh workspace, Daily Log schema,
  Registry identity, preimage, and rollback evidence are unavailable.

`auth.json` is local and owner-only:

```json
{"cookie":"<existing Signal Deck session cookie>","csrf_token":"<existing CSRF token>"}
```

`checkin.json` contains only the shared typed fields, for example:

```json
{"date":"2026-07-28","bedtime":"23:30","wake_time":"07:00","mood":8,"morning_journal":"Focus.","evening_journal":"Review."}
```

## How to Run

Use `terminal` to invoke `scripts/daily_checkin.py` from the installed skill.
Keep the preview JSON as evidence; it carries the operation and packet
identities required by apply.

```bash
CLIENT=~/.hermes/skills/productivity/lifeos/scripts/daily_checkin.py
python3 "$CLIENT" --base-url https://signal-deck.example \
  --auth-file /secure/auth.json preview \
  --payload-file /secure/checkin.json \
  --idempotency-key daily.checkin:2026-07-28:owner-choice > /secure/preview.json
```

## Quick Reference

- `preview`: returns `operation_id`, `packet_id`, preimage, and zero mutations.
- `apply`: requires the preview, the identical payload and idempotency key, and
  the owner's exact `APPLICA` confirmation.
- replay: repeat `apply` unchanged and require `already-applied` with
  `mutation_count: 0`.
- `rollback`: requires the recorded operation ID and the owner's exact
  `ROLLBACK` confirmation.

## Procedure

After the owner says `APPLICA`, send the exact same payload and idempotency key:

```bash
python3 "$CLIENT" --base-url https://signal-deck.example \
  --auth-file /secure/auth.json apply \
  --payload-file /secure/checkin.json --preview-file /secure/preview.json \
  --idempotency-key daily.checkin:2026-07-28:owner-choice \
  --confirmation APPLICA
```

Re-run the same `apply` command only to verify `already-applied` and
`mutation_count: 0`. Do not change the key or payload.

After recording the receipt, and only after the owner says `ROLLBACK`, execute:

```bash
python3 "$CLIENT" --base-url https://signal-deck.example \
  --auth-file /secure/auth.json rollback \
  --operation-id "<operation_id from preview.json>" \
  --idempotency-key daily.checkin:2026-07-28:owner-choice \
  --confirmation ROLLBACK
```

## Pitfalls

- Signal Deck is an operator UI, not an AI agent. G4a is two caller surfaces
  (`Hermes` agent and Signal Deck UI) over one backend operation, not agent
  parity.
- The G4b handoff is ordinary ChatGPT Project via the Obsidian and Notion apps.
  It must carry the same operation/idempotency/Registry/preimage/rollback
  fields, but no browser, Project, or app work belongs to this skill.
- A local client response does not prove that Notion changed. Require the
  server's row ID, Registry IDs, receipt SHA, post-fetch, and rollback result.

## Verification

Report the full `operation_id`, Daily Log row ID, Registry IDs, receipt SHA,
replay result, rollback post-fetch, and host. Never claim a live Notion write
from a local client trace alone.
