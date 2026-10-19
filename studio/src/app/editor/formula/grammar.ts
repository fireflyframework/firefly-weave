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
// The formula language: text that maps one to one to Weave expression trees.
// Precedence, lowest first: or, and, not, comparisons (never chained),
// primaries. `not (` is the prefix operator over a parenthesized operand, so
// `not (a and b) == c` means not((a and b) == c), as the printer writes it.
// Errors carry a WV-FORMULA code (Studio's own, never sent anywhere), the
// source range and plain words. Loop roots and the text functions are
// checked against the loops around the field and the target's features.
import type { Expression } from "../ndv/registry";
import { columnOf, tokenize, type Token } from "./tokens";

export interface Span {
  from: number;
  to: number;
}
export interface FormulaError {
  code: string;
  message: string;
  from: number;
  to: number;
}
export interface ParseOptions {
  /** The target's language features: text.concat, text.join. */
  features: readonly string[];
  /** The loops around the field, innermost first; empty outside loops. */
  loops: readonly string[];
}
export type ParseResult =
  | { kind: "empty" }
  | { kind: "ok"; expression: Expression; spans: WeakMap<object, Span> }
  | { kind: "error"; error: FormulaError };
export type TemplatePart = { text: string } | { node: Expression };

export const ROOTS = new Set(["input", "steps", "item", "index", "loops"]);
export const LOOP_ROOTS = new Set(["item", "index", "loops"]);
export const COMPARISONS: Record<string, string> = {
  "==": "eq",
  "!=": "ne",
  "<": "lt",
  "<=": "lte",
  ">": "gt",
  ">=": "gte",
};
export const SYMBOLS: Record<string, string> = Object.fromEntries(
  Object.entries(COMPARISONS).map(([symbol, name]) => [name, symbol]),
);
export const BASE_FUNCTIONS = [
  "exists",
  "coalesce",
  "contains",
  "notContains",
  "in",
  "notIn",
  "startsWith",
  "endsWith",
] as const;
export const ARITY: Record<string, readonly [number, number]> = {
  eq: [2, 2],
  ne: [2, 2],
  lt: [2, 2],
  lte: [2, 2],
  gt: [2, 2],
  gte: [2, 2],
  contains: [2, 2],
  notContains: [2, 2],
  in: [2, 2],
  notIn: [2, 2],
  startsWith: [2, 2],
  endsWith: [2, 2],
  join: [2, 2],
  not: [1, 1],
  exists: [1, 1],
  and: [1, Infinity],
  or: [1, Infinity],
  coalesce: [1, Infinity],
  concat: [1, Infinity],
};
export const FUNCTIONS = new Set(Object.keys(ARITY));
const POINTER = /^(?:\/(?:[^~/]|~[01])*)*$/;
const INDEX = /^(?:0|[1-9][0-9]*)$/;

