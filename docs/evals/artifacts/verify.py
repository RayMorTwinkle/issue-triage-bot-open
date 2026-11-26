"""Check the saved comparison without a checkout or access to the private runner."""
from collections import Counter
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    def read(name):
        return json.loads((root / name).read_text())
    scriptsum = hashlib.sha256((root / "gate_probe.py").read_bytes()).hexdigest()
    before, after = read("before.json"), read("after.json")
    for field in ("rounds", "probe_sha256", "python", "machine"):
        if before[field] != after[field]:
            raise ValueError(f"comparison mismatch: {field}")
    if after["probe_sha256"] != scriptsum:
        raise ValueError("probe source differs from the recorded probe")
    cases = ("no_barging", "wall_clock_step", "shrink", "timeout_cleanup", "expand", "error_exit")
    for name, record in (("before", before), ("after", after)):
        keys = Counter((row["case"], row["round"]) for row in record["rows"])
        expected = Counter((case, i) for case in cases for i in range(1, record["rounds"] + 1))
        if keys != expected:
            raise ValueError("missing, duplicate or unexpected observations")
        counts = Counter(row["case"] for row in record["rows"] if row["passed"] is True)
        if sum(counts.values()) != record["passed"]:
            raise ValueError("gate summary disagrees with its observations")
        print(name, " ".join(f"{c}={counts[c]}/{record['rounds']}" for c in cases))
    rows = read("before-policy.json")["rows"]
    if rows != read("after-policy.json")["rows"] or rows != read("client-policy.json")["rows"]:
        raise ValueError("policy observations differ")
    distinct = {tuple(row["policy"][k] for k in ("global", "project", "issue")) for row in rows}
    if len(distinct) != 180 or len(rows) != 180 or any(r["expected"] != r["actual"] for r in rows):
        raise ValueError("policy matrix incomplete or an outcome differs")
    print(f"policy: all {len(rows)} effective-mode observations agree across all three runs")


if __name__ == "__main__":
    main()
