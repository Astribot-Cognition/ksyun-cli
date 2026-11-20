import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resources


class FixtureAPI:
    region = "cn-northwest-3"

    def __init__(self, **items):
        self.items = items
        self.calls = []

    def pages(self, action, version, params, *, items):
        self.calls.append((action, version, params, items))
        return iter(self.items[items])


def run(api, command):
    parser = argparse.ArgumentParser()
    resources.register(parser.add_subparsers(required=True))
    args = parser.parse_args(["resources", *command.split()])
    return args.handler(args, api)


def worker(i, used=0, **fields):
    return dict({"InstanceId": str(i), "K8sRole": "Worker", "InstanceStatus": "normal",
                 "UnSchedulable": False, "GpuType": "GM402",
                 "Gpu": {"Allocatable": "8", "Allocated": str(used)},
                 "Cpu": {"Allocatable": "194", "Allocated": "8000m"},
                 "Memory": {"Allocatable": "1899", "Allocated": "512Mi"}}, **fields)


def normal_queue():
    return {"QueueId": "q", "QueueType": "normal", "ResourcePoolId": "p", "AllowBorrowing": False,
            "Status": {"State": "normal"}, "Capability": {"GPUInfos": [{"GPUType": "GM402", "GPUNum": 8}]},
            "Allocated": {"GPUNum": 0.0}}


class ResourcesTest(unittest.TestCase):
    def test_distributed_fit_counts_whole_healthy_nodes(self):
        nodes = [worker(0), worker(1, 1), worker(2, UnSchedulable=True), worker(1)]
        result = run(FixtureAPI(ResourcePoolInstanceSet=nodes),
                     "capacity --pool p --gpu-type GM402 --nodes 2 --gpus-per-node 8")
        self.assertEqual(result["fit"], "insufficient")
        self.assertEqual(result["eligible_nodes"], 1)
        self.assertEqual(result["gpu_headroom"]["known_free_by_gpu_type"], {"GM402": 15})
        self.assertEqual(result["nodes"][0]["free_cpu"], 186)
        self.assertEqual(result["nodes"][0]["free_memory_gib"], 1898.5)

    def test_free_memory_and_cpu_can_bind(self):
        api = FixtureAPI(ResourcePoolInstanceSet=[worker(1)])
        for cmd in ("--cpu-per-node 187", "--memory-gib-per-node 1899"):
            with self.subTest(cmd=cmd):
                self.assertEqual(run(api, "capacity --pool p " + cmd)["fit"], "insufficient")

    def test_unknown_values_do_not_become_zero_or_unlimited(self):
        a = worker(1, UnSchedulable=None)
        b = worker(2, Gpu={"Allocatable": "unavailable"})
        result = run(FixtureAPI(ResourcePoolInstanceSet=[a, b]), "capacity --pool p")
        self.assertEqual(result["fit"], "unknown")
        self.assertEqual(result["indeterminate_nodes"], 2)
        self.assertEqual(result["gpu_headroom"]["unmeasured_nodes"], 2)
        self.assertIsNone(result["nodes"][1]["free_gpu"])

    def test_empty_pool_or_missing_queue_is_unknown(self):
        self.assertEqual(run(FixtureAPI(ResourcePoolInstanceSet=[]), "capacity --pool p")["fit"], "unknown")
        result = run(FixtureAPI(ResourcePoolInstanceSet=[worker(1)], QueueSet=[]),
                     "capacity --pool p --queue missing")
        self.assertEqual(result["pool_fit"], "fits")
        self.assertEqual(result["fit"], "unknown")

    def test_queue_quota_and_borrowing(self):
        queue = normal_queue()
        queue["Allocated"]["GPUNum"] = 6.0
        api = FixtureAPI(ResourcePoolInstanceSet=[worker(1)], QueueSet=[queue])
        request = "capacity --pool p --queue q --gpus-per-node 4"
        self.assertEqual(run(api, request)["fit"], "insufficient")
        queue["AllowBorrowing"] = True
        result = run(api, request)
        self.assertEqual(result["fit"], "unknown")
        self.assertEqual(result["queue"]["free_quota_gpu"], 2)

    def test_mixed_model_quota_separates_total_from_per_type(self):
        queue = normal_queue()
        queue["Capability"]["GPUInfos"].append({"GPUType": "OTHER", "GPUNum": 8})
        queue["Allocated"]["GPUNum"] = 8
        api = FixtureAPI(ResourcePoolInstanceSet=[worker(1)], QueueSet=[queue])
        request = "capacity --pool p --queue q --gpus-per-node 8"
        self.assertEqual(run(api, request)["fit"], "fits")
        result = run(api, request + " --gpu-type GM402")
        self.assertIsNone(result["queue"]["free_quota_gpu"])
        self.assertEqual(result["fit"], "unknown")

    def test_partial_permission_keeps_queue_evidence(self):
        class AccessDenied(resources.CloudError):
            status = 403
            code = "AccessDenied"

            def __init__(self):
                RuntimeError.__init__(self, "node list is not visible")

        class Restricted(FixtureAPI):
            def pages(self, action, version, params, *, items):
                if action == "DescribeResourcePoolInstances":
                    raise AccessDenied()
                return super().pages(action, version, params, items=items)

        result = run(Restricted(QueueSet=[normal_queue()]), "capacity --pool p --queue q")
        self.assertEqual(result["queue"]["fit"], "fits")
        self.assertEqual(result["fit"], "unknown")
        self.assertIsNone(result["gpu_headroom"])
        self.assertTrue(result["notes"])

    def test_cpu_only_fit_with_no_gpu_telemetry(self):
        api = FixtureAPI(ResourcePoolInstanceSet=[worker(1, Gpu=None)])
        result = run(api, "capacity --pool p --gpus-per-node 0 --cpu-per-node 1")
        self.assertEqual(result["fit"], "fits")

    def test_queue_pool_mismatch_raises(self):
        api = FixtureAPI(ResourcePoolInstanceSet=[worker(1)], QueueSet=[normal_queue()])
        with self.assertRaisesRegex(ValueError, "selected resource pool"):
            run(api, "capacity --pool other --queue q")

    def test_list_storage_uses_allowlist(self):
        api = FixtureAPI(StorageConfigSet=[{"StorageConfigId": "s", "MountPath": "/data",
                                             "Ak": "example", "Ks3Info": {"Sk": "example"}}])
        self.assertEqual(run(api, "storage"), [{"StorageConfigId": "s", "MountPath": "/data"}])
        self.assertEqual(api.calls, [("DescribeStorageConfigs", "2024-06-12", {}, "StorageConfigSet")])

    def test_queue_pool_filter_before_limit(self):
        api = FixtureAPI(QueueSet=[{"QueueId": "wrong", "ResourcePoolId": "other"}, normal_queue()])
        self.assertEqual(run(api, "queues --pool p --limit 1")[0]["QueueId"], "q")

    def test_list_node_uses_scalar_pool_id(self):
        api = FixtureAPI(ResourcePoolInstanceSet=[])
        self.assertEqual(run(api, "nodes --pool p"), [])
        self.assertEqual(api.calls[0][2], {"ResourcePoolId": "p"})


if __name__ == "__main__":
    unittest.main()
