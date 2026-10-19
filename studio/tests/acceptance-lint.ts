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
// The static half of the Studio journeys' no-mocks rule (the runtime half is
// the guarded fixtures and contexts in tests/acceptance/harness.ts). It parses
// the journeys with the TypeScript compiler, not with patterns:
// - A journey spec imports only named, unaliased values from ./harness (never
//   its `test`) and types from @playwright/test. Every other import form fails:
//   aliased, namespace or default imports, other modules, re-exports, dynamic
//   import(), require and import = require.
// - Every file under tests/acceptance (any extension) and the Playwright
//   configuration are read whole. In other files, the type checker follows each
//   name the reached code uses, through aliases, namespaces, default exports and
//   re-exports, to the top-level statement that declares it, and reads that
//   statement plus the module's top-level code that runs on import.
// - The reached code may not intercept or shape requests, inject sign-in state,
//   force or synthesize input, control the clock, open browsers, contexts or
//   pages outside the harness, or write from inside the page. Each evaluate
//   call is a read-only audit with a note before its statement.
import {
  existsSync,
  mkdirSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import ts from "typescript";

export interface Finding {
  file: string;
  line: number;
  rule: string;
}

type Kind = "spec" | "harness" | "journey" | "config" | "reached";

const SOURCE = /\.(?:ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;

/** Members no reached code may touch, by any spelling the parser resolves. */
const FORBIDDEN_MEMBERS: Record<string, string> = {
  route: "request interception",
  unroute: "request interception",
  unrouteAll: "request interception",
  routeFromHAR: "recorded responses",
  routeWebSocket: "socket interception",
  fulfill: "fulfilled request",
  newCDPSession: "browser protocol session",
  newBrowserCDPSession: "browser protocol session",
  setOffline: "network shaping",
  serviceWorkers: "service worker access",
  storageState: "stored sign-in state",
  addCookies: "injected cookies",
  clearCookies: "cookie write",
  setExtraHTTPHeaders: "injected headers",
  setHTTPCredentials: "injected credentials",
  addInitScript: "page script injection",
  addScriptTag: "page script injection",
  addStyleTag: "page script injection",
  setContent: "page content replaced",
  evaluateHandle: "page handle evaluation",
  $eval: "element evaluation",
  $$eval: "element evaluation",
  evaluateAll: "element evaluation",
  waitForFunction: "page function",
  exposeBinding: "page binding",
  exposeFunction: "page binding",
  setItem: "storage write",
  removeItem: "storage write",
  deleteDatabase: "storage write",
  dispatchEvent: "synthetic event",
  insertText: "synthetic input",
  clock: "clock control",
  launchPersistentContext: "own browser",
  connectOverCDP: "own browser",
  launchServer: "own browser",
  grantPermissions: "context permissions",
  setGeolocation: "context shaping",
};
/** Object literal keys that shape a browser, a context or a request. */
const FORBIDDEN_OPTIONS: Record<string, string> = {
  storageState: "stored sign-in state",
  extraHTTPHeaders: "injected headers",
  httpCredentials: "injected credentials",
  offline: "network shaping",
  recordHar: "recorded traffic",
  proxy: "proxied traffic",
  bypassCSP: "content policy bypass",
  serviceWorkers: "service worker access",
  clientCertificates: "injected credentials",
};
/** Configuration keys that run code or shape every test outside the harness. */
const CONFIG_OPTIONS: Record<string, string> = {
  globalSetup: "configuration code",
  globalTeardown: "configuration code",
  webServer: "configuration code",
  args: "browser arguments",
  ignoreDefaultArgs: "browser arguments",
  connectOptions: "remote browser",
};
/** Only the harness opens browsers, contexts and pages. */
const HARNESS_MEMBERS: Record<string, string> = {
  newContext: "browser context outside Journey.signIn",
  newPage: "page outside Journey.signIn",
  launch: "own browser",
};
/** Globals a journey spec may not reach for. */
const SPEC_GLOBALS: Record<string, string> = {
  fetch: "direct request",
  XMLHttpRequest: "direct request",
  WebSocket: "direct request",
  require: "module loading",
  process: "process access",
  globalThis: "global access",
  module: "module loading",
  exports: "module loading",
};
/** The only globals a journey spec may name outside page functions: the language's own. */
const LANGUAGE_GLOBALS = new Set([
  "undefined",
  "NaN",
  "Infinity",
  "Object",
  "Array",
  "JSON",
  "Promise",
  "Math",
  "Date",
  "RegExp",
  "String",
  "Number",
  "Boolean",
  "Error",
  "Set",
  "Map",
  "Symbol",
  "parseInt",
  "parseFloat",
  "isNaN",
  "encodeURIComponent",
  "decodeURIComponent",
]);
/** Globals no reached code may use: they hide what a call reaches. */
const HIDDEN_CALLS: Record<string, string> = {
  Reflect: "reflective call",
  eval: "evaluated code",
  Function: "evaluated code",
};
/** Members that reach an object's methods around its own properties. */
const REFLECTIVE = new Set([
  "constructor",
  "__proto__",
  "prototype",
  "getPrototypeOf",
  "setPrototypeOf",
  "getOwnPropertyDescriptor",
  "getOwnPropertyDescriptors",
  "getOwnPropertyNames",
  "defineProperty",
  "defineProperties",
]);
/** Playwright's own fixtures, which open a browser, context or page the harness never sees. */
const FIXTURES = new Set(["page", "context", "request", "browser"]);
/** Inside a page function: globals that send requests or run code. */
const PAGE_GLOBALS = new Set([
  "fetch",
  "XMLHttpRequest",
  "WebSocket",
  "EventSource",
  "eval",
  "Function",
  "importScripts",
]);
/** Inside a page function: members that write or act. */
const PAGE_WRITES = new Set([
  "setItem",
  "removeItem",
  "clear",
  "put",
  "add",
  "delete",
  "deleteDatabase",
  "click",
  "submit",
  "requestSubmit",
  "sendBeacon",
  "postMessage",
  "pushState",
  "replaceState",
  "createObjectStore",
  "deleteObjectStore",
]);
const TOKEN_TEXT = /(?:access|id|refresh)_token=|javascript:/;

function walk(directory: string): string[] {
  if (!existsSync(directory)) return [];
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return walk(path);
    return SOURCE.test(name) ? [path] : [];
  });
}

