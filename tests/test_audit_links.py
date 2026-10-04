"""All HTTP observations are fake responses; this suite never opens a socket."""

import contextlib
import io
from http.client import BadStatusLine
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import audit_links as audit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/index.md"


class Response(io.BytesIO):
    def __init__(self, code, location=None):
        super().__init__(b"body must never be read")
        self.status = code
        self.headers = {} if location is None else {"Location": location}

    def read(self, *args):
        raise AssertionError("HEAD audit must not consume response bodies")


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, request.get_method(), timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ExtractionTests(unittest.TestCase):
    def test_fixture_extraction_and_conservative_canonical_deduplication(self):
        entries = audit.extract_entries(FIXTURE.read_text())
        self.assertEqual(
            list(entries),
            [
                "https://example.com/list",
                "https://example.com/list?view=all",
                "https://example.com/List",
                "https://github.com/topics/awesome",
            ],
        )
        sources = entries["https://example.com/list"]
        self.assertEqual([item["label"] for item in sources], ["First", "Duplicate"])
        self.assertEqual([item["line"] for item in sources], [7, 8])
        self.assertEqual(sources[0]["url"], "HTTPS://Example.COM:443/list#intro")

    def test_index_rows_fail_closed_on_unsupported_link_syntax_or_scheme(self):
        for row in [
            "| [Reference][id] | text |",
            "| [Broken](https://example.com | text |",
            "| [Local](file:///tmp/example) | text |",
            "| Plain URL https://example.com | text |",
            "| [Login](https://name:password@example.com/a) | text |",
        ]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                audit.extract_entries("| 名称 | 内容 |\n| --- | --- |\n" + row)

    def test_default_report_accounts_for_every_entry_without_network(self):
        with patch.object(audit, "check_url", side_effect=AssertionError("network")):
            report = audit.make_report(FIXTURE, check=False)
        self.assertEqual(report["summary"], {"checked": 0, "failed": 0, "skipped": 4})
        self.assertTrue(report["generated_at"].endswith("Z"))
        for entry in report["entries"].values():
            self.assertEqual(entry["status"], "skipped")
            self.assertEqual(entry["reason"], "network_not_requested")
            self.assertIsNone(entry["attempted_at"])
            self.assertIsNone(entry["checked_at"])
            self.assertIsNone(entry["http_status"])
            self.assertEqual(entry["redirects"], [])

    def test_selected_bounded_run_keeps_unselected_and_budget_skips(self):
        selected = ["https://example.com/list", "https://example.com/List"]
        observed = audit.check_url(selected[0], opener=Opener(Response(200)))
        with patch.object(audit, "check_url", return_value=observed) as checker:
            report = audit.make_report(FIXTURE, check=True, urls=selected, max_checks=1)
        self.assertEqual(checker.call_count, 1)
        self.assertEqual(report["summary"], {"checked": 1, "failed": 0, "skipped": 3})
        self.assertEqual(
            report["entries"][selected[1]]["reason"], "request_budget_exhausted"
        )
        self.assertEqual(
            report["entries"]["https://example.com/list?view=all"]["reason"],
            "not_selected",
        )

    def test_unknown_selection_is_rejected_before_any_http_request(self):
        with patch.object(audit, "check_url", side_effect=AssertionError("network")):
            with self.assertRaises(ValueError):
                audit.make_report(
                    FIXTURE, check=True, urls=["https://example.com/typo"]
                )


