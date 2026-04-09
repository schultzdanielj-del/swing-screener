"""
Tests for grind_uploader — covers all 5 defensive failure modes.

Run:  python tests/test_grind_uploader.py

Uses a mock HTTP server so no real Railway calls are made.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Setup path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "local_runner"))
import grind_uploader


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════

def make_result(n_conditions=10, setup="dtss", blackout=False):
    """Build a valid grind result dict."""
    return {
        "setup_type": setup,
        "timestamp": "2026-03-08T12:00:00+00:00",
        "n_conditions": n_conditions,
        "all_conditions": [
            {
                "tier": "D1" if i < 3 else "T3",
                "expression_name": f"expr_{i}",
                "low": -1.0 + i * 0.1,
                "high": 1.0 + i * 0.1,
                "filter_power": 0.95 - i * 0.01,
            }
            for i in range(n_conditions)
        ],
        "examples_passing": 72,
        "examples_failing": 0,
        "params": {
            "beam_width": 10000,
            "depth": 100,
            "peak_target": 3,
            "multi_pass": True,
            "blackout": blackout,
            "source": "pyramid_grinder",
        },
        "summary": {"final_total": 168, "final_peak": 3, "final_avg": 1.4},
    }


def make_temp_json(result):
    """Write result to a temp file, return path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(result, f)
    return path


class MockResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


# ════════════════════════════════════════════════════════════════
# Test: Validation (#3 defense — schema mismatch)
# ════════════════════════════════════════════════════════════════

class TestValidation(unittest.TestCase):

    def test_valid_result(self):
        ok, err = grind_uploader.validate_result(make_result())
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_missing_top_keys(self):
        result = {"setup_type": "dtss"}  # missing all_conditions etc
        ok, err = grind_uploader.validate_result(result)
        self.assertFalse(ok)
        self.assertIn("Missing top-level keys", err)

    def test_empty_conditions(self):
        result = make_result()
        result["all_conditions"] = []
        ok, err = grind_uploader.validate_result(result)
        self.assertFalse(ok)
        self.assertIn("empty", err)

    def test_condition_missing_keys(self):
        result = make_result()
        result["all_conditions"][0] = {"tier": "D1"}  # missing expression_name, low, high
        ok, err = grind_uploader.validate_result(result)
        self.assertFalse(ok)
        self.assertIn("Condition 0 missing keys", err)

    def test_not_a_dict(self):
        ok, err = grind_uploader.validate_result("not a dict")
        self.assertFalse(ok)
        self.assertIn("expected dict", err)

    def test_conditions_not_a_list(self):
        result = make_result()
        result["all_conditions"] = "not a list"
        ok, err = grind_uploader.validate_result(result)
        self.assertFalse(ok)
        self.assertIn("expected list", err)


# ════════════════════════════════════════════════════════════════
# Test: Hashing (#5 defense — manual edit detection)
# ════════════════════════════════════════════════════════════════

class TestHashing(unittest.TestCase):

    def test_hash_returns_hex(self):
        result = make_result()
        path = make_temp_json(result)
        try:
            h = grind_uploader.compute_hash(path)
            self.assertIsNotNone(h)
            self.assertEqual(len(h), 64)  # SHA-256 hex
        finally:
            os.unlink(path)

    def test_hash_nonexistent_file(self):
        h = grind_uploader.compute_hash("/nonexistent/file.json")
        self.assertIsNone(h)

    def test_hash_deterministic(self):
        result = make_result()
        path = make_temp_json(result)
        try:
            h1 = grind_uploader.compute_hash(path)
            h2 = grind_uploader.compute_hash(path)
            self.assertEqual(h1, h2)
        finally:
            os.unlink(path)


# ════════════════════════════════════════════════════════════════
# Test: Successful upload (happy path)
# ════════════════════════════════════════════════════════════════

