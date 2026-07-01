# Coverage Checklist — Feature Spec (Echo Audit)

**Status:** design locked pending final OK · **Local doc, do not commit** · Drafted 2026-06-27

> A per-campaign, per-property **call-coverage tracker** so a live 4-person calling
> team can see, at a glance, which properties still need calls this campaign — and
> which have been tried but haven't answered. Built generically for every company
> (only Mayfair is live), on by default in every campaign.

---

## 1. Concept

Each **campaign** has, per **property (location)**, a **target number of calls**
(the "Coverage Checklist"). Default target = **1** for every in-scope property;
admins raise it (e.g. Bellevue = 3) or set it to **0** to drop a property from that
campaign's checklist. Progress is shown as a row of boxes that the whole team shares
live.

Decisions locked with the user:
- **Name:** Coverage Checklist (`target_calls` per campaign×location).
- **Default:** every in-scope property = target 1, auto-created in every campaign
  (new + backfilled). Only exceptions get touched.
- **0 = excluded** from that campaign's checklist.
- **Purely visual** — never warns, never blocks anything.
- Config control = a **shopping-cart stepper**: `[ − ] [ N ] [ + ]`, min 0, click the
  number to type a large value.

---

## 2. The box rendering rule (core logic)

For a property with target **N** in the selected campaign:

- `answered`   = # answered calls for (campaign, property)  → **green ✓**
- `no_answer`  = # no-answer attempts for (campaign, property) → **red ✗**
- Boxes are capped at **N**. Fill order is **greens first, then reds:**

```
green_boxes = min(answered, N)
red_boxes   = min(no_answer, N - green_boxes)
empty_boxes = N - green_boxes - red_boxes
render: [green ✓] * green_boxes , then [red ✗] * red_boxes , then [ ▢ ] * empty_boxes
covered = (green_boxes == N)          # complete only when ALL boxes are green
```

- **N = 0** → property is off the checklist (render nothing / not listed).
- Extra attempts beyond N never add boxes (a green always *replaces* a red).
- Hover tooltip shows raw effort, e.g. `3 answered · 5 no-answer · 8 attempts`,
  so capping at N doesn't hide how hard a property was worked.

Worked examples (all confirmed with user):

| Target | answered | no_answer | Boxes shown |
|---|---|---|---|
| 1 | 0 | 1 | `✗` |
| 1 | 0 | 2 | `✗` (still one box) |
| 1 | 1 | 2 | `✓` |
| 2 | 0 | 2 | `✗ ✗` |
| 2 | 1 | 2 | `✓ ✗` (green replaced a red) |
| 3 | 1 | 1 | `✓ ✗ ▢` |
| 2 | 2 | 3 | `✓ ✓` (covered; reds gone) |

---

## 3. What counts as "answered" vs "no-answer"

Counted from the existing `interactions` table (no new tracking needed):

- **no-answer (red)** = `status_id = 44` (no_answer).
- **answered (green)** = any non-deleted, non-test interaction that is **not** a
  no-answer and **not** revoked — i.e. a real call with data, counted **live** the
  moment it's logged (does NOT wait for AI grading to finish).
- **Excluded from both:** `interaction_deleted_at IS NOT NULL`, `interaction_is_test =
  TRUE`. (Note: status 50 is **API-key** revoked, NOT an interaction status — do not
  reference it; rely on `interaction_deleted_at` for removed calls.)
- **⚠ To finalize in build:** confirm "answered" excludes never-submitted / abandoned
  draft rows (default status 45 'pending'). Likely require `interaction_submitted_at
  IS NOT NULL` OR audio/transcript present, so a half-filled grade form doesn't fill a
  box. Verify against the real status lifecycle (40 transcribing, 42 grading, 43
  graded, 44 no_answer, 45 pending, 50 revoked) before shipping.

Progress query (per campaign):

```sql
SELECT interaction_location_id AS location_id,
       COUNT(*) FILTER (WHERE status_id <> 44) AS answered,
       COUNT(*) FILTER (WHERE status_id  = 44) AS no_answer
FROM interactions
WHERE campaign_id = :cid
  AND interaction_deleted_at IS NULL
  AND interaction_is_test = FALSE
GROUP BY interaction_location_id;
```

