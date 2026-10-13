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
  errorLine,
  networkProblems,
  redact,
  secretsOf,
  stepOutcome,
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
  ];

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

  it("deletes nothing when the baseline is unknown", () => {
    expect(accountsToDelete(undefined, new Set(["mine", "theirs"]))).toEqual(
      [],
    );
  });

  it("deletes only the sign-ins created after the baseline", () => {
    expect(
      accountsToDelete(
        new Set(["person-own"]),
        new Set(["person-own", "run-b", "run-a"]),
      ),
    ).toEqual(["run-a", "run-b"]);
  });

  it("names viewports by size", () => {
    expect(viewportName({ width: 1440, height: 900 })).toBe("1440x900");
    expect(viewportName(null)).toBe("unknown");
  });

  it("allows only the host, the identity provider and the loopback callback", () => {
    const allowed = ["http://127.0.0.1:41000", "http://localhost:18080"];
    expect(
      networkProblems(
        [
          "http://127.0.0.1:41000/main.js",
          "http://localhost:18080/realms/weave/protocol/openid-connect/auth?x=1",
          "http://127.0.0.1:43123/callback?code=abc",
          "data:image/png;base64,AAAA",
          "https://fonts.gstatic.com/s/manrope.woff2",
          "http://127.0.0.1:41000/assets/lumi.png",
        ],
        allowed,
      ),
    ).toEqual([
      "request to https://fonts.gstatic.com",
      "mascot asset /assets/lumi.png",
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