function memberName(node: ts.Node): string | undefined {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isElementAccessExpression(node)) {
    const argument = node.argumentExpression;
    if (
      ts.isStringLiteralLike(argument) ||
      ts.isNoSubstitutionTemplateLiteral(argument)
    )
      return argument.text;
  }
  return undefined;
}

function propertyKey(name: ts.PropertyName): string | undefined {
  if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name)) return name.text;
  if (ts.isStringLiteralLike(name) || ts.isNumericLiteral(name))
    return name.text;
  return undefined;
}

function calleeName(call: ts.CallExpression): string | undefined {
  const callee = call.expression;
  if (ts.isIdentifier(callee)) return callee.text;
  return memberName(callee);
}

/** The statement that holds `node` directly in a block or file. */
function statementOf(node: ts.Node): ts.Node {
  let current = node;
  while (
    current.parent &&
    !ts.isSourceFile(current.parent) &&
    !ts.isBlock(current.parent) &&
    !ts.isModuleBlock(current.parent) &&
    !ts.isCaseClause(current.parent)
  )
    current = current.parent;
  return current;
}

function topLevel(node: ts.Node): ts.Node {
  let current = node;
  while (current.parent && !ts.isSourceFile(current.parent))
    current = current.parent;
  return current;
}

function audited(node: ts.CallExpression, file: ts.SourceFile): boolean {
  const statement = statementOf(node);
  const text = file.getFullText();
  const comments = ts.getLeadingCommentRanges(text, statement.getFullStart());
  return (comments ?? []).some((range) =>
    text.slice(range.pos, range.end).includes("read-only audit"),
  );
}

