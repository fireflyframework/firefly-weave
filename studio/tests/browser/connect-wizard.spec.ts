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
// Clusters › Add target › Kubernetes: the wizard shows the commands that
// create and connect a local kind or k3d cluster, and notices the target the
// connect command registers while it stays open. It only reads; the routes
// here stand in for the platform.
import { expect, test } from "@playwright/test";
import {
  CONNECT,
  CREATE,
  clusterRunner,
  clusterTarget,
  command,
  copyButton,
  field,
  observation,
  openClusters,
  openWizard,
  settled,
  shown,
  status,
  targetId,
} from "./connect-wizard-fixture";

test.use({ viewport: { width: 1440, height: 900 } });

test("Add target offers Kubernetes with Connect a cluster and the guide for clusters elsewhere", async ({
  page,
}) => {
  await openClusters(page);
  await page.getByRole("button", { name: "Add target" }).click();
  const choices = page.locator("#add-target-choices");
  await expect(choices).toContainText("Docker on this computer");
  const kubernetes = choices.locator(".chooser-option", {
    hasText: "Kubernetes",
  });
  await expect(kubernetes).toContainText(
    "Connect a kind or k3d cluster on this computer.",
  );
  await expect(kubernetes).toContainText(
    "For a cluster that isn't on this computer, use Other and follow the Kubernetes operations guide.",
  );
  await expect(
    kubernetes.getByRole("link", { name: "Kubernetes operations guide" }),
  ).toHaveAttribute(
    "href",
    "https://fireflyframework.github.io/firefly-weave/operations/kubernetes/",
  );
  await expect(
    kubernetes.getByRole("button", { name: "Connect a cluster" }),
  ).toHaveClass(/primary/);
  // Other keeps the generic registration.
  await choices.getByText("Other", { exact: true }).click();
  await expect(
    choices.getByRole("button", { name: "Register target" }),
  ).toBeVisible();
});

test("the form shows both commands, the target name and what each command changes", async ({
  page,
}) => {
  await openClusters(page);
  await openWizard(page);
  const provider = page.getByRole("group", { name: "Provider" });
  await expect(provider.getByRole("radio", { name: "kind" })).toBeChecked();
  await expect(page.getByRole("textbox", { name: "Cluster name" })).toHaveValue(
    "weave-test",
  );
  await expect(page.getByRole("textbox", { name: "Namespace" })).toHaveValue(
    "weave-workers",
  );
  await expect(page.getByText("The target will be named")).toHaveText(
    "The target will be named kind-weave-test.",
  );
  const before = page.getByRole("list", { name: "Before you start" });
  await expect(before.getByRole("listitem")).toHaveCount(2);
  await expect(before.getByRole("listitem").first()).toHaveText(
    "The local platform is running (weave platform up).",
  );
  await expect(before.getByRole("listitem").nth(1)).toContainText(
    "Create the cluster with this command. Skip it if you already did.",
  );
  expect(await shown(command(page, "Create"))).toBe(CREATE);
  await expect(before).toContainText(
    "It adds the kind-weave-test context to your kubeconfig and makes it current. If no cluster command has run on this computer yet, it first downloads about 150 MB of kind, k3d and kubectl into the platform folder.",
  );
  await expect(page.locator(".connect-changes")).toHaveText(
    "Creates the namespaces weave-system and weave-workers, a runner and a worker deployment at 0 workers, and network rules that let only them reach the platform. Registers the target kind-weave-test. It also joins the platform's containers to the cluster's Docker network and, if no cluster command has run on this computer yet, downloads about 150 MB of kind, k3d and kubectl into the platform folder. Studio never receives your kubeconfig.",
  );
  expect(await shown(command(page, "Connect"))).toBe(CONNECT);
  // Detection starts as the wizard opens.
  const line = status(page).getByRole("status");
  await expect(line).toContainText("Waiting for the cluster");
  await expect(line).toContainText(
    "Waiting for kind-weave-test… Studio checks every 5 s.",
  );
  await expect(field(page, "Target")).toHaveText("kind-weave-test");
  await expect(field(page, "Namespace")).toHaveText("weave-workers");
});

