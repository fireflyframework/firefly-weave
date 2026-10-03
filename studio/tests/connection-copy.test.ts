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
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  checkDraft,
  connectionFailureCopy,
  connectionProblems,
  connectionRequest,
  destinationEntries,
  destinationsOf,
  emptyDraft,
  fieldForPointer,
  handoffCommand,
  handoffRequest,
  looksLikeSecretValue,
  originOf,
  prefillFromBuilder,
  sameRequest,
  suggestedName,
  type ConnectionDraft,
} from "../src/app/integrations/connection-copy";

const versionId = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
const oauth: ConnectionDraft = emptyDraft({
  name: "pets",
  origin: "https://api.pets.example",
  auth: "machine-token",
  clientId: "studio-client",
  endpoint: "https://login.pets.example/oauth2/token",
  scopes: "pets.read pets.write",
  secrets: { client_secret: "pets-client-secret" },
});
const fields = (draft: ConnectionDraft, local = false) =>
  checkDraft(draft, { local }).map((p) => p.field);

describe("connection request", () => {
  it("uses AuthProfile field names and handle names only", () => {
    expect(connectionRequest(oauth, versionId)).toEqual({
      name: "pets",
      connector_version_id: versionId,
      config: {
        baseUrl: "https://api.pets.example",
        auth: {
          kind: "machine-token",
          client_id: "studio-client",
          endpoint: "https://login.pets.example/oauth2/token",
          scopes: ["pets.read", "pets.write"],
        },
      },
      secretRef: { client_secret: "pets-client-secret" },
      allowed_destinations: [
        "https://api.pets.example",
        "https://login.pets.example",
      ],
    });
    expect(checkDraft(oauth)).toEqual([]);
  });
  it("prefills the token endpoint origin and keeps extra origins once", () => {
    const draft = {
      ...oauth,
      extraDestinations: [
        "https://files.pets.example/",
        "https://api.pets.example",
      ],
    };
    expect(destinationsOf(draft)).toEqual([
      "https://api.pets.example",
      "https://login.pets.example",
      "https://files.pets.example",
    ]);
    expect(destinationsOf({ ...draft, auth: "bearer" })).toEqual([
      "https://api.pets.example",
      "https://files.pets.example",
    ]);
  });
  it("lists locked origins first and marks repeated additions", () => {
    const entries = destinationEntries({
      ...oauth,
      extraDestinations: [
        "https://api.pets.example",
        "",
        "https://files.pets.example",
      ],
    });
    expect(entries.map((e) => [e.source, e.index, e.extra])).toEqual([
      ["api", 0, -1],
      ["token", 1, -1],
      ["extra", -1, 0],
      ["extra", -1, 1],
      ["extra", 2, 2],
    ]);
  });
  it("sends only the slots of the chosen authentication", () => {
    const draft = emptyDraft({
      name: "pets",
      origin: "https://api.pets.example",
      auth: "api-key",
      header: "X-API-Key",
      secrets: { api_key: "pets-api-key", token: "left-over" },
    });
    expect(connectionRequest(draft, versionId).secretRef).toEqual({
      api_key: "pets-api-key",
    });
    expect(connectionRequest(draft, versionId).config.auth).toEqual({
      kind: "api-key",
      header: "X-API-Key",
    });
    expect(
      connectionRequest(
        { ...oauth, authentication: "client_secret_basic" },
        versionId,
      ).config.auth["authentication"],
    ).toBe("client_secret_basic");
  });
});