function isFunctionLike(node: ts.Node): node is ts.FunctionLikeDeclaration {
  return ts.isArrowFunction(node) || ts.isFunctionExpression(node);
}

/** Writes and requests inside a page function passed to evaluate. */
function pageFunctionProblems(body: ts.Node): string[] {
  const found: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isIdentifier(node) && PAGE_GLOBALS.has(node.text)) {
      const parent = node.parent;
      const isMember =
        parent && ts.isPropertyAccessExpression(parent) && parent.name === node;
      if (!isMember) found.push(`request or code from the page: ${node.text}`);
    }
    if (ts.isCallExpression(node)) {
      const name = calleeName(node);
      if (name && PAGE_WRITES.has(name))
        found.push(`write from the page: ${name}`);
      if (name === "open" && node.arguments.length > 1)
        found.push("write from the page: a database upgrade");
      const callee = node.expression;
      if (
        ts.isPropertyAccessExpression(callee) &&
        ts.isIdentifier(callee.expression) &&
        ["location", "history"].includes(callee.expression.text)
      )
        found.push(`navigation from the page: ${callee.expression.text}`);
    }
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment &&
      node.operatorToken.kind <= ts.SyntaxKind.LastAssignment
    ) {
      const target = node.left;
      const name = memberName(target);
      const handler = name !== undefined && /^on[a-z]+$/.test(name);
      if (
        (ts.isPropertyAccessExpression(target) ||
          ts.isElementAccessExpression(target)) &&
        !handler
      )
        found.push("write from the page: an assignment");
    }
    if (
      ts.isStringLiteralLike(node) &&
      ["readwrite", "versionchange"].includes(node.text)
    )
      found.push("write from the page: a writing transaction");
    ts.forEachChild(node, visit);
  };
  visit(body);
  return found;
}

/** The rule findings in one reached node of a file of the given kind. */
/** Where a name is declared: in this project, only in a library's declarations, or nowhere. */
type Origin = "project" | "library" | "none";

/** Inside a function passed to evaluate: it runs in the page, where page globals are the point. */
function inPageFunction(node: ts.Node): boolean {
  for (let current = node.parent; current; current = current.parent) {
    if (
      isFunctionLike(current) &&
      ts.isCallExpression(current.parent) &&
      current.parent.arguments[0] === current &&
      calleeName(current.parent) === "evaluate"
    )
      return true;
  }
  return false;
}

function inType(node: ts.Node): boolean {
  for (let current = node.parent; current; current = current.parent)
    if (
      ts.isTypeNode(current) ||
      ts.isTypeAliasDeclaration(current) ||
      ts.isInterfaceDeclaration(current)
    )
      return true;
  return false;
}

