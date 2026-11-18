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
// Webhook signing help: a copyable cURL that signs a request the way the
// platform checks it (triggers/webhooks.py, authenticate), with the secret's
// value read from $WEAVE_WEBHOOK_SECRET, never written into the command.
import { spawnSync } from "node:child_process";
import { createHmac } from "node:crypto";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  secretCommand,
  secretCommands,
  signedCurl,
  webhookUrls,
} from "../src/app/editor/triggers/webhook-help";

const URL =
  "https://weave.example/webhooks/0b6c1f3e-7a51-4e9a-9d61-7f0c2f9d1e11";

/** Runs the command with a stand-in curl that records its arguments; POSIX shells with OpenSSL only. */
function run(command: string, secret: string) {
  const directory = mkdtempSync(join(tmpdir(), "weave-webhook-help-"));
  try {
    const record = join(directory, "arguments");
    writeFileSync(
      join(directory, "curl"),
      `#!/bin/sh\nfor value in "$@"; do printf '%s\\0' "$value"; done > "${record}"\n`,
    );
    chmodSync(join(directory, "curl"), 0o755);
    const result = spawnSync("sh", ["-c", command], {
      env: {
        PATH: `${directory}:${process.env["PATH"]}`,
        WEAVE_WEBHOOK_SECRET: secret,
      },
      encoding: "utf8",
    });
    expect(result.stderr).toBe("");
    return readFileSync(record, "utf8").split("\0").slice(0, -1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}
const canSign =
  process.platform !== "win32" &&
  spawnSync("openssl", ["version"]).status === 0;

describe("the signed cURL", () => {
  it("names the platform's headers and reads the secret from the environment", () => {
    const command = signedCurl({
      url: URL,
      payloadSample: { customerId: "123" },
    });
    for (const header of [
      "X-Weave-Timestamp",
      "X-Weave-Event-Id",
      "X-Weave-Signature",
      "Content-Type: application/json",
    ])
      expect(command).toContain(header);
    expect(command).toContain('"$WEAVE_WEBHOOK_SECRET"');
    expect(command).toContain(`curl -sS -X POST '${URL}'`);
    expect(command.split("\n")[0]).toBe(
      "# Set WEAVE_WEBHOOK_SECRET to the secret's value first; Studio never sees it.",
    );
  });

  it.runIf(canSign)(
    "signs exactly the bytes it sends, as the platform checks them",
    () => {
      const sample = {
        customerId: "123",
        note: 'it\'s $HOME `here` \\ "quoted"',
      };
      const values = run(
        signedCurl({ url: URL, payloadSample: sample }),
        "s3cret-value",
      );
      const header = (name: string) =>
        values[
          values.findIndex((value) => value.startsWith(`${name}: `))
        ].slice(name.length + 2);
      const body = values[values.indexOf("--data-binary") + 1];
      const stamp = header("X-Weave-Timestamp");
      expect(values.slice(0, 4)).toEqual(["-sS", "-X", "POST", URL]);
      expect(stamp).toMatch(/^[0-9]{1,12}$/);
      expect(Math.abs(Date.now() / 1000 - Number(stamp))).toBeLessThan(60);
      const envelope = JSON.parse(body) as {
        eventId: string;
        payload: unknown;
      };
      expect(Object.keys(envelope).sort()).toEqual(["eventId", "payload"]);
      expect(envelope.payload).toEqual(sample);
      expect(envelope.eventId).toMatch(/^[A-Za-z0-9_.:-]{1,128}$/);
      expect(header("X-Weave-Event-Id")).toBe(envelope.eventId);
      expect(header("X-Weave-Signature")).toBe(
        createHmac("sha256", "s3cret-value")
          .update(`${stamp}.${body}`)
          .digest("hex"),
      );
    },
  );

  it.runIf(canSign)("sends an empty object without a sample", () => {
    const values = run(signedCurl({ url: URL }), "k");
    expect(
      JSON.parse(values[values.indexOf("--data-binary") + 1]).payload,
    ).toEqual({});
  });
});

describe("the secret command", () => {
  it("is the platform command that stores the handle's value", () => {
    expect(secretCommand("orders-signing")).toBe(
      "weave platform secret set --handle orders-signing",
    );
  });

  it("offers nothing for a handle the platform would refuse", () => {
    expect(secretCommand("bad name; rm -rf /")).toBeNull();
    expect(secretCommand("")).toBeNull();
  });

  it("gives the production and the test secret their own command", () => {
    expect(
      secretCommands({
        id: "t",
        kind: "webhook",
        secretRef: "orders-signing",
        testSecretRef: "orders-test",
      }),
    ).toEqual([
      {
        label: "Production secret",
        command: "weave platform secret set --handle orders-signing",
      },
      {
        label: "Test secret",
        command: "weave platform secret set --handle orders-test",
      },
    ]);
    expect(
      secretCommands({ id: "t", kind: "webhook", secretRef: "orders-signing" }),
    ).toEqual([
      {
        label: "Production secret",
        command: "weave platform secret set --handle orders-signing",
      },
    ]);
    expect(
      secretCommands({
        id: "t",
        kind: "webhook",
        secretRef: "same",
        testSecretRef: "same",
      }).map((item) => item.label),
    ).toEqual(["Production secret"]);
  });
});

describe("the webhook's addresses", () => {
  it("labels the test URL and the production URL", () => {
    expect(webhookUrls({ production: URL, test: `${URL}-test` })).toEqual([
      {
        label: "Production URL",
        url: URL,
        hint: "Starts runs of the active version.",
      },
      {
        label: "Test URL",
        url: `${URL}-test`,
        hint: "Works while Studio listens for a test event; its runs are marked Test.",
      },
    ]);
    expect(webhookUrls({}).length).toBe(0);
  });
});
