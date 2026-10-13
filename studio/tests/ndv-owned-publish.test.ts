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
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type {
  PlatformCompileResult,
  PublishedVersion,
} from "../src/app/integrations/http-action-client";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { COMMON_ACTIONS } from "../src/app/editor/ndv/owned/common-actions";
import { insertOwnedStep } from "../src/app/editor/ndv/owned/insert-owned";
import {
  nameOfUses,
  versionOfUses,
  type OwnedRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import {
  OwnedPublisher,
  publishLabel,
  type OwnedPublishPorts,
} from "../src/app/editor/ndv/owned/owned-publish";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import {
  markPublished,
  moveOwned,
  pendingOwned,
} from "../src/app/editor/ndv/owned/owned-versions";
import { StructuredCanvasAdapter } from "../src/app/model";

const API = "order-intake.call-api@1.0.0";
const EMAIL = "order-intake.send-email@1.0.0";
const action = (id: string) => COMMON_ACTIONS.find((item) => item.id === id)!;
/** A stand-in for the document Studio builds: what the publisher reads is its name, version and connector. */
const documentOf = (uses: string) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: nameOfUses(uses), version: versionOfUses(uses) },
  spec: {
    connection: {
      connector: uses.includes("send-email")
        ? "weave-email@1.0.0"
        : "weave-http@2.0.0",
    },
  },
});
const ok: PlatformCompileResult = { ok: true, diagnostics: [] };
const taken: PlatformCompileResult = {
  ok: false,
  diagnostics: [
    {
      code: "WV-COMP-IMMUTABLE_VERSION",
      severity: "error",
      message: "Immutable version",
      path: "/metadata/version",
    },
  ],
};
const version = (uses: string): PublishedVersion => ({
  id: "v",
  name: nameOfUses(uses),
  version: versionOfUses(uses),
});
interface Sent {
  yaml: string;
  key: string;
}
const nameIn = (yaml: string) => /\n {2}name: (\S+)/.exec(yaml)![1];

/** order-intake: Call an API (GET /orders), then Send email; both actions are the workflow's own. */
function setup(
  change: (context: {
    model: StructuredCanvasAdapter;
    sent: Sent[];
  }) => Partial<OwnedPublishPorts> = () => ({}),
) {
  const model = new StructuredCanvasAdapter();
  model.definition.metadata.name = "order-intake";
  insertOwnedStep(model, action("send-email"), "root", 0);
  insertOwnedStep(model, action("call-api"), "root", 0);
  model.canvas = withOwnedAction(model.canvas, API, {
    ...ownedActionsOf(model.canvas)[API],
    pathTemplate: "/orders",
  } as OwnedRecipe);
  model.clearHistory();
  const sent: Sent[] = [];
  const checked: string[] = [];
  const ports: OwnedPublishPorts = {
    model,
    purpose: "publish",
    editingLocked: () => false,
    canCompile: () => true,
    versionsOf: () => [],
    reserved: () => new Set(),
    sync: () => undefined,
    settled: async () => undefined,
    document: (uses) =>
      Object.hasOwn(ownedActionsOf(model.canvas), uses)
        ? documentOf(uses)
        : null,
    problems: () => new Map(),
    compile: async (document) => {
      checked.push((document["metadata"] as { name: string }).name);
      return ok;
    },
    publish: async (yaml, key) => {
      sent.push({ yaml, key });
      return version(`${nameIn(yaml)}@1.0.0`);
    },
    perform: (edit) => {
      try {
        edit();
        return true;
      } catch {
        return false;
      }
    },
    record: (edit) => {
      model.amend(edit);
      return true;
    },
    ...change({ model, sent }),
  };
  return { model, sent, checked, publisher: new OwnedPublisher(ports) };
}
const headers = (model: StructuredCanvasAdapter) => {
  const step = model.nodes().find((node) => node.step.id === "call-api")!.step;
  model.update(
    "call-api",
    JSON.stringify({
      ...step,
      with: {
        object: { headers: { object: { "X-Trace": { literal: "1" } } } },
      },
    }),
  );
};

beforeAll(async () => {
  await loadKindRegistrations();
});

