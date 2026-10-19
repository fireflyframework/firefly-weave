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
// The static no-mocks rule for the Studio journeys (tests/acceptance-lint.ts):
// the committed journeys pass, and each bypass in the corpus below is either
// caught or listed as one only the run-time guard can stop.
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { analyzeJourneys, lintJourneys, lintTree } from "./acceptance-lint";

const STUDIO = resolve(__dirname, "..");
const folders: string[] = [];

function scratch(): string {
  const folder = mkdtempSync(join(tmpdir(), "acceptance-lint-"));
  folders.push(folder);
  return folder;
}

afterEach(() => {
  while (folders.length)
    rmSync(folders.pop()!, { recursive: true, force: true });
});

/** A journey spec whose step body is `code`. */
function spec(code: string): string {
  return [
    'import { journey } from "./harness";',
    'journey("J9", async (j: any) => {',
    '  const page: any = await j.signIn("builder");',
    "  const context: any = page.context();",
    "  const browser: any = undefined;",
    "  const host = 'http://127.0.0.1:1';",
    "  const token = 'x';",
    code,
    "});",
    "",
  ].join("\n");
}

function rules(code: string): string[] {
  return lintTree(scratch(), { "acceptance/probe.spec.ts": spec(code) }).map(
    (finding) => finding.rule,
  );
}

describe("the committed journeys", () => {
  it("use no mocks, shortcuts, forced input or writing page functions", () => {
    expect(
      lintJourneys({
        journeys: join(STUDIO, "tests/acceptance"),
        config: join(STUDIO, "playwright.acceptance.config.ts"),
        root: STUDIO,
      }),
    ).toEqual([]);
  });

  it("read the declarations the harness uses in other files, and only those", () => {
    const { reached } = analyzeJourneys({
      journeys: join(STUDIO, "tests/acceptance"),
      config: join(STUDIO, "playwright.acceptance.config.ts"),
      root: STUDIO,
    });
    const support = reached["tests/browser/real-platform-support.ts"] ?? [];
    expect(support).toContain("connectAndSignIn");
    expect(support).toContain("removePlatform");
    // The run starter posts through the page on purpose; no journey reaches it.
    expect(support).not.toContain("startDemoRun");
  });
});

/**
 * Probes that found earlier bypasses (48), in four groups:
 * structural, Playwright's own fixtures, APIs the regex lint lacked and
 * adversarial spellings. Each runs as a journey step's body.
 */