test("a choice of provider and namespace changes every name and command at once", async ({
  page,
}) => {
  await openClusters(page);
  await openWizard(page);
  const kind = page.getByRole("radio", { name: "kind" });
  await kind.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("radio", { name: "k3d" })).toBeChecked();
  await page.getByRole("textbox", { name: "Namespace" }).fill("team-a");
  await expect(page.getByText("The target will be named")).toHaveText(
    "The target will be named k3d-weave-test.team-a.",
  );
  expect(await shown(command(page, "Create"))).toBe(
    "weave platform cluster create --provider k3d --name weave-test",
  );
  expect(await shown(command(page, "Connect"))).toBe(
    "weave platform cluster connect --provider k3d --name weave-test --namespace team-a",
  );
  await expect(
    page.getByRole("list", { name: "Before you start" }),
  ).toContainText("It adds the k3d-weave-test context to your kubeconfig");
  await expect(page.locator(".connect-changes")).toContainText(
    "Creates the namespaces weave-system and team-a,",
  );
  await expect(page.locator(".connect-changes")).toContainText(
    "Registers the target k3d-weave-test.team-a.",
  );
  await expect(status(page)).toContainText(
    "Waiting for k3d-weave-test.team-a… Studio checks every 5 s.",
  );
  await expect(field(page, "Namespace")).toHaveText("team-a");
});

test("an invalid cluster name hides both commands until it is fixed", async ({
  page,
}) => {
  await openClusters(page);
  await openWizard(page);
  const name = page.getByRole("textbox", { name: "Cluster name" });
  await name.fill("Weave_Test");
  const refusal =
    "Use a cluster name of 1 to 32 lowercase letters, digits and hyphens that starts and ends with a letter or digit.";
  await expect(name).toHaveAttribute("aria-invalid", "true");
  await expect(name).toHaveAccessibleDescription(refusal);
  await expect(name).toBeFocused();
  for (const which of ["Create", "Connect"] as const) {
    await expect(command(page, which)).toContainText(
      "Fix the cluster name to see this command.",
    );
    await expect(command(page, which)).not.toContainText("weave platform");
  }
  for (const which of ["create", "connect"] as const)
    await expect(copyButton(page, which)).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  await expect(page.getByText("The target will be named")).toHaveCount(0);
  await expect(page.getByText("It adds the")).toHaveCount(0);
  await expect(page.locator(".connect-changes")).toHaveText(
    "Fix the cluster name to see what connecting changes.",
  );
  await expect(status(page).getByRole("status")).toContainText(
    "Not looking yet",
  );
  await expect(status(page)).toContainText(
    "Studio starts looking for the target once the cluster name is fixed.",
  );
  await expect(field(page, "Target")).toHaveText("—");
  await expect(field(page, "Namespace")).toHaveText("weave-workers");
  await name.fill("weave-test");
  await expect(name).not.toHaveAttribute("aria-invalid", "true");
  expect(await shown(command(page, "Connect"))).toBe(CONNECT);
  await expect(status(page)).toContainText("Waiting for kind-weave-test…");
});

test("while a name is invalid Studio isn't looking, so it never claims a fresh look", async ({
  page,
}) => {
  await page.clock.install();
  await openClusters(page);
  await openWizard(page);
  await settled(page);
  await page.getByRole("textbox", { name: "Cluster name" }).fill("Weave_Test");
  await expect(status(page)).toContainText("Not looking yet");
  await page.clock.fastForward(60_000);
  await expect(page.locator(".refresh-status .updated")).toHaveText(
    "Updated 1 min ago",
  );
});

test("a namespace Kubernetes uses is refused with the command line's sentence", async ({
  page,
}) => {
  await openClusters(page);
  await openWizard(page);
  const namespace = page.getByRole("textbox", { name: "Namespace" });
  await namespace.fill("default");
  await expect(namespace).toHaveAccessibleDescription(
    "default is used by Kubernetes. Choose another namespace.",
  );
  await expect(command(page, "Connect")).toContainText(
    "Fix the namespace to see this command.",
  );
  await expect(status(page)).toContainText(
    "Studio starts looking for the target once the namespace is fixed.",
  );
  await expect(field(page, "Target")).toHaveText("—");
  await expect(field(page, "Namespace")).toHaveText("—");
});

test("the commands copy exactly what they show", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openClusters(page);
  await openWizard(page);
  const connect = copyButton(page, "connect");
  await connect.click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    CONNECT,
  );
  expect(await shown(command(page, "Connect"))).toBe(CONNECT);
  await expect(connect).toHaveAccessibleName("Copied connect command");
  await expect(connect).toBeFocused();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Copied. Run it in a terminal" }),
  ).toHaveText(
    "Copied. Run it in a terminal in the folder where you ran weave platform up.",
  );
  await copyButton(page, "create").click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    CREATE,
  );
  expect(await shown(command(page, "Create"))).toBe(CREATE);
  // After two seconds the button reads Copy again.
  await expect(copyButton(page, "create")).toHaveAccessibleName(
    "Copy create command",
    { timeout: 5_000 },
  );
});

test("a refused clipboard selects the command for copying by hand", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("denied")) },
    });
    document.execCommand = () => false;
  });
  await openClusters(page);
  await openWizard(page);
  await copyButton(page, "connect").click();
  await expect(
    page.getByRole("status").filter({ hasText: "Select the command" }),
  ).toHaveText("Select the command and copy it.");
  const text = command(page, "Connect").locator("pre");
  await expect(text).toBeFocused();
  expect(
    (await page.evaluate(() => getSelection()!.toString()))
      .replace(/\s+/g, " ")
      .trim(),
  ).toBe(CONNECT);
});