class TestSuccessfulUpload(unittest.TestCase):

    @patch("grind_uploader.requests")
    def test_happy_path(self, mock_requests):
        """Full upload succeeds — create, conditions, verify, activate, patch."""
        result = make_result()
        path = make_temp_json(result)

        # Mock all HTTP calls to succeed
        def mock_post(url, json=None, timeout=None):
            if "/conditions" in url:
                return MockResponse(200, {"inserted": 10})
            if "/activate" in url:
                return MockResponse(200, {"activated": True})
            # create cycle
            return MockResponse(200, {"cycle_id": "test", "created": True})

        def mock_get(url, timeout=None):
            return MockResponse(200, {"conditions": [{}] * 10})

        def mock_patch(url, json=None, timeout=None):
            return MockResponse(200, {"updated": ["status"]})

        mock_requests.post = mock_post
        mock_requests.get = mock_get
        mock_requests.patch = mock_patch
        mock_requests.RequestException = Exception

        try:
            # Ensure no pending dir interferes
            orig_pending = grind_uploader.PENDING_DIR
            grind_uploader.PENDING_DIR = tempfile.mkdtemp()

            cycle_id = grind_uploader.upload(
                result=result,
                result_path=path,
                step_type="signal_grind",
                setup_type="dtss",
            )
            self.assertIsNotNone(cycle_id)
            self.assertIn("dtss_signal_grind_", cycle_id)

            # No pending files should exist
            pending_files = os.listdir(grind_uploader.PENDING_DIR)
            self.assertEqual(len(pending_files), 0)
        finally:
            os.unlink(path)
            shutil.rmtree(grind_uploader.PENDING_DIR, ignore_errors=True)
            grind_uploader.PENDING_DIR = orig_pending


# ════════════════════════════════════════════════════════════════
# Test: Network failure → pending file (#1/#2 defense)
# ════════════════════════════════════════════════════════════════

class TestNetworkFailure(unittest.TestCase):

    @patch("grind_uploader.requests")
    def test_network_failure_saves_pending(self, mock_requests):
        """All HTTP calls fail → pending upload file created."""
        result = make_result()
        path = make_temp_json(result)

        def mock_fail(*args, **kwargs):
            raise Exception("Connection refused")

        mock_requests.post = mock_fail
        mock_requests.get = mock_fail
        mock_requests.patch = mock_fail
        mock_requests.RequestException = Exception

        orig_pending = grind_uploader.PENDING_DIR
        grind_uploader.PENDING_DIR = tempfile.mkdtemp()

        try:
            # Override retry backoff to speed up test
            orig_backoff = grind_uploader.RETRY_BACKOFF
            grind_uploader.RETRY_BACKOFF = [0, 0, 0]

            cycle_id = grind_uploader.upload(
                result=result,
                result_path=path,
                step_type="signal_grind",
                setup_type="dtss",
            )
            self.assertIsNone(cycle_id)

            # Pending file should exist
            pending_files = [f for f in os.listdir(grind_uploader.PENDING_DIR) if f.endswith(".json")]
            self.assertEqual(len(pending_files), 1)

            # Verify pending file content
            with open(os.path.join(grind_uploader.PENDING_DIR, pending_files[0])) as f:
                pending = json.load(f)
            self.assertEqual(pending["step_type"], "signal_grind")
            self.assertEqual(pending["setup_type"], "dtss")
            self.assertIn("result", pending)

        finally:
            os.unlink(path)
            shutil.rmtree(grind_uploader.PENDING_DIR, ignore_errors=True)
            grind_uploader.PENDING_DIR = orig_pending
            grind_uploader.RETRY_BACKOFF = orig_backoff


# ════════════════════════════════════════════════════════════════
# Test: Partial upload → error status (#4 defense)
# ════════════════════════════════════════════════════════════════

