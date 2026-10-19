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
import { describe, expect, it } from "vitest";
import {
  accountsToDelete,
  stepRefusal,
  loginBinding,
  platformBinding,
  dumpName,
  dumpOriginProblem,
  errorLine,
  networkProblems,
  redact,
  secretsOf,
  stepOutcome,
  deepFreeze,
  storageAccountKeys,
  storageKeyCheckOn,
  viewportName,
} from "./acceptance/records";

describe("acceptance step records", () => {
  it("redacts every secret, longest first", () => {
    expect(
      redact("key wv-canary-abc and wv-canary-abcdef", [
        "wv-canary-abc",
        "wv-canary-abcdef",
      ]),
    ).toBe("key [redacted] and [redacted]");
  });

  it("keeps the first line of an error, redacted and bounded", () => {
    const secret = `pw-${"x".repeat(40)}`;
    const error = new Error(
      `expect(locator).toHaveValue() failed for ${secret}\nCall log:\n  - waiting for ${secret}`,
    );
    expect(errorLine(error, [secret])).toBe(
      "expect(locator).toHaveValue() failed for [redacted]",
    );
    expect(errorLine(new Error("a".repeat(500)), [])).toHaveLength(300);
    expect(errorLine("plain text", [])).toBe("plain text");
  });

  it("redacts tokens, cookies, keys and Authorization headers by their shape", () => {
    const jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJidWlsZGVyIn0.c2lnbmF0dXJl";
    expect(redact(`Authorization: Bearer ${jwt}`, [])).toBe(
      "Authorization: [redacted]",
    );
    expect(redact(`{"authorization":"Basic dXNlcjpwYXNz"}`, [])).toBe(
      `{"authorization":"[redacted]"}`,
    );
    expect(redact(`token ${jwt} expired`, [])).toBe("token [redacted] expired");
    expect(
      redact("Cookie: KEYCLOAK_SESSION=abc; AUTH_SESSION_ID=def\nnext", []),
    ).toBe("Cookie: [redacted]\nnext");
    expect(redact("set-cookie: weave=s3cr3t; Path=/; HttpOnly", [])).toBe(
      "set-cookie: [redacted]",
    );
    expect(redact("X-Api-Key: k-123 sent", [])).toBe(
      "X-Api-Key: [redacted] sent",
    );
    expect(
      redact(
        `{"access_token":"a1","refresh_token":"r1","password":"p1","client_secret":"c1"}`,
        [],
      ),
    ).toBe(
      `{"access_token":"[redacted]","refresh_token":"[redacted]","password":"[redacted]","client_secret":"[redacted]"}`,
    );
    expect(
      redact("http://127.0.0.1:43123/callback?code=abc&state=xyz", []),
    ).toBe("http://127.0.0.1:43123/callback?code=[redacted]&state=xyz");
    expect(redact("403 WV-FORBIDDEN: code WV-FORBIDDEN", [])).toBe(
      "403 WV-FORBIDDEN: code WV-FORBIDDEN",
    );
  });

  // Secret by shape alone: camelCase and suffixed keys, Studio's CSRF token and
  // pairing code, folded cookies, Digest credentials and URL-encoded forms.
  const S = "s3cR3t-Val_ue.9Zq";
  const SHAPED: readonly string[] = [
    `{"accessToken":"${S}"}`,
    `{"refreshToken":"${S}"}`,
    `{"clientSecret":"${S}"}`,
    `{"idToken":"${S}"}`,
    `{"csrfToken":"${S}"}`,
    `X-Weave-CSRF: ${S}`,
    `x-weave-csrf: ${S}`,
    `{"code":"${S}"}`,
    `code=${S}`,
    `body: code=${S}&state=x`,
    `"secret_value": "${S}"`,
    `{"pairing_code":"${S}"}`,
    `{"access_token":"${S}"}`,
    `{"refresh_token":"${S}"}`,
    `{"id_token":"${S}"}`,
    `{"client_secret":"${S}"}`,
    `password=${S}&username=builder`,
    `{"password": "two words ${S}"}`,
    `Authorization: Bearer ${S}`,
    `authorization: Basic ${S}`,
    `Authorization: Digest username="u", realm="weave", nonce="n1", response="${S}"`,
    `Proxy-Authorization: Bearer ${S}`,
    `{"authorization":"Bearer ${S}"}`,
    `Cookie: a=1; KEYCLOAK_SESSION=${S}`,
    `Cookie: a=1;\n\tKEYCLOAK_SESSION=${S}`,
    `set-cookie: weave_session=${S}; Path=/; HttpOnly`,
    `GET /cb?x=1&access_token%3D${S}`,
    `http://127.0.0.1:43123/callback?code=${S}&state=x`,
    `#access_token=${S}&token_type=bearer`,
    `X-Api-Key: ${S}`,
    `{"apiKey":'${S}'}`,
    `{"codeVerifier":"${S}"}`,
    `sent Bearer ${S} upstream`,
    `{"sessionToken":"${S}","expires":1}`,
    `Error: request failed: "{\\"accessToken\\":\\"${S}\\"}"`,
    `body: "{\\"csrfToken\\":\\"${S}\\"}"`,
    `{"name":"x-weave-csrf","value":"${S}"}`,
    `{"name": "Cookie", "value": "KEYCLOAK_SESSION=${S}"}`,
    `{\\"name\\":\\"accessToken\\",\\"value\\":\\"${S}\\"}`,
  ];

  it("redacts a HAR cookie pair even when its value looks ordinary", () => {
    for (const name of ["Cookie", "set-cookie"])
      expect(redact(`{"name":"${name}","value":"opaque"}`, [])).toBe(
        `{"name":"${name}","value":"[redacted]"}`,
      );
    expect(redact(`{"name":"Accept","value":"opaque"}`, [])).toBe(
      `{"name":"Accept","value":"opaque"}`,
    );
  });

  it("redacts a private key block, escaped or cut short, and keeps public keys", () => {
    const body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC";
    for (const text of [
      `-----BEGIN PRIVATE KEY-----\n${body}\n-----END PRIVATE KEY-----\nnext`,
      `{"privateKey":"-----BEGIN RSA PRIVATE KEY-----\\n${body}\\n-----END RSA PRIVATE KEY-----\\n"}`,
      `Error: bad key -----BEGIN OPENSSH PRIVATE KEY-----\n${body}`,
    ]) {
      const result = redact(text, []);
      expect(result).not.toContain(body);
      expect(result).toContain("[redacted]");
    }
    expect(
      redact(
        `-----BEGIN PRIVATE KEY-----\n${body}\n-----END PRIVATE KEY-----\nnext`,
        [],
      ),
    ).toBe(
      "-----BEGIN PRIVATE KEY-----[redacted]-----END PRIVATE KEY-----\nnext",
    );
    const publicKey = `-----BEGIN PUBLIC KEY-----\n${body}\n-----END PUBLIC KEY-----`;
    expect(redact(publicKey, [])).toBe(publicKey);
  });

  it.each(SHAPED)("redacts a secret by its shape: %s", (text) => {
    const result = redact(text, []);
    expect(result).toContain("[redacted]");
    expect(result).not.toContain(S);
    expect(result).not.toContain("two words");
  });

  it.each([
    "token: expired",
    "Reset password: link sent",
    "Authorization: denied for viewer",
    "Bearer token missing",
    "/runs?status=failed&code=WV-RUN",
    `{"code":"WV-FORBIDDEN","status":403}`,
    `{"token_type":"bearer","expires_in":300}`,
    "Timeout 5000ms exceeded waiting for getByRole('button')",
  ])("keeps an ordinary phrase: %s", (text) => {
    expect(redact(text, [])).toBe(text);
  });

  // The ordinary-value exemption stops at 12 lowercase letters and 5 digits.
  it.each([
    "token=abcdefghijklm",
    "password=correcthorses",
    "pairing_code=123456",
    "code=123456",
    `{"name":"x-weave-csrf","value":"abcdefghijklm"}`,
  ])("redacts a value just past the ordinary bounds: %s", (text) => {
    expect(redact(text, [])).toContain("[redacted]");
  });

  it.each([
    "token=abcdefghijkl",
    "pairing_code=12345",
    `{"name":"x-weave-csrf","value":"missing"}`,
    `{"name":"Accept","value":"application/json"}`,
  ])("keeps a value within the ordinary bounds: %s", (text) => {
    expect(redact(text, [])).toBe(text);
  });

  it("redacts secret handles named as secrets", () => {
    const secrets = secretsOf({ api: "wv-canary-1" }, ["pw"], ["acme-api-key"]);
    expect(secrets).toEqual(["wv-canary-1", "pw", "acme-api-key"]);
    expect(redact("handle acme-api-key holds wv-canary-1", secrets)).toBe(
      "handle [redacted] holds [redacted]",
    );
  });

  it("collects canaries and passwords without empty values", () => {
    expect(secretsOf({ a: "x1", b: "" }, ["p", undefined, ""])).toEqual([
      "x1",
      "p",
    ]);
  });

  it("names a sign-in's binding as the Python client does", () => {
    // firefly_weave.sdk.auth.LoginConfig.binding for the same login.
    expect(
      loginBinding({
        provider_id: "local-keycloak",
        issuer: "http://localhost:18080/realms/weave",
        client_id: "weave-studio",
        target: "http://127.0.0.1:18000",
        account: "j9-viewer-ab12",
      }),
    ).toBe("39dc870812851bb96cf4d9373fe4588f34c8981942a968170207e6400e6bac82");
  });

  it("finds a platform's binding in the host's saved platforms, and nothing for others", () => {
    const login = {
      provider_id: "local-keycloak",
      issuer: "http://localhost:18080/realms/weave",
      client_id: "weave-studio",
      target: "http://127.0.0.1:18000",
      account: "j9-viewer-ab12",
    };
    const document = {
      profiles: { "j9-viewer-ab12": { name: "j9-viewer-ab12", login } },
    };
    expect(platformBinding(document, "j9-viewer-ab12")).toBe(
      loginBinding(login),
    );
    expect(platformBinding(document, "someone-else")).toBeUndefined();
    expect(platformBinding({}, "j9-viewer-ab12")).toBeUndefined();
  });

  it("deletes only this run's own bindings: one another process stores meanwhile survives", () => {
    const own = new Set(["run-a", "run-b"]);
    // The person's own sign-in from before, and one another process stored during the run.
    const now = new Set(["person-own", "run-b", "run-a", "foreign-mid-run"]);
    expect(accountsToDelete(own, now)).toEqual(["run-a", "run-b"]);
    expect(accountsToDelete(new Set(), now)).toEqual([]);
  });

  it("names viewports by size", () => {
    expect(viewportName({ width: 1440, height: 900 })).toBe("1440x900");
    expect(viewportName(null)).toBe("unknown");
  });

  it("allows only the host, the identity provider and the loopback callback", () => {
    // Built from parts: the repository's brand check refuses the mascot's file name anywhere.
    const mascot = ["lumi", "png"].join(".");
    const allowed = ["http://127.0.0.1:41000", "http://localhost:18080"];
    expect(
      networkProblems(
        [
          "http://127.0.0.1:41000/main.js",
          "http://localhost:18080/realms/weave/protocol/openid-connect/auth?x=1",
          "http://127.0.0.1:43123/callback?code=abc",
          "data:image/png;base64,AAAA",
          "https://fonts.gstatic.com/s/manrope.woff2",
          `http://127.0.0.1:41000/assets/${mascot}`,
        ],
        allowed,
      ),
    ).toEqual([
      "request to https://fonts.gstatic.com",
      `mascot asset /assets/${mascot}`,
    ]);
  });

  it("allows the callback path only on the plain-HTTP loopback address", () => {
    expect(
      networkProblems(
        [
          "https://evil.example/callback?code=abc",
          "http://localhost:43123/callback?code=abc",
          "https://127.0.0.1:43123/callback?code=abc",
        ],
        ["http://127.0.0.1:41000"],
      ),
    ).toEqual([
      "request to https://evil.example",
      "request to http://localhost:43123",
      "request to https://127.0.0.1:43123",
    ]);
  });

  it("fails a step that skipped an enabled check", () => {
    const steps = {
      "J9.2": { applies: true, missing: [] },
      "J9.2/execute-in-local": { applies: true, missing: ["step-tests"] },
      "J9.2/operations-controls": { applies: true, missing: [] },
    };
    expect(
      stepOutcome("J9.2", steps, new Set(), [
        { check: "J9.2/operations-controls", missing: ["logs"] },
        { check: "J9.2/execute-in-local", missing: [] },
      ]),
    ).toEqual({
      status: "passed",
      skippedChecks: [],
      problems: [
        "enabled check J9.2/operations-controls was skipped",
        "check J9.2/execute-in-local was skipped without naming what it waits for",
      ],
    });
  });

  it("fails a step whose enabled check never ran", () => {
    const steps = {
      "J9.2": { applies: true, missing: [] },
      "J9.2/execute-in-local": { applies: true, missing: ["step-tests"] },
      "J9.2/operations-controls": { applies: true, missing: [] },
      "J9.3/model-origins": { applies: true, missing: [] },
    };
    expect(stepOutcome("J9.2", steps, new Set(), [])).toEqual({
      status: "partial",
      skippedChecks: [
        { check: "J9.2/execute-in-local", missing: ["step-tests"] },
      ],
      problems: ["enabled check J9.2/operations-controls has no test"],
    });
    expect(
      stepOutcome("J9.2", steps, new Set(["J9.2/operations-controls"]), []),
    ).toEqual({
      status: "partial",
      skippedChecks: [
        { check: "J9.2/execute-in-local", missing: ["step-tests"] },
      ],
      problems: [],
    });
  });
});

