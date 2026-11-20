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
// The Kubernetes connect wizard against a REAL local platform and a REAL kind
// or k3d cluster: an operator signs in, opens Clusters › Add target ›
// Connect a cluster, enters the run's cluster and a namespace and copies the
// connect command. With the wizard still open, the test runs the copied text
// exactly as copied, in the run's private folder where `weave platform up`
// ran, with the run's own kubeconfig. Studio must say "Cluster connected"
// within 15 s of the command's end, without a reload. The namespace is
// disconnected afterwards. Uses the variables of real-platform.spec.ts, plus:
//
//   WEAVE_ACC_RUN_DIR              A kept acceptance run of the cluster or k3d
//                                  profile (`scripts/acceptance.py --profile
//                                  cluster --keep`): its run.json names the
//                                  cluster, private/kubeconfig reaches it and
//                                  private/people/operator.json signs in.
//   WEAVE_E2E_OPERATIONS_EVIDENCE  A folder that receives the measured
//                                  detection time as JSON.
import { expect, test } from "@playwright/test";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { python, repository } from "../python-path";
import {
  cleanups,
  connectAndSignIn,
  expectNoCredentialsLeft,
  openSidebar,
  pair,
  removePlatform,
  session,
  shot,
  uniqueName,
  useRealPlatformSuite,
  type Person,
} from "./real-platform-support";

useRealPlatformSuite("cluster-connect-real");

const runDir = process.env["WEAVE_ACC_RUN_DIR"]?.trim() ?? "";
const evidence = process.env["WEAVE_E2E_OPERATIONS_EVIDENCE"]?.trim() ?? "";
/** A namespace of its own, so the cluster journeys' namespaces stay untouched. */
const NAMESPACE = "weave-workers-studio";
/** How soon after the command ends Studio must say the cluster is connected. */
const DETECTION_MS = 15_000;

interface RunCluster {
  provider: "kind" | "k3d";
  name: string;
  context: string;
  network?: string;
}

/** The run's cluster, once prepare has made it; null without one. */
function runCluster(): RunCluster | null {
  if (!runDir) return null;
  const file = join(runDir, "run.json");
  if (!existsSync(file)) return null;
  const state = JSON.parse(readFileSync(file, "utf8")) as {
    cluster?: RunCluster;
  };
  return state.cluster?.network ? state.cluster : null;
}

/** The cluster commands' environment: the run's kubeconfig and no Weave settings. */
function clusterEnvironment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(process.env))
    if (!key.startsWith("WEAVE_") && !key.startsWith("PYFLY_"))
      env[key] = value;
  return {
    ...env,
    PYTHONPATH: join(repository, "src"),
    KUBECONFIG: join(runDir, "private", "kubeconfig"),
  };
}

/**
 * Runs a command line that starts with `weave`, word for word, from the run's
 * private folder (where `weave platform up` ran), through this checkout's
 * `weave`. Resolves when it ends, with the moment it ended.
 */
function runCopied(
  text: string,
): Promise<{ status: number | null; output: string; endedAt: number }> {
  const [program, ...words] = text.split(" ");
  expect(program, "the copied command").toBe("weave");
  return new Promise((done) => {
    const child = spawn(python, ["-m", "firefly_weave.cli.main", ...words], {
      cwd: join(runDir, "private"),
      env: clusterEnvironment(),
    });
    let output = "";
    child.stdout.on("data", (chunk) => (output += chunk));
    child.stderr.on("data", (chunk) => (output += chunk));
    child.on("close", (status) =>
      done({ status, output: output.slice(-4000), endedAt: Date.now() }),
    );
  });
}

function disconnect(cluster: RunCluster) {
  const result = spawnSync(
    python,
    [
      ...["-m", "firefly_weave.cli.main", "platform", "cluster", "disconnect"],
      ...["--provider", cluster.provider, "--name", cluster.name],
      ...["--namespace", NAMESPACE, "--output", "json"],
    ],
    {
      cwd: join(runDir, "private"),
      env: clusterEnvironment(),
      encoding: "utf8",
      timeout: 600_000,
    },
  );
  if (result.status !== 0)
    console.error(`disconnect exited ${result.status}: ${result.stdout}`);
}

function record(seconds: number) {
  if (!evidence) return;
  mkdirSync(evidence, { recursive: true });
  writeFileSync(
    join(evidence, "cluster-connect-real.json"),
    JSON.stringify({ detection_seconds: seconds }, null, 2) + "\n",
  );
}

test("Studio notices the cluster its copied connect command connects", async ({
  page,
  context,
}) => {
  const cluster = runCluster();
  test.skip(
    !cluster,
    "Set WEAVE_ACC_RUN_DIR to a kept cluster or k3d acceptance run with its cluster.",
  );
  if (!cluster) return;
  // connect waits for the runner's contact and a first observation.
  test.setTimeout(20 * 60_000);
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const operator = JSON.parse(
    readFileSync(join(runDir, "private", "people", "operator.json"), "utf8"),
  ) as Person;
  const s = await session(page);
  const name = uniqueName("cluster");
  await pair(page, s.host!, "cluster-pairing");
  await connectAndSignIn(page, name, operator);
  await openSidebar(page, "Clusters");
  await page.getByRole("button", { name: "Add target" }).click();
  await page.getByRole("button", { name: "Connect a cluster" }).click();
  await page.getByRole("radio", { name: cluster.provider }).check();
  await page.getByRole("textbox", { name: "Cluster name" }).fill(cluster.name);
  await page.getByRole("textbox", { name: "Namespace" }).fill(NAMESPACE);
  const status = page.getByRole("complementary", { name: "What Studio sees" });
  await expect(status.getByRole("status")).toContainText(
    "Waiting for the cluster",
  );
  await page.getByRole("button", { name: "Copy connect command" }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toBe(
    `weave platform cluster connect --provider ${cluster.provider} --name ${cluster.name} --namespace ${NAMESPACE}`,
  );
  await shot(page, "cluster-connect-copied");
  cleanups.push(() => disconnect(cluster));
  const ran = await runCopied(copied);
  expect(ran.status, ran.output).toBe(0);
  await expect(status.getByRole("status")).toContainText("Cluster connected", {
    timeout: DETECTION_MS,
  });
  const seconds = Math.max(0, (Date.now() - ran.endedAt) / 1000);
  record(seconds);
  console.log(
    `Studio showed Cluster connected ${seconds.toFixed(1)} s after the command ended.`,
  );
  const target = `${cluster.provider}-${cluster.name}.${NAMESPACE}`;
  await expect(status.locator(`dt:text-is("Target") + dd`)).toHaveText(target);
  await expect(status.locator(`dt:text-is("Runner") + dd`)).toHaveText(
    "Runner online",
  );
  await shot(page, "cluster-connect-connected");
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});