function nodeProblems(
  root: ts.Node,
  file: ts.SourceFile,
  kind: Kind,
  origin: (node: ts.Identifier) => Origin = () => "project",
): { node: ts.Node; rule: string }[] {
  const found: { node: ts.Node; rule: string }[] = [];
  const add = (node: ts.Node, rule: string) => found.push({ node, rule });
  const spec = kind === "spec";
  const visit = (node: ts.Node): void => {
    // Module loading other than static imports.
    if (ts.isCallExpression(node)) {
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword)
        add(node, "dynamic import()");
      if (
        ts.isIdentifier(node.expression) &&
        node.expression.text === "require"
      )
        add(node, "require()");
    }
    if (ts.isImportEqualsDeclaration(node)) add(node, "import = require");
    // Members, by property access, literal element access or destructuring.
    const member = memberName(node);
    if (member !== undefined) {
      if (Object.hasOwn(FORBIDDEN_MEMBERS, member))
        add(node, `${FORBIDDEN_MEMBERS[member]}: ${member}`);
      if (kind !== "harness" && Object.hasOwn(HARNESS_MEMBERS, member))
        add(node, `${HARNESS_MEMBERS[member]}: ${member}`);
      if (member === "request") requestUse(node, kind, add);
      if (member === "continue" && ts.isCallExpression(node.parent)) {
        if (node.parent.expression === node && node.parent.arguments.length)
          add(node, "rewritten request: continue");
      }
      if (spec && REFLECTIVE.has(member))
        add(node, `reflective access in a journey: ${member}`);
      if (member === "browser" && ts.isCallExpression(node.parent))
        add(node, "the context's browser: browser()");
      if (member === "clear" && ts.isPropertyAccessExpression(node)) {
        const target = node.expression.getText(file);
        if (/(?:local|session)Storage$/.test(target))
          add(node, "storage write: clear");
      }
      if (member === "cookie" && isAssigned(node))
        add(node, "cookie write: document.cookie");
      if (
        /(?:local|session)Storage$/.test(
          (
            node as ts.PropertyAccessExpression | ts.ElementAccessExpression
          ).expression.getText(file),
        ) &&
        isAssigned(node)
      )
        add(node, "storage write: an assignment");
    }
    if (ts.isElementAccessExpression(node) && member === undefined) {
      const argument = node.argumentExpression;
      if (!ts.isNumericLiteral(argument) && spec)
        add(node, "computed member in a journey");
    }
    if (ts.isBindingElement(node)) {
      const key = node.propertyName
        ? propertyKey(node.propertyName)
        : ts.isIdentifier(node.name)
          ? node.name.text
          : undefined;
      if (key !== undefined) {
        if (Object.hasOwn(FORBIDDEN_MEMBERS, key))
          add(node, `${FORBIDDEN_MEMBERS[key]}: ${key}`);
        if (key === "request" && kind !== "harness")
          add(node, "API request context: request");
        if (spec && REFLECTIVE.has(key))
          add(node, `reflective access in a journey: ${key}`);
        if (spec && FIXTURES.has(key))
          add(node, `Playwright fixture outside Journey.signIn: ${key}`);
      }
    }
    // Options objects.
    if (
      ts.isPropertyAssignment(node) ||
      ts.isShorthandPropertyAssignment(node)
    ) {
      const key = propertyKey(node.name);
      if (key !== undefined) {
        if (Object.hasOwn(FORBIDDEN_OPTIONS, key))
          add(node, `${FORBIDDEN_OPTIONS[key]}: ${key}`);
        if (kind === "config" && Object.hasOwn(CONFIG_OPTIONS, key))
          add(node, `${CONFIG_OPTIONS[key]}: ${key}`);
        // The harness traces each context it opens; Playwright's own tracing
        // would start first and make the harness's tracing.start fail.
        if (
          kind === "config" &&
          key === "trace" &&
          !(
            ts.isPropertyAssignment(node) &&
            ts.isStringLiteralLike(node.initializer) &&
            node.initializer.text === "off"
          )
        )
          add(node, "Playwright-managed traces: trace");
        if (key === "force" && !folderRemoval(node)) {
          const value = ts.isPropertyAssignment(node) ? node.initializer : node;
          if (value.kind !== ts.SyntaxKind.FalseKeyword)
            add(node, "forced action: force");
        }
      }
    }
    if (ts.isIdentifier(node) && !isPropertyName(node)) {
      if (Object.hasOwn(HIDDEN_CALLS, node.text))
        add(node, `${HIDDEN_CALLS[node.text]}: ${node.text}`);
      if (spec && Object.hasOwn(SPEC_GLOBALS, node.text))
        add(node, `${SPEC_GLOBALS[node.text]}: ${node.text}`);
      if (
        spec &&
        !LANGUAGE_GLOBALS.has(node.text) &&
        !inType(node) &&
        !inPageFunction(node)
      ) {
        const where = origin(node);
        if (where === "none")
          add(node, `undeclared global in a journey: ${node.text}`);
        if (where === "library")
          add(node, `global outside the journey allowlist: ${node.text}`);
      }
    }
    if (ts.isMetaProperty(node) && spec)
      add(node, "module metadata: import.meta");
    if (
      (ts.isStringLiteralLike(node) || ts.isTemplateLiteral(node)) &&
      TOKEN_TEXT.test(node.getText(file))
    )
      add(node, "token or script in a URL");
    // Page functions.
    if (ts.isCallExpression(node) && calleeName(node) === "evaluate") {
      if (!audited(node, file))
        add(node, "page.evaluate without a read-only audit note");
      const [first] = node.arguments;
      if (!first || !isFunctionLike(first))
        add(node, "page.evaluate of code that is not a function literal");
      else
        for (const problem of pageFunctionProblems(first.body))
          add(node, problem);
    }
    ts.forEachChild(node, visit);
  };
  visit(root);
  return found;
}

