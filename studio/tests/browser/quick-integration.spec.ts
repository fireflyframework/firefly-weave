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
// The quick-integration journeys (WP-19/20/21/09/22a): describe an API request
// or paste an OpenAPI document, publish the action, insert it with its
// connection slot, create the connection with secret handle names only,
// activate with the connector release pinned, and start a run from a form.
// The local authoring endpoints run the repository's real Python code; the
// platform is mocked. Every step leaves a screenshot under journey/.
import { selectChoice } from "./support";
import { test, expect, Page, Request } from "@playwright/test";
import { parse } from "yaml";
import {
  allCapabilities,
  command,
  connected,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";
import {
  hasPython,
  localAuthoring,
  stopLocalAuthoring,
} from "./local-authoring";
import {
  actionPicker,
  openPaletteIntegrations,
  openYaml,
  shot,
} from "./integrations-po";

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const environment = `${project}/environments/development`;
const digest =
  "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8";
const connectorVersion = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
const httpRelease = "3f2a9b1c-0000-4000-8000-000000000001";
const revisionId = "0f8fad5b-d9cb-469f-a165-70867728950e";
const versionId = "11111111-1111-4111-8111-111111111111";
const compiled = {
  ok: true,
  validationOk: true,
  errorCount: 0,
  partial: false,
  diagnostics: [],
  artifact: {},
};
const petstore = `openapi: 3.1.0
info:
  title: Pet Store
  version: 1.0.0
servers:
  - url: https://api.petstore.test
paths:
  /pets/{petId}:
    get:
      operationId: getPet
      summary: Read one pet
      parameters:
        - name: petId
          in: path
          required: true
          schema:
            type: string
            maxLength: 64
      responses:
        "200":
          description: The pet
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
                    maxLength: 64
                  name:
                    type: string
                    maxLength: 200
  /pets:
    post:
      operationId: createPet
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                name:
                  type: string
                  maxLength: 200
      responses:
        "201":
          description: Created
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
                    maxLength: 64
`;

// CI must have the Python environment; locally the journey is skipped without it.
test.skip(
  !hasPython && !process.env.CI,
  "Needs the repository's Python environment.",
);
test.afterAll(() => stopLocalAuthoring());

interface Platform {
  /** Published action documents by catalog ID. */
  actions: Map<string, Record<string, unknown>>;
  publishes: Request[];
  compiles: Request[];
  connections: Request[];
  activation: Record<string, unknown> | null;
  runs: Request[];
}

/** A platform where weave-http@2.0.0 is installed, published and released. */
async function platform(page: Page): Promise<Platform> {
  const state: Platform = {
    actions: new Map(),
    publishes: [],
    compiles: [],
    connections: [],
    activation: null,
    runs: [],
  };
  await connected(page, {
    capabilities: [...allCapabilities, "compile"],
    catalog: [],
  });
  await page.route(`${project}/actions?*`, (r) =>
    r.fulfill({
      json: {
        items: [...state.actions].map(([id, document]) => ({
          id,
          name: (document["metadata"] as Record<string, string>)["name"],
          version: (document["metadata"] as Record<string, string>)["version"],
        })),
        next_cursor: null,
      },
    }),
  );
  await page.route(`${project}/actions/*/export`, (r) => {
    const id = r.request().url().split("/").at(-2)!;
    return r.fulfill({ json: { id, document: state.actions.get(id) } });
  });
  await page.route(`${project}/actions`, (r) => {
    state.publishes.push(r.request());
    const body = r.request().postDataJSON() as { source: string };
    const document = parse(body.source) as Record<string, unknown>;
    const id = `h${state.actions.size + 1}`;
    state.actions.set(id, document);
    const metadata = document["metadata"] as Record<string, string>;
    return r.fulfill({
      status: 201,
      json: { id, kind: "Action", ...metadata },
    });
  });
  await page.route(`${project}/compiler/compile`, (r) => {
    state.compiles.push(r.request());
    return r.fulfill({ json: compiled });
  });
  await page.route(`${project}/capabilities`, (r) =>
    r.fulfill({ json: { connectors: ["weave-http-v2"] } }),
  );
  await page.route(`${project}/connector-descriptors/weave-http-v2`, (r) =>
    r.fulfill({
      json: {
        adapter: "weave-http-v2",
        reference: "weave-http@2.0.0",
        digest,
        manifest: {},
        source: "{}",
        implementation_version: "2.0.0",
        capabilities: [],
        bindings: [],
        actions: [],
        connection: { config_schema: {}, auth_schema: {} },
        published_version_id: connectorVersion,
      },
    }),
  );
  await page.route(`${project}/connectors?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: connectorVersion,
            kind: "Connector",
            name: "weave-http",
            version: "2.0.0",
            digest: "a".repeat(64),
            definition_digest: digest,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${environment}/worker-releases?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: httpRelease,
            image_digest: "sha256:" + "b".repeat(64),
            capabilities: [
              { taskType: "weave-connector-http-read", taskVersion: "2.0.0" },
            ],
            connector_bindings: [
              {
                connector_digest: digest,
                action: "read",
                adapter: "weave-http-v2",
                implementation_version: "2.0.0",
                task_reference: "weave-connector-http-read@2.0.0",
              },
            ],
          },
        ],
        next_cursor: null,
      },
    }),
  );
  const created = () =>
    state.connections.map((request) => ({
      ...(request.postDataJSON() as Record<string, unknown>),
      id: revisionId,
      revision: 1,
      connector: "weave-http@2.0.0",
      connector_digest: digest,
      adapter: "weave-http-v2",
    }));
  await page.route(`${environment}/connections?*`, (r) =>
    r.fulfill({ json: { items: created(), next_cursor: null } }),
  );
  await page.route(`${environment}/connections`, (r) => {
    state.connections.push(r.request());
    return r.fulfill({ status: 201, json: created().at(-1) });
  });
  await page.route(`${project}/workflows`, (r) =>
    r.fulfill({
      status: 201,
      json: {
        id: versionId,
        name: "call-an-api",
        version: "1.0.0",
        digest: "e".repeat(64),
      },
    }),
  );
  await page.route(`${project}/workflows/${versionId}/export`, (r) => {
    const action = [...state.actions.values()][0];
    const metadata = action["metadata"] as Record<string, string>;
    return r.fulfill({
      json: {
        id: versionId,
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "call-an-api", version: "1.0.0" },
          spec: {
            inputSchema: {
              type: "object",
              required: ["recordId"],
              properties: {
                recordId: { type: "string", title: "Record ID", minLength: 1 },
              },
            },
            outputSchema: { type: "object" },
            connections: { api: { connector: "weave-http@2.0.0" } },
            steps: [],
            output: { literal: {} },
          },
        },
        artifact: {
          executable: {
            connections: {
              api: { connector: "weave-http@2.0.0", required: true },
            },
            dependencies: [
              {
                kind: "Action",
                reference: `${metadata["name"]}@${metadata["version"]}`,
                digest: "2".repeat(64),
                document: action,
              },
              {
                kind: "Connector",
                reference: "weave-http@2.0.0",
                digest,
                document: { spec: { adapter: "weave-http-v2" } },
              },
            ],
            graph: { nodes: [] },
          },
        },
      },
    });
  });
  await page.route(`${environment}/activations`, (r) => {
    state.activation = r.request().postDataJSON();
    return r.fulfill({
      status: 201,
      json: {
        id: "act-1",
        name: "call-an-api",
        revision: 1,
        request: { version_id: versionId },
      },
    });
  });
  await page.route(`${environment}/runs`, (r) => {
    state.runs.push(r.request());
    return r.fulfill({
      status: 201,
      json: { id: "run-1", state: { status: "queued" } },
    });
  });
  await page.route(`${environment}/runs/run-1`, (r) =>
    r.fulfill({ json: { id: "run-1", state: { status: "queued" } } }),
  );
  return state;
}

const builder = (page: Page) =>
  page.getByRole("dialog", { name: "New API action" });
const preview = (page: Page) =>
  page.getByRole("region", { name: /Action YAML for/ });

/** Closes an inspector that covers the toolbar on narrow layouts. */
async function closeInspector(page: Page) {
  const close = page.getByRole("button", { name: "Close inspector" });
  if (await close.isVisible()) await close.click();
}

for (const [width, height] of [
  [1440, 900],
  [600, 500],
] as const)
  test.describe(`${width}x${height}`, () => {
    test.use({ viewport: { width, height } });

    test("quick integration: template, describe, publish, use, connect, activate, run", async ({
      page,
    }, info) => {
      test.setTimeout(120_000);
      const key = `quick-${width}`;
      const state = await platform(page);
      const local = await localAuthoring(page);
      const designer = new DesignerPage(page);

      // 1. Home offers templates; "Call an API" opens as a new draft.
      const gallery = page.locator("weave-template-gallery");
      await gallery
        .locator('[data-template="api-call"]')
        .scrollIntoViewIfNeeded();
      await shot(page, info, `${key}-1-home`);
      await page
        .getByRole("button", { name: "Use template: Call an API" })
        .click();
      await expect(designer.node("call-api")).toBeVisible();
      expect(await sourceText(page)).toContain("uses: your-action@1.0.0");

      // 2. The placeholder step offers to create its action from an API.
      await designer.selectStep("call-api");
      const create = designer.inspector.getByRole("button", {
        name: "New API action",
      });
      await create.scrollIntoViewIfNeeded();
      await shot(page, info, `${key}-2-placeholder-step`);
      await create.click();
      await expect(builder(page)).toBeVisible();
      await expect(
        builder(page).getByLabel("Name", { exact: true }),
      ).toBeFocused();
      // Readiness is one line, with the checklist behind Show.
      await expect(builder(page).locator(".readiness-line")).toContainText(
        "Platform ready for API actions",
      );

      // 3. Describe GET /v1/records/{id} with a response sample.
      await page.keyboard.type("get-record");
      await builder(page)
        .getByLabel("API address")
        .fill("https://api.records.test");
      await builder(page)
        .getByLabel("Path", { exact: true })
        .fill("/v1/records/{id}");
      await selectChoice(
        builder(page).getByLabel("How the API checks who is calling"),
        "api-key",
      );
      await builder(page)
        .getByLabel("Header that carries the key")
        .fill("X-API-Key");
      await builder(page)
        .getByLabel("Example response")
        .fill('{"id": "r-7", "status": "open"}');
      await openYaml(page);
      await expect(preview(page)).toContainText("sideEffect: read_only");
      await expect(preview(page)).toContainText("uses: weave-http@2.0.0");
      await expect(preview(page)).toContainText(/required:\s+- path/);
      // The origin stays with the connection; sample values are never kept.
      await expect(preview(page)).not.toContainText("api.records.test");
      await expect(preview(page)).not.toContainText("r-7");
      await preview(page).scrollIntoViewIfNeeded();
      await shot(page, info, `${key}-3-describe`);
      const built = local.filter((c) => c.path === "/studio/local/http-action");
      expect(built.length).toBeGreaterThan(0);
      for (const call of built) {
        // The method fixes the effect: nothing else may choose it.
        expect(call.body["request"]).not.toHaveProperty("sideEffect");
        expect(call.body["request"]).not.toHaveProperty("retry");
        expect(JSON.stringify(call.body)).not.toContain("api.records.test");
      }

      // 4. Publish, checked against the platform first.
      await builder(page)
        .getByRole("button", { name: "Publish action" })
        .click();
      await page
        .getByRole("dialog", { name: "Publish get-record 1.0.0?" })
        .getByRole("button", { name: "Publish action" })
        .click();
      await expect(builder(page)).toContainText("Published get-record@1.0.0.");
      expect(state.compiles.length).toBeGreaterThan(0);
      expect(state.publishes).toHaveLength(1);
      expect(state.publishes[0].headers()["idempotency-key"]).toBeTruthy();
      const published = parse(
        (state.publishes[0].postDataJSON() as { source: string }).source,
      );
      expect(published.spec.sideEffect).toBe("read_only");
      await builder(page)
        .getByRole("button", { name: "Use in this step" })
        .scrollIntoViewIfNeeded();
      await shot(page, info, `${key}-4-published`);

      // 5. "Use in this step": the template's slot and input mapping stay.
      await builder(page)
        .getByRole("button", { name: "Use in this step" })
        .click();
      await expect(builder(page)).toHaveCount(0);
      await expect(page.locator("#integration-heading")).toBeFocused();
      await expect(actionPicker(page)).toHaveValue("get-record@1.0.0");
      let source = await sourceText(page);
      expect(source).toContain("uses: get-record@1.0.0");
      expect(source).toContain("connection: api");
      expect(source).toMatch(/id:\s+ref: \/input\/recordId/);
      expect(source).not.toContain("uses: your-action@1.0.0");
      // One undo step restores the placeholder.
      await command(page, "Undo");
      expect(await sourceText(page)).toContain("uses: your-action@1.0.0");
      await command(page, "Redo");
      source = await sourceText(page);
      expect(source).toContain("uses: get-record@1.0.0");
      await designer.selectStep("call-api");
      await shot(page, info, `${key}-5-step`);

      // 6. Create the connection for the slot: secret handle names only.
      await designer.inspector
        .getByRole("button", { name: "Create a connection for this API" })
        .click();
      const connect = page.getByRole("dialog", { name: "New API connection" });
      await expect(
        connect.getByLabel("Connection name", { exact: true }),
      ).toHaveValue("records");
      await expect(
        connect.getByLabel("API address", { exact: true }),
      ).toHaveValue("https://api.records.test");
      await connect
        .getByLabel("API key handle", { exact: true })
        .fill("records-api-key");
      await connect.getByRole("button", { name: "Create connection" }).click();
      await expect.poll(() => state.connections.length).toBe(1);
      expect(state.connections[0].postDataJSON()).toEqual({
        name: "records",
        connector_version_id: connectorVersion,
        config: {
          baseUrl: "https://api.records.test",
          auth: { kind: "api-key", header: "X-API-Key" },
        },
        secretRef: { api_key: "records-api-key" },
        allowed_destinations: ["https://api.records.test"],
      });
      await shot(page, info, `${key}-6-connection`);
      await page.keyboard.press("Escape");
      await expect(connect).toHaveCount(0);

      // 7. Publish the workflow, then activate with the release pinned.
      await closeInspector(page);
      await command(page, "Publish…");
      await page
        .getByRole("dialog")
        .getByRole("button", { name: "Publish version" })
        .click();
      await command(page, "Activate…");
      const activate = page.getByRole("dialog");
      await expect(activate).toContainText(/Activate \S+ \d+\.\d+\.\d+/);
      await expect(activate.getByRole("status")).toHaveCount(0);
      await activate
        .getByRole("button", { name: "Review", exact: true })
        .click();
      await expect(activate.getByLabel(/^weave-http@2\.0\.0/)).toHaveValue(
        httpRelease,
      );
      await expect(activate.getByLabel(/^api/)).toHaveValue(revisionId);
      await shot(page, info, `${key}-7-activate`);
      const submit = activate.getByRole("button", { name: "Activate version" });
      await submit.scrollIntoViewIfNeeded();
      await submit.click();
      await expect(activate).toHaveCount(0);
      expect(state.activation).toMatchObject({
        version_id: versionId,
        connection_revision_ids: { api: revisionId },
        connector_release_ids: { [connectorVersion]: httpRelease },
      });

      // 8. Start a run from a form built from the version's input schema.
      await command(page, "Start run…");
      const run = page.getByRole("dialog", { name: "Start a run" });
      await expect(
        run.getByLabel("Version to run", { exact: true }),
      ).toHaveValue("act-1");
      await run.getByLabel("Record ID").fill("r-7");
      await shot(page, info, `${key}-8-run`);
      await run.getByRole("button", { name: "Start run" }).click();
      await expect(run).toHaveCount(0);
      expect(state.runs).toHaveLength(1);
      expect(state.runs[0].postDataJSON()).toMatchObject({
        activation_id: "act-1",
        input: { recordId: "r-7" },
      });
      // The published version holds these edits: the toast opens the new
      // run without asking to leave unsaved work.
      const toast = page.locator(".toast");
      await expect(toast).toContainText("Started run run-1.");
      await toast.getByRole("button", { name: "View" }).click();
      await expect(page.locator(".record-detail")).toContainText("run-1");
      await expect(
        page.getByRole("dialog", { name: "Leave the designer?" }),
      ).toHaveCount(0);
    });

    test("describe a request from the palette and insert it with its slot", async ({
      page,
    }, info) => {
      test.setTimeout(90_000);
      const key = `palette-${width}`;
      const state = await platform(page);
      await localAuthoring(page);
      await page
        .getByRole("button", { name: "New workflow", exact: true })
        .click();
      const palette = await openPaletteIntegrations(page);
      await expect(palette).toContainText(
        "This project has no published actions yet.",
      );
      await palette.getByRole("button", { name: "New API action" }).click();
      await expect(
        builder(page).getByLabel("Name", { exact: true }),
      ).toBeFocused();
      await page.keyboard.type("get-pet");
      await builder(page)
        .getByLabel("API address")
        .fill("https://api.petstore.test");
      await builder(page)
        .getByLabel("Path", { exact: true })
        .fill("/v1/pets/{petId}");
      await builder(page)
        .getByLabel("Example response")
        .fill('{"id": 7, "name": "Rex"}');
      await openYaml(page);
      await expect(preview(page)).toContainText("petId");
      await builder(page)
        .getByRole("button", { name: "Publish action" })
        .click();
      await page
        .getByRole("dialog", { name: "Publish get-pet 1.0.0?" })
        .getByRole("button", { name: "Publish action" })
        .click();
      await expect(builder(page)).toContainText("Published get-pet@1.0.0.");
      await builder(page)
        .getByRole("button", { name: "Insert into workflow" })
        .click();
      await expect(builder(page)).toHaveCount(0);
      const designer = new DesignerPage(page);
      await expect(designer.node("call-action-1")).toBeVisible();
      await expect(
        designer.node("call-action-1").locator(".node-body"),
      ).toBeFocused();
      const source = await sourceText(page);
      expect(source).toContain("uses: get-pet@1.0.0");
      expect(source).toContain("connection: petstore");
      expect(source).toMatch(
        /connections:\s+petstore:\s+connector: weave-http@2\.0\.0\s+required: true/,
      );
      await shot(page, info, `${key}-inserted`);
      // The step and its slot are one undo step.
      await command(page, "Undo");
      const undone = await sourceText(page);
      expect(undone).not.toContain("get-pet@1.0.0");
      expect(undone).not.toContain("petstore");
      // The published action is now listed in the palette and inserts again.
      const again = await openPaletteIntegrations(page);
      await again
        .getByRole("button", { name: /^Insert get-pet@1\.0\.0/ })
        .click();
      await expect(designer.node("call-action-1")).toBeVisible();
      expect(await sourceText(page)).toContain("connection: weave-http");
      expect(state.publishes).toHaveLength(1);

      // The builder's hand-off belongs to this workflow: another workflow
      // with a slot of the same name starts its connection form empty.
      await closeInspector(page);
      await page.getByRole("button", { name: "Home", exact: true }).click();
      await page.getByRole("button", { name: "Leave designer" }).click();
      await page
        .getByRole("button", { name: "New workflow", exact: true })
        .click();
      await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: other-api
  version: 1.0.0
spec:
  inputSchema:
    type: object
  outputSchema:
    type: object
  connections:
    petstore:
      connector: weave-http@2.0.0
  steps:
    - id: call
      kind: action
      uses: your-action@1.0.0
      connection: petstore
      with:
        literal: {}
  output:
    literal: {}
`);
      await designer.selectStep("call");
      await designer.inspector
        .getByRole("button", { name: "Create a connection for this API" })
        .click();
      const connect = page.getByRole("dialog", { name: "New API connection" });
      await expect(
        connect.getByLabel("Connection name", { exact: true }),
      ).toHaveValue("petstore");
      await expect(
        connect.getByLabel("API address", { exact: true }),
      ).toHaveValue("");
    });

    test("OpenAPI import: paste a small YAML document, publish and insert", async ({
      page,
    }, info) => {
      test.setTimeout(90_000);
      const key = `openapi-${width}`;
      const state = await platform(page);
      const local = await localAuthoring(page);
      await page
        .getByRole("button", { name: "New workflow", exact: true })
        .click();
      const palette = await openPaletteIntegrations(page);
      await palette.getByRole("button", { name: "New API action" }).click();
      await builder(page).getByRole("tab", { name: "Import OpenAPI" }).click();
      const panel = page.getByRole("tabpanel", { name: "Import OpenAPI" });
      // Paste or upload only: nothing takes a web address.
      await expect(panel.locator('input[type="url"]')).toHaveCount(0);
      await panel.getByLabel("Or paste it here").fill(petstore);
      await panel.getByRole("button", { name: "List operations" }).click();
      const operation = panel
        .locator(".hb-op")
        .filter({ hasText: "/pets/{petId}" });
      await expect(operation).toContainText("Ready");
      await shot(page, info, `${key}-1-listed`);
      await operation.getByRole("checkbox").check();
      await panel.getByRole("button", { name: "Create 1 action" }).click();
      await expect(
        panel.getByRole("heading", { name: "1 action created" }),
      ).toBeVisible();
      await openYaml(page);
      await expect(preview(page)).toContainText("sideEffect: read_only");
      await expect(preview(page)).toContainText("name: get-pet");
      await shot(page, info, `${key}-2-created`);
      await builder(page)
        .getByRole("button", { name: "Publish action" })
        .click();
      await page
        .getByRole("dialog", { name: /^Publish get-pet/ })
        .getByRole("button", { name: "Publish action" })
        .click();
      await expect(builder(page)).toContainText("Published get-pet@");
      await builder(page)
        .getByRole("button", { name: "Insert into workflow" })
        .click();
      await expect(builder(page)).toHaveCount(0);
      const source = await sourceText(page);
      expect(source).toMatch(/uses: get-pet@\d+\.\d+\.\d+/);
      expect(source).toContain("connection: pet-store");
      expect(source).toMatch(
        /connections:\s+pet-store:\s+connector: weave-http@2\.0\.0/,
      );
      await shot(page, info, `${key}-3-inserted`);
      expect(state.publishes).toHaveLength(1);
      const imports = local.filter(
        (c) => c.path === "/studio/local/openapi/import",
      );
      expect(imports).toHaveLength(1);
      expect(imports[0].body).toMatchObject({
        format: "yaml",
        selection: ["getPet"],
        target: "builtin",
      });
      for (const call of local) expect(call.body).not.toHaveProperty("url");
    });
  });
