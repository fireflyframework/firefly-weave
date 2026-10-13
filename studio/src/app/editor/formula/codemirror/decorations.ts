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
// The formula editor's drawing: colors over the shared tokens, references
// as atomic pills (one Backspace removes a pill, the caret steps over it),
// the parse error underlined, and an error at the end shown as a red caret.
import {
  Facet,
  StateEffect,
  StateField,
  type EditorState,
  type Range,
} from "@codemirror/state";
import {
  Decoration,
  EditorView,
  WidgetType,
  type DecorationSet,
} from "@codemirror/view";
import { iconDrawing } from "../../../icon";
import { highlightSpans, referenceSpans, type ReferenceSpan } from "../spans";
import { tokenize, type TokenMode } from "../tokens";

export interface PillLook {
  /** The breadcrumb's parts: "Input", "customerId". */
  parts: readonly string[];
  icon: string;
  /** The formula text, shown on hover. */
  title: string;
  /** "reference Input customerId". */
  name: string;
}
export interface DecorationConfig {
  mode: TokenMode;
  look(pointer: string): PillLook;
  pick(span: ReferenceSpan): void;
  error(): { from: number; to: number } | null;
}
export const decorationConfig = Facet.define<
  DecorationConfig,
  DecorationConfig
>({
  combine: (values) => values[0],
});
/** Redraw: the pills' looks or the error changed outside the text. */
export const refreshDecorations = StateEffect.define<null>();

const SVG = "http://www.w3.org/2000/svg";
function icon(name: string): SVGSVGElement {
  const svg = document.createElementNS(SVG, "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24",
    width: "16",
    height: "16",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.5",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  }))
    svg.setAttribute(key, value);
  for (const node of iconDrawing(name)) {
    const element = document.createElementNS(SVG, node.tag);
    for (const [key, value] of Object.entries(node.attrs))
      element.setAttribute(key, String(value));
    svg.append(element);
  }
  return svg;
}

class PillWidget extends WidgetType {
  constructor(
    readonly look: PillLook,
    readonly pointer: string,
  ) {
    super();
  }
  override eq(other: PillWidget): boolean {
    return (
      other.pointer === this.pointer &&
      JSON.stringify(other.look) === JSON.stringify(this.look)
    );
  }
  override toDOM(view: EditorView): HTMLElement {
    const pill = document.createElement("span");
    pill.className = "ref-pill cm-formula-pill";
    pill.title = this.look.title;
    pill.setAttribute("aria-label", this.look.name);
    pill.append(icon(this.look.icon));
    this.look.parts.forEach((part, i) => {
      if (i) {
        const separator = document.createElement("span");
        separator.className = "ref-sep";
        separator.setAttribute("aria-hidden", "true");
        separator.textContent = "›";
        pill.append(separator);
      }
      const text = document.createElement("span");
      text.textContent = part;
      pill.append(text);
    });
    pill.addEventListener("dblclick", (event) => {
      event.preventDefault();
      const from = view.posAtDOM(pill);
      const span = view.state
        .field(formulaState)
        .spans.find((item) => item.from === from);
      if (span) view.state.facet(decorationConfig).pick(span);
    });
    return pill;
  }
  override ignoreEvent(event: Event): boolean {
    return event.type !== "mousedown";
  }
}

class CaretWidget extends WidgetType {
  override toDOM(): HTMLElement {
    const caret = document.createElement("span");
    caret.className = "cm-formula-caret";
    caret.setAttribute("aria-hidden", "true");
    return caret;
  }
}

interface Drawn {
  decorations: DecorationSet;
  pills: DecorationSet;
  spans: ReferenceSpan[];
}

function draw(state: EditorState): Drawn {
  const config = state.facet(decorationConfig);
  const tokens = tokenize(state.doc.toString(), config.mode);
  const spans = referenceSpans(tokens);
  const inPill = (from: number, to: number) =>
    spans.some((span) => from < span.to && to > span.from);
  const ranges: Range<Decoration>[] = [];
  for (const span of highlightSpans(tokens))
    if (span.to > span.from && !inPill(span.from, span.to))
      ranges.push(
        Decoration.mark({ class: `cm-formula-${span.kind}` }).range(
          span.from,
          span.to,
        ),
      );
  const error = config.error();
  if (error && error.to > error.from)
    ranges.push(
      Decoration.mark({ class: "cm-formula-error" }).range(
        error.from,
        Math.min(error.to, state.doc.length),
      ),
    );
  else if (error)
    ranges.push(
      Decoration.widget({ widget: new CaretWidget(), side: 1 }).range(
        Math.min(error.from, state.doc.length),
      ),
    );
  const pills = spans.map((span) =>
    Decoration.replace({
      widget: new PillWidget(config.look(span.pointer), span.pointer),
    }).range(span.from, span.to),
  );
  return {
    decorations: Decoration.set([...ranges, ...pills], true),
    pills: Decoration.set(pills, true),
    spans,
  };
}

export const formulaState = StateField.define<Drawn>({
  create: draw,
  update: (value, transaction) =>
    transaction.docChanged ||
    transaction.effects.some((effect) => effect.is(refreshDecorations))
      ? draw(transaction.state)
      : value,
  provide: (field) => [
    EditorView.decorations.from(field, (value) => value.decorations),
    EditorView.atomicRanges.of((view) => view.state.field(field).pills),
  ],
});

/** CodeMirror's own parts, drawn with Studio's tokens (field chrome lives in params.css). */
export const formulaTheme = EditorView.theme({
  "&": {
    color: "var(--text)",
    backgroundColor: "transparent",
    flex: "1",
    minWidth: "0",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": {
    fontFamily: "inherit",
    lineHeight: "inherit",
    overflowX: "auto",
  },
  ".cm-content": { padding: "0", caretColor: "var(--text)" },
  ".cm-line": { padding: "0" },
  ".cm-placeholder": { color: "var(--subtle)" },
  ".cm-formula-keyword, .cm-formula-operator, .cm-formula-brace": {
    color: "var(--syntax-keyword)",
  },
  ".cm-formula-function": { color: "var(--text)" },
  ".cm-formula-string": { color: "var(--syntax-string)" },
  ".cm-formula-number": { color: "var(--syntax-number)" },
  ".cm-formula-error": {
    textDecorationLine: "underline",
    textDecorationStyle: "wavy",
    textDecorationColor: "var(--danger)",
    textUnderlineOffset: "3px",
  },
  ".cm-formula-caret": {
    borderLeft: "2px solid var(--danger)",
    marginLeft: "1px",
  },
});