---

## 4. Data model

One new table. Progress is **derived** (never stored) so 4 concurrent callers can't
desync a counter, and calls already made this campaign count instantly.

```sql
CREATE TABLE campaign_location_targets (
    campaign_location_target_id SERIAL PRIMARY KEY,
    campaign_id   INTEGER NOT NULL REFERENCES campaigns (campaign_id) ON DELETE CASCADE,
    location_id   INTEGER NOT NULL REFERENCES locations (location_id) ON DELETE CASCADE,
    target_calls  INTEGER NOT NULL DEFAULT 1,       -- 0 = excluded from this campaign
    clt_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clt_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_clt_target_nonneg CHECK (target_calls >= 0)
);
CREATE UNIQUE INDEX uq_campaign_location_target ON campaign_location_targets (campaign_id, location_id);
CREATE INDEX idx_clt_campaign ON campaign_location_targets (campaign_id);
```

Resolution rule: **row present → use `target_calls` (incl. 0). Row absent → default 1**
(so a property added to the company mid-campaign still shows on the checklist).

**In-scope property universe for a campaign** = the properties covered by the
campaign's project. For Mayfair's all-locations projects that's every company
location (~116). *(To confirm in code: projects.project_all_locations vs the project's
single location; seed rows accordingly.)*

---

## 5. Seeding & backfill — NONE NEEDED

Because **absent row = default target 1**, we do **not** seed a row per property and do
**not** backfill existing campaigns. Every property in a campaign's scope already shows a
1-box checklist with zero stored rows; we write a row only when an admin overrides
(e.g. 3) or excludes (0). New properties added to the company mid-campaign auto-appear
at target 1. This is identical UX to seeding, with ~zero rows instead of thousands and
no drift risk.

- The table lands via `db.py _ADDITIVE_MIGRATIONS` (Postgres, existing prod DB) +
  `schema.sql` (fresh installs), per project convention. **Done in Phase 1.**

---

## 6. UI surfaces

1. **Property picker boxes** (the main ask) — render the box row next to each property
   in the **shared multiselect** component so it shows in **both** the grade form's
   property picker **and** the analytics/dashboard location dropdown, plus the
   **Locations page** list. Scoped to the **currently-selected campaign** (boxes are
   meaningless without a campaign in context — if no campaign selected, hide boxes or
   show a muted state).
2. **Campaign editor** — the Coverage Checklist config: list of properties, each with
   the shopping-cart stepper `[ − ] [ N ] [ + ]` (default 1, 0 = off, typeable). Plus a
   rollup like "42 / 116 properties covered."
3. **Color code:** green ✓ = covered box, red ✗ = tried/no-answer, empty ▢ = to do —
   consistent with the app's existing green semantics.

---

## 7. Build phases (proposed)

1. **Schema (table only)** — `campaign_location_targets` table in `schema.sql` +
   `_ADDITIVE_MIGRATIONS`. No seed-on-create, no backfill (absent = default 1). ✅ DONE.
2. **Read model** — a helper that returns, for a campaign, `{location_id: {target,
   answered, no_answer, green, red, empty, covered}}`; unit-tested against the box rule
   in §2.
3. **Config API + stepper UI** — campaign editor endpoints to get/set per-property
   targets + the shopping-cart control.
4. **Boxes in the property picker** — extend the shared multiselect + Locations list to
   render the box row for the active campaign; tooltip with raw counts.
5. **Polish** — campaign rollup, empty/edge states, styling pass.

Each phase: build → self-review → confirm with user before the next. Deploys need
explicit per-instance authorization (standing rule).

---

## 8. Open items / to confirm before/while building

- [ ] "Answered" precise definition vs abandoned draft rows (§3 ⚠).
- [ ] In-scope property universe per campaign (all-locations vs single-location
      projects) (§4).
- [ ] Exact multiselect component + where campaign context is available in the grade
      form and analytics (implementation detail).
- [ ] Whether the Locations page boxes need a campaign selector of their own (that page
      may not currently have a campaign in context).