describe("publishing the actions a workflow owns", () => {
  it("lists the unpublished actions with their steps, checked on the platform, ready to confirm", async () => {
    const { publisher, checked } = setup();
    await publisher.prepare();
    expect(publisher.phase()).toBe("confirm");
    expect(
      publisher.rows().map((row) => [row.uses, row.steps, row.state]),
    ).toEqual([
      [API, ["call-api"], "ready"],
      [EMAIL, ["send-email"], "ready"],
    ]);
    expect(checked).toEqual([
      "order-intake.call-api",
      "order-intake.send-email",
    ]);
  });
  it("publishes in step order with keys from the content, records each, and an Undo past it forgets only Studio's record", async () => {
    const { publisher, sent, model } = setup();
    // One edit before publishing, for the record to join.
    model.batch(() => model.insert("wait"));
    await publisher.prepare();
    expect(await publisher.publish()).toBe(true);
    expect(publisher.phase()).toBe("done");
    expect(publisher.outcome()).toEqual({
      done: true,
      published: 2,
      source: model.source,
    });
    expect(sent.map((item) => nameIn(item.yaml))).toEqual([
      "order-intake.call-api",
      "order-intake.send-email",
    ]);
    expect(sent[0].key).toMatch(
      /^owned-order-intake\.call-api@1\.0\.0-[0-9a-f]{14}$/,
    );
    expect(pendingOwned(model)).toEqual([]);
    model.undo();
    expect(pendingOwned(model)).toEqual([API, EMAIL]);
  });
  it("on a workflow opened with no undo history, publishing adds no undo step", async () => {
    const { publisher, model } = setup();
    expect(model.canUndo).toBe(false);
    await publisher.prepare();
    expect(await publisher.publish()).toBe(true);
    expect(pendingOwned(model)).toEqual([]);
    expect(model.canUndo).toBe(false);
  });
  it("stops at a failure, keeps what was published, and Try again sends the same request", async () => {
    let failures = 1;
    const { publisher, sent, model } = setup(({ sent }) => ({
      publish: async (yaml, key) => {
        sent.push({ yaml, key });
        if (yaml.includes("send-email") && failures-- > 0)
          throw new ApiError(503, { code: "WV-UNAVAILABLE", message: "Busy" });
        return version(`${nameIn(yaml)}@1.0.0`);
      },
    }));
    await publisher.prepare();
    expect(await publisher.publish()).toBe(false);
    expect(publisher.phase()).toBe("partial");
    expect(publisher.rows().map((row) => row.state)).toEqual([
      "published",
      "unknown",
    ]);
    expect(publisher.rows()[1].message).toBe(
      "Studio couldn't confirm whether this was published. Try again sends the same request, so nothing is published twice.",
    );
    expect(publisher.summary()).toBe(
      "1 of 2 actions published. The workflow wasn't published. What was published stays as it is. Try again publishes the rest.",
    );
    expect(publisher.outcome()).toEqual({
      done: false,
      published: 1,
      reason: publisher.summary(),
    });
    expect(await publisher.publish()).toBe(true);
    expect(sent.map((item) => item.key)).toEqual([
      sent[0].key,
      sent[1].key,
      sent[1].key,
    ]);
    expect(publisher.outcome()).toEqual({
      done: true,
      published: 2,
      source: model.source,
    });
  });
  it("offers a free name for a taken action never published, and the next version for one published before", async () => {
    const { publisher, model, sent } = setup(() => ({
      compile: async (document) => {
        const { name, version: at } = document["metadata"] as {
          name: string;
          version: string;
        };
        return [EMAIL, "order-intake.call-api@1.0.1"].includes(`${name}@${at}`)
          ? taken
          : ok;
      },
      versionsOf: (name) => (name === "order-intake.call-api" ? ["1.0.1"] : []),
    }));
    // Call an API was published at 1.0.0 and changed since: it is at 1.0.1, which another draft took.
    model.canvas = markPublished(model, API);
    model.batch(() => moveOwned(model, API, "order-intake.call-api@1.0.1"));
    await publisher.prepare();
    expect(
      publisher.rows().map((row) => [row.uses, row.state, row.proposal]),
    ).toEqual([
      [
        "order-intake.call-api@1.0.1",
        "conflict",
        "order-intake.call-api@1.0.2",
      ],
      [EMAIL, "conflict", "order-intake.send-email-2@1.0.0"],
    ]);
    expect(publisher.rows()[0].message).toBe(
      "Version 1.0.1 is already published with different content. Studio publishes this one as version 1.0.2.",
    );
    expect(publisher.rows()[1].message).toBe(
      "Version 1.0.0 of order-intake.send-email is already published with different content. Studio publishes this one as order-intake.send-email-2 1.0.0.",
    );
    expect(await publisher.publish()).toBe(true);
    expect(model.nodes().map((node) => node.step["uses"])).toEqual([
      "order-intake.call-api@1.0.2",
      "order-intake.send-email-2@1.0.0",
    ]);
    expect(sent.map((item) => nameIn(item.yaml))).toEqual([
      "order-intake.call-api",
      "order-intake.send-email-2",
    ]);
  });
  it("without permission to compile, a taken version found while publishing becomes a suggestion", async () => {
    const { publisher, model } = setup(({ sent }) => ({
      canCompile: () => false,
      publish: async (yaml, key) => {
        sent.push({ yaml, key });
        if (nameIn(yaml) === "order-intake.send-email")
          throw new ApiError(409, {
            code: "WV-VERSION-CONFLICT",
            message: "An immutable version already has different content",
          });
        return version(`${nameIn(yaml)}@1.0.0`);
      },
    }));
    await publisher.prepare();
    expect(publisher.phase()).toBe("confirm");
    expect(await publisher.publish()).toBe(false);
    expect(publisher.rows().map((row) => row.state)).toEqual([
      "published",
      "conflict",
    ]);
    expect(publisher.summary()).toBe(
      "1 of 2 actions published. The workflow wasn't published. What was published stays as it is. Publish as suggested publishes the rest.",
    );
    expect(await publisher.publish()).toBe(true);
    expect(model.nodes().map((node) => node.step["uses"])).toEqual([
      API,
      "order-intake.send-email-2@1.0.0",
    ]);
  });
  it("publishes nothing when an action can't be built or the project lacks its connector", async () => {
    // Call an API has no document yet (as without a URL); Send email's connector is missing.
    const missing = setup(({ model }) => ({
      document: (uses) =>
        uses !== API && Object.hasOwn(ownedActionsOf(model.canvas), uses)
          ? documentOf(uses)
          : null,
      compile: async () => ({
        ok: false,
        diagnostics: [
          {
            code: "WV-COMP-UNKNOWN_CONNECTOR",
            severity: "error",
            message: "Unknown connector",
            path: "/spec/connection/connector",
          },
        ],
      }),
    }));
    await missing.publisher.prepare();
    expect(missing.publisher.phase()).toBe("blocked");
    expect(
      missing.publisher.rows().map((row) => [row.state, row.message]),
    ).toEqual([
      ["blocked", "Fill in the URL of step call-api first."],
      [
        "blocked",
        "This project doesn't have the connector weave-email 1.0.0 yet. Ask a developer to publish it, then check again.",
      ],
    ]);
    expect(missing.publisher.summary()).toBe(
      "2 of 2 actions can't be published yet. Nothing was published.",
    );
    expect(missing.publisher.outcome()).toEqual({
      done: false,
      published: 0,
      reason: missing.publisher.summary(),
    });
    expect(missing.sent).toEqual([]);
    // A build the Studio host refused shows its own words.
    const refused = setup(() => ({
      problems: () =>
        new Map([
          [
            API,
            [
              {
                code: "WV-STUDIO-ACTION-BUILD",
                severity: "error" as const,
                message:
                  "Couldn't build the API action. Check the URL and try again.",
                path: "/pathTemplate",
              },
            ],
          ],
        ]),
    }));
    await refused.publisher.prepare();
    expect(refused.publisher.rows()[0]).toMatchObject({
      state: "blocked",
      message: "Couldn't build the API action. Check the URL and try again.",
    });
    expect(refused.sent).toEqual([]);
  });
  it("gives actions changed outside step details their next version before listing them", async () => {
    const { publisher, model } = setup();
    model.canvas = markPublished(model, API);
    headers(model);
    await publisher.prepare();
    expect(publisher.rows()[0]).toMatchObject({
      uses: "order-intake.call-api@1.0.1",
      state: "ready",
      message: "Changed after version 1.0.0 was published.",
    });
    expect(model.nodes()[0].step["uses"]).toBe("order-intake.call-api@1.0.1");
  });
  it("needs an editable workflow to move actions, and a readable source", async () => {
    const locked = setup(() => ({ editingLocked: () => true }));
    locked.model.canvas = markPublished(locked.model, API);
    headers(locked.model);
    await locked.publisher.prepare();
    expect(locked.publisher.phase()).toBe("blocked");
    expect(locked.publisher.summary()).toBe(
      "Stop the simulation first: actions that changed after they were published need new versions.",
    );
    expect(locked.model.nodes()[0].step["uses"]).toBe(API);
    const broken = setup();
    broken.model.setSource("kind: [");
    await broken.publisher.prepare();
    expect(broken.publisher.phase()).toBe("blocked");
    expect(broken.publisher.summary()).toBe("Fix the workflow source first.");
  });
  it("says plainly when the account can't publish actions", async () => {
    const { publisher } = setup(() => ({
      publish: async () => {
        throw new ApiError(403, { code: "WV-FORBIDDEN", message: "Forbidden" });
      },
    }));
    await publisher.prepare();
    expect(await publisher.publish()).toBe(false);
    expect(publisher.rows()[0]).toMatchObject({
      state: "blocked",
      message: "Your account can't publish actions in this workspace.",
    });
  });
  it("for a simulation, says the simulation didn't start", async () => {
    const { publisher } = setup(() => ({
      purpose: "simulate",
      publish: async () => {
        throw new ApiError(503, { code: "WV-UNAVAILABLE", message: "Busy" });
      },
    }));
    await publisher.prepare();
    expect(await publisher.publish()).toBe(false);
    expect(publisher.summary()).toBe(
      "0 of 2 actions published. The simulation didn't start. Try again publishes the rest.",
    );
  });
  it("is busy from the moment publishing starts, takes no second start, and sends nothing once abandoned", async () => {
    let release = () => undefined as void;
    const slow = new Promise<void>((done) => (release = done));
    let checks = 0;
    const { publisher, model, sent } = setup(() => ({
      compile: async (document) => {
        const { name, version: at } = document["metadata"] as {
          name: string;
          version: string;
        };
        // The first check finds the email's name taken; the check of the new name is slow.
        if (checks++ >= 2) await slow;
        return `${name}@${at}` === EMAIL ? taken : ok;
      },
    }));
    await publisher.prepare();
    expect(publisher.rows()[1].proposal).toBe(
      "order-intake.send-email-2@1.0.0",
    );
    const going = publisher.publish();
    expect(publisher.busy()).toBe(true);
    expect(publisher.summary()).toBe("Checking the suggested names…");
    expect(await publisher.publish()).toBe(false);
    publisher.abandon();
    release();
    expect(await going).toBe(false);
    expect(sent).toEqual([]);
    expect(pendingOwned(model)).toEqual([
      API,
      "order-intake.send-email-2@1.0.0",
    ]);
  });
  it("asks again, sending nothing, when a suggested name turns out to be taken too", async () => {
    const takenToo = [EMAIL, "order-intake.send-email-2@1.0.0"];
    const { publisher, model, sent } = setup(() => ({
      compile: async (document) => {
        const { name, version: at } = document["metadata"] as {
          name: string;
          version: string;
        };
        return takenToo.includes(`${name}@${at}`) ? taken : ok;
      },
    }));
    await publisher.prepare();
    expect(await publisher.publish()).toBe(false);
    expect(sent).toEqual([]);
    expect(publisher.phase()).toBe("confirm");
    expect(
      publisher.rows().map((row) => [row.uses, row.state, row.proposal]),
    ).toEqual([
      [API, "ready", undefined],
      [
        "order-intake.send-email-2@1.0.0",
        "conflict",
        "order-intake.send-email-2-2@1.0.0",
      ],
    ]);
    expect(await publisher.publish()).toBe(true);
    expect(sent.map((item) => nameIn(item.yaml))).toEqual([
      "order-intake.call-api",
      "order-intake.send-email-2-2",
    ]);
    expect(model.nodes().map((node) => node.step["uses"])).toEqual([
      API,
      "order-intake.send-email-2-2@1.0.0",
    ]);
  });
  it("sends a request the platform saw with other content again under a fresh key, for that action only", async () => {
    let conflicts = 1;
    const { publisher, sent } = setup(({ sent }) => ({
      publish: async (yaml, key) => {
        sent.push({ yaml, key });
        if (yaml.includes("send-email") && conflicts-- > 0)
          throw new ApiError(409, {
            code: "WV-IDEMPOTENCY-CONFLICT",
            message: "This key was used with other content",
          });
        return version(`${nameIn(yaml)}@1.0.0`);
      },
    }));
    await publisher.prepare();
    expect(await publisher.publish()).toBe(false);
    expect(publisher.rows()[1]).toMatchObject({
      state: "failed",
      message:
        "The platform saw this request before with different content. Try again sends it as a new request.",
    });
    expect(await publisher.publish()).toBe(true);
    const keys = sent.map((item) => item.key);
    expect(keys).toHaveLength(3);
    expect(keys[2]).not.toBe(keys[1]);
    expect(keys[2].startsWith(keys[1].slice(0, 186))).toBe(true);
    expect(keys[2].length).toBeLessThanOrEqual(200);
    expect(nameIn(sent[2].yaml)).toBe("order-intake.send-email");
  });
  it("tells screen readers what a taken action is published as, and stops once the names move", async () => {
    let release = () => undefined as void;
    const slow = new Promise<void>((done) => (release = done));
    let checks = 0;
    const { publisher } = setup(() => ({
      compile: async (document) => {
        const { name, version: at } = document["metadata"] as {
          name: string;
          version: string;
        };
        if (checks++ >= 2) await slow;
        return `${name}@${at}` === EMAIL ? taken : ok;
      },
    }));
    expect(publisher.proposals()).toBe("");
    await publisher.prepare();
    expect(publisher.proposals()).toBe(
      "Version 1.0.0 of order-intake.send-email is already published with different content. Studio publishes this one as order-intake.send-email-2 1.0.0.",
    );
    const going = publisher.publish();
    // The names moved: nothing is proposed while the new one is checked.
    expect(publisher.proposals()).toBe("");
    release();
    expect(await going).toBe(true);
  });
  it("after Publish as suggested finds another taken name, asks to publish only what remains", async () => {
    const takenToo = ["order-intake.send-email-2@1.0.0"];
    const { publisher, sent } = setup(({ sent }) => ({
      compile: async (document) => {
        const { name, version: at } = document["metadata"] as {
          name: string;
          version: string;
        };
        return takenToo.includes(`${name}@${at}`) ? taken : ok;
      },
      // The email's version is taken between the check and the publish.
      publish: async (yaml, key) => {
        sent.push({ yaml, key });
        if (nameIn(yaml) === "order-intake.send-email")
          throw new ApiError(409, {
            code: "WV-VERSION-CONFLICT",
            message: "An immutable version already has different content",
          });
        return version(`${nameIn(yaml)}@1.0.0`);
      },
    }));
    await publisher.prepare();
    expect(publishLabel("publish", publisher.rows())).toBe(
      "Publish 2 actions and the workflow",
    );
    expect(await publisher.publish()).toBe(false);
    expect(publisher.phase()).toBe("partial");
    // Publish as suggested: the suggested name is taken too, so it asks again.
    expect(await publisher.publish()).toBe(false);
    expect(publisher.phase()).toBe("confirm");
    expect(publisher.rows().map((row) => row.state)).toEqual([
      "published",
      "conflict",
    ]);
    expect(publishLabel("publish", publisher.rows())).toBe(
      "Publish 1 action and the workflow",
    );
    expect(publishLabel("simulate", publisher.rows())).toBe(
      "Publish 1 action and simulate",
    );
    expect(publisher.proposals()).toContain(
      "Studio publishes this one as order-intake.send-email-2-2 1.0.0.",
    );
  });
});
