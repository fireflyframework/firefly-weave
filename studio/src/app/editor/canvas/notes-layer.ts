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
// Sticky notes on the canvas: Markdown when shown, a text box while edited,
// six colors, resized from the edges, moved by drag, arrow keys or Move…,
// and drawn behind the steps. The canvas owns the sidecar; this layer draws
// notes and reports what the person did to one.
import {
  ChangeDetectionStrategy,
  Component,
  Directive,
  ElementRef,
  Injector,
  afterNextRender,
  effect,
  inject,
  input,
  output,
} from "@angular/core";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import {
  MAX_NOTE_TEXT,
  NOTE_COLORS,
  NOTE_HEIGHT,
  NOTE_SIZES,
  NOTE_WIDTH,
  type NoteColor,
} from "../state/canvas-sidecar";
import { parseNote, renderNote } from "./note-markdown";

export interface NoteView {
  id: string;
  text: string;
  color: NoteColor;
  /** Where it is drawn, in canvas units: an anchored note follows its step. */
  x: number;
  y: number;
  width: number;
  height: number;
  /** The step it follows, or "". */
  anchor: string;
  title: string;
}
export type NoteEdit =
  | { kind: "text"; id: string; text: string }
  | { kind: "color"; id: string; color: NoteColor }
  | {
      kind: "box";
      id: string;
      x: number;
      y: number;
      width: number;
      height: number;
    }
  | { kind: "remove"; id: string }
  | { kind: "place"; id: string };

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}
type Grip = "move" | "e" | "s" | "se";
const COLOR_LABELS: Readonly<Record<NoteColor, string>> = {
  yellow: "Yellow",
  blue: "Blue",
  green: "Green",
  pink: "Pink",
  purple: "Purple",
  gray: "Gray",
};
const clamp = (value: number, range: { min: number; max: number }) =>
  Math.min(range.max, Math.max(range.min, value));

/** Draws a note's Markdown as DOM nodes, never as HTML. */
@Directive({ selector: "[weaveNoteBody]", standalone: true })
export class NoteBody {
  text = input.required<string>({ alias: "weaveNoteBody" });
  private readonly element: HTMLElement = inject(ElementRef).nativeElement;
  constructor() {
    effect(() => {
      this.element.replaceChildren(
        renderNote(document, parseNote(this.text())),
      );
    });
  }
}

@Component({
  selector: "weave-notes-layer",
  standalone: true,
  imports: [RowMenu, NoteBody],
  changeDetection: ChangeDetectionStrategy.Eager,
  host: { "[style.--zoom]": "zoom()" },
  template: `@for (note of notes(); track note.id) {
    @let b = box(note);
    <div
      class="sticky-note f-drag-blocker"
      role="group"
      aria-roledescription="sticky note"
      tabindex="0"
      [attr.aria-label]="'Note: ' + note.title"
      [attr.data-note]="note.id"
      [attr.data-color]="note.color"
      [class.editing]="editing() === note.id"
      [style.left.px]="b.x"
      [style.top.px]="b.y"
      [style.width.px]="b.width"
      [style.height.px]="b.height"
      (dblclick)="startEdit(note)"
      (pointerdown)="press($event, note)"
      (pointermove)="move($event)"
      (pointerup)="release($event)"
      (pointercancel)="cancel()"
    >
      @if (editing() === note.id) {
        <textarea
          class="sticky-note-text"
          aria-label="Note text"
          [attr.maxlength]="maxText"
          [value]="note.text"
          (keydown)="textKey($event, note)"
          (blur)="commit($event, note)"
        ></textarea>
      } @else if (note.text) {
        <div class="sticky-note-body" [weaveNoteBody]="note.text"></div>
      } @else {
        <p class="sticky-note-empty">Double-click to write. Markdown works.</p>
      }
      <div
        class="sticky-note-toolbar"
        role="toolbar"
        [attr.aria-label]="'Commands for note ' + note.title"
      >
        <weave-row-menu
          icon="stickyNote"
          [label]="'Color of note ' + note.title"
          [items]="colorItems(note)"
        />
        <weave-row-menu
          [label]="'More commands for note ' + note.title"
          [items]="moreItems(note)"
        />
      </div>
      <span
        class="note-grip note-grip-e"
        data-grip="e"
        aria-hidden="true"
      ></span>
      <span
        class="note-grip note-grip-s"
        data-grip="s"
        aria-hidden="true"
      ></span>
      <span
        class="note-grip note-grip-se"
        data-grip="se"
        aria-hidden="true"
      ></span>
    </div>
  }`,
})
export class NotesLayer {
  notes = input.required<NoteView[]>();
  zoom = input.required<number>();
  locked = input(false);
  /** The note whose text is being edited, or "". */
  editing = input("");
  edit = output<NoteEdit>();
  editingChange = output<string>();
  readonly maxText = MAX_NOTE_TEXT;
  private readonly element: HTMLElement = inject(ElementRef).nativeElement;
  private readonly injector = inject(Injector);
  private drag: {
    id: string;
    grip: Grip;
    start: { x: number; y: number };
    box: Box;
    pointer: number;
    moved: boolean;
  } | null = null;
  /** Where a note is drawn while it is dragged or resized. */
  preview: ({ id: string } & Box) | null = null;