function isPropertyName(node: ts.Identifier): boolean {
  const parent = node.parent;
  return (
    (ts.isPropertyAccessExpression(parent) && parent.name === node) ||
    (ts.isPropertyAssignment(parent) && parent.name === node) ||
    (ts.isMethodDeclaration(parent) && parent.name === node) ||
    (ts.isPropertyDeclaration(parent) && parent.name === node) ||
    (ts.isBindingElement(parent) && parent.propertyName === node) ||
    ts.isImportSpecifier(parent) ||
    ts.isExportSpecifier(parent) ||
    ts.isQualifiedName(parent)
  );
}

function isAssigned(node: ts.Node): boolean {
  const parent = node.parent;
  return (
    ts.isBinaryExpression(parent) &&
    parent.left === node &&
    parent.operatorToken.kind >= ts.SyntaxKind.FirstAssignment &&
    parent.operatorToken.kind <= ts.SyntaxKind.LastAssignment
  );
}

/** `rmSync(path, { recursive: true, force: true })` removes a folder; it is no forced click. */
function folderRemoval(node: ts.Node): boolean {
  const literal = node.parent;
  const call = literal?.parent;
  return (
    !!call &&
    ts.isCallExpression(call) &&
    ["rm", "rmSync", "rmdir", "rmdirSync"].includes(calleeName(call) ?? "")
  );
}

/**
 * `page.request` and `context.request` send requests with the person's
 * cookies. Only the harness's readiness probe may use one, as
 * `x.request.get(…)`, and never from a journey. `response.request()` is a call
 * and stays allowed.
 */
function requestUse(
  node: ts.Node,
  kind: Kind,
  add: (node: ts.Node, rule: string) => void,
): void {
  const parent = node.parent;
  if (ts.isCallExpression(parent) && parent.expression === node) return;
  // The harness wraps every context's and page's request in its guard.
  if (kind === "harness") return;
  const get =
    ts.isPropertyAccessExpression(parent) &&
    parent.name.text === "get" &&
    ts.isCallExpression(parent.parent) &&
    parent.parent.expression === parent;
  if (get && kind !== "spec") return;
  add(node, "API request context: request");
}

/** Import findings for a journey spec: only ./harness values and @playwright/test types. */
function specImports(file: ts.SourceFile): { node: ts.Node; rule: string }[] {
  const found: { node: ts.Node; rule: string }[] = [];
  for (const statement of file.statements) {
    if (ts.isExportDeclaration(statement) && statement.moduleSpecifier)
      found.push({ node: statement, rule: "re-export in a journey" });
    if (ts.isExportAssignment(statement))
      found.push({ node: statement, rule: "default export in a journey" });
    if (!ts.isImportDeclaration(statement)) continue;
    const source = (statement.moduleSpecifier as ts.StringLiteral).text;
    const clause = statement.importClause;
    if (source === "./harness" || source === "./harness.ts") {
      if (!clause) continue;
      if (clause.name)
        found.push({ node: statement, rule: "default import in a journey" });
      const bindings = clause.namedBindings;
      if (bindings && ts.isNamespaceImport(bindings))
        found.push({ node: statement, rule: "namespace import in a journey" });
      if (bindings && ts.isNamedImports(bindings))
        for (const element of bindings.elements) {
          if (element.propertyName)
            found.push({ node: element, rule: "aliased import in a journey" });
          if ((element.propertyName ?? element.name).text === "test")
            found.push({
              node: element,
              rule: "Playwright test outside the harness: test",
            });
        }
      continue;
    }
    if (source === "@playwright/test") {
      const typesOnly =
        !!clause &&
        (clause.isTypeOnly ||
          (!clause.name &&
            !!clause.namedBindings &&
            ts.isNamedImports(clause.namedBindings) &&
            clause.namedBindings.elements.every((e) => e.isTypeOnly)));
      if (!typesOnly)
        found.push({
          node: statement,
          rule: "Playwright values in a journey (import them from the harness)",
        });
      continue;
    }
    found.push({ node: statement, rule: `import of ${source} in a journey` });
  }
  return found;
}