class TestPartialUpload(unittest.TestCase):

    @patch("grind_uploader.requests")
    def test_conditions_upload_fails_marks_error(self, mock_requests):
        """Cycle created but conditions fail → cycle marked as error."""
        result = make_result()
        path = make_temp_json(result)

        call_log = []

        def mock_post(url, json=None, timeout=None):
            call_log.append(("POST", url))
            if "/conditions" in url:
                return MockResponse(500, {"error": "DB full"})
            return MockResponse(200, {"cycle_id": "test", "created": True})

        def mock_patch(url, json=None, timeout=None):
            call_log.append(("PATCH", url, json))
            return MockResponse(200, {"updated": True})

        def mock_get(url, timeout=None):
            return MockResponse(200, {"conditions": []})

        mock_requests.post = mock_post
        mock_requests.patch = mock_patch
        mock_requests.get = mock_get
        mock_requests.RequestException = Exception

        orig_pending = grind_uploader.PENDING_DIR
        grind_uploader.PENDING_DIR = tempfile.mkdtemp()
        orig_backoff = grind_uploader.RETRY_BACKOFF
        grind_uploader.RETRY_BACKOFF = [0, 0, 0]

        try:
            cycle_id = grind_uploader.upload(
                result=result,
                result_path=path,
                step_type="signal_grind",
                setup_type="dtss",
            )
            self.assertIsNone(cycle_id)

            # Verify error was marked via PATCH
            error_patches = [c for c in call_log if c[0] == "PATCH" and len(c) > 2 and c[2] and c[2].get("status") == "error"]
            self.assertGreater(len(error_patches), 0, "Should have PATCHed status to error")

        finally:
            os.unlink(path)
            shutil.rmtree(grind_uploader.PENDING_DIR, ignore_errors=True)
            grind_uploader.PENDING_DIR = orig_pending
            grind_uploader.RETRY_BACKOFF = orig_backoff


# ════════════════════════════════════════════════════════════════
# Test: Verification mismatch (#4 defense — count check)
# ════════════════════════════════════════════════════════════════

class TestVerificationMismatch(unittest.TestCase):

    @patch("grind_uploader.requests")
    def test_readback_count_mismatch_fails(self, mock_requests):
        """Conditions uploaded but read-back count doesn't match → fail."""
        result = make_result(n_conditions=10)
        path = make_temp_json(result)

        def mock_post(url, json=None, timeout=None):
            if "/conditions" in url:
                return MockResponse(200, {"inserted": 10})
            return MockResponse(200, {"cycle_id": "test", "created": True})

        def mock_get(url, timeout=None):
            # Return wrong count — only 5 instead of 10
            return MockResponse(200, {"conditions": [{}] * 5})

        def mock_patch(url, json=None, timeout=None):
            return MockResponse(200, {"updated": True})

        mock_requests.post = mock_post
        mock_requests.get = mock_get
        mock_requests.patch = mock_patch
        mock_requests.RequestException = Exception

        orig_pending = grind_uploader.PENDING_DIR
        grind_uploader.PENDING_DIR = tempfile.mkdtemp()
        orig_backoff = grind_uploader.RETRY_BACKOFF
        grind_uploader.RETRY_BACKOFF = [0, 0, 0]

        try:
            cycle_id = grind_uploader.upload(
                result=result,
                result_path=path,
                step_type="signal_grind",
                setup_type="dtss",
            )
            self.assertIsNone(cycle_id)

            # Should have saved pending
            pending_files = [f for f in os.listdir(grind_uploader.PENDING_DIR) if f.endswith(".json")]
            self.assertEqual(len(pending_files), 1)

        finally:
            os.unlink(path)
            shutil.rmtree(grind_uploader.PENDING_DIR, ignore_errors=True)
            grind_uploader.PENDING_DIR = orig_pending
            grind_uploader.RETRY_BACKOFF = orig_backoff


# ════════════════════════════════════════════════════════════════
# Test: Schema validation blocks upload (#3 defense)
# ════════════════════════════════════════════════════════════════

class TestSchemaValidationBlocksUpload(unittest.TestCase):

    @patch("grind_uploader.requests")
    def test_bad_schema_skips_upload(self, mock_requests):
        """Invalid result structure → no HTTP calls made, no pending file."""
        result = {"setup_type": "dtss"}  # missing required keys
        path = make_temp_json(result)

        call_log = []

        def mock_post(url, json=None, timeout=None):
            call_log.append(url)
            return MockResponse(200, {})

        mock_requests.post = mock_post
        mock_requests.RequestException = Exception

        orig_pending = grind_uploader.PENDING_DIR
        grind_uploader.PENDING_DIR = tempfile.mkdtemp()

        try:
            cycle_id = grind_uploader.upload(
                result=result,
                result_path=path,
                step_type="signal_grind",
                setup_type="dtss",
            )
            self.assertIsNone(cycle_id)

            # No HTTP calls should have been made (except maybe pending retry)
            cycle_calls = [u for u in call_log if "/api/v2/cycles" in u and "/conditions" not in u and "/activate" not in u]
            self.assertEqual(len(cycle_calls), 0, "No cycle creation should happen on bad schema")

            # No pending file — this isn't a transient failure, it's a code bug
            pending_files = [f for f in os.listdir(grind_uploader.PENDING_DIR) if f.endswith(".json")]
            self.assertEqual(len(pending_files), 0)

        finally:
            os.unlink(path)
            shutil.rmtree(grind_uploader.PENDING_DIR, ignore_errors=True)
            grind_uploader.PENDING_DIR = orig_pending