const SNIPPETS: Record<string, string> = {
  // Structural
  "aliased import":
    'const s: any = await import("../browser/real-platform-support");',
  "namespace import through require":
    'const s = require("../browser/real-platform-support"); await s.startDemoRun(page);',
  "dynamic import": 'await (await import("./other")).helper(page);',
  "require of a helper": 'require("./other").helper(page);',
  "import.meta resolution": "const here = import.meta.url;",
  "process environment": 'process.env["WEAVE_TOKEN"] = token;',
  "globalThis fetch": 'await globalThis.fetch(host, { method: "POST" });',
  "module exports": "module.exports = page;",
  // Playwright's own fixtures, as parameters
  "page fixture": "const run = async ({ page }: any) => page.goto(host);",
  "context fixture":
    "const run = async ({ context }: any) => context.addCookies([]);",
  "request fixture":
    "const run = async ({ request }: any) => request.post(host);",
  "browser fixture":
    "const run = async ({ browser }: any) => browser.newContext();",
  "fixture renamed": "const run = async ({ page: p }: any) => p.goto(host);",
  // APIs the regex lint lacked
  "http credentials":
    'await context.setHTTPCredentials({ username: "a", password: "b" });',
  "page content": 'await page.setContent("<p>signed in</p>");',
  "script tag": 'await page.addScriptTag({ path: "fake-auth.js" });',
  "writing page function":
    'await page.waitForFunction(() => localStorage.setItem("k", "v"));',
  "storage assignment":
    '// read-only audit: no\nawait page.evaluate(() => { localStorage["k"] = "v"; });',
  "IndexedDB write in a noted evaluate":
    '// read-only audit: reads\nawait page.evaluate(() => indexedDB.open("weave").onsuccess = (e: any) => e.target.result.transaction("s", "readwrite").objectStore("s").put(1, "k"));',
  "persistent context":
    'await browser.launchPersistentContext("/tmp/profile");',
  "browser over CDP": 'await browser.connectOverCDP("http://127.0.0.1:9222");',
  "browser CDP session": "await browser.newBrowserCDPSession();",
  "own browser": "await browser.launch();",
  "request alias": "const api = page.request; await api.post(host);",
  "request destructured": "const { request } = page; await request.post(host);",
  "request GET from a journey":
    "await page.request.get(`${host}/?code=${token}`);",
  "string page function": "await page.evaluate('localStorage.clear()');",
  // Adversarial spellings
  "concatenated member": 'await page["ro" + "ute"]("**/*", () => {});',
  "template member": 'await page[`route`]("**/*", () => {});',
  "variable member":
    'const name = "route"; await page[name]("**/*", () => {});',
  "String.raw member": 'await page[String.raw`route`]("**/*", () => {});',
  "Reflect.get":
    'await Reflect.get(page, "route").call(page, "**/*", () => {});',
  "Reflect.apply": 'await Reflect.apply(page.route, page, ["**/*", () => {}]);',
  "call on the method": 'await page.route.call(page, "**/*", () => {});',
  "bound method": 'const r = page.route.bind(page); await r("**/*", () => {});',
  "destructured method":
    'const { route } = page; await route("**/*", () => {});',
  "unicode escape": 'await page.\\u0072oute("**/*", () => {});',
  eval: "eval(\"page.route('**/*', () => {})\");",
  "new Function": 'new Function("page", "return page.route")(page);',
  "fetch alias": "const f = fetch; await f(host);",
  "child process curl":
    'const cp = await import("node:child_process"); cp.execSync(`curl ${host}`);',
  "Node http": 'const http = require("node:http"); http.get(host);',
  "forged step record":
    'const fs = require("node:fs"); fs.appendFileSync("steps.jsonl", "{}");',
  "constructor escape":
    'const F = (async () => {}).constructor; await new (F as any)("p", "return p.route")(page);',
  "prototype method":
    'await Object.getPrototypeOf(page).route.call(page, "**/*", () => {});',
  "prototype descriptor":
    'await Object.getOwnPropertyDescriptor(Object.getPrototypeOf(page), "ro" + "ute").value.call(page, "**/*", () => {});',
  "page evaluate by name":
    'await page["evaluate"](() => localStorage.clear());',
  "proxy option":
    'const options = { proxy: { server: "http://127.0.0.1:8888" } };',
};
/**
 * Later bypasses: Node's `global`, destructured reflection
 * reaching the prototype's original `route`, and emptying the guard's deny
 * list. Each must fail statically; the prototype one also fails at run time
 * (tests/browser/acceptance-guard.spec.ts).
 */
const LATER_BYPASSES: Record<string, string> = {
  "global fetch": 'await global.fetch(host, { method: "POST" });',
  "global process":
    'global.process.getBuiltinModule("node:fs").appendFileSync("steps.jsonl", "{}");',
  "global Reflect and eval":
    'global.Reflect.get(page, "ro" + "ute"); global.eval("1");',
  "destructured prototype reflection":
    'const { getPrototypeOf: gp, getOwnPropertyDescriptors: gd } = Object;\nconst [, d]: any = Object.entries(gd(gp(page))).find(([k]) => k === "ro" + "ute");\nawait d.value.call(page, "**/*", (r: any) => r.fulfill({ status: 200 }));',
  "emptied deny list":
    'const { DENIED_PAGE } = await import("./harness");\n(DENIED_PAGE as any).length = 0;',
  "browser globals":
    "navigator.sendBeacon(host); structuredClone(page); setTimeout(() => {}, 1);",
};

/** Ordinary journey code that must stay quiet. */
const LEGITIMATE = [
  'await page.keyboard.type("order-review");',
  "await page.getByRole('button', { name: 'Publish…' }).click();",
  "// read-only audit: the font the page computed\nconst family = await page.evaluate(() => getComputedStyle(document.body).fontFamily);",
  "const first = [1, 2][0];",
];