test("Studio notices the new cluster within 15 s of it appearing", async ({
  page,
}) => {
  await page.clock.install();
  const backend = await openClusters(page);
  await openWizard(page);
  await expect(status(page)).toContainText("Waiting for the cluster");
  await settled(page);
  // The connect command finishes: the target and its online runner appear.
  backend.targets = [clusterTarget()];
  backend.runners = [clusterRunner(true)];
  backend.observations = [observation(12_000)];
  await page.clock.fastForward(5_000);
  await expect(status(page).getByRole("status")).toContainText(
    "Cluster connected",
    { timeout: 3_000 },
  );
  await expect(field(page, "Target")).toHaveText("kind-weave-test");
  await expect(field(page, "Runner")).toHaveText("Runner online");
  await expect(field(page, "Deployment")).toHaveText(/^Observed \d+ s ago$/);
  await expect(
    status(page).getByRole("button", { name: "Open target" }),
  ).toBeVisible();
  await status(page)
    .getByRole("button", { name: "New change request" })
    .click();
  await expect(
    page.getByRole("heading", { name: "New change request" }),
  ).toBeVisible();
});

test("a registered target waits for its runner to make contact", async ({
  page,
}) => {
  await openClusters(page, {
    targets: [clusterTarget()],
    runners: [clusterRunner(false)],
  });
  await openWizard(page);
  await expect(status(page).getByRole("status")).toContainText("Registered");
  await expect(status(page)).toContainText(
    "kind-weave-test is registered. Waiting for its runner to make contact…",
  );
});

test("opening with a name whose target is connected says it is already connected", async ({
  page,
}) => {
  await openClusters(page, {
    targets: [clusterTarget()],
    runners: [clusterRunner(true)],
    observations: [],
  });
  await openWizard(page);
  await expect(status(page).getByRole("status")).toContainText(
    "Already connected",
  );
  await expect(status(page)).toContainText(
    "kind-weave-test is already connected.",
  );
  await expect(
    status(page).getByRole("button", { name: "New change request" }),
  ).toHaveCount(0);
  await status(page).getByRole("button", { name: "Open target" }).click();
  await expect(
    page.getByRole("heading", { name: "kind-weave-test", level: 2 }),
  ).toBeFocused();
});

test("a target with the name on another adapter is reported, not connected", async ({
  page,
}) => {
  await openClusters(page, {
    targets: [clusterTarget({ adapter: "docker-compose" })],
    runners: [clusterRunner(true)],
  });
  await openWizard(page);
  await expect(status(page).getByRole("status")).toContainText("Name in use");
  await expect(status(page)).toContainText(
    "A target named kind-weave-test already exists and isn't this cluster. Choose another cluster name or namespace.",
  );
  await expect(
    status(page).getByRole("button", { name: "Open target" }),
  ).toHaveCount(0);
});

test("after 15 minutes Studio stops looking until Keep waiting", async ({
  page,
}) => {
  await page.clock.install();
  const backend = await openClusters(page);
  await openWizard(page);
  await settled(page);
  await page.clock.fastForward(15 * 60_000);
  await expect(status(page).getByRole("status")).toContainText("Not seen yet");
  await expect(status(page)).toContainText(
    "Studio hasn't seen kind-weave-test yet. Check the command's output in your terminal.",
  );
  await settled(page);
  const reads = backend.targetReads;
  for (let step = 0; step < 3; step++) {
    await page.clock.fastForward(5_000);
    await settled(page);
  }
  expect(backend.targetReads).toBe(reads);
  await status(page).getByRole("button", { name: "Keep waiting" }).click();
  await expect.poll(() => backend.targetReads).toBe(reads + 1);
  await expect(status(page)).toContainText("Waiting for the cluster");
});

test("an unreachable platform shows the partial state and waiting resumes on the next good poll", async ({
  page,
}) => {
  await page.clock.install();
  const backend = await openClusters(page);
  await openWizard(page);
  await settled(page);
  backend.unreachable = true;
  await page.clock.fastForward(5_000);
  const problem = page.getByRole("alert").filter({
    hasText: "This section could not be loaded.",
  });
  await expect(problem).toContainText("The platform didn't answer in time.");
  await expect(problem).toContainText("Support code: WV-UNAVAILABLE");
  await expect(status(page)).toContainText("Waiting for the cluster");
  await settled(page);
  backend.unreachable = false;
  backend.targets = [clusterTarget()];
  backend.runners = [clusterRunner(true)];
  // After a failure the next poll waits twice as long.
  await page.clock.fastForward(10_000);
  await expect(problem).toHaveCount(0);
  await expect(status(page).getByRole("status")).toContainText(
    "Cluster connected",
  );
});

