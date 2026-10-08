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
// The Studio host's local authoring endpoints, answered by the repository's
// own Python code: the same request models and functions `weave studio`
// calls (studio/host.py), in one helper process per test worker. Nothing is
// fetched: OpenAPI text is pasted, and HTTP actions are built offline.
import { ChildProcess, spawn } from "node:child_process";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import type { Page } from "@playwright/test";
import { python, pythonAvailable, repository } from "../python-path";

export { python };
export const hasPython = pythonAvailable();

// Mirrors the three handlers in src/firefly_weave/studio/host.py.
const helper = `
import json, sys
from pydantic import ValidationError
from firefly_weave.studio.host import HttpActionBuild, OpenAPIImportRequest, OpenAPIInventoryRequest
from firefly_weave.sdk.http_actions import author_http_action
from firefly_weave.sdk.openapi_import import import_openapi, init_policy, inventory

def dump(value):
    return value.model_dump(by_alias=True, mode="json")

def handle(path, raw):
    if path == "/studio/local/http-action":
        body = HttpActionBuild.model_validate_json(raw)
        return dump(author_http_action(body.request))
    if path == "/studio/local/openapi/inventory":
        body = OpenAPIInventoryRequest.model_validate_json(raw)
        return dump(inventory(body.source, source_format=body.format, relaxations=body.relaxations))
    body = OpenAPIImportRequest.model_validate_json(raw)
    if body.mixed():
        raise ValueError("mixed")
    policy, notes = body.policy, []
    if policy is None:
        scaffold = init_policy(body.source, body.selection, source_format=body.format, relaxations=body.relaxations, name=body.name)
        if not scaffold.ok or scaffold.policy is None:
            return {"ok": False, "actions": [], "policy": None, "diagnostics": [dump(d) for d in scaffold.diagnostics]}
        policy, notes = scaffold.policy, scaffold.diagnostics
    result = import_openapi(body.source, None, policy, source_format=body.format, target="builtin", all_diagnostics=True)
    seen = {(d.code, d.path) for d in result.diagnostics}
    diagnostics = list(result.diagnostics) + [d for d in notes if (d.code, d.path) not in seen]
    return {
        **result.model_dump(by_alias=True, mode="json", exclude={"diagnostics", "connector", "package"}),
        "policy": policy,
        "diagnostics": [dump(d) for d in diagnostics],
    }

for line in sys.stdin:
    request = json.loads(line)
    try:
        reply = {"status": 200, "body": handle(request["path"], request["body"])}
    except (ValidationError, ValueError):
        reply = {"status": 422, "body": {"code": "WV-STUDIO-REQUEST", "message": "Invalid request"}}
    print(json.dumps(reply), flush=True)
`;

let child: ChildProcess | null = null;
let waiting: ((line: string) => void)[] = [];
let queue: Promise<unknown> = Promise.resolve();

function start() {
  if (child && child.exitCode === null) return child;
  child = spawn(python, ["-u", "-c", helper], {
    cwd: repository,
    env: { ...process.env, PYTHONPATH: resolve(repository, "src") },
    stdio: ["pipe", "pipe", "inherit"],
  });
  waiting = [];
  createInterface({ input: child.stdout! }).on("line", (line) =>
    waiting.shift()?.(line),
  );
  return child;
}

/** One request at a time, in order, like the host's bounded worker slots. */
function ask(path: string, body: string) {
  const answer = queue.then(
    () =>
      new Promise<{ status: number; body: unknown }>((done) => {
        const helperProcess = start();
        waiting.push((line) => done(JSON.parse(line)));
        helperProcess.stdin!.write(JSON.stringify({ path, body }) + "\n");
      }),
  );
  queue = answer.catch(() => undefined);
  return answer;
}

export function stopLocalAuthoring() {
  child?.kill();
  child = null;
}

/** What Studio sent to the local authoring endpoints, in order. */
export interface LocalCalls {
  path: string;
  body: Record<string, unknown>;
}

/** Answers /studio/local/{http-action,openapi/*} with the real Python code. */
export async function localAuthoring(page: Page): Promise<LocalCalls[]> {
  const calls: LocalCalls[] = [];
  for (const path of [
    "/studio/local/http-action",
    "/studio/local/openapi/inventory",
    "/studio/local/openapi/import",
  ])
    await page.route(`**${path}`, async (route) => {
      const raw = route.request().postData() ?? "{}";
      calls.push({ path, body: JSON.parse(raw) });
      const reply = await ask(path, raw);
      await route
        .fulfill({ status: reply.status, json: reply.body })
        .catch(() => undefined);
    });
  return calls;
}