function inProject(file: ts.SourceFile, root: string): boolean {
  return (
    !file.isDeclarationFile &&
    !file.fileName.split(/[\\/]/).includes("node_modules") &&
    resolve(file.fileName).startsWith(root)
  );
}

/** Top-level statements that run when the module loads. */
function loadTime(file: ts.SourceFile): ts.Node[] {
  return file.statements.filter((statement) => {
    if (
      ts.isFunctionDeclaration(statement) ||
      ts.isInterfaceDeclaration(statement) ||
      ts.isTypeAliasDeclaration(statement)
    )
      return false;
    if (ts.isClassDeclaration(statement))
      return statement.members.some(
        (member) =>
          ts.isClassStaticBlockDeclaration(member) ||
          (ts.isPropertyDeclaration(member) &&
            !!member.initializer &&
            (ts.getCombinedModifierFlags(member) & ts.ModifierFlags.Static) !==
              0),
      );
    if (ts.isVariableStatement(statement))
      return statement.declarationList.declarations.some(
        (declaration) =>
          !!declaration.initializer &&
          !isFunctionLike(declaration.initializer) &&
          !ts.isClassExpression(declaration.initializer),
      );
    return true;
  });
}

export interface LintOptions {
  /** studio/tests/acceptance, read whole. */
  journeys: string;
  /** The Playwright configuration, read whole. */
  config?: string;
  /** Files outside this folder (node_modules, the standard library) are not read. */
  root: string;
}

/** Every finding in the journeys and the code they reach. */
export function lintJourneys(options: LintOptions): Finding[] {
  return analyzeJourneys(options).findings;
}

/** The name a reached top-level statement declares, or its first line. */
function statementName(node: ts.Node, file: ts.SourceFile): string {
  if (
    (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) &&
    node.name
  )
    return node.name.text;
  if (ts.isVariableStatement(node))
    return node.declarationList.declarations
      .map((declaration) => declaration.name.getText(file))
      .join(", ");
  return node.getText(file).split("\n")[0]!.slice(0, 80);
}

export interface Analysis {
  findings: Finding[];
  /** For each file read in part, the top-level statements read. */
  reached: Record<string, string[]>;
}

