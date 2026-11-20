import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud
import configure


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "keys", "config.json")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"KSYUN_CONFIG_FILE": str(self.path)}, clear=True).start()
        self.isatty = patch.object(sys.stdin, "isatty", return_value=True).start()
        self.hidden = patch.object(configure.getpass, "getpass", side_effect=["fixture-ak", "fixture-sk"]).start()

    def test_create_load_and_rerun(self):
        with patch("builtins.input", return_value="cn-example-1"):
            status = configure.configure()
        self.assertNotIn("fixture-", status)
        self.assertEqual(self.hidden.call_count, 2)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        api = cloud.Api()
        self.assertEqual((api.region, api.profile, api._ak, api._sk), ("cn-example-1", "default", "fixture-ak", "fixture-sk"))
        before = self.path.stat().st_ino
        configure.configure()
        self.assertEqual(self.path.stat().st_ino, before)
        self.assertEqual(self.hidden.call_count, 2)

    def test_noninteractive_and_check_never_ask_for_keys(self):
        with self.assertRaisesRegex(ValueError, "自己的终端"):
            configure.configure(check=True)
        self.isatty.return_value = False
        with self.assertRaisesRegex(ValueError, "自己的终端"):
            configure.configure(region="cn-example-1")
        self.assertFalse(self.path.exists())
        self.hidden.assert_not_called()

    def test_region_only_and_explicit_profile(self):
        self.path.parent.mkdir()
        other = {"name": "team", "extra": True, "access_key_id": "old-ak", "access_key_secret": "old-sk", "region": "others"}
        original = {"current": "team", "profiles": [other], "future-field": 0}
        self.path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "KSYUN_REGION"):
            configure.configure(check=True)
        configure.configure(region="cn-example-1", check=True)
        self.assertEqual(json.loads(self.path.read_text()), original)
        with patch.dict(os.environ, {"KSYUN_ACCESS_KEY_ID": "unused-ak"}):
            configure.configure("second", "cn-example-2")
        result = json.loads(self.path.read_text())
        self.assertEqual(result["current"], "team")
        self.assertEqual(result["future-field"], 0)
        self.assertEqual(result["profiles"][0], other)
        self.assertEqual(cloud.Api(profile="second")._sk, "fixture-sk")
        with patch("builtins.input", return_value="cn-example-1"):
            configure.configure()
        self.assertEqual(cloud.Api()._sk, "old-sk")
        self.assertEqual(self.hidden.call_count, 2)

    def test_environment_only_and_partial_environment(self):
        with patch.dict(os.environ, {"KSYUN_ACCESS_KEY_ID": "env-ak", "KSYUN_REGION": "cn-example-1"}):
            with self.assertRaisesRegex(ValueError, "环境变量凭证不完整"):
                configure.configure()
            with patch.dict(os.environ, {"KSYUN_SECRET_ACCESS_KEY": "env-sk"}):
                status = configure.configure()
                self.assertIn("环境变量", status)
                self.assertNotIn("env-sk", status)
        self.hidden.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_partial_entry_and_concurrent_change(self):
        self.path.parent.mkdir()
        row = {"name": "default", "access_key_id": "existing-ak", "region": "cn-example-1"}
        self.path.write_text(json.dumps({"current": "default", "profiles": [row]}))
        self.hidden.side_effect = ["first-sk"]
        configure.configure()
        self.assertEqual((cloud.Api()._ak, cloud.Api()._sk), ("existing-ak", "first-sk"))
        with self.assertRaisesRegex(ValueError, "已变更"):
            configure.save_profile("default", row, {"access_key_secret": "other-sk"})
        self.assertEqual(cloud.Api()._sk, "first-sk")

    def test_cancelled_secret_entry_leaves_no_profile(self):
        self.hidden.side_effect = ["fixture-ak", KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            configure.configure(region="cn-example-1")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
