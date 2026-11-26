/*
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
*/
// The platform's side of the platform event, from the repository's Python:
// the view the Studio host serves, and whether the platform takes an event as
// the input of a run.
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import type { JsonObject } from "../src/app/forms/core/json";
import { python, pythonAvailable, repository } from "./python-path";

export const available = pythonAvailable();

const script = `
import json, sys
from pydantic import ValidationError
from firefly_weave.compiler.platform_catalog import platform_schema_bundle
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.platform_events import PlatformEvent
from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA

bundle = platform_schema_bundle()

def taken(event):
    try:
        PlatformEvent.model_validate_json(json.dumps(event))
    except ValidationError:
        return False
    return validate_payload({"$ref": PLATFORM_EVENT_SCHEMA}, event, bundle) == ()

# The bytes, which are UTF-8 whatever the locale is, as on Windows.
events = json.load(sys.stdin.buffer)
print(json.dumps({"view": bundle[PLATFORM_EVENT_SCHEMA], "taken": [taken(event) for event in events]}))
`;
const ask = (events: readonly unknown[]) =>
  JSON.parse(
    execFileSync(python, ["-c", script], {
      encoding: "utf8",
      input: JSON.stringify(events),
      env: { ...process.env, PYTHONPATH: join(repository, "src") },
    }),
  ) as { view: JsonObject; taken: boolean[] };

/** The platform's own view of its event, as the Studio host serves it. */
export const platformView = (): JsonObject => (available ? ask([]).view : {});
/** For each event, whether the platform takes it: its model reads it, and it fits the view. */
export const platformTakes = (events: readonly unknown[]): boolean[] =>
  ask(events).taken;