class ObservationTests(unittest.TestCase):
    def test_actual_status_categories_include_retryable_limits_and_transient_errors(
        self,
    ):
        for code, status, reason, retryable in [
            (200, "checked", "http_success", False),
            (204, "checked", "http_success", False),
            (404, "failed", "http_not_found", False),
            (410, "failed", "http_not_found", False),
            (429, "failed", "rate_limited", True),
            (408, "failed", "transient_http_error", True),
            (425, "failed", "transient_http_error", True),
            (503, "failed", "transient_http_error", True),
            (401, "failed", "access_denied", False),
            (403, "failed", "access_denied", False),
            (405, "skipped", "head_not_supported", False),
            (418, "failed", "http_error", False),
        ]:
            with self.subTest(code=code):
                response = Response(code)
                opener = Opener(response)
                result = audit.check_url(
                    "https://example.com/a", timeout=2, opener=opener
                )
                self.assertEqual(
                    (result["status"], result["reason"], result["retryable"]),
                    (status, reason, retryable),
                )
                self.assertEqual(result["http_status"], code)
                self.assertTrue(result["checked_at"].endswith("Z"))
                self.assertEqual(result["final_url"], "https://example.com/a")
                self.assertEqual(
                    opener.requests, [("https://example.com/a", "HEAD", 2)]
                )
                self.assertTrue(response.closed)

    def test_redirects_record_each_observed_hop_and_final_url(self):
        first, last = Response(301, "/new#section"), Response(200)
        opener = Opener(first, last)
        result = audit.check_url("https://example.com/old", opener=opener)
        self.assertEqual(result["status"], "checked")
        self.assertEqual(result["final_url"], "https://example.com/new")
        hop = result["redirects"][0]
        self.assertEqual(
            (hop["from"], hop["to"], hop["http_status"]),
            ("https://example.com/old", "https://example.com/new", 301),
        )
        self.assertTrue(hop["observed_at"].endswith("Z"))
        self.assertTrue(first.closed and last.closed)

    def test_http_error_response_is_closed_and_classified(self):
        body = io.BytesIO(b"unused")
        error = HTTPError(
            "https://example.com/a",
            429,
            "Too Many Requests",
            {"Retry-After": "120"},
            body,
        )
        result = audit.check_url(error.url, opener=Opener(error))
        self.assertEqual(result["reason"], "rate_limited")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_after"], "120")
        self.assertTrue(body.closed)

    def test_transport_failures_are_never_reported_as_checked_or_dead(self):
        for error in [
            URLError("DNS unavailable"),
            TimeoutError("timeout"),
            OSError("connection reset"),
            BadStatusLine("invalid HTTP"),
        ]:
            with self.subTest(error=error):
                result = audit.check_url("https://example.com/a", opener=Opener(error))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["reason"], "transport_error")
                self.assertTrue(result["retryable"])
                self.assertIsNone(result["http_status"])
                self.assertIsNone(result["checked_at"])
                self.assertTrue(result["attempted_at"].endswith("Z"))

    def test_redirect_loop_scheme_and_hop_budget_are_explicit_failures(self):
        for responses, expected in [
            ([Response(301, "/a")], "redirect_loop"),
            ([Response(301, "file:///tmp/no")], "invalid_redirect"),
            ([Response(302)], "missing_redirect_location"),
            ([Response(301, f"/{i}") for i in range(6)], "redirect_limit"),
        ]:
            with self.subTest(reason=expected):
                opener = Opener(*responses)
                result = audit.check_url("https://example.com/a", opener=opener)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["reason"], expected)
                self.assertLessEqual(len(opener.requests), 6)
                self.assertTrue(all(r.closed for r in responses))


class CLITests(unittest.TestCase):
    def test_offline_cli_json_and_actual_readme_inventory(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/audit_links.py")],
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(result.stdout)
        self.assertGreater(len(report["entries"]), 0)
        self.assertEqual(report["summary"]["skipped"], len(report["entries"]))
        self.assertEqual(report["summary"]["checked"], 0)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(result.stderr, "")

    def test_invalid_bounds_and_input_fail_without_a_success_report(self):
        for args in [
            ["--timeout", "nan"],
            ["--timeout", "0"],
            ["--max-checks", "101"],
            ["--readme", "/nonexistent/awesome-test.md"],
        ]:
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaises(SystemExit) as error:
                    audit.main(args)
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
