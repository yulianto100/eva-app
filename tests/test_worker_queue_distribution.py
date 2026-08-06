import fnmatch
import json
import re
from pathlib import Path

from jobs import RedisJobStore


class FakeRedis:
    def __init__(self, values):
        self.values = dict(values)

    def scan_iter(self, match, count=200):
        return [key for key in self.values if fnmatch.fnmatch(key, match)]

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, numkeys, key, expected):
        if self.values.get(key) != expected:
            return 0
        del self.values[key]
        return 1


def test_terminal_job_slot_is_reclaimed_before_new_work():
    store = object.__new__(RedisJobStore)
    store._prefix = "eva"
    store._redis = FakeRedis(
        {
            "eva:slot:job-slot:0": "cancelled-job:100.0",
            "eva:slot:job-slot:1": "active-job:101.0",
            "eva:job:cancelled-job": json.dumps({"status": "cancelled"}),
            "eva:job:active-job": json.dumps({"status": "processing"}),
        }
    )

    assert store.reclaim_terminal_slots() == 1
    assert store._redis.get("eva:slot:job-slot:0") is None
    assert store._redis.get("eva:slot:job-slot:1") == "active-job:101.0"


def test_each_eva_worker_has_one_fair_conversion_process():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    for service in ("worker", "worker2"):
        match = re.search(
            rf"^  {service}:\n(?P<body>.*?)(?=^  \w|\Z)",
            compose,
            re.MULTILINE | re.DOTALL,
        )
        assert match, service
        command = re.search(r"^    command: (.+)$", match.group("body"), re.MULTILINE)
        assert command
        assert "--concurrency=1" in command.group(1)
        assert f"--hostname=eva-{service}@%h" in command.group(1)

    assert compose.count("JOB_SLOT_TTL_SECONDS: '180'") == 3
