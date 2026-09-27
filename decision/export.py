"""Read-only, snapshot-consistent exports. Teacher choices are not ground truth."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from decision.contracts import dumps, SCHEMA_VERSION
from decision.journal import DecisionJournal


def export_dataset(database, output, *, layout="requests", run_id=None, include_fixtures=False, completed_only=False):
    if layout not in ("requests", "questions", "sft"):
        raise ValueError("layout must be requests, questions or sft")
    database = Path(database).resolve()
    output = Path(output).resolve()
    if not database.is_file(): raise ValueError("Decision database does not exist")
    if output == database: raise ValueError("Output must not replace the database")
    output.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    view = DecisionJournal.__new__(DecisionJournal)
    view.db = db
    counts = {"rows_written": 0, "requests_seen": 0, "fixtures_excluded": 0, "without_valid_teacher": 0,
              "incomplete_excluded": 0, "layout": layout}
    try:
        db.execute("BEGIN")  # One WAL snapshot across all joins, no writes to the running bot.
        with output.open("w", encoding="utf-8") as stream:
            for record in view.records(run_id):
                counts["requests_seen"] += 1
                fixture = record["run"]["metadata"].get("fixture", False) or record["source"] == "fixture"
                if fixture and not include_fixtures:
                    counts["fixtures_excluded"] += 1
                    continue
                if completed_only and (not record["outcomes"] or any(o["status"] != "observed" for o in record["outcomes"])):
                    counts["incomplete_excluded"] += 1
                    continue
                if layout == "requests":
                    rows = [record]  # Includes errors/operator/safety records; filter by source when training.
                else:
                    teacher = record.get("teacher") or {}
                    if record["source"] not in ("model", "fixture") or not teacher.get("valid"):
                        counts["without_valid_teacher"] += 1
                        continue
                    answers = teacher["raw"]["answers"]
                    common = {"schema_version": SCHEMA_VERSION, "decision_id": record["decision_id"],
                              "run_id": record["run_id"], "parent_id": record["parent_id"], "stage": record["stage"],
                              "symbol": record["symbol"], "as_of": record["as_of"], "source": record["source"],
                              "request_hash": record["request_hash"], "run": record["run"],
                              "served_model": teacher["raw"].get("model"), "provider": teacher["raw"].get("provider"),
                              "outcomes": record["outcomes"], "execution": record["execution"],
                              "target_semantics": "teacher_imitation_not_verified_optimal_action",
                              "outcomes_must_not_be_input_features": True}
                    if layout == "questions":
                        rows = [{**common, "question_id": qid,
                                 "input": {"state": record["input"]["state"], "question": question},
                                 "target": answers[qid]} for qid, question in record["input"]["questions"].items()]
                    else:
                        # A generic supervised-finetuning interchange format, not a promise of any trainer's schema.
                        rows = [{**common, "messages": [
                            {"role": "system", "content": "Return structured answers to the supplied typed questions using only the supplied state."},
                            {"role": "user", "content": dumps({"state": record["input"]["state"], "questions": record["input"]["questions"]})},
                            {"role": "assistant", "content": dumps({"answers": answers})}]}]
                for row in rows:
                    stream.write(dumps(row) + "\n")
                    counts["rows_written"] += 1
    finally:
        db.close()
    return counts
