# New tests for the root analysis phase implementation.
"""Offline checks for capped LLM assist paths and graceful fallback."""

from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import types

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_engine.config import AnalysisConfig
from analysis_engine.llm_client import LLMCallResult
from analysis_engine.llm_client import GroqKeyRotatingClient
from analysis_engine.llm_resolution import run_capped_counterparty_assist
from analysis_engine.pipeline import AnalysisPipeline


def test_llm_client_no_keys_is_graceful() -> None:
    os.environ.pop("GROQ_API_KEYS", None)
    os.environ.pop("GROQ_API_KEY", None)
    client = GroqKeyRotatingClient(AnalysisConfig())
    result = client.chat_json([{"role": "user", "content": "{}"}])
    assert not result.ok
    assert "no_keys" in result.error


def test_llm_client_rotates_after_rate_limit() -> None:
    os.environ["GROQ_API_KEYS"] = "first-key,second-key"
    calls: list[str] = []

    class _Message:
        content = '{"ok": true}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def create(self, **kwargs):
            del kwargs
            calls.append(self.api_key)
            if self.api_key == "first-key":
                raise RuntimeError("429 rate limit")
            return _Response()

    class _Chat:
        def __init__(self, api_key: str) -> None:
            self.completions = _Completions(api_key)

    class _FakeGroq:
        def __init__(self, api_key: str) -> None:
            self.chat = _Chat(api_key)

    fake_module = types.ModuleType("groq")
    fake_module.Groq = _FakeGroq
    original = sys.modules.get("groq")
    sys.modules["groq"] = fake_module
    try:
        client = GroqKeyRotatingClient(AnalysisConfig())
        result = client.chat_json([{"role": "user", "content": "{}"}])
    finally:
        if original is None:
            sys.modules.pop("groq", None)
        else:
            sys.modules["groq"] = original
        os.environ.pop("GROQ_API_KEYS", None)

    assert result.ok
    assert result.key_index == 1
    assert calls == ["first-key", "second-key"]
    assert result.rotation_events[0]["reason"] == "rate_limited"


def test_role_a_cap_summary_without_llm() -> None:
    config = AnalysisConfig(enable_llm_fallback=False, llm_role_a_max_calls=5)
    summary = run_capped_counterparty_assist(config, None, candidate_count=12).to_dict()
    assert summary["candidate_count"] == 12
    assert summary["skipped_due_to_cap"] == 7
    assert summary["unavailable"] is True


def test_role_a_positive_candidate_updates_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE accounts(account_id TEXT, account_holder TEXT);
        CREATE TABLE transactions(
            row_id INTEGER PRIMARY KEY,
            txn_id TEXT,
            account_id TEXT,
            date TEXT,
            narration TEXT,
            debit_amount REAL,
            credit_amount REAL,
            eligible_for_detection INTEGER,
            counterparty_account TEXT,
            counterparty_name_raw TEXT,
            counterparty_resolution_method TEXT,
            llm_reasoning TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO accounts(account_id, account_holder) VALUES (?, ?)",
        [("100001", "Alice Sender"), ("200002", "Bob Receiver")],
    )
    connection.execute(
        """
        INSERT INTO transactions(
            row_id, txn_id, account_id, date, narration, debit_amount, credit_amount,
            eligible_for_detection, counterparty_account, counterparty_resolution_method
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1, "t1", "100001", "2025-01-01", "transfer to Bob Receiver", 500.0, 0.0, 1, None, "unresolved"),
    )

    class _FakeClient:
        available = True

        def chat_json(self, messages):
            assert "Bob Receiver" in messages[1]["content"]
            return LLMCallResult(ok=True, content='{"is_match": true, "reasoning": "Name overlap."}')

    summary = run_capped_counterparty_assist(
        AnalysisConfig(llm_role_a_max_calls=5),
        _FakeClient(),
        connection=connection,
    ).to_dict()
    row = connection.execute(
        "SELECT counterparty_account, counterparty_name_raw, counterparty_resolution_method, llm_reasoning FROM transactions WHERE row_id = 1"
    ).fetchone()

    assert summary["candidate_count"] == 1
    assert summary["attempted_calls"] == 1
    assert summary["inferred_edges"] == 1
    assert row["counterparty_account"] == "200002"
    assert row["counterparty_resolution_method"] == "llm_assisted_resolution"
    assert "Name overlap" in row["llm_reasoning"]


def test_pattern_22_not_triggered_shape() -> None:
    rows = [
        {
            "account_number": "100001",
            "account_holder": "Holder 100001",
            "ifsc_code": "TEST0000001",
            "Date": "01/01/2025",
            "Time": "",
            "Narration": "OPENING",
            "Transaction_ID": "",
            "Reference_Number": "",
            "Transaction_Reference": "R1",
            "Cheque_Number": "",
            "Debit": 0,
            "Credit": 1000,
            "Balance": 1000,
            "Transaction_Type": "credit",
            "Bank_Name": "Test Bank",
            "txn_id": "100001_000000",
            "duplicate_of": "",
            "is_reversed": False,
        }
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "one.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        out = Path(tmp) / "out"
        result = AnalysisPipeline(path, out, AnalysisConfig(enable_llm_fallback=False)).run()
        findings = result.findings_by_pattern["22_llm_investigated_anomalies"]
        assert len(findings) == 1
        assert findings[0].details["trigger_status"] == "not_triggered"
        assert findings[0].detection_method == "llm_investigated_anomaly"


if __name__ == "__main__":
    test_llm_client_no_keys_is_graceful()
    test_llm_client_rotates_after_rate_limit()
    test_role_a_cap_summary_without_llm()
    test_role_a_positive_candidate_updates_transaction()
    test_pattern_22_not_triggered_shape()
    print("llm assist and rotation tests passed")