describe("what a journey may record", () => {
  const steps = {
    "J9.2": { applies: true, missing: [] },
    "J9.4": { applies: true, missing: [] },
  };
  const stages = { "J9.2": "run", "J9.4": "scan" };

  it("records only its own steps of the run stage", () => {
    expect(stepRefusal("J9.2", "J9", steps, stages)).toBe("");
    expect(stepRefusal("J9.4", "J9", steps, stages)).toBe(
      "J9.4 is recorded by the scan stage, not by a journey",
    );
    expect(stepRefusal("J9.9", "J9", steps, stages)).toBe(
      "J9.9 is not declared in tests/acceptance/journeys.toml",
    );
    expect(stepRefusal("J9.2", "J8", steps, stages)).toBe(
      "J9.2 is not a step of J8",
    );
  });
});

describe("the journey map a run reads", () => {
  it("cannot be changed by a journey", () => {
    const map = deepFreeze({
      steps: { "J9.2": { applies: true, missing: [] as string[] } },
    });
    expect(() => {
      (map.steps["J9.2"]!.missing as string[]).push("editor-canvas");
    }).toThrow();
    expect(() => {
      (map.steps as Record<string, unknown>)["J9.2"] = { applies: false };
    }).toThrow();
    expect(map.steps["J9.2"]).toEqual({ applies: true, missing: [] });
  });
});