describe("client checks mirror the platform", () => {
  it("requires an HTTPS origin without a path", () => {
    expect(fields({ ...oauth, origin: "http://api.pets.example" })).toContain(
      "origin",
    );
    expect(
      fields({ ...oauth, origin: "https://user:pw@api.pets.example" }),
    ).toContain("origin");
    const withPath = checkDraft({
      ...oauth,
      origin: "https://api.pets.example/v1",
    });
    expect(withPath[0]).toMatchObject({ field: "origin" });
    expect(withPath[0].message).toContain("base path (/v1)");
    expect(originOf("https://API.Pets.Example:443/")).toBe(
      "https://api.pets.example",
    );
    expect(originOf("https://api.pets.example/./x")).toBeNull();
  });
  it("rejects reserved headers for API keys", () => {
    const draft = emptyDraft({
      name: "pets",
      origin: "https://api.pets.example",
      auth: "api-key",
      header: "Authorization",
      secrets: { api_key: "pets-api-key" },
    });
    expect(checkDraft(draft)).toEqual([
      expect.objectContaining({
        field: "header",
        message: expect.stringContaining("reserved"),
      }),
    ]);
    expect(fields({ ...draft, header: "X-Forwarded-For" })).toEqual(["header"]);
    expect(fields({ ...draft, header: "X API" })).toEqual(["header"]);
  });
  it("checks OAuth client credentials", () => {
    expect(
      fields({ ...oauth, clientId: "", endpoint: "http://login" }),
    ).toEqual(["clientId", "endpoint"]);
    expect(fields({ ...oauth, scopes: 'a "b"' })).toEqual(["scopes"]);
    expect(fields({ ...oauth, scopes: "a a" })).toEqual(["scopes"]);
  });
  it("refuses secret values where a handle name belongs", () => {
    for (const value of [
      "sk-live-1234567890abcdef",
      "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
      "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
      "Qm9vNz1aXyTrB7c2kLp9x",
      // base64url keys may contain '-' or '_'; hex keys are one long piece.
      "q3J_Xx9vLm2-PbR7tYw0aZ4kC8nH1sD5fG6jK2lM9oQ",
      "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "Bearer abc.def",
      "a".repeat(64),
    ]) {
      expect(looksLikeSecretValue(value)).toBe(true);
      const problems = checkDraft({
        ...oauth,
        secrets: { client_secret: value },
      });
      expect(problems[0]).toMatchObject({ field: "secret.client_secret" });
      expect(problems[0].message).toContain("looks like a secret value");
    }
    for (const handle of [
      "pets-api-key",
      "crm.client_secret",
      "0f8fad5b-d9cb-469f-a165-70867728950e",
      "stripe-key-2024",
      // Long handles made of words are names, not values (the server allows 128).
      "production-payments-gateway-api-key-for-eu-region",
    ])
      expect(looksLikeSecretValue(handle)).toBe(false);
  });
  it("requires every slot and the local handle format on this computer", () => {
    const basic = emptyDraft({
      name: "pets",
      origin: "https://api.pets.example",
      auth: "basic",
      secrets: { username: "pets-user" },
    });
    expect(fields(basic)).toEqual(["secret.password"]);
    expect(
      fields(
        { ...basic, secrets: { username: "Pets-User", password: "pets-pass" } },
        true,
      ),
    ).toEqual(["secret.username"]);
  });
  it("flags an extra destination that is not an HTTPS origin", () => {
    expect(
      fields({
        ...oauth,
        extraDestinations: ["https://files.pets.example/upload"],
      }),
    ).toEqual(["destination.2"]);
    expect(fields({ ...oauth, extraDestinations: ["*.pets.example"] })).toEqual(
      ["destination.2"],
    );
  });
  it("names the connection", () => {
    expect(fields({ ...oauth, name: "" })).toEqual(["name"]);
    expect(fields({ ...oauth, name: "-pets" })).toEqual(["name"]);
    expect(suggestedName("https://api.pets.example")).toBe("pets");
    expect(suggestedName("not a url")).toBe("");
  });
});

describe("server diagnostics", () => {
  it("maps every documented pointer to its field", () => {
    expect(fieldForPointer("/config/baseUrl")).toBe("origin");
    expect(fieldForPointer("/config/auth")).toBe("auth");
    expect(fieldForPointer("/config/auth/header")).toBe("header");
    expect(fieldForPointer("/config/auth/client_id")).toBe("clientId");
    expect(fieldForPointer("/config/auth/endpoint")).toBe("endpoint");
    expect(fieldForPointer("/config/auth/scopes/1")).toBe("scopes");
    expect(fieldForPointer("/secretRef/api_key")).toBe("secret.api_key");
    expect(fieldForPointer("/secretRef")).toBe("secrets");
    expect(fieldForPointer("/allowed_destinations/1")).toBe("destination.1");
    expect(fieldForPointer("/allowed_destinations")).toBe("destinations");
    expect(fieldForPointer("/connector_version_id")).toBe("connector");
    expect(fieldForPointer("/config/extra")).toBe("general");
    expect(fieldForPointer("not a pointer")).toBe("general");
  });
  it("reads diagnostics from the problem body or its result", () => {
    const body = {
      code: "WV-CONNECTION",
      message: "Connection requirements are unavailable or incompatible",
      result: {
        diagnostics: [
          {
            code: "WV-CONNECTION-DESTINATION",
            severity: "error",
            stage: "semantic",
            path: "/allowed_destinations",
            message:
              "Add https://api.pets.example to allowed_destinations so requests can reach the API.",
          },
          {
            code: "WV-CONNECTION-SECRET",
            path: "/secretRef/api_key",
            message: "This secret handle is not available in this environment.",
          },
          { code: "WV-CONNECTION-CONFIG", path: "/config", message: "{raw}" },
        ],
      },
    };
    expect(connectionProblems(body)).toEqual([
      {
        field: "destinations",
        message:
          "Add https://api.pets.example to the allowed destinations so requests can reach the API.",
        code: "WV-CONNECTION-DESTINATION",
      },
      {
        field: "secret.api_key",
        message:
          "This secret handle is not available in this environment. Check the handle name with your platform operator.",
        code: "WV-CONNECTION-SECRET",
      },
      {
        field: "general",
        message: "The platform doesn't accept this setting.",
        code: "WV-CONNECTION-CONFIG",
      },
    ]);
    expect(
      connectionProblems({ diagnostics: body.result.diagnostics }),
    ).toHaveLength(3);
    expect(connectionProblems({ code: "WV-CONNECTION" })).toEqual([]);
  });
  it("explains failures in this form's context", () => {
    expect(connectionFailureCopy(403, "WV-DENIED", "create")).toContain(
      "Hand the request below to an administrator",
    );
    expect(connectionFailureCopy(422, "WV-CONNECTION", "create")).toContain(
      "highlighted settings",
    );
    expect(connectionFailureCopy(500, "WV-INTERNAL", "create")).toBe("");
  });
});

