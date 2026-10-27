# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Write the steps the Studio host keeps to execute each step of the shared slice workflow.

The host's ``slice_for_target`` answers, so the expected IDs are never written by hand. Studio's vitest suite slices
the same workflow with its own mirror and expects the same steps.

Run ``uv run --locked --all-extras python scripts/studio_slice_cases.py`` to regenerate the file, or add ``--check``
to fail when it is out of date.
"""

import argparse
import json
import sys
from pathlib import Path

from firefly_weave.studio.simulation import slice_for_target, steps_of

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "tests" / "fixtures" / "studio" / "slice-workflow.json"
OUTPUT = ROOT / "studio" / "tests" / "fixtures" / "slice-cases.json"
GENERATOR = "scripts/studio_slice_cases.py"


def render() -> str:
    """One case per step: the IDs of the steps that run in its slice (an emptied branch runs nothing)."""
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    cases = []
    for step in steps_of(workflow["spec"]["steps"]):
        sliced = slice_for_target(workflow, step["id"])
        assert sliced is not None
        cases.append({"stepId": step["id"], "kept": [kept["id"] for kept in steps_of(sliced["spec"]["steps"])]})
    lines = ",\n".join("    " + json.dumps(case) for case in cases)
    return f'{{\n  "generator": "{GENERATOR}",\n  "cases": [\n{lines}\n  ]\n}}\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed file is out of date")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        # read_text applies universal newlines, so CRLF checkouts compare equal.
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != text:
            print(f"{args.output} is out of date; run {GENERATOR}", file=sys.stderr)
            return 1
        return 0
    args.output.write_bytes(text.encode("utf-8"))
    print(f"{args.output}: written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
