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
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { python, pythonAvailable } from "./python-path";
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  HOST_BODY_LIMIT,
  HTTP_CONNECTOR,
  PARAMETER_LIMIT,
  RELAXATIONS,
  blankDraft,
  buildRequest,
  classifyPublishError,
  detectFormat,
  emptyStatusFor,
  explainDiagnostic,
  filterOperations,
  fitsHostRequest,
  isProtectedHeader,
  locateDiagnostic,
  methodEffect,
  needsConnector,
  nextPatchVersion,
  operationForPointer,
  parseApiAddress,
  parseSample,
  pathPlaceholders,
  plainRelaxationText,
  relaxationPayload,
  serviceSlug,
  suggestVersion,
  suggestedRelaxations,
  syncPathRows,
  validName,
  validVersion,
  versionTaken,
  type HttpActionDraft,
  type ParamRow,
} from "../src/app/integrations/http-action-model";

const root = resolve(import.meta.dirname, "../..");
const available = pythonAvailable();

/** Runs a Python snippet that prints JSON, with the repository on the path. */
function py<T>(source: string, input: unknown): T {
  const out = execFileSync(python, ["-c", source], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: resolve(root, "src") },
    input: JSON.stringify(input),
    encoding: "utf8",
  });
  return JSON.parse(out) as T;
}

let next = 0;
const id = () => `row-${++next}`;

function draft(patch: Partial<HttpActionDraft> = {}): HttpActionDraft {
  return { ...blankDraft(id), name: "pets.get-pet", ...patch };
}

describe("method effect", () => {
  it("locks reads to read_only and every other method to non_idempotent", () => {
    for (const method of ["GET", "HEAD"] as const) {
      const effect = methodEffect(method);
      expect(effect.action).toBe("read");
      expect(effect.sideEffect).toBe("read_only");
      expect(effect.explanation).toMatch(/Reads data/);
    }
    for (const method of ["POST", "PUT", "PATCH", "DELETE"] as const) {
      const effect = methodEffect(method);
      expect(effect.action).toBe("write");
      expect(effect.sideEffect).toBe("non_idempotent");
      expect(effect.explanation).toMatch(/Changes data/);
      expect(effect.explanation).toMatch(/twice/);
      expect(effect.explanation).not.toMatch(/WV-|non_idempotent/);
    }
  });
});

describe("path placeholders", () => {
  it("returns names in order and rejects what the profile rejects", () => {
    expect(pathPlaceholders("/v1/pets/{petId}/toys/{toyId}")).toEqual({
      names: ["petId", "toyId"],
    });
    expect(pathPlaceholders("/v1/pets")).toEqual({ names: [] });
    for (const bad of [
      "v1/pets",
      "//pets",
      "/pets?limit=1",
      "/pets/{id}/{id}",
      "/pets/{pet-id}x",
      "/pets/../x",
      "/pets/%20",
      "/pets/{1id}",
    ])
      expect(pathPlaceholders(bad)).toHaveProperty("error");
  });
  it.skipIf(!available)(
    "agrees with the Python template_names for every case",
    () => {
      const cases = [
        "/v1/pets/{petId}",
        "/",
        "/a//b",
        "/a/{b}/{c_d-e}",
        "/a/{b}{c}",
        "/a/{}",
        "/a/{_x}",
        "/a/.",
        "/a/b c",
        "/a/é",
        "/a\\b",
        "/a#b",
        "//x",
        "x",
        "/a/{b}/{b}",
      ];
      const verdicts = py<boolean[]>(
        `import json,sys
from firefly_weave.contracts.http_profiles import template_names
out=[]
for c in json.load(sys.stdin):
    try:
        template_names(c); out.append(True)
    except ValueError:
        out.append(False)
print(json.dumps(out))`,
        cases,
      );
      expect(cases.map((c) => "names" in pathPlaceholders(c))).toEqual(
        verdicts,
      );
    },
  );
});

