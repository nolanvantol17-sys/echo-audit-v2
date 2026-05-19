# Exile Island Sync — Integration Design

**Status:** Design only. No code written. This doc is (a) the artifact to
resolve the role→permission question with Carlos, (b) the build spec for the
property+user sync, (c) a pre-code checklist with a hard "validate against
the live API first" gate.

Author: Claude Code · 2026-05-19

---

## 1. What this replaces

Today `mayfair_sync.py` calls the **old** endpoint `/api/properties/managers`
(documented in `Property_Directory_API_Documentation.pdf`): a *fuzzy
name search*, one property at a time. It guesses the property by Levenshtein
distance on the name, which is brittle (mis-links, requires a manual re-link
UI, breaks when a property is renamed).

The two **new** bulk endpoints (shapes provided 2026-05-19; NOT in the PDF)
are the proper feed:

- `GET /api/properties/property-directory` — every property, keyed on a
  stable **`YardiCode`** (+ `PropertyId`), with names, address, phone, and
  per-role stable **user-ID lists**.
- `GET /api/properties/active-users` — every user, keyed on a stable **`ID`**,
  with name, email, Teams phone.

Replacing the fuzzy path with these eliminates name-matching **and** the
re-link UI entirely.

## 2. Locked constraints (do not violate)

1. **Never wipe.** `locations` and `users` are databases of record. Sync is
   **update-in-place only** — upsert by stable ID. Never TRUNCATE/DROP/DELETE
   a row that the feed simply stopped mentioning (status-flag it instead).
2. **Match on stable ID, never name.** Properties → `YardiCode`. Users →
   the directory's user `ID`. Names change; IDs don't. Mayfair has been
   burned by name-matching before.
3. **Permission mapping is Carlos-blocked.** Sync the raw role data, but do
   NOT wire it into a CM/RM/VP access model until Carlos defines the
   title→permission mapping (see §6).

## 3. Field mapping

### Properties — `/api/properties/property-directory` → `locations`

| Feed field | Echo Audit column | Action |
|---|---|---|
| `YardiCode` | **NEW** `location_yardi_code` (TEXT, unique) | **Join key.** Schema migration required (see §4). |
| `PropertyId` | `mayfair_property_id` (exists) | Update-in-place. |
| `LongName` / `ShortName` | `location_name` | Update-in-place (LongName preferred; confirm w/ Carlos). |
| `FullAddress` | *(no column today)* | Optional: add `location_address` later if wanted; out of scope v1. |
| `PhoneNumber` | `location_phone` | Update-in-place. |
| `*UserIds` buckets | *(see §6 — deferred)* | Store raw; do NOT map to permissions yet. |

Rows in `locations` with a `YardiCode` no longer present in the feed:
**status-flag, never delete** (exact disposition = open question Q4).

### Users — `/api/properties/active-users` → `users`

| Feed field | Echo Audit column | Action |
|---|---|---|
| `ID` | `mayfair_user_id` (exists, unique idx) | **Join key.** No schema change. |
| `FirstName` / `LastName` | `user_first_name` / `user_last_name` | Update-in-place. |
| `Email` | `user_email` | Update-in-place (NOTE: `user_email` is UNIQUE + the SSO/login key — collision handling is open question Q5). |
| `TeamsPhone` | *(no column today)* | Optional later; out of scope v1. |

Users not in the feed: **never delete** (they may be Echo-Audit-only
accounts e.g. the AI Caller bot). Disposition = Q4.

## 4. Schema delta (v1, properties+users only)

```sql
ALTER TABLE locations ADD COLUMN location_yardi_code TEXT;
CREATE UNIQUE INDEX uq_locations_yardi_code
    ON locations (location_yardi_code)
    WHERE location_yardi_code IS NOT NULL;

-- C1: raw role buckets, stored only (no permission logic — §9 C1)
ALTER TABLE locations ADD COLUMN location_pm_user_ids TEXT;
ALTER TABLE locations ADD COLUMN location_rm_user_ids TEXT;
ALTER TABLE locations ADD COLUMN location_compliance_user_ids TEXT;
ALTER TABLE locations ADD COLUMN location_onsite_user_ids TEXT;
ALTER TABLE locations ADD COLUMN location_all_assigned_user_ids TEXT;

-- C2/Q4: soft-inactivate marker for feed-origin rows absent from a pull
ALTER TABLE locations ADD COLUMN location_inactive_since TIMESTAMP;
ALTER TABLE users ADD COLUMN user_inactive_since TIMESTAMP;
```