describe("the snippet corpus", () => {
  it("holds 48 probes", () => {
    expect(Object.keys(SNIPPETS)).toHaveLength(48);
  });

  it.each(Object.entries(SNIPPETS))("%s is caught", (_, code) => {
    expect(rules(code)).not.toEqual([]);
  });

  it.each(Object.entries(LATER_BYPASSES))(
    "later bypass %s is caught",
    (_, code) => {
      expect(rules(code)).not.toEqual([]);
    },
  );

  it.each([
    [
      "const p = (page as any).__proto__;",
      "reflective access in a journey: __proto__",
    ],
    [
      "await page.evaluate(() => document.title);",
      "page.evaluate without a read-only audit note",
    ],
    [
      'await page.goto("https://idp.test/#access_token=abc");',
      "token or script in a URL",
    ],
    [
      "// read-only audit: the form\nawait page.evaluate(() => document.forms[0].submit());",
      "write from the page: submit",
    ],
    ['await page.goto("javascript" + ":void(0)");', "token or script in a URL"],
  ])("one rule alone catches %s", (code, rule) => {
    expect(rules(code)).toEqual([rule]);
  });

  it("refuses reflective names when they are destructured", () => {
    expect(rules("const { getPrototypeOf: gp } = Object; gp(page);")).toContain(
      "reflective access in a journey: getPrototypeOf",
    );
  });

  it("allows only language globals in a journey, and page globals only inside page functions", () => {
    expect(
      rules("const n = Math.max(1, JSON.parse('2'), Number('3'));"),
    ).toEqual([]);
    expect(rules("const g = global;")).toContain(
      "undeclared global in a journey: global",
    );
    expect(rules("navigator.onLine;")).toContain(
      "global outside the journey allowlist: navigator",
    );
    expect(
      rules(
        "// read-only audit: the title\nconst t = await page.evaluate(() => document.title);",
      ),
    ).toEqual([]);
  });

  it.each(LEGITIMATE)("ordinary code passes: %s", (code) => {
    expect(rules(code)).toEqual([]);
  });
});

/**
 * The 17 structural layouts: how a helper outside the
 * journeys folder can be reached. Each helper hides `page.route` in the
 * function the importer uses. Every layout is tried from a spec (the import
 * rule fails it) and from the harness (the type checker must follow it).
 */
const HIDDEN =
  "export async function helper(page: any) { await page.route('**/*', () => {}); }\n";
const LAYOUTS: Record<
  string,
  { importer: string; files: Record<string, string> }
