"""Durable decision/event/outcome ledger. SQLite is the source of truth, JSONL is export.

Input rows never gain future features. Later observations live in separate outcome rows
and append-only events. Every model response, failed inference and execution override is
retained. The store is written BEFORE an inference and BEFORE an order mutation.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from decision.contracts import SCHEMA_VERSION, POLICY_VERSION, clean, dumps, digest

DDL = """
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, metadata_json TEXT NOT NULL,
 ended_at REAL, end_reason TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
 decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), parent_id TEXT,
 stage TEXT NOT NULL, symbol TEXT, position_id TEXT, as_of REAL NOT NULL,
 source TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
 schema_version TEXT NOT NULL, policy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_run ON decisions(run_id, as_of);
CREATE TABLE IF NOT EXISTS events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
 decision_id TEXT REFERENCES decisions(decision_id), event_time REAL NOT NULL,
 recorded_at REAL NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_decision ON events(decision_id, sequence);
CREATE TABLE IF NOT EXISTS outcomes (
 decision_id TEXT NOT NULL REFERENCES decisions(decision_id), name TEXT NOT NULL,
 horizon_seconds INTEGER NOT NULL, due_at REAL, status TEXT NOT NULL,
 observed_at REAL, data_json TEXT NOT NULL,
 PRIMARY KEY(decision_id, name, horizon_seconds)
);
CREATE INDEX IF NOT EXISTS outcome_pending ON outcomes(status, due_at);
CREATE TABLE IF NOT EXISTS position_ledger (
 run_id TEXT NOT NULL, position_id TEXT NOT NULL, symbol TEXT NOT NULL,
 initial_cost REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0,
 total_fees REAL NOT NULL DEFAULT 0, remaining_quantity REAL NOT NULL,
 status TEXT NOT NULL, PRIMARY KEY(run_id, position_id)
);
CREATE TABLE IF NOT EXISTS position_links (
 decision_id TEXT NOT NULL REFERENCES decisions(decision_id), run_id TEXT NOT NULL,
 position_id TEXT NOT NULL, PRIMARY KEY(decision_id, run_id, position_id)
);
"""


class DecisionJournal:
    def __init__(self, path: str, *, clock=None):
        self.path = str(path)
        self.clock = clock or time.time
        self.lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(DDL)
        self.db.commit()

    def create_run(self, metadata: dict, *, run_id=None) -> str:
        run_id = run_id or str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute("INSERT INTO runs(run_id,started_at,metadata_json) VALUES(?,?,?)", (run_id, self.clock(), dumps(metadata)))
            self._event(run_id, None, "run_started", metadata)
        return run_id

    def _event(self, run_id, decision_id, kind, data, event_time=None):
        self.db.execute("INSERT INTO events(run_id,decision_id,event_time,recorded_at,kind,data_json) VALUES(?,?,?,?,?,?)",
                        (run_id, decision_id, self.clock() if event_time is None else event_time, time.time(), kind, dumps(data)))

    def event(self, run_id: str, decision_id: str | None, kind: str, data: dict, *, event_time=None):
        with self.lock, self.db:
            self._event(run_id, decision_id, kind, data, event_time)

    def begin_decision(self, run_id, stage, state, questions, model, *, source="model", parent_id=None, horizons=()):
        decision_id = str(uuid.uuid4())
        as_of = float(state["as_of"])
        symbol = state.get("market", {}).get("symbol")
        position_id = (state.get("position") or {}).get("position_id")
        request = {"model": model, "state": state, "questions": questions}
        with self.lock, self.db:
            self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                decision_id, run_id, parent_id, stage, symbol, position_id, as_of, source,
                dumps(request), digest(request), SCHEMA_VERSION, POLICY_VERSION))
            self._event(run_id, decision_id, "decision_requested", {"stage": stage, "source": source}, as_of)
            if position_id:
                self.db.execute("INSERT OR IGNORE INTO position_links VALUES(?,?,?)", (decision_id, run_id, position_id))
            # A missing reference quote is recorded as missing, never a zero return.
            market = state.get("market") or {}
            reference_ok = (market.get("quote_valid") is True and isinstance(market.get("bid"), (int, float))
                            and isinstance(market.get("ask"), (int, float)) and 0 < market["bid"] <= market["ask"])
            for horizon in horizons:
                name = "market_markout" if symbol else "portfolio_equity"
                equity = state.get("portfolio", {}).get("total_equity")
                equity_ok = (state.get("portfolio_marks_fresh") is True
                             and isinstance(equity, (int, float)) and not isinstance(equity, bool)
                             and math.isfinite(equity) and equity > 0)
                valid = reference_ok if symbol else equity_ok
                data = {"reference_bid": market.get("bid") if reference_ok else None,
                        "reference_ask": market.get("ask") if reference_ok else None,
                        "reference_equity": state.get("portfolio", {}).get("total_equity"),
                        "reference_portfolio_marks_fresh": state.get("portfolio_marks_fresh") is True,
                        "reference_as_of": as_of,
                        "reference_quote_at": market.get("timestamp"),
                        "fee_rate_pct": state.get("costs", {}).get("fee_rate_pct", 0),
                        "slippage_bps": state.get("costs", {}).get("slippage_bps", 0),
                        "label_semantics": "future_market_context_not_causal_action_reward" if symbol else "subsequent_policy_equity_not_isolated_action_reward"}
                self.db.execute("INSERT INTO outcomes VALUES(?,?,?,?,?,?,?)",
                                (decision_id, name, horizon, as_of + horizon, "pending" if valid else "no_reference", None, dumps(data)))
        return decision_id

    def link_position(self, run_id, decision_ids, position: dict):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO position_ledger VALUES(?,?,?,?,?,?,?,?)", (
                run_id, position["position_id"], position["symbol"], position["invested_cost"],
                0.0, 0.0, position["quantity"], "open"))
            for decision_id in dict.fromkeys(x for x in decision_ids if x):
                self.db.execute("INSERT OR IGNORE INTO position_links VALUES(?,?,?)", (decision_id, run_id, position["position_id"]))
                self._event(run_id, decision_id, "position_linked", {"position_id": position["position_id"]})

    def position_fill(self, run_id, position_id, trade, remaining_quantity):
        """Cumulative net realized result, including partial exits, linked to ALL position decisions."""
        with self.lock, self.db:
            existing = self.db.execute("SELECT * FROM position_ledger WHERE run_id=? AND position_id=?", (run_id, position_id)).fetchone()
            if existing is None:
                raise ValueError("Cannot label an unregistered position")
            pnl = existing["realized_pnl"] + float(trade["net_pnl"])
            fees = existing["total_fees"] + float(trade.get("total_fees", 0.0))
            status = "closed" if remaining_quantity <= 1e-12 else "open"
            self.db.execute("UPDATE position_ledger SET realized_pnl=?,total_fees=?,remaining_quantity=?,status=? WHERE run_id=? AND position_id=?",
                            (pnl, fees, remaining_quantity, status, run_id, position_id))
            links = self.db.execute("SELECT decision_id FROM position_links WHERE run_id=? AND position_id=?", (run_id, position_id)).fetchall()
            for link in links:
                data = {"position_id": position_id, "net_realized_pnl_try": pnl, "total_fees_try": fees,
                        "initial_cost_try": existing["initial_cost"], "net_realized_return_pct": pnl / existing["initial_cost"] * 100 if existing["initial_cost"] else None,
                        "remaining_quantity": remaining_quantity,
                        "attribution": "linked_position_lifecycle_not_causal_credit_for_this_decision"}
                self.db.execute("INSERT INTO outcomes VALUES(?,?,?,?,?,?,?) ON CONFLICT(decision_id,name,horizon_seconds) "
                                "DO UPDATE SET status=excluded.status,observed_at=excluded.observed_at,data_json=excluded.data_json",
                                (link["decision_id"], f"position:{position_id}", 0, None, "observed" if status == "closed" else "partial", self.clock(), dumps(data)))
                self._event(run_id, link["decision_id"], "position_outcome", {"status": status, **data})

    def observe(self, run_id, *, symbol=None, market=None, equity=None, max_lateness=60.0):
        now = self.clock()
        query = ("SELECT o.*,d.as_of FROM outcomes o JOIN decisions d USING(decision_id) "
                 "WHERE d.run_id=? AND o.status='pending' AND o.due_at<=? AND ")
        params = [run_id, now]
        if symbol is None:
            query += "d.symbol IS NULL"
        else:
            query += "d.symbol=?"
            params.append(symbol)
        with self.lock, self.db:
            for row in self.db.execute(query, params).fetchall():
                reference = json.loads(row["data_json"])
                status, observed_at, result = None, None, {}
                if symbol is not None and market and market.get("quote_valid") is True:
                    qt = float(market.get("timestamp", 0))
                    bid, ask = float(market.get("bid", 0)), float(market.get("ask", 0))
                    if row["due_at"] <= qt <= now and qt - row["due_at"] <= max_lateness and 0 < bid <= ask:
                        mid0 = (reference["reference_bid"] + reference["reference_ask"]) / 2
                        fee = reference["fee_rate_pct"] / 100
                        slip = reference["slippage_bps"] / 10000
                        net_buy_hold = (bid * (1 - slip) / (reference["reference_ask"] * (1 + slip)) * (1 - fee) ** 2 - 1) * 100
                        result = {"future_bid": bid, "future_ask": ask, "mid_return_pct": (((bid + ask) / 2) / mid0 - 1) * 100,
                                  "hypothetical_buy_hold_net_return_pct": net_buy_hold,
                                  "actual_horizon_seconds": qt - row["as_of"], "lateness_seconds": qt - row["due_at"],
                                  "quote_source": market.get("quote_source"),
                                  "hypothetical_assumptions": "entry_ask_exit_bid_configured_fees_slippage_no_depth_no_latency"}
                        status, observed_at = "observed", qt
                elif symbol is None and equity is not None and equity > 0 and now - row["due_at"] <= max_lateness:
                    result = {"future_equity_try": equity, "equity_change_try": equity - reference["reference_equity"],
                              "equity_return_pct": (equity / reference["reference_equity"] - 1) * 100,
                              "actual_horizon_seconds": now - row["as_of"], "lateness_seconds": now - row["due_at"]}
                    status, observed_at = "observed", now
                if status is None and now - row["due_at"] > max_lateness:
                    status, result = "missing_observation", {"reason": "no_valid_quote_or_equity_within_label_window"}
                if status is not None:
                    data = {**reference, **result}
                    self.db.execute("UPDATE outcomes SET status=?,observed_at=?,data_json=? WHERE decision_id=? AND name=? AND horizon_seconds=?",
                                    (status, observed_at, dumps(data), row["decision_id"], row["name"], row["horizon_seconds"]))
                    self._event(run_id, row["decision_id"], "outcome_updated", {"name": row["name"], "horizon_seconds": row["horizon_seconds"], "status": status, **data}, observed_at)

    def end_run(self, run_id, reason, open_positions=()):
        with self.lock, self.db:
            for row in self.db.execute("SELECT o.* FROM outcomes o JOIN decisions d USING(decision_id) WHERE d.run_id=? AND o.status='pending'", (run_id,)).fetchall():
                data = {**json.loads(row["data_json"]), "reason": reason}
                self.db.execute("UPDATE outcomes SET status='censored',data_json=? WHERE decision_id=? AND name=? AND horizon_seconds=?",
                                (dumps(data), row["decision_id"], row["name"], row["horizon_seconds"]))
                self._event(run_id, row["decision_id"], "outcome_censored", {"name": row["name"], "horizon_seconds": row["horizon_seconds"], "reason": reason})
            for pos in open_positions:
                links = self.db.execute("SELECT decision_id FROM position_links WHERE run_id=? AND position_id=?", (run_id, pos["position_id"])).fetchall()
                for link in links:
                    data = {"position_id": pos["position_id"], "remaining_quantity": pos["quantity"],
                            "reason": "position_still_open_at_run_end", "mark_to_market_not_realized": pos.get("unrealized_pnl")}
                    self.db.execute("INSERT INTO outcomes VALUES(?,?,?,?,?,?,?) ON CONFLICT(decision_id,name,horizon_seconds) "
                                    "DO UPDATE SET status=excluded.status,data_json=excluded.data_json",
                                    (link["decision_id"], f"position_open:{pos['position_id']}", 0, None, "censored", None, dumps(data)))
                    self._event(run_id, link["decision_id"], "position_censored", data)
            self.db.execute("UPDATE runs SET ended_at=?,end_reason=? WHERE run_id=? AND ended_at IS NULL", (self.clock(), reason, run_id))
            self._event(run_id, None, "run_ended", {"reason": reason, "open_positions": len(list(open_positions))})

    def stats(self, run_id=None):
        with self.lock:
            where, args = (" WHERE run_id=?", (run_id,)) if run_id else ("", ())
            n = self.db.execute("SELECT count(*) FROM decisions" + where, args).fetchone()[0]
            last = self.db.execute("SELECT kind,data_json,event_time FROM events" + where + " ORDER BY sequence DESC LIMIT 1", args).fetchone()
            return {"decision_requests": n, "last_event": dict(last) if last else None}

    def records(self, run_id=None):
        """Materialize one request at a time. A caller exporting concurrently should use a read transaction."""
        sql, args = "SELECT * FROM decisions", ()
        if run_id:
            sql += " WHERE run_id=?"
            args = (run_id,)
        for row in self.db.execute(sql + " ORDER BY rowid", args):
            record = dict(row)
            request = json.loads(record.pop("request_json"))
            record["input"] = request
            events = []
            for event in self.db.execute("SELECT * FROM events WHERE decision_id=? ORDER BY sequence", (row["decision_id"],)):
                ev = dict(event)
                ev["data"] = json.loads(ev.pop("data_json"))
                events.append(ev)
            record["events"] = events
            record["outcomes"] = []
            for outcome in self.db.execute("SELECT * FROM outcomes WHERE decision_id=? ORDER BY name,horizon_seconds", (row["decision_id"],)):
                o = dict(outcome)
                o["data"] = json.loads(o.pop("data_json"))
                record["outcomes"].append(o)
            response = next((ev["data"] for ev in reversed(events) if ev["kind"] == "model_response"), None)
            record["teacher"] = response  # Never called ground truth.
            intent_count = sum(ev["kind"] == "execution_intent" for ev in events)
            result_count = sum(ev["kind"] == "execution_result" and ev["data"].get("attribution") != "prebuy_child_execution" for ev in events)
            record["execution_integrity"] = "intent_without_result_reconciliation_required" if intent_count > result_count else "no_unresolved_intent"
            record["teacher_status"] = ("valid_response" if response and response.get("valid") else
                                         "invalid_response" if response else "no_response_or_non_model")
            record["execution"] = [ev["data"] for ev in events if ev["kind"] in ("execution_result", "decision_disposition")]
            metadata = self.db.execute("SELECT metadata_json,started_at,ended_at,end_reason FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            record["run"] = {**dict(metadata), "metadata": json.loads(metadata["metadata_json"])}
            record["run"].pop("metadata_json")
            yield record

    def close(self):
        with self.lock:
            self.db.close()