describe("prefill from the API action builder", () => {
  it("takes the name, origin and authentication kind, never secrets", () => {
    expect(
      prefillFromBuilder({
        name: "pets",
        baseUrl: "https://api.pets.example",
        auth: { kind: "api-key", header: "X-API-Key" },
      }),
    ).toEqual({
      name: "pets",
      origin: "https://api.pets.example",
      auth: "api-key",
      header: "X-API-Key",
    });
    expect(
      prefillFromBuilder({ name: "", baseUrl: null, auth: { kind: "bearer" } }),
    ).toEqual({ auth: "bearer" });
    expect(prefillFromBuilder(null)).toEqual({});
  });
});

describe("re-listing after a lost answer", () => {
  it("recognizes the revision that carries this exact request", () => {
    const request = connectionRequest(oauth, versionId);
    const listed = {
      ...JSON.parse(JSON.stringify(request)),
      id: "x",
      revision: 2,
      connector: "weave-http@2.0.0",
    };
    expect(sameRequest(listed, request)).toBe(true);
    expect(
      sameRequest(
        { ...listed, secretRef: { client_secret: "other" } },
        request,
      ),
    ).toBe(false);
  });
});

describe("administrator hand-off", () => {
  const profile = {
    baseUrl: "https://weave.example.com",
    tenantId: "tenant",
    projectId: "project",
    environmentId: "development",
  };
  it("uses the guided flags when the connector version is unknown", () => {
    expect(handoffCommand(oauth, "", profile)).toBe(
      "weave connections create --tenant tenant --project project --environment development --name pets --api-url https://api.pets.example --auth machine-token --client-id studio-client --token-endpoint https://login.pets.example/oauth2/token --scope pets.read --scope pets.write --secret client_secret=pets-client-secret",
    );
    expect(handoffRequest(oauth, "").connector_version_id).toBe(
      "CONNECTOR_VERSION_ID",
    );
  });
  it("submits the request file when the version is known", () => {
    expect(handoffCommand(oauth, versionId, profile)).toBe(
      "weave connections create --tenant tenant --project project --environment development --request connection.json",
    );
  });
  it("quotes values the shell would interpret", () => {
    expect(handoffCommand({ ...oauth, scopes: "a;b" }, "", null)).toContain(
      "--scope 'a;b'",
    );
  });
  it("never carries anything but handle names", () => {
    const text = JSON.stringify(handoffRequest(oauth, versionId));
    expect(text).toContain("pets-client-secret");
    expect(Object.keys(handoffRequest(oauth, versionId))).toEqual([
      "name",
      "connector_version_id",
      "config",
      "secretRef",
      "allowed_destinations",
    ]);
  });
});

// The platform's own connection check is the authority: the request this
// module builds must pass connection_issues and the profile check, and a
// request missing the token endpoint origin must fail at the same pointer.
const root = resolve(import.meta.dirname, "../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
describe.skipIf(!existsSync(python))(
  "parity with the platform's connection check",
  () => {
    it("accepts what the form builds and rejects at the same pointers", () => {
      const requests = [
        connectionRequest(oauth, versionId),
        connectionRequest(
          emptyDraft({
            name: "pets",
            origin: "https://api.pets.example",
            auth: "api-key",
            header: "X-API-Key",
            secrets: { api_key: "pets-api-key" },
          }),
          versionId,
        ),
        connectionRequest(
          emptyDraft({
            name: "pets",
            origin: "https://api.pets.example",
            auth: "basic",
            secrets: { username: "pets-user", password: "pets-pass" },
          }),
          versionId,
        ),
        connectionRequest(
          emptyDraft({ name: "pets", origin: "https://api.pets.example" }),
          versionId,
        ),
        {
          ...connectionRequest(oauth, versionId),
          allowed_destinations: ["https://api.pets.example"],
        },
      ];
      const output = execFileSync(
        python,
        [
          "-c",
          `
import json, sys
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.http_profile_checks import connection_issues
result = []
for body in json.loads(sys.stdin.read()):
    issues = connection_issues(ConnectionRequest.model_validate_json(json.dumps(body)))
    result.append([[i.code, i.path] for i in issues])
print(json.dumps(result))
`,
        ],
        {
          cwd: root,
          encoding: "utf8",
          timeout: 180_000,
          input: JSON.stringify(requests),
          env: { ...process.env, PYTHONPATH: resolve(root, "src") },
        },
      );
      const issues = JSON.parse(output) as [string, string][][];
      expect(issues.slice(0, 4)).toEqual([[], [], [], []]);
      expect(issues[4]).toEqual([["DESTINATION", "/allowed_destinations"]]);
      expect(fieldForPointer(issues[4][0][1])).toBe("destinations");
    });
  },
);
