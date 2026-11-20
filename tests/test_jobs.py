"""Regression checks for query scope, output projection, and progress matching."""

import argparse
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import jobs


class SampleAPI:
    region = "example"

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.records = {}

    def call(self, action, version, params):
        self.calls.append((action, version, params))
        return self.responses[action]

    def pages(self, action, version, params, **paging):
        self.calls.append((action, version, params, paging))
        yield from self.records[action]


class JobTests(unittest.TestCase):
    def setUp(self):
        self.api = SampleAPI()
        self.parser = argparse.ArgumentParser()
        jobs.register(self.parser.add_subparsers(required=True))
        self.api.records["DescribeNotebooks"] = [
            {"NotebookId": "dev-b", "NotebookName": "second", "Status": {"State": "running"},
             "EnableSSH": True, "EnablePublicNetworkSSH": False, "PodIp": "10.1.1.1", "ExternalIp": "stale",
             "StorageConfigs": [{"MountPath": "/mnt"}], "SSHPort": 2200, "Envs": [{"Name": "key", "Value": "secret"}]},
        ]
        self.api.records["DescribeTrainJobs"] = [
            {"TrainJobId": "job-a", "TrainJobName": "run", "JobStatus": {"Status": "succeeded",
             "SubmitTime": "2020-01-01 12:13:00"}, "ResourcePoolId": "pool"},
            {"TrainJobId": "job-b", "TrainJobName": "run-2", "JobStatus": {"Status": "running",
             "SubmitTime": "2025-01-01 12:13:00"}, "ResourcePoolId": "pool", "Roles": [
                 {"RoleName": "Master", "Replicas": 1, "RunCommand": "train --steps=500",
                  "Envs": [{"Name": "APP", "Value": "sensitive"}]}]}
        ]
        self.api.records["DescribeTrainJobPods"] = [
            {"PodName": "w-0", "Role": "Worker"}, {"PodName": "m-0", "Role": "Master"}]
        self.api.responses["DescribeTrainJobPodLogs"] = {"RequestId": "req", "PodLogs":
            "step:2000 loss=1\nIter 20/100 loss=3\nsteps:200 loss=2\nstep:20 loss=4\nnum_steps=20\n"}
        self.api.responses["DescribeNotebookLog"] = {"PodLogs": "started"}
        self.api.records["DescribeResourcePools"] = [{"ResourcePoolId": "pool", "EnableKlog": False}]

    def query(self, *args):
        options = self.parser.parse_args(args)
        return options.handler(options, self.api)

    def test_ambiguous_exact_name_lists_candidates(self):
        self.api.records["DescribeTrainJobs"].append({"TrainJobId": "job-c", "TrainJobName": "run"})
        with self.assertRaisesRegex(ValueError, "job-a, job-c"):
            self.query("train", "get", "--name", "run")

    def test_ids_verified_even_if_platform_filters_are_ignored(self):
        with self.assertRaisesRegex(ValueError, "No matching train"):
            self.query("train", "get", "--id", "other")
        self.assertEqual(self.api.calls[0][2]["TrainJobId.1"], "other")

    def test_dev_ssh_ignores_stale_public_address(self):
        info = self.query("dev", "connect", "--id", "dev-b")
        self.assertNotIn("ExternalIp", info)
        self.assertEqual((info["SSHPort"], info["PodIp"]), (2200, "10.1.1.1"))

    def test_signed_url_without_a_list_request(self):
        self.api.responses["GetWebIdeUrl"] = {"JupyterWebUrl": "https://example.com"}
        self.query("dev", "connect", "--id", "dev-b", "--kind", "ide")
        self.assertEqual(self.api.calls, [("GetWebIdeUrl", jobs.API, {"NotebookId": "dev-b", "ExpirationMinute": "15"})])

    def test_recent_jobs_and_status(self):
        info = self.query("train", "list", "--status", "RUNNING", "--limit", "1")
        self.assertEqual([r["TrainJobId"] for r in info["jobs"]], ["job-b"])
        self.assertEqual(self.query("train", "list", "--since-hours", "1")["jobs"], [])
        self.assertEqual(self.query("train", "list", "--limit", "1")["jobs"][0]["TrainJobId"], "job-b")

    def test_detail_returns_commands_without_role_env_values(self):
        row = self.query("train", "get", "--id", "job-b")
        self.assertEqual(row["Roles"][0]["RunCommand"], "train --steps=500")
        self.assertNotIn("Envs", row["Roles"][0])
        self.assertNotIn("Envs", self.query("dev", "get", "--name", "second"))

    def test_master_log_exact_iteration_and_pagination(self):
        data = self.query("train", "logs", "--id", "job-b", "--iteration", "20")
        self.assertEqual(data["lines"], ["Iter 20/100 loss=3", "step:20 loss=4"])
        action, version, params, page = self.api.calls[1]
        self.assertEqual((action, version, params), ("DescribeTrainJobPods", jobs.API, {"TrainJobId": "job-b"}))
        self.assertEqual((page["page"], page["size"], page["start"]), ("Marker", "MaxResults", 1))

    def test_explicit_pool_and_pod_still_check_membership(self):
        result = self.query("train", "logs", "--id", "job-b", "--pool-id", "pool", "--pod", "w-0",
                            "--contains", "does not exist")
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["lines_in_window"], 5)
        self.assertEqual(self.api.calls[0][0], "DescribeTrainJobPods")
        self.assertEqual(self.api.calls[-1][2]["SinceSeconds"], 1800)
        self.assertTrue(result["truncated"])

    def test_empty_or_unrecognized_log_response(self):
        self.api.responses["DescribeNotebookLog"] = {}
        with self.assertRaisesRegex(ValueError, "no PodLogs text"):
            self.query("dev", "logs", "--id", "dev-b", "--source", "aicp")

    def test_enabled_klog_only_returns_matching_pod(self):
        self.api.responses["DescribeTrainJobPodLogs"] = {"PodLogs": ""}
        self.api.records["DescribeResourcePools"] = [{"ResourcePoolId": "pool", "EnableKlog": True,
                                                     "LogProjectName": "proj", "LogPoolName": "logs"}]
        self.api.responses["GetLogs"] = {"Total": 2, "Logs": [
            {"podName": "sibling", "message": "step:20 in m-0"},
            {"podName": "m-0", "message": "step:20 loss=3"}]}
        r = self.query("train", "logs", "--id", "job-b", "--pod", "m-0", "--iteration", "20")
        action, version, params = self.api.calls[-1]
        self.assertEqual((action, version), ("GetLogs", "2020-07-31"))
        self.assertEqual(params["To"] - params["From"], 86400 * 1000)
        self.assertEqual(params["Query"], 'podName:"m-0"')
        self.assertEqual(r["backend"], "klog")
        self.assertEqual(r["lines"], ["step:20 loss=3"])

    def test_development_klog_preserves_window_and_avoids_kubernetes_call(self):
        self.api.records["DescribeNotebooks"][0]["ResourcePoolId"] = "pool"
        self.api.records["DescribeResourcePools"] = [{"ResourcePoolId": "pool", "EnableKlog": True,
                                                     "LogProjectName": "proj", "LogPoolName": "logs"}]
        self.api.responses["GetLogs"] = {"Logs": [{"podName": "dev-b-0", "message": "server ready"}]}
        call = self.api.call
        def fail_notebook_log(action, version, params):
            if action == "DescribeNotebookLog":
                raise RuntimeError("K8sAccessFault")
            return call(action, version, params)
        self.api.call = fail_notebook_log
        result = self.query("dev", "logs", "--id", "dev-b", "--contains", "ready",
                            "--until", "2026-11-20T23:00:00+08:00")
        self.assertNotIn("DescribeNotebookLog", [c[0] for c in self.api.calls])
        self.assertEqual(self.api.calls[-1][2]["To"], 1795186800000)
        self.assertEqual(result["lines"], ["server ready"])
        self.assertEqual(self.api.calls[-1][2]["Query"], 'podName:"dev-b-0"')

    def test_klog_rejects_cross_job_pod(self):
        self.api.records["DescribeResourcePools"] = [{"ResourcePoolId": "pool", "EnableKlog": True}]
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.query("train", "logs", "--id", "job-b", "--pod", "sibling", "--source", "klog")

    def test_aicp_does_not_read_cross_job_pod(self):
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.query("train", "logs", "--id", "job-b", "--pod", "sibling", "--source", "aicp")
        self.assertNotIn("DescribeTrainJobPodLogs", [c[0] for c in self.api.calls])

    def test_klog_truncation_reported_with_no_match(self):
        self.api.records["DescribeResourcePools"] = [{"ResourcePoolId": "pool", "EnableKlog": True,
                                                     "LogProjectName": "proj", "LogPoolName": "logs"}]
        self.api.responses["GetLogs"] = {"Total": 20000, "Logs": [{"podName": "m-0", "message": "step:200"}]}
        r = self.query("train", "logs", "--id", "job-b", "--iteration", "20", "--tail", "1")
        self.assertEqual(r["lines"], [])
        self.assertTrue(r["truncated"])
        self.assertIn("--until", r["coverage_hint"])

    def test_klog_tail_counts_lines_before_iteration_matching(self):
        self.api.records["DescribeResourcePools"] = [{"ResourcePoolId": "pool", "EnableKlog": True,
                                                     "LogProjectName": "proj", "LogPoolName": "logs"}]
        self.api.responses["GetLogs"] = {"Total": 2, "Logs": [
            {"podName": "m-0", "message": "step:40", "_timestamp_": 2},
            {"podName": "m-0", "message": "step:20\nstep:30", "_timestamp_": 1}]}
        base = ("train", "logs", "--id", "job-b", "--tail", "2")
        result = self.query(*base)
        self.assertEqual(result["lines"], ["step:30", "step:40"])
        self.assertEqual(result["omitted_lines_from_response"], 1)
        self.assertEqual(result["lines_in_window"], 2)
        self.assertTrue(result["truncated"])
        no_match = self.query(*base, "--iteration", "20")
        self.assertEqual(no_match["lines"], [])
        self.assertIn("--tail", no_match["coverage_hint"])

    def test_aicp_tail_crops_an_oversized_response_before_text_match(self):
        result = self.query("train", "logs", "--id", "job-b", "--source", "aicp", "--tail", "2",
                            "--contains", "loss=1")
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["lines_in_window"], 2)
        self.assertEqual(result["omitted_lines_from_response"], 3)
        self.assertTrue(result["truncated"])

    def test_unknown_klog_field_rejected(self):
        self.api.records["DescribeResourcePools"] = [{"ResourcePoolId": "pool", "EnableKlog": True,
                                                     "LogProjectName": "proj", "LogPoolName": "logs"}]
        self.api.responses["GetLogs"] = {"Logs": [{"log": "m-0 step:20"}]}
        with self.assertRaisesRegex(ValueError, "pod identity"):
            self.query("train", "logs", "--id", "job-b", "--source", "klog")

    def test_no_guessing_master_when_no_pod_exists(self):
        self.api.records["DescribeTrainJobPods"] = []
        with self.assertRaisesRegex(ValueError, "unique Master"):
            self.query("train", "logs", "--id", "job-b")

    def test_official_train_event_pool_and_id(self):
        self.api.responses["DescribeTrainJobEvents"] = {"DataSet": []}
        self.query("train", "events", "--name", "run")
        self.assertEqual(self.api.calls[-1], ("DescribeTrainJobEvents", jobs.API, {
            "ResourcePoolId": "pool", "TrainJobId": "job-a"}))


if __name__ == "__main__":
    unittest.main()