describe("API address", () => {
  it("keeps the HTTPS origin for the connection and splits off a base path", () => {
    expect(parseApiAddress("https://api.example.com")).toEqual({
      origin: "https://api.example.com",
      basePath: "",
    });
    expect(parseApiAddress(" https://api.example.com:8443/v1/ ")).toEqual({
      origin: "https://api.example.com:8443",
      basePath: "/v1",
    });
    // Plain HTTP is accepted like HTTPS; the platform's egress check decides
    // whether the address is reachable.
    expect(parseApiAddress("http://acme.acceptance.test:8080")).toEqual({
      origin: "http://acme.acceptance.test:8080",
      basePath: "",
    });
    const ftp = parseApiAddress("ftp://api.example.com");
    expect("error" in ftp && ftp.error).toMatch(/https:\/\//);
    for (const bad of [
      "https://user:pw@api.example.com",
      "https://api.example.com?x=1",
      "https://*.example.com",
      "https://api.example.com./",
      "https://api.example.com:0",
      "ftp://api.example.com",
      "",
    ])
      expect(parseApiAddress(bad)).toHaveProperty("error");
  });
  it.skipIf(!available)(
    "agrees with the Python origin check used for connections",
    () => {
      const cases = [
        "https://api.example.com",
        "https://API.Example.com:443",
        "https://api.example.com/v1",
        "https://api.example.com/v1/",
        "https://api.example.com//v1",
        "https://api.example.com/{x}",
        "https://user@api.example.com",
        "https://api.example.com?",
        "https://api.example.com?q=1",
        "https://api.example.com#f",
        "https://api.example.com:99999",
        "https://api.example.com:0",
        "https://[::1]:8443",
        "https://10.0.0.1",
        "https://ex_ample.com",
        "https://-bad.com",
        "https://a.b.",
        "http://a.com",
        "ftp://a.com",
        "https://a.com/%2e",
        "https://a.com/é",
      ];
      const verdicts = py<(string | null)[]>(
        `import json,sys
from firefly_weave.contracts.http_profiles import fixed_server
from firefly_weave.sdk.http_actions import _HOST
from urllib.parse import urlsplit
out=[]
for c in json.load(sys.stdin):
    try:
        origin, base = fixed_server(c, plain_http=True)
        if not _HOST.fullmatch(urlsplit(c).hostname or ""):
            raise ValueError
        out.append(origin + "|" + base)
    except ValueError:
        out.append(None)
print(json.dumps(out))`,
        cases,
      );
      expect(
        cases.map((c) => {
          const parsed = parseApiAddress(c);
          return "error" in parsed
            ? null
            : `${parsed.origin}|${parsed.basePath}`;
        }),
      ).toEqual(verdicts);
    },
  );
});

describe("names, versions and headers", () => {
  it.skipIf(!available)("mirror the Python patterns", () => {
    const names = ["pets.get-pet", "a", "_x", "-a", "a b", "a@1", "9lives"];
    const versions = ["1.0.0", "1.0", "01.0.0", "1.0.0-beta.1", "1.0.0+b.2"];
    const headers = [
      "X-Request-Id",
      "authorization",
      "Content-Type",
      "Proxy-Foo",
      "x-forwarded-for",
      "X Bad",
      "Accept",
      "Via",
      "te",
      "X-Api-Key",
    ];
    const verdict = py<{
      names: boolean[];
      versions: boolean[];
      headers: boolean[];
    }>(
      `import json,re,sys
from firefly_weave.contracts.definitions import NAME_PATTERN, SEMVER_PATTERN
from firefly_weave.contracts.http_profiles import HEADER_NAME, protected
c=json.load(sys.stdin)
print(json.dumps({
 "names":[bool(re.fullmatch(NAME_PATTERN,n)) for n in c["names"]],
 "versions":[bool(re.fullmatch(SEMVER_PATTERN,v)) for v in c["versions"]],
 "headers":[bool(HEADER_NAME.fullmatch(h)) and not protected(h) for h in c["headers"]],
}))`,
      { names, versions, headers },
    );
    expect(names.map(validName)).toEqual(verdict.names);
    expect(versions.map(validVersion)).toEqual(verdict.versions);
    expect(
      headers.map(
        (h) => /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(h) && !isProtectedHeader(h),
      ),
    ).toEqual(verdict.headers);
  });
});

describe("path rows follow the path", () => {
  it("adds required rows for new placeholders and drops stale ones", () => {
    const query: ParamRow = {
      id: "q",
      location: "query",
      name: "limit",
      type: "integer",
      list: false,
      required: false,
    };
    const first = syncPathRows([query], ["petId"], () => "p1");
    expect(first).toEqual([
      {
        id: "p1",
        location: "path",
        name: "petId",
        type: "string",
        list: false,
        required: true,
      },
      query,
    ]);
    const typed = first.map((r) =>
      r.id === "p1" ? { ...r, type: "integer" as const } : r,
    );
    const second = syncPathRows(typed, ["petId", "toyId"], () => "p2");
    expect(second[0]).toMatchObject({ name: "petId", type: "integer" });
    expect(second[1]).toMatchObject({ id: "p2", name: "toyId" });
    expect(syncPathRows(second, ["petId", "toyId"], id)).toBe(second);
    expect(syncPathRows(second, [], id).map((r) => r.location)).toEqual([
      "query",
    ]);
  });
});

describe("request building", () => {
  it("sends path rows first and maps request indexes back to rows", () => {
    let d = draft({ path: "/v1/pets/{petId}" });
    d = { ...d, parameters: syncPathRows(d.parameters, ["petId"], id) };
    d.parameters.push({
      id: "limit",
      location: "query",
      name: "limit",
      type: "integer",
      list: false,
      required: false,
    });
    const built = buildRequest(d);
    expect(built.issues.filter((i) => i.severity === "error")).toEqual([]);
    expect(built.request).toMatchObject({
      name: "pets.get-pet",
      version: "1.0.0",
      method: "GET",
      pathTemplate: "/v1/pets/{petId}",
      parameters: [
        { name: "petId", location: "path", type: "string", required: true },
        { name: "limit", location: "query", type: "integer", required: false },
      ],
      statuses: [200],
      emptyStatuses: [],
      timeoutSeconds: 30,
    });
    expect(built.map.parameters[1]).toBe("limit");
    expect(built.request).not.toHaveProperty("bodySample");
    expect(built.request).not.toHaveProperty("sideEffect");
  });
  it("blocks protected and duplicate headers inline instead of sending them", () => {
    const d = draft({ path: "/v1/pets" });
    d.parameters = [
      {
        id: "auth",
        location: "header",
        name: "Authorization",
        type: "string",
        list: false,
        required: false,
      },
      {
        id: "h1",
        location: "header",
        name: "X-Trace",
        type: "string",
        list: false,
        required: false,
      },
      {
        id: "h2",
        location: "header",
        name: "x-trace",
        type: "string",
        list: false,
        required: false,
      },
    ];
    const built = buildRequest(d);
    expect(built.request).toBeNull();
    const auth = built.issues.find((i) => i.row === "auth")!;
    expect(auth.field).toBe("parameters");
    expect(auth.message).toMatch(/Weave sets this header/);
    expect(built.issues.find((i) => i.row === "h2")!.message).toMatch(
      /already/,
    );
  });
  it("rejects the connection's API-key header as a parameter", () => {
    const d = draft({
      path: "/v1/pets",
      auth: { kind: "api-key", header: "X-Api-Key" },
    });
    d.parameters = [
      {
        id: "k",
        location: "header",
        name: "x-api-key",
        type: "string",
        list: false,
        required: false,
      },
    ];
    const built = buildRequest(d);
    expect(built.issues.find((i) => i.row === "k")!.message).toMatch(/API key/);
  });
  it("sends a body only for writes and forces 204/205 and HEAD to empty", () => {
    const post = draft({ method: "POST", path: "/v1/pets" });
    post.body = {
      enabled: true,
      mode: "sample",
      text: '{"name":"Rex"}',
      required: true,
    };
    post.statuses = [
      { id: "s1", code: "201", empty: false },
      { id: "s2", code: "204", empty: false },
    ];
    const built = buildRequest(post);
    expect(built.request).toMatchObject({
      bodySample: { name: "Rex" },
      bodyRequired: true,
      statuses: [201, 204],
      emptyStatuses: [204],
    });
    const get = draft({ path: "/v1/pets" });
    get.body = {
      enabled: true,
      mode: "sample",
      text: '{"x":1}',
      required: true,
    };
    expect(buildRequest(get).request).not.toHaveProperty("bodySample");
    const head = draft({ method: "HEAD", path: "/v1/pets" });
    expect(buildRequest(head).request).toMatchObject({ emptyStatuses: [200] });
    expect(emptyStatusFor("HEAD", 200, false)).toEqual({
      empty: true,
      forced: true,
    });
    expect(emptyStatusFor("GET", 205, false).forced).toBe(true);
    expect(emptyStatusFor("GET", 200, true)).toEqual({
      empty: true,
      forced: false,
    });
  });
  it("stops at the host's parameter limit before sending", () => {
    const rows = (count: number): ParamRow[] =>
      Array.from({ length: count }, (_, i) => ({
        id: `q${i}`,
        location: "query",
        name: `q${i}`,
        type: "string",
        list: false,
        required: false,
      }));
    expect(PARAMETER_LIMIT).toBe(64);
    const fits = buildRequest(draft({ path: "/x", parameters: rows(64) }));
    expect(fits.request).not.toBeNull();
    const over = buildRequest(
      draft({ path: "/x/{id}", parameters: [...rows(64)] }),
    );
    // syncPathRows adds the path row in the form; here the request has 64 query rows only.
    expect(over.request).not.toBeNull();
    const tooMany = buildRequest(draft({ path: "/x", parameters: rows(65) }));
    expect(tooMany.request).toBeNull();
    expect(tooMany.issues).toContainEqual(
      expect.objectContaining({ field: "parameters", severity: "error" }),
    );
  });
  it("explains out-of-range values before calling the host", () => {
    const d = draft({ name: "", version: "1.0", path: "pets", timeout: "45" });
    d.statuses = [{ id: "s", code: "302", empty: false }];
    d.response = { mode: "sample", text: "{oops" };
    const built = buildRequest(d);
    expect(built.request).toBeNull();
    const fields = built.issues.map((i) => i.field);
    expect(fields).toEqual(
      expect.arrayContaining([
        "name",
        "version",
        "path",
        "timeout",
        "statuses",
        "response",
      ]),
    );
    expect(built.issues.find((i) => i.field === "timeout")!.message).toMatch(
      /1 to 30/,
    );
    for (const issue of built.issues) expect(issue.message).not.toMatch(/WV-/);
  });
  it("accepts YAML or JSON samples and rejects aliases", () => {
    expect(parseSample('{"a": 1}')).toEqual({ value: { a: 1 } });
    expect(parseSample("a: 1\nb: [x]")).toEqual({
      value: { a: 1, b: ["x"] },
    });
    expect(parseSample("a: &x 1\nb: *x")).toHaveProperty("error");
    expect(parseSample("")).toHaveProperty("error");
  });
  it.skipIf(!available)(
    "produces requests the host builder accepts, with the effect fixed by method",
    () => {
      const get = draft({
        path: "/v1/pets/{petId}",
        description: "Find one pet",
      });
      get.parameters = syncPathRows(get.parameters, ["petId"], id);
      get.response = { mode: "sample", text: '{"id": 7, "name": "Rex"}' };
      const post = draft({
        name: "pets.create-pet",
        method: "POST",
        path: "/v1/pets",
      });
      post.body = {
        enabled: true,
        mode: "schema",
        text: '{"type":"object","properties":{"name":{"type":"string","maxLength":50}}}',
        required: true,
      };
      post.statuses = [{ id: "s", code: "201", empty: false }];
      const requests = [get, post].map((d) => buildRequest(d).request);
      const results = py<
        { ok: boolean; effect: string; action: string; groups: string[] }[]
      >(
        `import json,sys
from firefly_weave.sdk.http_actions import author_http_action
out=[]
for r in json.load(sys.stdin):
    res = author_http_action(r)
    a = res.action or {}
    s = a.get("spec", {})
    out.append({"ok": res.ok, "effect": s.get("sideEffect"), "action": s.get("implementation", {}).get("action"),
      "groups": sorted(s.get("inputSchema", {}).get("properties", {}).keys())})
print(json.dumps(out))`,
        requests,
      );
      expect(results).toEqual([
        { ok: true, effect: "read_only", action: "read", groups: ["path"] },
        {
          ok: true,
          effect: "non_idempotent",
          action: "write",
          groups: ["body"],
        },
      ]);
    },
  );
});

describe("diagnostics map back to the form", () => {
  const map = { parameters: ["p", "q"], statuses: ["s0", "s1"], empty: ["s1"] };
  it.each([
    ["/name", { field: "name" }],
    ["/metadata/version", { field: "version" }],
    ["/pathTemplate", { field: "path" }],
    ["/parameters/1/name", { field: "parameters", row: "q" }],
    ["/parameters/9", { field: "parameters" }],
    [
      "/spec/implementation/config/parameters/0/name",
      { field: "parameters", row: "p" },
    ],
    ["/bodySample", { field: "body" }],
    ["/spec/inputSchema/properties/body/properties/x", { field: "body" }],
    ["/responseSample", { field: "response" }],
    ["/spec/outputSchema/oneOf/0", { field: "response" }],
    ["/emptyStatuses/0", { field: "statuses", row: "s1" }],
    [
      "/spec/implementation/config/emptyStatuses/0",
      { field: "statuses", row: "s1" },
    ],
    [
      "/spec/implementation/config/statuses/0",
      { field: "statuses", row: "s0" },
    ],
    ["/timeoutSeconds", { field: "timeout" }],
    ["/spec/timeoutSeconds", { field: "timeout" }],
    ["/spec/sideEffect", { field: "method" }],
    ["/auth/header", { field: "auth" }],
    ["", { field: "general" }],
    ["/spec/connection", { field: "general" }],
  ])("%s", (pointer, expected) => {
    expect(locateDiagnostic(pointer, map)).toEqual(expected);
  });
  it("finds parameter rows from input schema pointers by location and name", () => {
    const params: ParamRow[] = [
      {
        id: "a",
        location: "path",
        name: "petId",
        type: "string",
        list: false,
        required: true,
      },
      {
        id: "b",
        location: "header",
        name: "X-Trace",
        type: "string",
        list: false,
        required: false,
      },
    ];
    expect(
      locateDiagnostic(
        "/spec/inputSchema/properties/headers/properties/X-Trace",
        { parameters: ["a", "b"], statuses: [], empty: [] },
        params,
      ),
    ).toEqual({ field: "parameters", row: "b" });
  });
  it("uses plain copy and keeps the server's specific contract messages", () => {
    const local = explainDiagnostic({
      code: "WV-HTTP-ACTION-PROTECTED_HEADER",
      severity: "error",
      message: "This header is reserved for the platform.",
      hint: "Authentication comes from the connection.",
    });
    expect(local.text).toBe("This header is reserved for the platform.");
    expect(local.hint).toBe("Authentication comes from the connection.");
    const contract = explainDiagnostic({
      code: "WV-COMP-CONFIG_CONTRACT",
      severity: "error",
      message:
        "Path variable {petId} needs a required path parameter named 'petId'.",
    });
    expect(contract.text).toMatch(/Path variable/);
    const generic = explainDiagnostic({
      code: "WV-COMP-UNKNOWN_CONNECTOR",
      severity: "error",
      message: "Unknown connector weave-http@2.0.0",
    });
    expect(generic.text).not.toMatch(/WV-/);
    expect(generic.code).toBe("WV-COMP-UNKNOWN_CONNECTOR");
  });
});

describe("versions, slots and formats", () => {
  it("suggests the next free patch version", () => {
    expect(nextPatchVersion("1.0.0", [])).toBe("1.0.1");
    expect(nextPatchVersion("1.0.0", ["1.0.1", "1.0.2"])).toBe("1.0.3");
    expect(nextPatchVersion("2.3.4-beta.1", [])).toBe("2.3.5");
    expect(nextPatchVersion("nope", [])).toBe("1.0.1");
  });
  it("names the connection slot after the service", () => {
    expect(serviceSlug("https://api.petstore.test", "x")).toBe("petstore");
    expect(serviceSlug("https://www.github.com", "x")).toBe("github");
    expect(serviceSlug("https://Billing-Prod.example.com:8443", "x")).toBe(
      "billing-prod",
    );
    expect(serviceSlug("https://10.0.0.1", "pets.get-pet")).toBe("pets");
    expect(serviceSlug("", "Get Pet!")).toBe("get-pet");
    expect(serviceSlug("", "")).toBe("api");
  });
  it("measures OpenAPI text the way the host receives it", () => {
    expect(HOST_BODY_LIMIT).toBe(2 * 1024 * 1024);
    expect(fitsHostRequest("openapi: 3.1.0\n")).toBe(true);
    // Under the limit as text, over it once quotes and newlines are escaped.
    const quoted = '"q",\n'.repeat(330_000);
    expect(quoted.length).toBeLessThan(1_900_000);
    expect(fitsHostRequest(quoted)).toBe(false);
    // Multi-byte characters count as their UTF-8 bytes.
    expect(fitsHostRequest("é".repeat(1_100_000))).toBe(false);
    expect(fitsHostRequest("a".repeat(1_900_000))).toBe(true);
  });
  it("detects JSON or YAML from the file name, then the first character", () => {
    expect(detectFormat("spec.json", "a: 1")).toBe("json");
    expect(detectFormat("spec.YML", "{}")).toBe("yaml");
    expect(detectFormat("", '  {"openapi": "3.1.0"}')).toBe("json");
    expect(detectFormat("", "openapi: 3.1.0")).toBe("yaml");
  });
});

describe("OpenAPI relaxations and operations", () => {
  it("sends only the relaxations that are switched on", () => {
    expect(relaxationPayload({})).toBeUndefined();
    expect(
      relaxationPayload({
        numericFormats: true,
        jsonMediaOnly: false,
        defaultStringMaxLength: 256,
      }),
    ).toEqual({ numericFormats: true, defaultStringMaxLength: 256 });
  });
  it("finds the relaxation each reason suggests and rewrites the key in plain words", () => {
    const reasons = [
      {
        code: "WV-IMPORT-UNSUPPORTED",
        message: "The response declares headers.",
        hint: "Set relaxations.ignoreResponseHeaders to import the body only.",
      },
      {
        code: "WV-SCHEMA-UNSUPPORTED_FORMAT",
        message: "Format is outside the supported schema profile.",
        hint: "set relaxations.numericFormats to replace int32/int64",
      },
    ];
    expect(suggestedRelaxations(reasons)).toEqual([
      "ignoreResponseHeaders",
      "numericFormats",
    ]);
    const text = plainRelaxationText(reasons[0].hint);
    expect(text).not.toMatch(/relaxations\./);
    expect(text).toContain(RELAXATIONS.ignoreResponseHeaders.label);
  });
  it("maps document pointers to operations", () => {
    expect(operationForPointer("/paths/~1pets~1{petId}/get/parameters/0")).toBe(
      "GET /pets/{petId}",
    );
    expect(operationForPointer("/paths/~1a~0b/post")).toBe("POST /a~b");
    expect(operationForPointer("/components/schemas/Pet")).toBeNull();
  });
  it("filters operations by every word", () => {
    const ops = [
      {
        key: "listPets",
        method: "GET",
        path: "/pets",
        summary: "List pets",
        tags: ["pets"],
      },
      {
        key: "createPet",
        method: "POST",
        path: "/pets",
        summary: "Create a pet",
        tags: [],
      },
      { key: "GET /health", method: "GET", path: "/health", tags: [] },
    ];
    expect(filterOperations(ops, "get pets").map((o) => o.key)).toEqual([
      "listPets",
    ]);
    expect(filterOperations(ops, "").length).toBe(3);
    expect(filterOperations(ops, "create").map((o) => o.key)).toEqual([
      "createPet",
    ]);
  });
});

describe("publish failures", () => {
  it("separates version conflicts, key reuse, compile errors and unknown outcomes", () => {
    expect(
      classifyPublishError(
        new ApiError(409, { code: "WV-VERSION-CONFLICT", message: "x" }),
      ).kind,
    ).toBe("version");
    expect(
      classifyPublishError(
        new ApiError(409, { code: "WV-IDEMPOTENCY-CONFLICT" }),
      ).kind,
    ).toBe("key");
    const compile = classifyPublishError(
      new ApiError(422, {
        code: "WV-COMPILE",
        result: {
          diagnostics: [
            {
              code: "WV-COMP-CONFIG_CONTRACT",
              severity: "error",
              message: "m",
              path: "/spec",
            },
          ],
        },
      }),
    );
    expect(compile.kind).toBe("compile");
    expect(compile.diagnostics).toHaveLength(1);
    expect(classifyPublishError(new ApiError(403, {})).kind).toBe("denied");
    expect(classifyPublishError(new ApiError(503, {})).kind).toBe("unknown");
    expect(classifyPublishError(new TypeError("Failed to fetch")).kind).toBe(
      "unknown",
    );
    expect(
      classifyPublishError(new DOMException("t", "TimeoutError")).kind,
    ).toBe("unknown");
    expect(classifyPublishError(new ApiError(422, {})).kind).toBe("rejected");
  });
  it("recognizes a taken version and a missing connector in platform diagnostics", () => {
    const taken = [
      {
        code: "WV-COMP-IMMUTABLE_VERSION",
        severity: "error",
        path: "/metadata/version",
      },
      { code: "WV-COMP-UNKNOWN_COMPATIBILITY", severity: "warning" },
    ];
    expect(versionTaken(taken)).toBe(true);
    expect(versionTaken([{ code: "WV-COMP-CONFIG_CONTRACT" }])).toBe(false);
    expect(needsConnector([{ code: "WV-COMP-UNKNOWN_CONNECTOR" }])).toBe(true);
    expect(needsConnector([{ code: "WV-COMP-UNKNOWN_ADAPTER" }])).toBe(true);
    expect(needsConnector(taken)).toBe(false);
  });
  it("suggests a version that is free for this action only", () => {
    const published = [
      "pets.get-pet@1.0.1",
      "pets.get-pet-v2@1.0.2",
      "other@1.0.2",
    ];
    expect(suggestVersion("pets.get-pet", "1.0.0", published)).toBe("1.0.2");
    expect(suggestVersion("pets.get-pet", "1.0.2", published)).toBe("1.0.3");
    expect(suggestVersion("other", "1.0.1", published)).toBe("1.0.3");
  });
  it("keeps the HTTP connector reference fixed", () => {
    expect(HTTP_CONNECTOR).toBe("weave-http@2.0.0");
  });
});

describe("connection slot names", () => {
  it("names the slot after the host for HTTP and HTTPS addresses", () => {
    expect(serviceSlug("http://acme.acceptance.test:8080", "x.get")).toBe(
      "acme",
    );
    expect(serviceSlug("https://api.pets.example", "x.get")).toBe("pets");
  });
});
