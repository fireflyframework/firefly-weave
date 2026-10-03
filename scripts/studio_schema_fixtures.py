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

"""Write the shipped-schema corpus that Studio's form tests measure against.

The corpus is every schema a person fills in through a Studio form today:
connection configuration and authentication for the eight first-party
connectors, each connector action's configuration and input, plus the example
workflow inputs, human-task form, signal payload and action inputs. The summary
records how the current TaskForm renders those fields (its ``groupedObject`` and
``scalarList`` rules, nested groups to depth 4), so a schema-coverage change in
``studio/src/app/forms/core/resolve.ts`` is measured against a fixed baseline.

Run ``PYTHONPATH=src .venv/bin/python scripts/studio_schema_fixtures.py`` to
regenerate the file, or add ``--check`` to fail when it is out of date.
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml

from firefly_weave.compiler.catalog import FrozenDocument

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "studio" / "tests" / "fixtures" / "shipped-schemas.json"
WORKFLOW_EXAMPLES = (
    "examples/definitions/customer-onboarding.workflow.yaml",
    "examples/human_tasks/workflow.yaml",
    "examples/teams/reply.workflow.yaml",
)
ACTION_EXAMPLES = (
    "examples/definitions/check-customer.action.yaml",
    "examples/definitions/postgresql.action.yaml",
)
# TaskForm (studio/src/app/task-form.ts) renders these scalar types as inputs.
SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})
# TaskForm nests object groups to this depth, then falls back to JSON.
GROUP_DEPTH = 4


class ShippedSchema(TypedDict):
    id: str
    origin: str
    role: str
    schema: dict[str, Any]


def connector_manifests() -> list[FrozenDocument]:
    """The eight first-party connector manifests, in a fixed order."""
    from firefly_weave.connectors import email, teams, telegram, whatsapp
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR, KAFKA_DESCRIPTOR, POSTGRES_DESCRIPTOR
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR

    descriptors = [HTTP_DESCRIPTOR, HTTP_PROFILE_DESCRIPTOR, POSTGRES_DESCRIPTOR, KAFKA_DESCRIPTOR]
    descriptors += [module.package.descriptor for module in (email, teams, telegram, whatsapp)]
    return [descriptor.manifest for descriptor in descriptors]


def connector_schemas() -> Iterator[ShippedSchema]:
    for manifest in connector_manifests():
        document = manifest.value
        metadata = cast(dict[str, Any], document["metadata"])
        spec = cast(dict[str, Any], document["spec"])
        reference = f"{metadata['name']}@{metadata['version']}"
        yield ShippedSchema(
            id=f"{reference} connection", origin="connector", role="connectionConfig", schema=spec["configSchema"]
        )
        yield ShippedSchema(
            id=f"{reference} authentication", origin="connector", role="authentication", schema=spec["authSchema"]
        )
        for name, action in sorted(cast(dict[str, dict[str, Any]], spec["actions"]).items()):
            yield ShippedSchema(
                id=f"{reference} {name} configuration",
                origin="connector",
                role="actionConfig",
                schema=action["configSchema"],
            )
            yield ShippedSchema(
                id=f"{reference} {name} input", origin="connector", role="actionInput", schema=action["inputSchema"]
            )


def _steps(steps: Iterable[Any]) -> Iterator[dict[str, Any]]:
    """Every step, including those inside decision cases, defaults and parallel branches."""
    for step in steps:
        if not isinstance(step, dict):
            continue
        yield step
        for case in step.get("cases") or []:
            yield from _steps(case.get("steps") or [])
        if isinstance(step.get("default"), dict):
            yield from _steps(step["default"].get("steps") or [])
        branches = step.get("branches") or []
        for branch in branches.values() if isinstance(branches, dict) else branches:
            yield from _steps(branch.get("steps") or [])


def example_schemas() -> Iterator[ShippedSchema]:
    for path in WORKFLOW_EXAMPLES:
        spec = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))["spec"]
        yield ShippedSchema(id=f"{path} input", origin="example", role="workflowInput", schema=spec["inputSchema"])
        for step in _steps(spec.get("steps") or []):
            if "formSchema" in step:
                yield ShippedSchema(
                    id=f"{path} {step['id']} form", origin="example", role="form", schema=step["formSchema"]
                )
            if "payloadSchema" in step:
                yield ShippedSchema(
                    id=f"{path} {step['id']} payload", origin="example", role="payload", schema=step["payloadSchema"]
                )
    for path in ACTION_EXAMPLES:
        spec = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))["spec"]
        yield ShippedSchema(id=f"{path} input", origin="example", role="actionInput", schema=spec["inputSchema"])


def _grouped(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "object" and not schema.get("enum") and bool(schema.get("properties"))


def _scalar_list(schema: dict[str, Any]) -> bool:
    items = schema.get("items")
    return (
        schema.get("type") == "array"
        and not schema.get("enum")
        and isinstance(items, dict)
        and (bool(items.get("enum")) or str(items.get("type")) in SCALAR_TYPES)
    )


def task_form_widgets(schema: dict[str, Any], depth: int = 0) -> Counter[str]:
    """How TaskForm renders each field today: input, enum, list, checkbox, group or a JSON textarea."""
    widgets: Counter[str] = Counter()
    for field in (schema.get("properties") or {}).values():
        field = field if isinstance(field, dict) else {}
        if _grouped(field) and depth + 1 < GROUP_DEPTH:
            widgets["group"] += 1
            widgets += task_form_widgets(field, depth + 1)
        elif _scalar_list(field):
            widgets["list"] += 1
        elif field.get("enum"):
            widgets["enum"] += 1
        elif field.get("type") == "boolean":
            widgets["checkbox"] += 1
        elif str(field.get("type")) not in SCALAR_TYPES:
            widgets["json"] += 1
        else:
            widgets["input"] += 1
    return widgets


def corpus() -> dict[str, Any]:
    schemas = [*connector_schemas(), *example_schemas()]
    widgets: Counter[str] = Counter()
    for item in schemas:
        widgets += task_form_widgets(item["schema"])
    return {
        "generator": "scripts/studio_schema_fixtures.py",
        "summary": {
            "schemas": len(schemas),
            "fields": sum(widgets.values()),
            "taskFormWidgets": dict(sorted(widgets.items())),
        },
        "schemas": schemas,
    }


def render() -> str:
    return json.dumps(corpus(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed fixture is out of date")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(f"{args.output} is out of date; run scripts/studio_schema_fixtures.py", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    summary = json.loads(text)["summary"]
    print(f"{args.output}: {summary['schemas']} schemas, {summary['fields']} fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