> = {
  "aliased import": {
    importer:
      'import { helper as h } from "../browser/help";\nexport const run = (p: any) => h(p);\n',
    files: { "browser/help.ts": HIDDEN },
  },
  "namespace import": {
    importer:
      'import * as s from "../browser/help";\nexport const run = (p: any) => s.helper(p);\n',
    files: { "browser/help.ts": HIDDEN },
  },
  "default and named import": {
    importer:
      'import x, { helper } from "../browser/help";\nexport const run = (p: any) => helper(p) && x;\n',
    files: { "browser/help.ts": HIDDEN + "export default 1;\n" },
  },
  "export { real as helper }": {
    importer:
      'import { helper } from "../browser/help";\nexport const run = (p: any) => helper(p);\n',
    files: {
      "browser/help.ts":
        "async function real(page: any) { await page.route('**/*', () => {}); }\nexport { real as helper };\n",
    },
  },
  "default export arrow": {
    importer:
      'import helper from "../browser/help";\nexport const run = (p: any) => helper(p);\n',
    files: {
      "browser/help.ts":
        "export default async (page: any) => { await page.route('**/*', () => {}); };\n",
    },
  },
  "named re-export through an index": {
    importer:
      'import { helper } from "../browser/index";\nexport const run = (p: any) => helper(p);\n',
    files: {
      "browser/index.ts": 'export { helper } from "./help";\n',
      "browser/help.ts": HIDDEN,
    },
  },
  "star re-export through an index": {
    importer:
      'import { helper } from "../browser/index";\nexport const run = (p: any) => helper(p);\n',
    files: {
      "browser/index.ts": 'export * from "./help";\n',
      "browser/help.ts": HIDDEN,
    },
  },
  "dynamic import()": {
    importer:
      'export const run = async (p: any) => (await import("../browser/help")).helper(p);\n',
    files: { "browser/help.ts": HIDDEN },
  },
  "require()": {
    importer:
      'export const run = (p: any) => require("../browser/help").helper(p);\n',
    files: { "browser/help.ts": HIDDEN },
  },
  ".js suffix": {
    importer:
      'import { helper } from "../browser/help.js";\nexport const run = (p: any) => helper(p);\n',
    files: { "browser/help.ts": HIDDEN },
  },
  ".mjs helper": {
    importer:
      'import { helper } from "../browser/help.mjs";\nexport const run = (p: any) => helper(p);\n',
    files: {
      "browser/help.mjs":
        "export async function helper(page) { await page.route('**/*', () => {}); }\n",
    },
  },
  ".tsx helper": {
    importer:
      'import { helper } from "../browser/help";\nexport const run = (p: any) => helper(p);\n',
    files: { "browser/help.tsx": HIDDEN },
  },
  "multi-line import": {
    importer:
      'import {\n  other,\n  helper,\n} from "../browser/help";\nexport const run = (p: any) => helper(p) && other;\n',
    files: { "browser/help.ts": HIDDEN + "export const other = 1;\n" },
  },
  "object method": {
    importer:
      'import { tools } from "../browser/help";\nexport const run = (p: any) => tools.helper(p);\n',
    files: {
      "browser/help.ts":
        "export const tools = { async helper(page: any) { await page.route('**/*', () => {}); } };\n",
    },
  },
  "class static method": {
    importer:
      'import { Tools } from "../browser/help";\nexport const run = (p: any) => Tools.helper(p);\n',
    files: {
      "browser/help.ts":
        "export class Tools { static async helper(page: any) { await page.route('**/*', () => {}); } }\n",
    },
  },
  "code after a non-declaration line": {
    importer:
      'import { helper } from "../browser/help";\nexport const run = (p: any) => helper(p);\n',
    files: {
      "browser/help.ts":
        "export const helper = (page: any) => inner(page);\nconsole.log('loaded');\nasync function inner(page: any) { await page.route('**/*', () => {}); }\n",
    },
  },
  "nested spec and an inner function": {
    importer:
      'import { helper } from "../browser/help";\nexport const run = (p: any) => helper(p);\n',
    files: {
      "browser/help.ts":
        "export async function helper(page: any) { await inner(page); }\nasync function inner(page: any) { await page.route('**/*', () => {}); }\nexport function unused(page: any) { page.addCookies([]); }\n",
    },
  },
};

describe("the structural layouts", () => {
  it("hold 17 forms", () => {
    expect(Object.keys(LAYOUTS)).toHaveLength(17);
  });

  it.each(Object.entries(LAYOUTS))(
    "%s is caught in the helper when the harness uses it",
    (_, layout) => {
      const findings = lintTree(scratch(), {
        ...layout.files,
        "acceptance/harness.ts": layout.importer,
      });
      const helper = findings.filter((f) => f.file.startsWith("browser/"));
      // require() is refused where it stands; every other form is followed.
      expect(
        helper.map((f) => f.rule).includes("request interception: route") ||
          findings.some(
            (f) => f.file === "acceptance/harness.ts" && f.rule === "require()",
          ),
      ).toBe(true);
      // Declarations the harness does not reach stay out.
      expect(helper.map((f) => f.rule)).not.toContain(
        "injected cookies: addCookies",
      );
    },
  );

  it.each(Object.entries(LAYOUTS))(
    "%s is refused in a journey spec",
    (_, layout) => {
      const findings = lintTree(scratch(), {
        ...layout.files,
        "acceptance/nested/probe.spec.ts": layout.importer.replace(
          "../browser/",
          "../../browser/",
        ),
      });
      expect(
        findings.some((f) => f.file === "acceptance/nested/probe.spec.ts"),
      ).toBe(true);
    },
  );
});

describe("a journey's failures stay failures", () => {
  it.each([
    [
      'try { await j.step("J9.1", async () => { throw Error("x"); }); } catch {}',
      "caught step failure",
    ],
    [
      'await j.step("J9.1", async () => {}).catch(() => false);',
      "caught step failure",
    ],
    [
      'try { await j.check("J9.2/x", async () => {}); } finally {}',
      "caught step failure",
    ],
  ])("refuses %s", (code, rule) => {
    expect(rules(code)).toContain(
      `${rule}: a step or check inside try or .catch`,
    );
  });

  it("refuses a journey declared twice in the tree", () => {
    const body =
      'import { journey } from "./harness";\njourney("J9", "x", async () => {});\n';
    const findings = lintTree(scratch(), {
      "acceptance/a.spec.ts": body,
      "acceptance/nested/b.spec.ts": body.replace("./harness", "../harness"),
    });
    expect(findings.map((f) => f.rule)).toContain(
      "journey J9 is declared more than once",
    );
  });
});

