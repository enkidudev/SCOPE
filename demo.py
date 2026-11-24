"""Deterministic, explicitly synthetic replay workloads. Never live Windows data."""
import random
from datetime import datetime, timedelta, timezone

SCENARIOS = ("repetitive", "diverse", "entropy", "chain")


def fixture(n=600, scenario="repetitive", seed=173):
    if scenario not in SCENARIOS or type(n) is not int or not 1 <= n <= 20_000:
        raise ValueError("Unknown scenario or invalid event count")
    if scenario == "chain":
        n = 4
    rng = random.Random(seed)
    start = datetime(2026, 11, 9, tzinfo=timezone.utc)
    result = []
    for i in range(n):
        varied = scenario in ("diverse", "entropy")
        event = {
            "event_id": str(i), "source_id": "lab-win01", "event_type": "synthetic_correlated_event",
            "timestamp": (start + timedelta(milliseconds=i)).strftime("%Y-%m-%dT%H:%M:%S") + f".{(i % 1000) * 1000000:09d}Z",
            "principal": "LAB\\analyst" if not varied else "LAB\\user" + str(i % 13),
            "session_id": "session-1" if not varied else "session-" + str(i // 17),
            "action": "connect" if not varied else ("create", "connect", "modify")[i % 3],
            "process_id": "proc-guid-2" if not varied else "proc-" + str(i % 31),
            "parent_id": "proc-guid-1" if not varied else "parent-" + str(i % 7),
            "privilege_before": "medium", "privilege_after": "high",
            "remote_ip": "203.0.113.10" if not varied else "203.0.113." + str(i % 250 + 1),
            "direction": "outbound",
            "raw_payload": "SYNTHETIC REPLAY PAYLOAD " + "repeated diagnostic detail " * 24,
        }
        if scenario == "entropy":
            event["raw_payload"] = "SYNTHETIC RANDOM PAYLOAD " + "".join(rng.choice("0123456789abcdef") for _ in range(640))
        if scenario == "chain":
            event["process_id"] = "proc-" + str(i)
            event["parent_id"] = "ROOT" if i == 0 else "proc-" + str(i - 1)
        result.append(event)
    return result
