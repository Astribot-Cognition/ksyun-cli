import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import create


DEV = dict(NotebookName="dev-demo", ResourcePoolId="pool", QueueName="dev-queue",
           AccessType="Creator", ImageSource="Personal", ImageId="image", EnableSSH=False,
           GPUType="GM402", GPUNumber="0.5", CPUNum=8, Memory=64)
TRAIN = dict(TrainJobName="train-demo", ResourcePoolId="pool", QueueName="train-queue", Framework="pytorch",
             Roles=[dict(RoleName="Master", Replicas=1, ImageConfig=dict(ImageId="image", ImageSource="Official"),
                         ResourceConfig=dict(GPUType="GM402", GPUNumber=8, CPUNum=160, Memory=1899),
                         Envs=[dict(Name="SECRET", Value="private-test-value")],
                         RunCommand="python -m train --batch_size=12 --steps=160000")])


class FakeAPI:
    region = "r"
    profile = "p"

    def __init__(self):
        self.calls = []
        self.error = None

    def call(self, action, version, params):
        self.calls.append((action, version, deepcopy(params)))
        if self.error:
            raise self.error
        return {"RequestId": "request", create.ACTIONS["dev" if action == "CreateNotebook" else "train"][3]: "job-id"}


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dev = self.root / "dev.json"
        self.dev.write_text(json.dumps(DEV))
        self.train = self.root / "train.json"
        self.train.write_text(json.dumps(TRAIN))
        self.api = FakeAPI()
        self.parser = argparse.ArgumentParser()
        create.register(self.parser.add_subparsers())
        env = patch.dict(os.environ, {"KSYUN_CLI_STATE_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def run_cmd(self, *args):
        parsed = self.parser.parse_args(["create", *map(str, args)])
        return parsed.handler(parsed, self.api)

    def test_offline_train_4x8_and_exact_export_replay(self):
        exported = self.root / "exact.json"
        preview = self.run_cmd("train", "--spec", self.train, "--nodes", "4", "--save-spec", exported)
        self.assertEqual([], self.api.calls)
        self.assertEqual(0o600, exported.stat().st_mode & 0o777)
        self.assertNotIn("private-test-value", json.dumps(preview))
        self.assertEqual([(r["name"], r["replicas"]) for r in preview["roles"]], [("Master", 1), ("Worker", 3)])
        submitted = self.run_cmd("train", "--spec", exported, "--submit")
        self.assertEqual(submitted["fingerprint"], preview["fingerprint"])
        action, version, body = self.api.calls[0]
        self.assertEqual((action, version), ("CreateTrainJob", "2025-12-12"))
        self.assertEqual(sum(r["Replicas"] * r["ResourceConfig"]["GPUNumber"] for r in body["Roles"]), 32)
        self.assertEqual(body["Roles"][1]["RunCommand"], TRAIN["Roles"][0]["RunCommand"])
        self.assertNotIn("private-test-value", Path(submitted["receipt"]).read_text())

    def test_dev_payload_and_replayed_receipt(self):
        first = self.run_cmd("dev", "--spec", self.dev, "--submit")
        second = self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.assertEqual(self.api.calls, [("CreateNotebook", "2024-06-12", DEV)])
        self.assertTrue(second["reused_receipt"])
        self.assertEqual(first["id"], "job-id")
        self.assertEqual(first["id"], second["id"])

    def test_race_submits_once(self):
        barrier = threading.Barrier(3)
        def submit():
            barrier.wait()
            return self.run_cmd("dev", "--spec", self.dev, "--submit")
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda _: submit(), range(3)))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(sum(r.get("reused_receipt", False) for r in results), 2)

    def test_ambiguous_outcome_suppresses_retry_even_after_client_restart(self):
        self.api.error = TimeoutError("private-test-value")
        with self.assertRaisesRegex(RuntimeError, "unconfirmed") as raised:
            self.run_cmd("train", "--spec", self.train, "--submit")
        self.assertNotIn("private-test-value", str(raised.exception))
        self.api = FakeAPI()
        with self.assertRaisesRegex(ValueError, "earlier submission is unresolved"):
            self.run_cmd("train", "--spec", self.train, "--submit")
        self.assertEqual(self.api.calls, [])

    def test_clear_rejection_can_retry(self):
        class Rejected(RuntimeError):
            definitely_rejected = True
        self.api.error = Rejected("unauthorized")
        with self.assertRaises(Rejected):
            self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.api.error = None
        self.assertEqual(self.run_cmd("dev", "--spec", self.dev, "--submit")["id"], "job-id")

    def test_bad_payload_is_never_posted(self):
        for invalid in [dict(DEV, Status="running"), dict(DEV, ImageId=""), dict(DEV, Memory=True),
                        dict(DEV, EnableSSH=True), dict(DEV, EnableVolume=True)]:
            self.dev.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError):
                self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.assertEqual([], self.api.calls)

    def test_clone_strips_read_only_fields_and_retains_resources_mounts(self):
        src = deepcopy(TRAIN)
        src.update(TrainJobId="old", JobStatus={"Status": "running"}, StorageConfigs=[
            {"StorageConfigId": "storage", "MountType": "Code", "MountPath": "/proj", "MountProtocol": "POSIX",
             "StorageSubPath": "", "ResponseOnly": True}])
        src["Roles"][0].update(IsChiefRole=True, DefaultPort=3333)
        src["Roles"][0]["ImageConfig"]["ImageTagName"] = "name"
        body = create.clone_body("train", src)
        create.check("train", body)
        self.assertEqual(body["StorageConfigs"][0], dict(StorageConfigId="storage", MountType="Code",
                                                        MountPath="/proj", MountProtocol="POSIX", StorageSubPath=""))
        self.assertEqual(body["Roles"], TRAIN["Roles"])
        self.assertNotIn("TrainJobId", body)
        dev = create.clone_body("dev", dict(DEV, GPUNumber=1, RunOnCPU=False, NotebookId="old"))
        self.assertEqual(dev["GPUNumber"], "1")
        self.assertEqual(dev["RunOnCPU"], "false")
        self.assertNotIn("NotebookId", dev)

    def test_patch_and_command_and_node_shrink(self):
        body = deepcopy(TRAIN)
        body["Roles"].append(dict(body["Roles"][0], RoleName="Worker", Replicas=2))
        self.train.write_text(json.dumps(body))
        override = self.root / "patch.json"
        override.write_text(json.dumps({"Priority": "kaic-normal"}))
        cmd = self.root / "script.sh"
        cmd.write_text("python main.py --steps=21\n")
        result = self.run_cmd("train", "--spec", self.train, "--override", override, "--nodes", "1",
                              "--gpus-per-node", "4", "--command-file", cmd, "--submit")
        self.assertEqual(result["status"], "submitted")
        body = self.api.calls[0][2]
        self.assertEqual(body["Priority"], "kaic-normal")
        self.assertEqual(len(body["Roles"]), 1)
        self.assertEqual(body["Roles"][0]["RunCommand"], cmd.read_text())
        self.assertEqual(body["Roles"][0]["ResourceConfig"]["GPUNumber"], 4)

    def test_from_id_through_cli(self):
        row = dict(DEV, NotebookId="original", Status={"State": "running"}, GPUNumber=1)
        with patch.object(self.api, "pages", return_value=iter([row]), create=True) as read:
            data = self.run_cmd("dev", "--from-id", "original", "--name", "cloned", "--submit")
        self.assertEqual(data["name"], "cloned")
        self.assertEqual(read.call_args[0][0], "DescribeNotebooks")
        self.assertEqual(self.api.calls[0][2], dict(DEV, NotebookName="cloned", GPUNumber="1"))

    def test_missing_response_id_suppresses_resubmission(self):
        with patch.object(self.api, "call", return_value={"RequestId": "ambiguous"}) as call:
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                self.run_cmd("dev", "--spec", self.dev, "--submit")
            with self.assertRaisesRegex(ValueError, "earlier submission is unresolved"):
                self.run_cmd("dev", "--spec", self.dev, "--submit")
            self.assertEqual(call.call_count, 1)

    def test_interruption_preserves_pending_receipt(self):
        with patch.object(self.api, "call", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cmd("dev", "--spec", self.dev, "--submit")
        with self.assertRaisesRegex(ValueError, "earlier submission is unresolved"):
            self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.assertEqual(self.api.calls, [])

    def test_expand_only_copies_create_fields_and_rejects_inherited_fixed_rank(self):
        row = deepcopy(TRAIN)
        row["TrainJobId"] = "old"
        row["Roles"][0].update(IsChiefRole=True, DefaultPort=23456, AdditionalPort=[8080])
        with patch.object(self.api, "pages", return_value=iter([row]), create=True):
            self.run_cmd("train", "--from-id", "old", "--name", "new", "--nodes", 4, "--submit")
        master, worker = self.api.calls[0][2]["Roles"]
        self.assertEqual((worker["RoleName"], worker["Replicas"]), ("Worker", 3))
        self.assertEqual(worker["Envs"], master["Envs"])
        for role in (master, worker):
            self.assertTrue({"IsChiefRole", "DefaultPort", "AdditionalPort"}.isdisjoint(role))
        self.assertTrue(row["Roles"][0]["IsChiefRole"])
        row["Roles"][0]["Envs"].append({"Name": "RANK", "Value": "0"})
        with patch.object(self.api, "pages", return_value=iter([row]), create=True):
            with self.assertRaisesRegex(ValueError, "explicit rank"):
                self.run_cmd("train", "--from-id", "old", "--name", "new", "--nodes", 4, "--submit")
        self.assertEqual(len(self.api.calls), 1)

    def test_resize_keeps_distinct_worker_settings(self):
        body = deepcopy(TRAIN)
        worker = deepcopy(body["Roles"][0])
        worker.update(RoleName="Worker", RunCommand="python worker.py",
                      Envs=[{"Name": "PLACE", "Value": "worker"}])
        body["Roles"].append(worker)
        self.train.write_text(json.dumps(body))
        self.run_cmd("train", "--spec", self.train, "--nodes", "4", "--submit")
        resized = self.api.calls[0][2]["Roles"][1]
        self.assertEqual(resized, dict(worker, Replicas=3))

    def test_clone_eip_conflict_can_be_exported_for_editing_before_submit(self):
        row = dict(DEV, NotebookId="source", EnablePublicNetworkSSH=True, EnableSSH=True,
                   SSHAuthorizedKeys="ssh-ed25519 fixture", AllocationId="attached", Status={"State": "running"})
        def run(*flags):
            with patch.object(self.api, "pages", return_value=iter([row]), create=True):
                return self.run_cmd("dev", "--from-id", "source", "--name", "new", *flags)
        self.assertIn("AllocationId", run()["submit_blockers"][0])
        with self.assertRaisesRegex(ValueError, "available EIP"):
            run("--submit")
        draft = self.root / "draft.json"
        self.assertIn("AllocationId", run("--save-spec", draft)["submit_blockers"][0])
        self.assertEqual(json.loads(draft.read_text())["AllocationId"], "attached")
        patch_file = self.root / "override.json"
        patch_file.write_text(json.dumps({"AllocationId": "attached"}))
        with self.assertRaisesRegex(ValueError, "available EIP"):
            run("--override", patch_file, "--submit")
        patch_file.write_text(json.dumps({"AllocationId": "available"}))
        self.assertEqual(run("--override", patch_file, "--submit")["status"], "submitted")
        self.assertEqual(self.api.calls[-1][2]["AllocationId"], "available")
        patch_file.write_text(json.dumps({"EnablePublicNetworkSSH": False}))
        run("--override", patch_file, "--submit")
        self.assertNotIn("AllocationId", self.api.calls[-1][2])
        row["Status"]["State"] = "stopped"
        patch_file.write_text(json.dumps({"AllocationId": "attached"}))
        self.assertTrue(run()["submit_blockers"])
        self.assertEqual(run("--override", patch_file, "--submit")["status"], "submitted")

    def test_masked_password_never_submitted_or_exported(self):
        row = dict(DEV, NotebookId="source", AutoSave=True,
                   AutoSaveConfig={"ImageType": "Official", "UserName": "registry", "Password": "********"})
        with patch.object(self.api, "pages", return_value=iter([row]), create=True):
            preview = self.run_cmd("dev", "--from-id", "source", "--name", "new")
        self.assertIn("Password", preview["submit_blockers"][0])
        self.assertNotIn("********", json.dumps(preview))
        self.dev.write_text(json.dumps({k: v for k, v in row.items() if k != "NotebookId"}))
        with self.assertRaisesRegex(ValueError, "redacted"):
            self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.assertEqual(self.api.calls, [])
        override = self.root / "private.json"
        override.write_text(json.dumps({"AutoSaveConfig": {"Password": "fixture-fresh-secret"}}))
        with patch.object(self.api, "pages", return_value=iter([row]), create=True):
            result = self.run_cmd("dev", "--from-id", "source", "--name", "new", "--override", override, "--submit")
        self.assertEqual(self.api.calls[-1][2]["AutoSaveConfig"]["Password"], "fixture-fresh-secret")
        self.assertNotIn("fixture-fresh-secret", Path(result["receipt"]).read_text())

    def test_receipt_identity_follows_account_not_profile_alias(self):
        self.api.receipt_identity = "account-one"
        a = self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.api.profile = "alias"
        b = self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.assertEqual(len(self.api.calls), 1)
        self.assertTrue(b["reused_receipt"])
        self.api.profile = "p"
        self.api.receipt_identity = "account-two"
        c = self.run_cmd("dev", "--spec", self.dev, "--submit")
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(a["fingerprint"], c["fingerprint"])
        self.assertNotEqual(a["receipt"], c["receipt"])

    def test_cli_transport_uses_versioned_json_for_both_creates(self):
        import cloud
        wire = []
        def simulate_send(api, action, service, req):
            from ksyun.common.http.request import ResponseInternal
            from urllib.parse import parse_qs
            body = json.loads(req.data)
            wire.append((action, service, req.method, req.header["Content-Type"], parse_qs(req.uri_params), body))
            result = {("NotebookId" if action == "CreateNotebook" else "TrainJobId"): "job-id"}
            return ResponseInternal(200, {}, json.dumps(result))
        with patch.dict(os.environ, {"KSYUN_ACCESS_KEY_ID": "fixture", "KSYUN_SECRET_ACCESS_KEY": "fixture",
                                     "KSYUN_PROFILE": ""}), patch.object(cloud.Api, "_send", simulate_send):
            self.api = cloud.Api("region")
            for kind, path in (("dev", self.dev), ("train", self.train)):
                self.run_cmd(kind, "--spec", path, "--submit")
        for (action, service, method, content, query, body), expected in zip(wire, (DEV, TRAIN)):
            version = create.ACTIONS["dev" if action == "CreateNotebook" else "train"][1]
            self.assertEqual((service, method, content), ("aicp", "POST", "application/json"))
            self.assertEqual(query, {"Action": [action], "Version": [version]})
            self.assertEqual({k: body[k] for k in expected}, expected)

    def test_save_refuses_existing_file(self):
        with self.assertRaises(FileExistsError):
            self.run_cmd("train", "--spec", self.train, "--save-spec", self.dev)
        self.assertEqual(json.loads(self.dev.read_text()), DEV)


if __name__ == "__main__":
    unittest.main()