test("new inputs are looked for at once, so a target that connects after them reads as connected", async ({
  page,
}) => {
  const backend = await openClusters(page);
  await openWizard(page);
  await settled(page);
  // The next scheduled poll is 5 s away: a look within 1.5 s is the wizard's own.
  const reads = backend.targetReads;
  await page.getByRole("textbox", { name: "Namespace" }).fill("team-a");
  await expect
    .poll(() => backend.targetReads, { timeout: 1_500 })
    .toBeGreaterThan(reads);
  await settled(page);
  backend.targets = [
    clusterTarget({ name: "kind-weave-test.team-a", boundary: "team-a" }),
  ];
  backend.runners = [clusterRunner(true)];
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(status(page).getByRole("status")).toContainText(
    "Cluster connected",
  );
});

test("changing the namespace while a poll runs drops the answer for the old name", async ({
  page,
}) => {
  let release!: () => void;
  const backend = await openClusters(page, {
    targets: [clusterTarget()],
    runners: [clusterRunner(true)],
  });
  backend.hold = new Promise<void>((resolve) => (release = resolve));
  const reads = backend.targetReads;
  await openWizard(page);
  // The wizard's first look, for kind-weave-test, is still on its way.
  await expect.poll(() => backend.targetReads).toBe(reads + 1);
  await page.getByRole("textbox", { name: "Namespace" }).fill("team-a");
  release();
  await settled(page);
  await expect(status(page)).toContainText(
    "Waiting for kind-weave-test.team-a… Studio checks every 5 s.",
  );
  await expect(status(page)).not.toContainText("already connected");
  await expect(field(page, "Target")).toHaveText("kind-weave-test.team-a");
});

test("Clusters polls every 5 s while the wizard is open and every 15 s after it", async ({
  page,
}) => {
  await page.clock.install();
  const backend = await openClusters(page);
  await openWizard(page);
  await settled(page);
  const count = async (seconds: number) => {
    const before = backend.targetReads;
    for (let step = 0; step < seconds; step++) {
      await page.clock.fastForward(1_000);
      await settled(page);
    }
    return backend.targetReads - before;
  };
  expect(await count(30)).toBeGreaterThanOrEqual(5);
  await page.getByRole("button", { name: "Back to Clusters" }).click();
  await expect(page.getByRole("button", { name: "Add target" })).toBeFocused();
  await settled(page);
  expect(await count(30)).toBeLessThanOrEqual(3);
});

test("the wizard reads targets, and the new target's runners and observations, and sends nothing", async ({
  page,
}) => {
  const backend = await openClusters(page, {
    targets: [clusterTarget()],
    runners: [clusterRunner(true)],
    observations: [observation(12_000)],
  });
  await settled(page);
  backend.requests.length = 0;
  await openWizard(page);
  await expect(status(page)).toContainText("Already connected");
  await settled(page);
  const deployment = backend.requests.filter((line) =>
    line.includes("/deployment"),
  );
  const environment = "/environments/development/";
  expect(deployment.length).toBeGreaterThanOrEqual(3);
  for (const line of backend.requests) expect(line).toMatch(/^GET /);
  for (const line of deployment)
    expect(line.slice(line.indexOf(environment) + environment.length)).toMatch(
      new RegExp(
        `^(deployment-targets\\?limit=50|deployment-(runners|observations)\\?limit=50&target_id=${targetId})$`,
      ),
    );
});

test("Back to Clusters and Escape return to Add target", async ({ page }) => {
  await openClusters(page);
  await openWizard(page);
  // Back to Clusters, Provider, the two fields, then each command's Copy.
  await page.keyboard.press("Shift+Tab");
  const back = page.getByRole("button", { name: "Back to Clusters" });
  await expect(back).toBeFocused();
  for (const next of [
    page.getByRole("radio", { name: "kind" }),
    page.getByRole("textbox", { name: "Cluster name" }),
    page.getByRole("textbox", { name: "Namespace" }),
    copyButton(page, "create"),
    copyButton(page, "connect"),
  ]) {
    await page.keyboard.press("Tab");
    await expect(next).toBeFocused();
  }
  await back.click();
  await expect(page.getByRole("tab", { name: "Targets" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("button", { name: "Add target" })).toBeFocused();
  await page.getByRole("button", { name: "Add target" }).click();
  await page.getByRole("button", { name: "Connect a cluster" }).click();
  await page.getByRole("textbox", { name: "Cluster name" }).focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Add target" })).toBeFocused();
  await expect(
    page.getByRole("heading", { name: "Connect a kind or k3d cluster" }),
  ).toHaveCount(0);
});
