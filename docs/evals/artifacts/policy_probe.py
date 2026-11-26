"""Record the effective mode of every valid three-scope input. Outputs JSON."""
import argparse
from itertools import product
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["private", "client"])
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    modes = ["dry", "semi", "full", "yolo", "paused"]
    overrides = modes + ["inherit"]
    policies = [dict(zip(("global", "project", "issue"), vals))
                for vals in product(modes, overrides, overrides)]
    if args.target == "private":
        sys.path.insert(0, str(args.root.resolve()))
        from state import effective_mode
        def override(value):
            return "default" if value == "inherit" else value
        observed = [effective_mode({"global_mode": p["global"]},
                                   {"mode": override(p["issue"])}, override(p["project"]))
                    for p in policies]
    else:
        script = """
const { evaluate } = require('./docs/lab/engine.js');
const fixture = require('./examples/split-review.json');
const policies = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(policies.map(policy => evaluate({...fixture, policy}).effective.mode)));
"""
        result = subprocess.run(["node", "-e", script], cwd=args.root,
                                input=json.dumps(policies), text=True, capture_output=True,
                                check=True)
        observed = json.loads(result.stdout)
    rows = []
    for policy, actual in zip(policies, observed):
        expected = next(policy[k] for k in ("issue", "project", "global")
                        if policy[k] != "inherit")
        rows.append(dict(policy=policy, expected=expected, actual=actual))
    passed = sum(row["expected"] == row["actual"] for row in rows)
    print(json.dumps(dict(target=args.target, passed=passed, rows=rows), separators=(",", ":")))
    return 0 if passed == len(policies) == len(observed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