describe("module load", () => {
  it("reads the top-level code of every module the reached code imports", () => {
    const findings = lintTree(scratch(), {
      "acceptance/harness.ts":
        'import { helper } from "../browser/help";\nexport const run = () => helper;\n',
      "browser/help.ts": 'import "./side";\nexport const helper = 1;\n',
      "browser/side.ts":
        "declare const page: any;\npage.route('**/*', () => {});\nexport function quiet() {}\n",
    });
    expect(findings).toContainEqual({
      file: "browser/side.ts",
      line: 2,
      rule: "request interception: route",
    });
  });
});

describe("imports in a journey spec", () => {
  it("accepts named harness values and Playwright types", () => {
    const findings = lintTree(scratch(), {
      "acceptance/a.spec.ts":
        'import type { Page } from "@playwright/test";\nimport { type Locator } from "@playwright/test";\nimport { journey, expect } from "./harness";\nexport const x = (p: Page, l: Locator) => [p, l, journey, expect];\n',
    });
    expect(findings).toEqual([]);
  });

  it.each([
    [
      'import { test } from "./harness";',
      "Playwright test outside the harness: test",
    ],
    [
      'import { test } from "@playwright/test";',
      "Playwright values in a journey (import them from the harness)",
    ],
    [
      'import { journey as j } from "./harness";',
      "aliased import in a journey",
    ],
    ['import * as h from "./harness";', "namespace import in a journey"],
    ['import h from "./harness";', "default import in a journey"],
    [
      'import { readFileSync } from "node:fs";',
      "import of node:fs in a journey",
    ],
    ['export { journey } from "./harness";', "re-export in a journey"],
    ['import h = require("./harness");', "import = require"],
  ])("refuses %s", (line, rule) => {
    const findings = lintTree(scratch(), {
      "acceptance/a.spec.ts": line + "\n",
    });
    expect(findings.map((f) => f.rule)).toContain(rule);
  });
});

describe("the Playwright configuration", () => {
  it.each([
    [
      'testDir: "tests", testMatch: "*.spec.ts"',
      "configuration runs other files: testDir",
    ],
    [
      'testDir: "tests/acceptance", testMatch: "**/*.ts"',
      "configuration runs other files: testMatch",
    ],
    ['testMatch: "*.spec.ts"', "configuration runs other files: testDir"],
  ])("pins the folder and pattern the lint reads: %s", (option, rule) => {
    const folder = scratch();
    lintTree(folder, {});
    const config = join(folder, "playwright.acceptance.config.ts");
    lintTree(folder, {
      "playwright.acceptance.config.ts": `export default { ${option} };\n`,
    });
    expect(
      lintJourneys({
        journeys: join(folder, "acceptance"),
        config,
        root: folder,
      }).map((f) => f.rule),
    ).toContain(rule);
  });

  it.each([
    ["storageState", 'use: { storageState: "signed-in.json" }'],
    ["proxy", 'use: { proxy: { server: "http://127.0.0.1:1" } }'],
    ["globalSetup", 'globalSetup: "./setup.ts"'],
    ["webServer", 'webServer: { command: "x" }'],
    [
      "args",
      'use: { launchOptions: { args: ["--host-resolver-rules=MAP * 127.0.0.1"] } }',
    ],
    // Playwright would start tracing every context itself, and the harness's own
    // tracing.start would then fail on each sign-in.
    ["trace", 'use: { trace: "retain-on-failure" }'],
  ])("refuses %s", (key, option) => {
    const folder = scratch();
    lintTree(folder, {});
    const findings = lintTree(folder, {
      "playwright.acceptance.config.ts": `export default { ${option} };\n`,
    });
    expect(findings).toEqual([]);
    expect(
      lintJourneys({
        journeys: join(folder, "acceptance"),
        config: join(folder, "playwright.acceptance.config.ts"),
        root: folder,
      }).map((f) => f.rule),
    ).toContainEqual(expect.stringContaining(key));
  });
});