/** The findings, and what was read of each file outside the journeys. */
export function analyzeJourneys(options: LintOptions): Analysis {
  const journeys = resolve(options.journeys);
  const root = resolve(options.root);
  const files = walk(journeys);
  const config = options.config ? resolve(options.config) : undefined;
  const roots = config ? [...files, config] : files;
  const program = ts.createProgram(roots, {
    allowJs: true,
    checkJs: false,
    jsx: ts.JsxEmit.Preserve,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    target: ts.ScriptTarget.ES2022,
    allowImportingTsExtensions: true,
    resolveJsonModule: true,
    noEmit: true,
    skipLibCheck: true,
    types: [],
  });
  const checker = program.getTypeChecker();
  const whole = new Set(roots.map((path) => resolve(path)));
  const kindOf = (file: ts.SourceFile): Kind => {
    const path = resolve(file.fileName);
    if (path === config) return "config";
    if (!whole.has(path)) return "reached";
    if (path === join(journeys, "harness.ts")) return "harness";
    return /\.spec\.[cm]?[jt]sx?$/.test(path) ? "spec" : "journey";
  };
  const seen = new Set<ts.Node>();
  const loaded = new Set<string>();
  const queue: ts.Node[] = [];
  const reach = (node: ts.Node) => {
    if (seen.has(node)) return;
    seen.add(node);
    queue.push(node);
  };
  const load = (file: ts.SourceFile) => {
    const path = resolve(file.fileName);
    if (loaded.has(path) || !inProject(file, root)) return;
    loaded.add(path);
    (whole.has(path) ? file.statements : loadTime(file)).forEach(reach);
  };
  for (const path of roots) {
    const file = program.getSourceFile(path);
    if (file) load(file);
  }
  const findings: Finding[] = [];
  const record = (node: ts.Node, rule: string) => {
    const file = node.getSourceFile();
    findings.push({
      file: relative(root, file.fileName).split(sep).join("/"),
      line: file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1,
      rule,
    });
  };
  for (const path of files) {
    const file = program.getSourceFile(path);
    if (file && kindOf(file) === "spec")
      for (const problem of specImports(file))
        record(problem.node, problem.rule);
  }
  while (queue.length) {
    const node = queue.shift()!;
    const file = node.getSourceFile();
    for (const problem of nodeProblems(
      node,
      file,
      kindOf(file),
      (identifier) => {
        const symbol = checker.getSymbolAtLocation(identifier);
        const declarations = symbol?.declarations ?? [];
        if (!declarations.length) return "none";
        return declarations.some((declaration) => {
          const owner = declaration.getSourceFile();
          return (
            !owner.isDeclarationFile && !owner.fileName.includes("node_modules")
          );
        })
          ? "project"
          : "library";
      },
    ))
      record(problem.node, problem.rule);
    const follow = (child: ts.Node): void => {
      if (
        (ts.isImportDeclaration(child) || ts.isExportDeclaration(child)) &&
        child.moduleSpecifier
      ) {
        const target = checker.getSymbolAtLocation(child.moduleSpecifier);
        const declaration =
          target?.valueDeclaration ?? target?.declarations?.[0];
        if (declaration && ts.isSourceFile(declaration)) load(declaration);
      }
      if (ts.isIdentifier(child) || ts.isPrivateIdentifier(child)) {
        let symbol = checker.getSymbolAtLocation(child);
        if (symbol && symbol.flags & ts.SymbolFlags.Alias)
          symbol = checker.getAliasedSymbol(symbol);
        for (const declaration of symbol?.declarations ?? []) {
          const owner = declaration.getSourceFile();
          if (!inProject(owner, root)) continue;
          load(owner);
          if (ts.isSourceFile(declaration)) continue;
          if (!whole.has(resolve(owner.fileName))) reach(topLevel(declaration));
        }
      }
      ts.forEachChild(child, follow);
    };
    follow(node);
  }
  const unique = new Map(
    findings.map((finding) => [
      `${finding.file}:${finding.line}:${finding.rule}`,
      finding,
    ]),
  );
  const reached: Record<string, string[]> = {};
  for (const node of seen) {
    const file = node.getSourceFile();
    if (whole.has(resolve(file.fileName))) continue;
    const name = relative(root, file.fileName).split(sep).join("/");
    (reached[name] ??= []).push(statementName(node, file));
  }
  return {
    findings: [...unique.values()].sort(
      (a, b) => a.file.localeCompare(b.file) || a.line - b.line,
    ),
    reached,
  };
}

/** Writes `files` (paths relative to `folder`) and lints `folder/acceptance` with its harness. */
export function lintTree(
  folder: string,
  files: Record<string, string>,
): Finding[] {
  for (const [path, text] of Object.entries(files)) {
    const target = join(folder, path);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, text);
  }
  const harness = join(folder, "acceptance", "harness.ts");
  if (!existsSync(harness)) {
    mkdirSync(dirname(harness), { recursive: true });
    writeFileSync(
      harness,
      "export function journey(id: string, body: (j: unknown) => Promise<void>): void {}\n" +
        "export const expect = (value: unknown) => value;\n",
    );
  }
  return lintJourneys({ journeys: join(folder, "acceptance"), root: folder });
}
