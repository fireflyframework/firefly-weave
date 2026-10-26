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
import { ChildProcess, spawn, spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import type { Page } from "@playwright/test";
import { python, pythonAvailable, repository } from "../python-path";

export { python };
export const hasPython = pythonAvailable();

// Mirrors the local handlers in src/firefly_weave/studio/host.py, and answers
// a test compile against a catalog the test keeps.
const helper = `
import json, sys
from pydantic import ValidationError
from firefly_weave.studio.host import HttpActionBuild, OpenAPIImportRequest, OpenAPIInventoryRequest
from firefly_weave.sdk.http_actions import author_http_action
from firefly_weave.sdk.openapi_import import import_openapi, init_policy, inventory

def dump(value):
    return value.model_dump(by_alias=True, mode="json")

def test_catalog(definitions):
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.connectors import email
    from firefly_weave.contracts.definitions import ConnectorDefinition, load_definition
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
    from firefly_weave.sdk.http_actions import HTTP_ADAPTER
    http, mail = HTTP_PROFILE_DESCRIPTOR, email.package.descriptor
    return CatalogSnapshot.from_definitions(
        [ConnectorDefinition.model_validate(http.manifest.value), ConnectorDefinition.model_validate(mail.manifest.value), *(load_definition(d) for d in definitions)],
        tasks=[*http.capabilities, *mail.capabilities],
        adapters=[HTTP_ADAPTER, "weave-email"],
    )

def refused(status, code, message, paths=()):
    return {"__status": status, "status": status, "code": code, "message": message, **({"result": {"paths": list(paths)}} if paths else {})}

def execute_reply(raw):
    # As the host's execute route answers offline (catalog "none": the tests mock the platform).
    from firefly_weave.studio.local_contracts import ExecuteRequest
    from firefly_weave.studio.simulation import CatalogConflict, SimulationRefused, execute
    body = ExecuteRequest.model_validate_json(raw)
    try:
        return execute(body, None).model_dump(mode="json", by_alias=True, exclude_unset=True)
    except SimulationRefused as error:
        return refused(error.status, error.code, error.message, error.paths)
    except CatalogConflict:
        return refused(409, "WV-STUDIO-CATALOG-CONFLICT", "A definition on this computer has the same name and version as a different one in the catalog")

def handle(path, raw):
    if path == "/test/compile":
        from firefly_weave.compiler.api import compile_source
        body = json.loads(raw)
        return json.loads(compile_source(body["source"], format=body["format"], catalog=test_catalog(body["definitions"])).to_bytes())
    if path == "/studio/local/http-action":
        body = HttpActionBuild.model_validate_json(raw)
        return dump(author_http_action(body.request))
    if path == "/studio/local/openapi/inventory":
        body = OpenAPIInventoryRequest.model_validate_json(raw)
        return dump(inventory(body.source, source_format=body.format, relaxations=body.relaxations))
    if path == "/studio/local/debug/execute":
        return execute_reply(raw)
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
        body = handle(request["path"], request["body"])
        status = body.pop("__status", 200) if isinstance(body, dict) else 200
        reply = {"status": status, "body": body}
    except (ValidationError, ValueError):
        reply = {"status": 422, "body": {"code": "WV-STUDIO-REQUEST", "message": "Invalid request"}}
    print(json.dumps(reply), flush=True)
`;

let child: ChildProcess | null = null;
let waiting: ((line: string) => void)[] = [];
let queue: Promise<unknown> = Promise.resolve();

/**
 * Answers every request still waiting on a helper that is gone, so the
 * requests queued after them (in a later test of this worker) aren't stuck.
 */
function release() {
  const pending = waiting;
  waiting = [];
  for (const done of pending)
    done(
      JSON.stringify({
        status: 503,
        body: { code: "WV-STUDIO-REQUEST", message: "Helper stopped" },
      }),
    );
}

function start() {
  if (child && child.exitCode === null) return child;
  release();
  const started = spawn(python, ["-u", "-c", helper], {
    cwd: repository,
    env: { ...process.env, PYTHONPATH: resolve(repository, "src") },
    stdio: ["pipe", "pipe", "inherit"],
  });
  child = started;
  // Only the current helper answers; a stopped one's late lines are dropped.
  createInterface({ input: started.stdout! }).on("line", (line) => {
    if (child === started) waiting.shift()?.(line);
  });
  started.on("exit", () => {
    if (child !== started) return;
    child = null;
    release();
  });
  return started;
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
  const stopped = child;
  child = null;
  stopped?.kill();
  release();
}

/** What Studio sent to the local authoring endpoints, in order. */
export interface LocalCalls {
  path: string;
  body: Record<string, unknown>;
}

let builtins: unknown = null;
/** The Studio host's built-in email and file actions, as its route answers. */
export function builtinActions(): unknown {
  if (builtins) return builtins;
  const run = spawnSync(
    python,
    [
      "-c",
      "import json; from firefly_weave.contracts.connector_actions import builtin_connector_actions as b; print(json.dumps({'actions': b()}))",
    ],
    {
      cwd: repository,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: resolve(repository, "src") },
    },
  );
  if (run.status !== 0) throw new Error(run.stderr);
  builtins = JSON.parse(run.stdout);
  return builtins;
}

/**
 * The platform's compile, answered by the repository's compiler: the
 * bundled HTTP and email connectors plus `definitions` stand in for a
 * project catalog, as tests that keep their own catalog need.
 */
export async function platformCompile(body: {
  source: unknown;
  format: string;
  definitions: unknown[];
}): Promise<Record<string, unknown>> {
  const reply = await ask("/test/compile", JSON.stringify(body));
  if (reply.status !== 200)
    throw new Error(`The test compile failed: ${JSON.stringify(reply.body)}`);
  return reply.body as Record<string, unknown>;
}

/** Answers /studio/local/{http-action,openapi/*,debug/execute} with the real Python code. */
export async function localAuthoring(page: Page): Promise<LocalCalls[]> {
  const calls: LocalCalls[] = [];
  for (const path of [
    "/studio/local/http-action",
    "/studio/local/openapi/inventory",
    "/studio/local/openapi/import",
    "/studio/local/debug/execute",
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