  constructor() {
    // Editing starts with the caret at the end of the text.
    effect(() => {
      const id = this.editing();
      if (!id) return;
      afterNextRender(
        () => {
          const box = this.element.querySelector<HTMLTextAreaElement>(
            `[data-note="${CSS.escape(id)}"] textarea`,
          );
          box?.focus();
          box?.setSelectionRange(box.value.length, box.value.length);
        },
        { injector: this.injector },
      );
    });
  }

  box(note: NoteView): Box {
    return this.preview?.id === note.id ? this.preview : note;
  }
  colorItems(note: NoteView): RowMenuItem[] {
    return NOTE_COLORS.map((color) => ({
      label: COLOR_LABELS[color],
      ...(color === note.color ? { detail: "Current color" } : {}),
      disabled: this.locked(),
      run: () => this.edit.emit({ kind: "color", id: note.id, color }),
    }));
  }
  moreItems(note: NoteView): RowMenuItem[] {
    const locked = this.locked();
    const size = (
      name: keyof typeof NOTE_SIZES,
      label: string,
    ): RowMenuItem => ({
      label,
      disabled: locked,
      run: () =>
        this.edit.emit({
          kind: "box",
          id: note.id,
          x: note.x,
          y: note.y,
          ...NOTE_SIZES[name],
        }),
    });
    return [
      {
        label: "Edit text",
        disabled: locked,
        run: () => this.editingChange.emit(note.id),
      },
      size("small", "Size: small"),
      size("medium", "Size: medium"),
      size("large", "Size: large"),
      {
        label: "Move…",
        disabled: locked,
        run: () => this.edit.emit({ kind: "place", id: note.id }),
      },
      {
        label: "Delete note",
        danger: true,
        disabled: locked,
        run: () => this.edit.emit({ kind: "remove", id: note.id }),
      },
    ];
  }
  startEdit(note: NoteView) {
    if (!this.locked()) this.editingChange.emit(note.id);
  }
  /** A press on a note may move it; on an edge, may resize it. */
  press(event: PointerEvent, note: NoteView) {
    if (event.button !== 0 || this.locked() || this.editing() === note.id)
      return;
    const target = event.target as Element;
    if (target.closest("textarea, a, button, .row-menu, .sticky-note-toolbar"))
      return;
    const grip = (target.closest("[data-grip]")?.getAttribute("data-grip") ??
      "move") as Grip;
    this.drag = {
      id: note.id,
      grip,
      start: { x: event.clientX, y: event.clientY },
      box: { x: note.x, y: note.y, width: note.width, height: note.height },
      pointer: event.pointerId,
      moved: false,
    };
    (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  }
  move(event: PointerEvent) {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointer) return;
    const screenX = event.clientX - drag.start.x;
    const screenY = event.clientY - drag.start.y;
    if (!drag.moved && Math.hypot(screenX, screenY) < 4) return;
    drag.moved = true;
    const dx = screenX / this.zoom();
    const dy = screenY / this.zoom();
    const box = { ...drag.box };
    if (drag.grip === "move") {
      box.x += dx;
      box.y += dy;
    }
    if (drag.grip === "e" || drag.grip === "se")
      box.width = clamp(box.width + dx, NOTE_WIDTH);
    if (drag.grip === "s" || drag.grip === "se")
      box.height = clamp(box.height + dy, NOTE_HEIGHT);
    this.preview = { id: drag.id, ...box };
  }
  release(event: PointerEvent) {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointer) return;
    this.drag = null;
    const preview = this.preview;
    this.preview = null;
    if (drag.moved && preview)
      this.edit.emit({
        kind: "box",
        id: drag.id,
        x: Math.round(preview.x),
        y: Math.round(preview.y),
        width: Math.round(preview.width),
        height: Math.round(preview.height),
      });
  }
  cancel() {
    this.drag = null;
    this.preview = null;
  }
  /** Escape or Ctrl/Cmd+Enter ends editing and gives focus back to the note. */
  textKey(event: KeyboardEvent, note: NoteView) {
    const mod = event.ctrlKey || event.metaKey;
    if (event.key !== "Escape" && !(event.key === "Enter" && mod)) return;
    event.preventDefault();
    event.stopPropagation();
    (event.target as HTMLTextAreaElement).blur();
    afterNextRender(
      () =>
        this.element
          .querySelector<HTMLElement>(`[data-note="${CSS.escape(note.id)}"]`)
          ?.focus(),
      { injector: this.injector },
    );
  }
  commit(event: FocusEvent, note: NoteView) {
    const text = (event.target as HTMLTextAreaElement).value;
    this.editingChange.emit("");
    if (text !== note.text) this.edit.emit({ kind: "text", id: note.id, text });
  }
}