export const escapeSegment = (segment: string): string =>
  segment.replace(/~/g, "~0").replace(/\//g, "~1");
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
export const isTextLiteral = (node: unknown): node is { literal: string } =>
  isRecord(node) &&
  Object.keys(node).length === 1 &&
  typeof node["literal"] === "string";
export const isReference = (node: unknown): node is { ref: string } =>
  isRecord(node) &&
  Object.keys(node).length === 1 &&
  typeof node["ref"] === "string";
const isLiteral = (node: unknown): node is { literal: unknown } =>
  isRecord(node) && Object.keys(node).length === 1 && "literal" in node;
/** Sets a key as an own property, even "__proto__". */
function put<T>(target: Record<string, T>, key: string, value: T): void {
  Object.defineProperty(target, key, {
    value,
    enumerable: true,
    writable: true,
    configurable: true,
  });
}

/** "a, b and c". */
function listWords(items: readonly string[]): string {
  return items.length > 1
    ? `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`
    : (items[0] ?? "");
}
/** The functions a formula may call, in the order the error lists them. */
export function functionList(features: readonly string[]): string[] {
  return [
    ...BASE_FUNCTIONS,
    ...(features.includes("text.concat") ? ["concat"] : []),
    ...(features.includes("text.join") ? ["join"] : []),
  ];
}

/** The platform's template_expression: adjacent text merged, empty text dropped, text alone a literal. */
export function templateExpression(
  parts: readonly TemplatePart[],
  loneReference = false,
): Expression {
  const args: Expression[] = [];
  for (const part of parts) {
    if ("node" in part) args.push(part.node);
    else if (part.text) {
      const last = args[args.length - 1];
      if (last && isTextLiteral(last))
        args[args.length - 1] = { literal: last.literal + part.text };
      else args.push({ literal: part.text });
    }
  }
  if (args.every(isTextLiteral))
    return {
      literal: args.map((arg) => (arg as { literal: string }).literal).join(""),
    };
  if (loneReference && args.length === 1 && isReference(args[0]))
    return args[0];
  return { op: { name: "concat", args } };
}

class Stop extends Error {
  constructor(readonly error: FormulaError) {
    super(error.code);
  }
}

/**
 * How deeply formulas may nest while being read. Workflows allow 32 levels;
 * the bound only keeps a pasted or runaway text from exhausting the stack.
 */
const MAX_NESTING = 256;

class Parser {
  private at = 0;
  private depth = 0;
  readonly spans = new WeakMap<object, Span>();
  constructor(
    private readonly source: string,
    private readonly tokens: Token[],
    private readonly options: ParseOptions,
    private readonly end: number = source.length,
  ) {}

  private peek(offset = 0): Token | undefined {
    return this.tokens[this.at + offset];
  }
  private next(): Token {
    return this.tokens[this.at++];
  }
  private previousEnd(): number {
    return this.tokens[this.at - 1]?.to ?? 0;
  }
  private isPunct(text: string, offset = 0): boolean {
    const token = this.peek(offset);
    return token?.type === "punct" && token.text === text;
  }
  private node<T extends object>(value: T, from: number): T {
    this.spans.set(value, { from, to: this.previousEnd() });
    return value;
  }
  private fail(code: string, message: string, from: number, to: number): never {
    throw new Stop({ code, message, from, to });
  }
  private unexpected(token: Token): never {
    if (token.type === "error")
      this.fail(token.code!, token.message!, token.from, token.to);
    if (token.type === "assign")
      this.fail(
        "WV-FORMULA-ASSIGN",
        "Use `==` to compare. A single `=` is not an operator.",
        token.from,
        token.to,
      );
    this.fail(
      "WV-FORMULA-TOKEN",
      `Unexpected \`${token.text}\` at column ${columnOf(this.source, token.from)}.`,
      token.from,
      token.to,
    );
  }
  private endsEarly(): never {
    const last = this.tokens[this.at - 1];
    const words =
      last?.type === "punct" && last.text === "."
        ? "Add a field name after `.`."
        : last?.type === "open"
          ? "Add a value inside the braces."
          : `Add a value after \`${last?.text ?? ""}\`.`;
    this.fail(
      "WV-FORMULA-EOF",
      `The formula ends too early. ${words}`,
      this.end,
      this.end,
    );
  }
  private want(): Token {
    return this.peek() ?? this.endsEarly();
  }
  private expectPunct(text: string): Token {
    const token = this.want();
    if (token.type !== "punct" || token.text !== text) this.unexpected(token);
    return this.next();
  }

  formula(): Expression {
    const node = this.orExpr();
    const rest = this.peek();
    if (rest) this.unexpected(rest);
    return node;
  }

  templateParts(): TemplatePart[] {
    const parts: TemplatePart[] = [];
    for (let token = this.peek(); token; token = this.peek()) {
      if (token.type === "text") {
        this.next();
        parts.push({ text: token.value as string });
      } else if (token.type === "open")
        parts.push({ node: this.placeholder() });
      else this.unexpected(token);
    }
    return parts;
  }

  private placeholder(): Expression {
    this.next();
    if (this.peek()?.type === "close") this.endsEarly();
    const node = this.orExpr();
    const close = this.peek();
    if (!close)
      this.fail(
        "WV-FORMULA-EOF",
        "The formula ends too early. Close the placeholder with `}}`.",
        this.end,
        this.end,
      );
    if (close.type !== "close") this.unexpected(close);
    this.next();
    return node;
  }

  private chain(name: "or" | "and", next: () => Expression): Expression {
    const from = this.want().from;
    const args = [next()];
    const alias = name === "or" ? "||" : "&&";
    for (
      let token = this.peek();
      token &&
      ((token.type === "name" && token.text === name) ||
        (token.type === "op" && token.text === alias));
      token = this.peek()
    ) {
      this.next();
      args.push(next());
    }
    return args.length === 1
      ? args[0]
      : this.node({ op: { name, args } }, from);
  }
  /** Reads one nested part, refusing text nested beyond MAX_NESTING. */
  private nested<T>(token: Token, read: () => T): T {
    if (this.depth >= MAX_NESTING)
      this.fail(
        "WV-FORMULA-TOKEN",
        "This formula nests too deeply.",
        token.from,
        token.to,
      );
    this.depth++;
    try {
      return read();
    } finally {
      this.depth--;
    }
  }
  private orExpr(): Expression {
    return this.nested(this.want(), () =>
      this.chain("or", () => this.andExpr()),
    );
  }
  private andExpr(): Expression {
    return this.chain("and", () => this.notExpr());
  }

  private notExpr(): Expression {
    const token = this.want();
    const isNot =
      (token.type === "name" && token.text === "not") ||
      (token.type === "op" && token.text === "!");
    if (!isNot) return this.compare();
    this.next();
    if (this.isPunct("(")) {
      const open = this.next();
      if (this.isPunct(")")) {
        this.next();
        this.fail(
          "WV-FORMULA-ARITY",
          "`not` takes 1 value; this has 0.",
          token.from,
          this.previousEnd(),
        );
      }
      const items = [this.orExpr()];
      while (this.isPunct(",")) {
        this.next();
        items.push(this.orExpr());
      }
      this.expectPunct(")");
      if (items.length !== 1)
        this.fail(
          "WV-FORMULA-ARITY",
          `\`not\` takes 1 value; this has ${items.length}.`,
          token.from,
          this.previousEnd(),
        );
      const operand = this.compareRest(items[0], open.from);
      return this.node({ op: { name: "not", args: [operand] } }, token.from);
    }
    const operand = this.nested(token, () => this.notExpr());
    return this.node({ op: { name: "not", args: [operand] } }, token.from);
  }

  private compare(): Expression {
    const from = this.want().from;
    return this.compareRest(this.primary(), from);
  }
  private compareRest(left: Expression, from: number): Expression {
    const token = this.peek();
    if (token?.type === "assign") this.unexpected(token);
    if (token?.type !== "op" || !(token.text in COMPARISONS)) return left;
    this.next();
    const right = this.primary();
    const again = this.peek();
    if (again?.type === "op" && again.text in COMPARISONS)
      this.fail(
        "WV-FORMULA-CHAIN",
        "Comparisons can't be chained. Use `and`: `a < b and b < c`.",
        again.from,
        again.to,
      );
    if (again?.type === "assign") this.unexpected(again);
    return this.node(
      { op: { name: COMPARISONS[token.text], args: [left, right] } },
      from,
    );
  }

  private primary(): Expression {
    const token = this.want();
    switch (token.type) {
      case "string":
      case "number":
        this.next();
        return this.node({ literal: token.value }, token.from);
      case "tick":
        return this.backtick();
      case "punct":
        if (token.text === "[") return this.list();
        if (token.text === "{") return this.record();
        if (token.text === "(") {
          this.next();
          const inner = this.orExpr();
          this.expectPunct(")");
          return inner;
        }
        return this.unexpected(token);
      case "name":
        return this.named(token);
      default:
        return this.unexpected(token);
    }
  }

  private named(token: Token): Expression {
    const name = token.text;
    const call = this.isPunct("(", 1);
    if (name === "true" || name === "false" || name === "null") {
      this.next();
      return this.node(
        { literal: name === "null" ? null : name === "true" },
        token.from,
      );
    }
    if (ROOTS.has(name) && !call) return this.reference();
    if (name === "ref" && call) return this.refCall();
    if (name === "array" && this.isPunct("[", 1)) return this.explicitArray();
    if (name === "object" && this.isPunct("{", 1)) return this.explicitObject();
    if (call && FUNCTIONS.has(name)) return this.call();
    if (call)
      this.fail(
        "WV-FORMULA-FUNCTION",
        `Unknown function \`${name}\`. Formulas support ${listWords(functionList(this.options.features))}.`,
        token.from,
        token.to,
      );
    if (name === "and" || name === "or" || name === "not")
      this.unexpected(token);
    this.fail(
      "WV-FORMULA-NAME",
      `Unknown name \`${name}\`. Data references start with \`input.\` or \`steps.\`, and inside a loop with \`item\`, \`index\` or \`loops.\`.`,
      token.from,
      token.to,
    );
  }

  private reference(): Expression {
    const root = this.next();
    if (LOOP_ROOTS.has(root.text) && !this.options.loops.length)
      this.fail(
        "WV-FORMULA-LOOP",
        `\`${root.text}\` is only available inside a loop.`,
        root.from,
        root.to,
      );
    const segments = [root.text];
    for (;;) {
      if (this.isPunct(".")) {
        this.next();
        const segment = this.want();
        if (segment.type !== "segment") this.unexpected(segment);
        this.next();
        segments.push(segment.text);
      } else if (this.isPunct("[")) {
        this.next();
        const key = this.want();
        if (key.type === "number" && INDEX.test(key.text))
          segments.push(key.text);
        else if (key.type === "string") segments.push(key.value as string);
        else this.unexpected(key);
        this.next();
        this.expectPunct("]");
      } else break;
      if (
        root.text === "loops" &&
        segments.length === 2 &&
        !this.options.loops.includes(segments[1])
      )
        this.fail(
          "WV-FORMULA-LOOP",
          `\`loops.${segments[1]}\` is only available inside the loop ${segments[1]}.`,
          root.from,
          this.previousEnd(),
        );
    }
    return this.node(
      { ref: `/${segments.map(escapeSegment).join("/")}` },
      root.from,
    );
  }

  private refCall(): Expression {
    const name = this.next();
    this.expectPunct("(");
    const pointer = this.want();
    if (pointer.type !== "string") this.unexpected(pointer);
    this.next();
    if (!POINTER.test(pointer.value as string))
      this.fail(
        "WV-FORMULA-TOKEN",
        '`ref` needs a JSON Pointer, like ref("/input/name").',
        pointer.from,
        pointer.to,
      );
    this.expectPunct(")");
    return this.node({ ref: pointer.value as string }, name.from);
  }

  private items(close: string, trailing: boolean): Expression[] {
    const items: Expression[] = [];
    if (this.isPunct(close)) return items;
    for (;;) {
      items.push(this.orExpr());
      if (!this.isPunct(",")) break;
      this.next();
      if (trailing && this.isPunct(close)) break;
    }
    const token = this.want();
    if (token.type !== "punct" || token.text !== close) this.unexpected(token);
    return items;
  }

  private list(): Expression {
    const open = this.next();
    const items = this.items("]", true);
    this.next();
    return this.node(
      items.every(isLiteral)
        ? {
            literal: items.map(
              (item) => (item as { literal: unknown }).literal,
            ),
          }
        : { array: items },
      open.from,
    );
  }

  private explicitArray(): Expression {
    const name = this.next();
    this.next();
    const items = this.items("]", false);
    this.next();
    return this.node({ array: items }, name.from);
  }

  private entries(trailing: boolean): [string, Expression][] {
    const entries: [string, Expression][] = [];
    const seen = new Set<string>();
    if (this.isPunct("}")) return entries;
    for (;;) {
      const key = this.want();
      if (key.type !== "name" && key.type !== "string") this.unexpected(key);
      const text = key.type === "name" ? key.text : (key.value as string);
      this.next();
      if (seen.has(text))
        this.fail(
          "WV-FORMULA-TOKEN",
          `The key \`${text}\` appears twice.`,
          key.from,
          key.to,
        );
      seen.add(text);
      this.expectPunct(":");
      entries.push([text, this.orExpr()]);
      if (!this.isPunct(",")) break;
      this.next();
      if (trailing && this.isPunct("}")) break;
    }
    const token = this.want();
    if (token.type !== "punct" || token.text !== "}") this.unexpected(token);
    return entries;
  }

  private record(): Expression {
    const open = this.next();
    const entries = this.entries(true);
    this.next();
    const literal = entries.every(([, value]) => isLiteral(value));
    const body: Record<string, unknown> = {};
    for (const [key, value] of entries)
      put(body, key, literal ? (value as { literal: unknown }).literal : value);
    return this.node(literal ? { literal: body } : { object: body }, open.from);
  }

  private explicitObject(): Expression {
    const name = this.next();
    this.next();
    const entries = this.entries(false);
    this.next();
    const body: Record<string, Expression> = {};
    for (const [key, value] of entries) put(body, key, value);
    return this.node({ object: body }, name.from);
  }

  private call(): Expression {
    const name = this.next();
    const fname = name.text;
    if (fname === "concat" && !this.options.features.includes("text.concat"))
      this.fail(
        "WV-FORMULA-TEMPLATE",
        "`concat` needs a platform that supports text templates (text.concat).",
        name.from,
        name.to,
      );
    if (fname === "join" && !this.options.features.includes("text.join"))
      this.fail(
        "WV-FORMULA-TEMPLATE",
        "`join` needs a platform that supports joining lists (text.join).",
        name.from,
        name.to,
      );
    this.next();
    const args = this.items(")", false);
    this.next();
    const end = this.previousEnd();
    if (fname === "exists" && (args.length !== 1 || !isReference(args[0])))
      this.fail(
        "WV-FORMULA-EXISTS",
        "`exists` takes one data reference, like `exists(input.email)`.",
        name.from,
        end,
      );
    const [min, max] = ARITY[fname];
    if (args.length < min || args.length > max) {
      const values = (count: number) =>
        `${count} ${count === 1 ? "value" : "values"}`;
      this.fail(
        "WV-FORMULA-ARITY",
        min === max
          ? `\`${fname}\` takes ${values(min)}; this has ${args.length}.`
          : `\`${fname}\` takes at least ${values(min)}; this has ${args.length}.`,
        name.from,
        end,
      );
    }
    return this.node({ op: { name: fname, args } }, name.from);
  }

  private backtick(): Expression {
    const tick = this.next();
    if (!this.options.features.includes("text.concat"))
      this.fail(
        "WV-FORMULA-TEMPLATE",
        "Text templates need a platform that supports them (text.concat).",
        tick.from,
        tick.to,
      );
    const parts: TemplatePart[] = [];
    for (;;) {
      const token = this.peek();
      if (!token)
        this.fail(
          "WV-FORMULA-STRING",
          "This template isn't closed. Add a closing `` ` ``.",
          tick.from,
          this.end,
        );
      if (token.type === "tick") {
        this.next();
        break;
      }
      if (token.type === "text") {
        this.next();
        parts.push({ text: token.value as string });
      } else if (token.type === "open")
        parts.push({ node: this.placeholder() });
      else this.unexpected(token);
    }
    return this.node(templateExpression(parts), tick.from);
  }
}

/** The region inside one outer `{{ … }}` pair, when the whole text is one. */
function outerBraces(source: string): { from: number; to: number } | null {
  const from = source.length - source.trimStart().length;
  const to = source.trimEnd().length;
  if (
    to - from < 4 ||
    !source.startsWith("{{", from) ||
    !source.endsWith("}}", to)
  )
    return null;
  return { from: from + 2, to: to - 2 };
}

function run(
  source: string,
  tokens: Token[],
  options: ParseOptions,
  end: number,
  read: (parser: Parser) => Expression,
): ParseResult {
  const parser = new Parser(source, tokens, options, end);
  try {
    return { kind: "ok", expression: read(parser), spans: parser.spans };
  } catch (error) {
    if (error instanceof Stop) return { kind: "error", error: error.error };
    throw error;
  }
}

/** A formula field's text; one outer `{{ … }}` pair is read as n8n writes it. */
export function parseFormula(
  source: string,
  options: ParseOptions,
): ParseResult {
  const outer = outerBraces(source);
  const from = outer?.from ?? 0;
  const to = outer?.to ?? source.length;
  if (!source.slice(from, to).trim()) return { kind: "empty" };
  return run(
    source,
    tokenize(source, "formula", from, to),
    options,
    to,
    (parser) => parser.formula(),
  );
}

/** A template field's text: text with `{{ formula }}` placeholders. */
export function parseTemplate(
  source: string,
  options: ParseOptions,
): ParseResult {
  return run(
    source,
    tokenize(source, "template"),
    options,
    source.length,
    (parser) => templateExpression(parser.templateParts(), true),
  );
}