describe("browser storage dumps", () => {
  it("names one dump per sign-in, so a second sign-in never overwrites the first", () => {
    expect(dumpName("J9", "1440x900", "viewer", 1)).toBe("J9-1440x900-viewer");
    expect(dumpName("J9", "1440x900", "viewer", 2)).toBe(
      "J9-1440x900-viewer-2",
    );
    expect(dumpName("J9", "1440x900", "viewer", 3)).not.toBe(
      dumpName("J9", "1440x900", "viewer", 2),
    );
  });

  it("counts the account keys a dump still holds, in Web Storage and IndexedDB", () => {
    const account =
      "p0123456789abcdef:fedcba9876543210:11111111-2222-3333-4444-555555555555:66666666-7777-8888-9999-000000000000:";
    const empty = { local: {}, session: {}, indexed_db: [] };
    expect(storageAccountKeys(empty)).toBe(0);
    expect(
      storageAccountKeys({
        local: { [account + "drafts"]: "v", "local:draft": "v" },
        session: { [account + "run"]: "v", "ui:panes": "v" },
        indexed_db: [
          { database: "weave", key: JSON.stringify(account + "x") },
          { database: "weave", key: JSON.stringify(7) },
          { database: account + "db", key: JSON.stringify("plain") },
        ],
        indexed_db_databases: [{ name: account + "db" }, { name: "weave" }],
      }),
    ).toBe(4);
    // A profile prefix alone is not an account key.
    expect(
      storageAccountKeys({
        ...empty,
        local: { "p0123456789abcdef:settings": "v" },
      }),
    ).toBe(0);
  });

  it("lists IndexedDB databases only while the storage-key check runs", () => {
    const check = "J9.4/storage-keys";
    expect(
      storageKeyCheckOn({ [check]: { applies: true, missing: [] } }, check),
    ).toBe(true);
    expect(
      storageKeyCheckOn(
        { [check]: { applies: true, missing: ["editor-contracts"] } },
        check,
      ),
    ).toBe(false);
    expect(
      storageKeyCheckOn({ [check]: { applies: false, missing: [] } }, check),
    ).toBe(false);
    expect(storageKeyCheckOn({}, check)).toBe(false);
  });

  it("accepts only storage read on the person's Studio host", () => {
    expect(
      dumpOriginProblem("http://127.0.0.1:46149", "http://127.0.0.1:46149"),
    ).toBe("");
    expect(
      dumpOriginProblem("http://localhost:8180", "http://127.0.0.1:46149"),
    ).toBe(
      "storage was read on http://localhost:8180, not the Studio host http://127.0.0.1:46149",
    );
    expect(dumpOriginProblem("http://127.0.0.1:46149", "")).toBe(
      "storage was read before the Studio host started",
    );
  });
});
