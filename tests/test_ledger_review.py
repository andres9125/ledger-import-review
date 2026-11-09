import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ledger_review import EXAMPLE_SUGGESTIONS, build_plan, suggest_with_ollama

HEADER = "entry_id,property_code,period,amount_eur,status\n"
SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ledger.csv"


class ImportPlanTests(unittest.TestCase):
    def test_fixture_separates_ready_and_review_and_records_source_hash(self):
        source = SAMPLE.read_bytes()
        report = build_plan(source, EXAMPLE_SUGGESTIONS.get, "offline_example")

        self.assertEqual(report["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(report["counts"], {"input": 7, "ready": 2, "review": 5})
        self.assertEqual(report["ready"][0]["amount_minor"], 125000)
        self.assertEqual(report["review"], [
            {"record": 2, "reasons": ["unmapped_status"], "suggested_status": "open"},
            {"record": 3, "reasons": ["unmapped_status"], "suggestion": "unresolved"},
            {"record": 4, "reasons": ["invalid_amount"]},
            {"record": 5, "reasons": ["duplicate_entry_id"]},
            {"record": 6, "reasons": ["duplicate_entry_id"]},
        ])

    def test_provider_failure_stays_in_review_and_other_rows_are_processed(self):
        def fail(_label):
            raise ValueError("test provider failure")

        report = build_plan(SAMPLE.read_bytes(), fail, "test")

        self.assertEqual(report["counts"]["ready"], 2)
        self.assertEqual(report["review"][0]["suggestion"], "provider_error")

    def test_a_short_duplicate_still_blocks_the_well_formed_record(self):
        source = (HEADER + "A-1,P-1,2026-10,1.00,open\nA-1,P-2\n").encode()
        report = build_plan(source, EXAMPLE_SUGGESTIONS.get, "offline_example")

        self.assertEqual(report["ready"], [])
        self.assertEqual(report["review"][0]["reasons"], ["duplicate_entry_id"])
        self.assertEqual(report["review"][1]["reasons"], ["invalid_column_count"])

    def test_invalid_schema_or_csv_fails_before_creating_a_plan(self):
        for source in (b"id,status\nA,paid", (HEADER + '"unterminated').encode()):
            with self.subTest(source=source), self.assertRaises(ValueError):
                build_plan(source, EXAMPLE_SUGGESTIONS.get, "offline_example")


class OllamaTests(unittest.TestCase):
    @patch("ledger_review.http.client.HTTPConnection")
    def test_sends_only_status_field_and_accepts_structured_output(self, connect):
        connection = connect.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"done":true,"response":"{\\"status\\":\\"open\\"}"}'

        self.assertEqual(suggest_with_ollama("outstanding", "demo-model"), "open")

        connect.assert_called_once_with("127.0.0.1", 11434, timeout=5)
        method, path, payload, _headers = connection.request.call_args.args
        request = json.loads(payload)
        self.assertEqual((method, path), ("POST", "/api/generate"))
        self.assertEqual(json.loads(request["prompt"]), {"status_label": "outstanding"})
        self.assertFalse(request["stream"])
        connection.close.assert_called_once()

    @patch("ledger_review.http.client.HTTPConnection")
    def test_rejects_invalid_or_oversized_model_responses(self, connect):
        response = connect.return_value.getresponse.return_value
        response.status = 200
        for payload in (b"{}", b" " * 65537, b'{"done":true,"response":"{}"}'):
            response.read.return_value = payload
            with self.subTest(length=len(payload)), self.assertRaises(ValueError):
                suggest_with_ollama("outstanding", "demo-model")


if __name__ == "__main__":
    unittest.main()