One-time backfill: match existing `locations` to feed properties to seed
`location_yardi_code`. **This is the only place fuzzy matching is still
acceptable** — a single supervised migration pass, output a review report of
matches/misses for human sign-off, NOT an ongoing behavior. After backfill,
all syncing is YardiCode-keyed.

`users` reuses `mayfair_user_id` as the join key; the only `users` add is
`user_inactive_since` (C2 soft-inactivation, scoped to feed-origin users).

## 5. Sync algorithm (v1)

Daily job (replaces `mayfair_sync.run_sync` fuzzy path):

1. `GET /active-users` → upsert each by `mayfair_user_id = ID`; update name/
   email in place. Build `{ID → user_id}` map.
2. `GET /property-directory` → for each property, find `locations` row by
   `location_yardi_code`; update name/phone/`mayfair_property_id` in place.
   Unmatched YardiCode after backfill → log to a run report (do not
   auto-create locations without sign-off — Q3).
3. Transactional, snapshot-first (same playbook as the campaign / caller-
   dedup migrations: backup table, verify invariants, then commit).
4. Emit a run summary (counts updated / unmatched / skipped) — reuse the
   `mayfair_sync_runs` table.

Never: TRUNCATE, DELETE-by-absence, or name-keyed writes.

## 6. Carlos-blocked: role → permission mapping (DO NOT BUILD)

The feed exposes **four** role buckets per property plus an all-assigned
list: `PropertyManagerUserIds`, `RegionalMaintenanceUserIds`,
`ComplianceUserIds`, `OnsiteUserIds`, `AllAssignedUserIds`.

Echo Audit today models **one** `locations.mayfair_rm_user_id` per property
and a single `ff_permission_filtering` RM-scoping flag. The feed is far
richer than the current model. Reconciling them is a **product decision for
Carlos**, not an engineering one:

- Which bucket(s) grant Echo Audit visibility to a property's calls?
- Is "Regional Manager" scoping driven by `RegionalMaintenanceUserIds`,
  `Regional_Area_Manager`, or something else? (The old `/managers` endpoint
  used `RMUserId`; the new feed has no single `RMUserId`.)
- Do Compliance / Onsite users get any Echo Audit access at all?
- Multiple PMs per property — all get access, or a primary only?

**Until Carlos answers:** sync stores the raw bucket data (or defers even
that — Q2), and the permission model is untouched. Building the mapping now
would be building the explicitly-deferred piece.

## 6b. Validate-gate results (run 2026-05-19, read-only, zero DB)

Read-only probe against both live endpoints using the `.env` credentials
(`MPL_API_BASE` / `MPL_API_KEY`). No secrets printed, no DB access.

- **Auth & reachability:** both endpoints HTTP 200 with `X-Api-Key`.
- **Shape:** plain JSON **array**, **no pagination, no wrapper** — single
  full dump per endpoint. **→ Q1 and Q6 RESOLVED.**
- **Volume:** 123 properties, 464 users. Daily full pull is trivial; no
  paging logic required.
- **Join keys validated against real data:**
  - `YardiCode`: 0 null, **100% unique** across all 123 properties.
  - User `ID`: 0 null, all integers, **100% unique**.
  - → confirms the "match on stable ID, never name" constraint empirically.
- **Names unreliable (as expected):** 3 users null `FirstName`, 7 null
  `LastName`, 3 null both. Sync must tolerate null names — store what's
  present, never crash, never key on them.
- **Email (feeds Q5):** 2 users null/empty `Email`, **0 duplicate emails**
  at source. No source-side collision, but sync must skip the email write
  when feed email is empty (never write `''` into the `UNIQUE` SSO column).
- **Role buckets (feeds §6):** populated unevenly — `AllAssignedUserIds`
  123/123, PM 111/123, RM 84/123, Compliance 81/123, Onsite 108/123.
  39 properties have no RM in the feed: "no-RM → what permission?" is now a
  data-backed product question for Carlos.

