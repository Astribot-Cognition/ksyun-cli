import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud


class CloudTests(unittest.TestCase):
    def api(self):
        env = {"KSYUN_ACCESS_KEY_ID": "test-only-key", "KSYUN_SECRET_ACCESS_KEY": "test-only-secret"}
        with patch.dict(cloud.os.environ, env, clear=True):
            return cloud.Api("cn-northwest-3")

    def test_alias_identity_with_env_is_consistent(self):
        env_api = self.api()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            profile = {"name": "direct", "region": "cn-northwest-3", "access_key_id": "test-only-key",
                       "access_key_secret": "test-only-secret"}
            path.write_text(json.dumps({"current": "direct", "profiles": [profile]}))
            with patch.dict(cloud.os.environ, {"KSYUN_CONFIG_FILE": str(path)}, clear=True):
                api = cloud.Api()
                self.assertEqual(api.receipt_identity, env_api.receipt_identity)
                self.assertNotIn("test-only-key", api.receipt_identity)
                with self.assertRaisesRegex(cloud.CloudError, "credentials unavailable"):
                    cloud.Api(api.region, "missing").call("DescribeNotebooks", "2024-06-12", {})

    def test_error_payload_and_url_are_not_echoed(self):
        secret = "test-private-value"
        err = cloud.provider_error("CreateNotebook", 403, {
            "RequestId": "trace-17", "Error": {"Code": "AccessDenied", "Message": secret}})
        self.assertTrue(err.definitely_rejected)
        self.assertFalse(err.retryable)
        self.assertNotIn(secret, str(err))
        self.assertIn("trace-17", str(err))
        self.assertFalse(cloud.provider_error("CreateTrainJob", 503, {}).definitely_rejected)

    def test_runtime_setup_failure_is_known_before_any_network_send(self):
        api = self.api()
        with patch.object(cloud, "load_wheels", side_effect=ValueError("checksum mismatch")), patch.object(api, "_send") as send:
            with self.assertRaises(cloud.CloudError) as ctx:
                api.call("CreateNotebook", "2024-06-12", {})
            self.assertTrue(ctx.exception.definitely_rejected)
            send.assert_not_called()

    def test_transient_read_retried_but_uncertain_creation_sent_only_once(self):
        api = self.api()
        transient = cloud.CloudError("gateway timeout", retryable=True)
        with patch.object(api, "_send", side_effect=[transient, Mock(data='{"Notebooks":[]}', status=200)]) as send, patch.object(cloud.time, "sleep"):
            self.assertEqual(api.call("DescribeNotebooks", "2024-06-12", {}), {"Notebooks": []})
            self.assertEqual(send.call_count, 2)
        with patch.object(api, "_send", side_effect=transient) as send:
            with self.assertRaises(cloud.CloudError):
                api.call("CreateTrainJob", "2025-12-12", {})
            send.assert_called_once()

    def test_page_size_clamp_and_nested_items(self):
        api = self.api()
        data = [{"Data": {"Rows": [1, 2]}, "PageSize": 2, "TotalCount": 3},
                {"Data": {"Rows": [3]}, "PageSize": 2, "TotalCount": 3}]
        with patch.object(api, "call", side_effect=data) as call:
            self.assertEqual(list(api.pages("X", "V", {}, items="Data.Rows")), [1, 2, 3])
            self.assertEqual(call.call_args_list[1].args[-1]["Page"], 2)

    def test_incomplete_pagination_cannot_be_silently_reported_as_complete(self):
        api = self.api()
        for second in ([1], []):
            with self.subTest(second=second), patch.object(api, "call", side_effect=[
                {"Rows": [1], "TotalCount": 3}, {"Rows": second, "TotalCount": 3}]):
                with self.assertRaisesRegex(cloud.CloudError, "incomplete"):
                    list(api.pages("X", "V", {}, items="Rows"))
        with patch.object(api, "call", return_value={"Rows": None, "TotalCount": 0}):
            self.assertEqual(list(api.pages("X", "V", {}, items="Rows")), [])

    def test_route_and_profile_are_explicit(self):
        self.assertEqual(cloud.route("GetLogs", "2020-07-31"), ("klog", "POST", "/GetLogs"))
        with self.assertRaises(ValueError):
            cloud.route("DeleteNotebook", "2024-06-12")


if __name__ == "__main__":
    unittest.main()