# ════════════════════════════════════════════════════════════════
# Test: Pending retry works (#1/#2 defense — persistent queue)
# ════════════════════════════════════════════════════════════════

class TestPendingRetry(unittest.TestCase):

    @patch("grind_uploader.requests")
    def test_retry_pending_succeeds(self, mock_requests):
        """A pending upload file gets retried and uploaded successfully."""
        result = make_result()

        # Create a pending file manually
        pending_dir = tempfile.mkdtemp()
        orig_pending = grind_uploader.PENDING_DIR
        grind_uploader.PENDING_DIR = pending_dir

        pending_data = {
            "cycle_id": "dtss_signal_grind_20260308_120000",
            "step_type": "signal_grind",
            "setup_type": "dtss",
            "source_hash": "abc123",
            "activate": True,
            "grind_params": {"beam_width": 10000},
            "result": result,
        }
        pending_path = os.path.join(pending_dir, "dtss_signal_grind_20260308_120000.json")
        with open(pending_path, "w") as f:
            json.dump(pending_data, f)

        # Mock HTTP to succeed
        def mock_post(url, json=None, timeout=None):
            if "/conditions" in url:
                return MockResponse(200, {"inserted": 10})
            if "/activate" in url:
                return MockResponse(200, {"activated": True})
            return MockResponse(200, {"cycle_id": "test", "created": True})

        def mock_get(url, timeout=None):
            return MockResponse(200, {"conditions": [{}] * 10})

        def mock_patch(url, json=None, timeout=None):
            return MockResponse(200, {"updated": True})

        mock_requests.post = mock_post
        mock_requests.get = mock_get
        mock_requests.patch = mock_patch
        mock_requests.RequestException = Exception

        try:
            count = grind_uploader.retry_pending()
            self.assertEqual(count, 1)

            # Pending file should be removed after success
            self.assertFalse(os.path.exists(pending_path))

        finally:
            shutil.rmtree(pending_dir, ignore_errors=True)
            grind_uploader.PENDING_DIR = orig_pending


# ════════════════════════════════════════════════════════════════
# Test: Upload never raises (#0 defense — grind never blocked)
# ════════════════════════════════════════════════════════════════

class TestNeverRaises(unittest.TestCase):

    @patch("grind_uploader.requests")
    def test_upload_never_raises_on_total_failure(self, mock_requests):
        """Even with catastrophic failures, upload() returns None, never raises."""
        result = make_result()
        path = make_temp_json(result)

        def mock_explode(*args, **kwargs):
            raise RuntimeError("Total catastrophe")

        mock_requests.post = mock_explode
        mock_requests.get = mock_explode
        mock_requests.patch = mock_explode
        mock_requests.RequestException = Exception

        orig_pending = grind_uploader.PENDING_DIR
        grind_uploader.PENDING_DIR = "/dev/null/impossible"  # pending save will also fail
        orig_backoff = grind_uploader.RETRY_BACKOFF
        grind_uploader.RETRY_BACKOFF = [0, 0, 0]

        try:
            # This should NOT raise
            cycle_id = grind_uploader.upload(
                result=result,
                result_path=path,
                step_type="signal_grind",
                setup_type="dtss",
            )
            self.assertIsNone(cycle_id)
        finally:
            os.unlink(path)
            grind_uploader.PENDING_DIR = orig_pending
            grind_uploader.RETRY_BACKOFF = orig_backoff


if __name__ == "__main__":
    unittest.main(verbosity=2)