## 7. Open questions — RESOLVED by product owner 2026-05-19

| # | Question | Resolution |
|---|---|---|
| ~~Q1~~ | Base URL / auth / env var? | ✅ `MPL_API_BASE`/`MPL_API_KEY` in `.env`, `X-Api-Key` header, HTTP 200. |
| Q2 | Persist raw role-bucket columns in v1? | **Permission model deferred — build later.** Storage of raw buckets = remaining sub-decision (see §9 C1); leaning store-raw (cheap, avoids re-pulling history). Permission *logic* stays Carlos-blocked per [[feedback-access-levels-deferred-pending-carlos]]. |
| Q3 | Feed property not in `locations` — auto-create or report? | ✅ **Auto-create**, flagged active. |
| Q4 | Echo Audit row the feed stops mentioning — disposition? | ✅ **Properties: mark inactive, never delete.** Users: same intent, BUT must scope to feed-origin users only — see §9 C2 (Echo-Audit-native accounts like the AI Caller bot are never in the feed and must NOT be deactivated). |
| Q5 | Feed Email vs `users.user_email` (= SSO login)? | ✅ **Existing login email is authoritative. Never overwrite it from the feed.** On mismatch: keep the login email, log for human review. Never break a sign-in. |
| ~~Q6~~ | Pagination? Volume? | ✅ No pagination, full-dump arrays, 123 properties / 464 users. |
| Q7 | LongName vs ShortName for display? | ✅ **YardiCode is the only unique identifier.** `LongName` → `location_name` (primary display). ShortName = optional compact label for tight UI spots only; never an identifier. |

## 9. Build sub-decisions — RESOLVED 2026-05-19

- **C1 — Store raw role buckets now? → YES.** Persist the `*UserIds` CSV
  columns from day one (raw storage only; no permission logic). Storage is
  trivial and past role membership is impossible to reconstruct later.
  Schema delta: add `location_pm_user_ids`, `location_rm_user_ids`,
  `location_compliance_user_ids`, `location_onsite_user_ids`,
  `location_all_assigned_user_ids` (TEXT, raw CSV as delivered) to §4's
  migration. NOT wired to any access model — inert until the deferred
  permission workstream.
- **C2 — User-side inactivation scope → feed-origin users ONLY.**
  Auto-inactivation on feed-absence applies **only to users with a
  non-NULL `mayfair_user_id`** (came from the feed). Echo-Audit-native
  accounts — the **AI Caller bot** (see [[followup-ai-caller-user-convention]]),
  super-admins, any non-Mayfair account — are explicitly excluded and never
  touched by the sync. This is a hard guard in the sync's user pass, not a
  nice-to-have: blanket inactivation would deactivate the AI Caller bot and
  break automated calling.

**Spec status: fully pinned. No open product or build questions remain.**
Steps 3–4 (schema migration → property+user sync) are ready to build.
Step 5 (permission model) remains a deferred separate workstream.

## 8. Recommended sequence

1. ~~**This doc → Carlos** for Q2–Q5, Q7. Backend for Q1, Q6.~~ Backend
   side (Q1/Q6) is now self-resolved by the validate gate. **Still need
   Carlos for Q2–Q5, Q7.**
2. ~~**Validate gate**~~ — **DONE 2026-05-19** (see §6b). Shapes/volume/
   pagination confirmed against the pasted models; all expected fields
   present, no surprises.
3. **Schema migration:** add `location_yardi_code` + supervised backfill
   with a human-reviewed match report. *(Unblocked — can start.)*
4. **Build the property+user sync** (§5), snapshot-first, behind the
   existing `mayfair_sync_runs` reporting. Retire the fuzzy `/managers`
   path + re-link UI.
5. **Permission mapping** — separate workstream, only after Carlos resolves
   §6. Not in this scope.

Q1/Q6 + validate gate **passed**. Q2–Q5/Q7 **resolved by product owner
2026-05-19** (§7). Steps 3–4 are now fully unblocked once §9 C1/C2 are
confirmed (both have safe recommended defaults). Step 5 (permission model)
remains a deferred separate workstream.
