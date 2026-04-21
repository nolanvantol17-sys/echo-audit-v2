"""
run_phase3_tests.py — Focused Phase 3 grading-flow test.

Creates the minimum fixtures for a grade run, generates a short synthetic
WAV, exercises POST /api/grade three ways, verifies the DB landed correctly,
then tears down every row it created.

Live APIs used: AssemblyAI + Anthropic. Total cost expected ~= $0.05-0.10.
"""

import io
import math
import os
import secrets
import struct
import sys
import time
import traceback
import wave
from datetime import date

from dotenv import load_dotenv
load_dotenv(".env")

from cryptography.fernet import Fernet
os.environ.setdefault("VOIP_ENCRYPTION_KEY", Fernet.generate_key().decode())

import logging
logging.basicConfig(level=logging.WARNING)

import db
from app import create_app
from auth import create_user

app = create_app()
app.config["TESTING"] = False
app.config["PROPAGATE_EXCEPTIONS"] = False

SUFFIX = "phase3-" + str(int(time.time()))


# ── Result accumulator (same shape as main harness) ─────────
RESULTS = []

def rec(name, status, expected, outcome, note=""):
    RESULTS.append({
        "name": name, "status": status, "expected": expected,
        "outcome": outcome, "note": note,
    })


# ── Synthetic WAV generator ─────────────────────────────────
def make_wav_bytes(seconds=3.0, freq=440.0, rate=16000):
    """Return WAV bytes of a simple sine tone. AssemblyAI happily accepts a
    tone — it'll produce an empty or near-empty transcript, which is fine for
    route-flow testing; the grader still needs to return something valid."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        amp = 8000
        frames = b"".join(
            struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)
        )
        w.writeframes(frames)
    return buf.getvalue()


# ── Fixtures ─────────────────────────────────────────────────
def build_fixtures():
    fx = {}
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO companies (company_name, industry_id, status_id) "
            "VALUES (%s, 1, 1) RETURNING company_id",
            (f"P3 Company {SUFFIX}",),
        )
        fx["company_id"] = cur.fetchone()["company_id"]

        cur = conn.execute(
            "INSERT INTO locations (company_id, location_name, status_id) "
            "VALUES (%s, %s, 1) RETURNING location_id",
            (fx["company_id"], f"P3 Location {SUFFIX}"),
        )
        fx["location_id"] = cur.fetchone()["location_id"]

        cur = conn.execute(
            "INSERT INTO departments (company_id, department_name, status_id) "
            "VALUES (%s, %s, 1) RETURNING department_id",
            (fx["company_id"], f"P3 Dept {SUFFIX}"),
        )
        fx["department_id"] = cur.fetchone()["department_id"]

        db.seed_company_defaults(fx["company_id"], conn=conn)

        cur = conn.execute(
            "INSERT INTO rubric_groups (location_id, rg_name, rg_grade_target, status_id) "
            "VALUES (%s, %s, 'respondent', 1) RETURNING rubric_group_id",
            (fx["location_id"], f"P3 Rubric {SUFFIX}"),
        )
        fx["rubric_group_id"] = cur.fetchone()["rubric_group_id"]

        fx["rubric_item_ids"] = []
        for i, (name, stype) in enumerate([
            ("Greeting",     "out_of_10"),
            ("Knowledge",    "out_of_10"),
            ("Tour Offered", "yes_no"),
        ]):
            cur = conn.execute(
                "INSERT INTO rubric_items "
                "(rubric_group_id, ri_name, ri_score_type, ri_weight, ri_order, status_id) "
                "VALUES (%s, %s, %s, 1.00, %s, 1) RETURNING rubric_item_id",
                (fx["rubric_group_id"], name, stype, i + 1),
            )
            fx["rubric_item_ids"].append(cur.fetchone()["rubric_item_id"])

        cur = conn.execute(
            "INSERT INTO projects "
            "(company_id, project_name, rubric_group_id, project_start_date, status_id) "
            "VALUES (%s, %s, %s, %s, 1) RETURNING project_id",
            (fx["company_id"], f"P3 Project {SUFFIX}",
             fx["rubric_group_id"], date.today()),
        )
        fx["project_id"] = cur.fetchone()["project_id"]
        conn.commit()
    finally:
        conn.close()

    fx["caller_user_id"] = create_user(
        email=f"p3caller+{SUFFIX}@test.com",
        password="TestPass123!",
        role_name="caller",
        first_name="P3",
        last_name="Caller",
        department_id=fx["department_id"],
    )
    return fx


def login_client(user_id):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"]   = True
        sess["_id"]      = secrets.token_hex(16)
    return c


# ── Tests ────────────────────────────────────────────────────

def test_valid_grade(fx, client):
    print("\n[test 1/3] POST /api/grade — valid audio + project_id")
    wav = make_wav_bytes(seconds=3.0)
    data = {
        "project_id":       str(fx["project_id"]),
        "audio":            (io.BytesIO(wav), "tone.wav"),
    }
    start = time.time()
    r = client.post("/api/grade", data=data, content_type="multipart/form-data")
    elapsed = time.time() - start
    print(f"  status: {r.status_code}  elapsed: {elapsed:.1f}s")

    if r.status_code != 200:
        body = r.get_json(silent=True) or {}
        rec("POST /api/grade (valid)", r.status_code, 200, "FAIL",
            f"body={body} — elapsed={elapsed:.1f}s")
        return None

    body = r.get_json(silent=True) or {}
    iid = body.get("interaction_id")
    expected_fields = ("interaction_id", "scores", "strengths",
                        "weaknesses", "total_score")
    missing = [f for f in expected_fields if f not in body]
    note = (f"interaction_id={iid} "
            f"scores_keys={list((body.get('scores') or {}).keys())} "
            f"total_score={body.get('total_score')} "
            f"elapsed={elapsed:.1f}s")
    if missing:
        rec("POST /api/grade (valid)", r.status_code, 200, "FAIL",
            f"missing fields: {missing} — {note}")
        return iid
    if not iid:
        rec("POST /api/grade (valid)", r.status_code, 200, "FAIL",
            f"no interaction_id — {note}")
        return None

    print(f"  interaction_id:     {iid}")
    print(f"  scores (keys):      {list((body.get('scores') or {}).keys())}")
    print(f"  total_score:        {body.get('total_score')}")
    print(f"  strengths (len):    {len(body.get('strengths') or '')}")
    print(f"  weaknesses (len):   {len(body.get('weaknesses') or '')}")
    print(f"  clarifying_qs:      {len(body.get('clarifying_questions') or [])}")
    rec("POST /api/grade (valid)", r.status_code, 200, "PASS", note)

    # Verify DB ────────────────────────
    print("\n  verifying DB landed…")
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "SELECT interaction_id, project_id, status_id, "
            "       interaction_overall_score, interaction_transcript "
            "FROM interactions WHERE interaction_id = %s",
            (iid,),
        )
        interaction = dict(cur.fetchone() or {})

        cur = conn.execute(
            "SELECT rubric_item_id, irs_snapshot_score_type, irs_score_value, irs_snapshot_name "
            "FROM interaction_rubric_scores WHERE interaction_id = %s "
            "ORDER BY interaction_rubric_score_id",
            (iid,),
        )
        irs_rows = [dict(r) for r in cur.fetchall()]

        cur = conn.execute(
            "SELECT cq_order, cq_text, cq_response_format "
            "FROM clarifying_questions WHERE interaction_id = %s "
            "ORDER BY cq_order",
            (iid,),
        )
        cq_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    print(f"    interactions row: status_id={interaction.get('status_id')} "
          f"overall_score={interaction.get('interaction_overall_score')} "
          f"transcript_len={len(interaction.get('interaction_transcript') or '')}")
    print(f"    interaction_rubric_scores: {len(irs_rows)} rows")
    for row in irs_rows:
        print(f"      · item_id={row['rubric_item_id']} "
              f"type={row['irs_snapshot_score_type']} "
              f"value={row['irs_score_value']} "
              f"name={row['irs_snapshot_name']!r}")
    print(f"    clarifying_questions: {len(cq_rows)} rows")
    for row in cq_rows:
        print(f"      · #{row['cq_order']} ({row['cq_response_format']}) "
              f"{(row['cq_text'] or '')[:80]!r}")

    db_ok = (interaction
             and len(irs_rows) == len(fx["rubric_item_ids"])
             and interaction.get("status_id") in (5, 6, 7, 8))
    rec("  → interaction row exists",
        "found" if interaction else "missing", "found",
        "PASS" if interaction else "FAIL",
        f"status_id={interaction.get('status_id')}")
    rec("  → interaction_rubric_scores: 1 per rubric item",
        len(irs_rows), len(fx["rubric_item_ids"]),
        "PASS" if len(irs_rows) == len(fx["rubric_item_ids"]) else "FAIL",
        f"got {len(irs_rows)} / expected {len(fx['rubric_item_ids'])}")
    rec("  → clarifying_questions landed",
        len(cq_rows), "any",
        "PASS" if len(cq_rows) == len(body.get("clarifying_questions") or []) else "FAIL",
        f"db={len(cq_rows)} api={len(body.get('clarifying_questions') or [])}")
    return iid


def test_missing_project_id(client):
    print("\n[test 2/3] POST /api/grade — missing project_id")
    wav = make_wav_bytes(seconds=0.5)
    data = {
        "audio": (io.BytesIO(wav), "tone.wav"),
        # no project_id
    }
    r = client.post("/api/grade", data=data, content_type="multipart/form-data")
    body = r.get_json(silent=True) or {}
    print(f"  status: {r.status_code}  body: {body}")
    rec("POST /api/grade (no project_id)",
        r.status_code, 400,
        "PASS" if r.status_code == 400 else "FAIL",
        f"body={body}")


def test_unsupported_file(fx, client):
    print("\n[test 3/3] POST /api/grade — unsupported file type (.txt)")
    data = {
        "project_id": str(fx["project_id"]),
        "audio":      (io.BytesIO(b"not an audio file"), "dummy.txt"),
    }
    r = client.post("/api/grade", data=data, content_type="multipart/form-data")
    body = r.get_json(silent=True) or {}
    print(f"  status: {r.status_code}  body: {body}")
    rec("POST /api/grade (unsupported ext)",
        r.status_code, 400,
        "PASS" if r.status_code == 400 else "FAIL",
        f"body={body}")


# ── Teardown ─────────────────────────────────────────────────
def teardown(fx):
    print("\n[teardown] removing phase-3 test data from Railway…")
    conn = db.get_conn()
    deleted = {}
    try:
        cid = fx["company_id"]
        # Resolve dependent IDs
        user_ids = [r["user_id"] for r in [
            dict(x) for x in conn.execute(
                "SELECT user_id FROM users u "
                "JOIN departments d ON d.department_id = u.department_id "
                "WHERE d.company_id = %s", (cid,)).fetchall()]]
        proj_ids = [r["project_id"] for r in [
            dict(x) for x in conn.execute(
                "SELECT project_id FROM projects WHERE company_id = %s",
                (cid,)).fetchall()]]
        interaction_ids = []
        if proj_ids:
            placeholders = ",".join(["%s"] * len(proj_ids))
            interaction_ids = [r["interaction_id"] for r in [
                dict(x) for x in conn.execute(
                    f"SELECT interaction_id FROM interactions WHERE project_id IN ({placeholders})",
                    tuple(proj_ids)).fetchall()]]
        loc_ids = [r["location_id"] for r in [
            dict(x) for x in conn.execute(
                "SELECT location_id FROM locations WHERE company_id = %s",
                (cid,)).fetchall()]]
        rg_ids = [r["rubric_group_id"] for r in [
            dict(x) for x in conn.execute(
                "SELECT rubric_group_id FROM rubric_groups WHERE location_id IN " +
                ("(" + ",".join(["%s"] * len(loc_ids)) + ")" if loc_ids else "(SELECT NULL WHERE FALSE)"),
                tuple(loc_ids)).fetchall()]] if loc_ids else []

        def run(label, sql, params):
            if not any(params):
                deleted[label] = 0
                return
            cur = conn.execute(sql, params)
            deleted[label] = cur.rowcount or 0

        def run_in(table, col, ids):
            if not ids:
                deleted[table] = 0
                return
            ph = ",".join(["%s"] * len(ids))
            cur = conn.execute(f"DELETE FROM {table} WHERE {col} IN ({ph})", tuple(ids))
            deleted[table] = cur.rowcount or 0

        run_in("interaction_rubric_scores", "interaction_id", interaction_ids)
        run_in("clarifying_questions",      "interaction_id", interaction_ids)
        run_in("performance_reports",       "subject_user_id", user_ids)
        run_in("api_call_log",              "interaction_id", interaction_ids)
        run_in("voip_call_queue",           "voip_queue_interaction_id", interaction_ids)
        run_in("interactions",              "interaction_id",  interaction_ids)
        run_in("projects",                  "project_id",      proj_ids)
        run_in("rubric_items",              "rubric_group_id", rg_ids)
        run_in("rubric_groups",             "rubric_group_id", rg_ids)
        run_in("phone_routing",             "location_id",     loc_ids)
        run_in("locations",                 "location_id",     loc_ids)
        run_in("audit_log",                 "actor_user_id",   user_ids)
        run_in("users",                     "user_id",         user_ids)
        cur = conn.execute("DELETE FROM companies WHERE company_id = %s", (cid,))
        deleted["companies"] = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()

    for k, v in deleted.items():
        print(f"  {k:30s} {v}")


# ── Main ─────────────────────────────────────────────────────
def main():
    print("Bootstrapping fixtures…")
    fx = build_fixtures()
    print(f"  company_id={fx['company_id']} location_id={fx['location_id']} "
          f"project_id={fx['project_id']} caller_user_id={fx['caller_user_id']}")

    client = login_client(fx["caller_user_id"])
    iid = None
    try:
        try:
            iid = test_valid_grade(fx, client)
        except Exception:
            rec("POST /api/grade (valid) — harness crash", "-", 200, "FAIL",
                traceback.format_exc().splitlines()[-1])
            traceback.print_exc(file=sys.stderr)
        try:
            test_missing_project_id(client)
        except Exception:
            rec("POST /api/grade (no project_id) — harness crash",
                "-", 400, "FAIL", traceback.format_exc().splitlines()[-1])
            traceback.print_exc(file=sys.stderr)
        try:
            test_unsupported_file(fx, client)
        except Exception:
            rec("POST /api/grade (unsupported ext) — harness crash",
                "-", 400, "FAIL", traceback.format_exc().splitlines()[-1])
            traceback.print_exc(file=sys.stderr)
    except Exception:
        traceback.print_exc(file=sys.stderr)
    finally:
        try:
            teardown(fx)
            # Verify clean
            conn = db.get_conn()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) AS c FROM companies WHERE company_name = %s",
                    (f"P3 Company {SUFFIX}",),
                )
                remaining = dict(cur.fetchone())["c"]
                print(f"  verify: P3 companies remaining = {remaining}")
            finally:
                conn.close()
        except Exception:
            traceback.print_exc(file=sys.stderr)

    # ── Report ──
    print("\n\nPHASE 3 GRADING FLOW — RESULTS")
    print("=" * 72)
    passed = sum(1 for r in RESULTS if r["outcome"] == "PASS")
    total = len(RESULTS)
    print(f"Total: {passed}/{total} passed\n")
    for r in RESULTS:
        status = r["status"]
        expected = r["expected"]
        note = r["note"]
        print(f"  [{r['outcome']}] {r['name']:50s} — {status} (expected {expected})")
        if note:
            print(f"         · {note}")
    print()


if __name__ == "__main__":
    main()
