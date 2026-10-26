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
// The left-to-right canvas. One pure layout (layout-ltr.ts) places every
// tile, handle and edge; @foblex/flow pans and zooms it. The editor that
// hosts the canvas owns the workflow and every command: the canvas keeps
// its own view (zoom, pan, what the pointer is on) and asks the host to act.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  ViewEncapsulation,
  afterNextRender,
  inject,
  input,
  viewChild,
  type DoCheck,
  type OnInit,
} from "@angular/core";
import {
  FControlSchemeController,
  FFlowComponent,
  FFlowModule,
  F_SCROLL_PAN_CONTROL_SCHEME,
  isOnFlowBackground,
  primaryButtonEventTrigger,
  provideFFlow,
  withControlScheme,
  type FCanvasChangeEvent,
  type FDragStartedEvent,
  type FSelectionChangeEvent,
  type FTriggerEvent,
} from "@foblex/flow";
import {
  reveal,
  wheelFactor,
  zoomAt,
  type View,
} from "../../designer/viewport";
import { Modal } from "../../dialog";
import { Icon } from "../../icon";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import type { Workflow } from "../../model";
import { loadKindRegistrations } from "../ndv/kinds";
import { memo } from "../memo";
import { ndvRegistry, type KindContext } from "../ndv/registry";
import {
  canvasCommand,
  canvasSheet,
  keyPlatform,
  noteCommand,
  typingIn,
} from "../state/canvas-commands";
import { minimapPinned, setMinimapPinned } from "../state/canvas-preferences";
import {
  CANVAS_TOO_BIG,
  NOTE_SIZES,
  addNote,
  canvasBytes,
  removeNote,
  stepNoteLine,
  triggerInputs,
  updateNote,
  type CanvasSidecar,
  type NotePatch,
  type StickyNote,
  type TriggerIntent,
} from "../state/canvas-sidecar";
import { CHOICE_NOT_KEPT } from "../state/editor-flag";
import { formatCombo, type KeymapCommand } from "../state/keymap";
import {
  NO_SELECTION,
  blockedReason,
  covered,
  extended,
  moveAllowed,
  only,
  selectionOf,
  toggledCovering,
  topLevel,
  type Selection,
  type StepPlace,
} from "../state/selection";
import type { CanvasHost } from "./canvas-host";
import { CanvasTools } from "./canvas-tools";
import { EdgeLayer, type EdgeView, type InsertView } from "./edge-layer";
import { DROP_REFUSED, dropOutcome } from "./handles";
import {
  LTR,
  edgePath,
  labelScale,
  layoutLtr,
  levelOfDetail,
  midpoint,
  openView,
  pathLabelScale,
  tilePaintMargin,
  type Insertion,
  type LtrBounds,
  type LtrLayout,
  type LtrTile,
  type Point,
} from "./layout-ltr";
import { neighbor, placesOf, type Direction } from "./navigation";
import { toolbarSpot } from "./toolbar-spot";
import { NodeTile, type TileView } from "./node-tile";
import {
  edgeRun,
  groupsEntered,
  pulses,
  tileRun,
  type CanvasRun,
  type TileRun,
} from "./run-state";
import { SelectionToolbar } from "./selection-toolbar";
import { SubNodeRow, chipIcon, type SubNodeView } from "./sub-node-row";
import { TRIGGER_LABELS, triggerNoteKey, triggerTile } from "./trigger-tiles";
import { noteTitle } from "./note-markdown";
import { NotesLayer, type NoteEdit, type NoteView } from "./notes-layer";
import {
  freeSpot,
  noteObstacles,
  tidyNotes,
  tileBoxes,
  type Box,
} from "./tidy";
import { CANVAS_MAX_BYTES } from "../state/test-data";
import { ownedActionsOf } from "../ndv/owned/owned-store";
import {
  CLIPBOARD_WEB_FORMAT,
  copySteps,
  readClipboard,
  stillUnreadable,
  unreadableMessage,
  type Copied,
  type Unreadable,
} from "../state/clipboard";
import {
  runText,
  tileBadge,
  tileBorders,
  tileName,
  type StepFacts,
  type TileBorders,
} from "./tile-facts";

const NO_FACTS: StepFacts = { errors: [], warnings: [], setup: [] };
const NO_BORDERS: TileBorders = {
  selected: false,
  live: false,
  error: false,
};

/** "Insert a step between a and b" becomes "Move x between a and b". */
const moveName = (name: string, id: string) =>
  name.replace(/^(Insert|Add) a step/, `Move ${id}`);
/** A step's hover toolbar, or a chosen trigger's (one Remove button). */
interface ToolbarView {
  id: string;
  /** What "Commands for …" names: the step ID, or "the Webhook trigger". */
  name: string;
  x: number;
  y: number;
  /** How much larger than at 100% it draws, so its buttons keep their size on screen. */
  scale: number;
  /** Nothing else is drawn where it sits. */
  clear: boolean;
  /** From its bottom to its step's top, in canvas units. */
  gap: number;
  locked: boolean;
  tabIndex: 0 | -1;
  menu: RowMenuItem[];
  /** A trigger's buttons: Execute workflow, and Remove for a chosen trigger. */
  trigger: { intent: string | null; label: string; execute: boolean } | null;
}

/** Ruling 19: the canvas never makes data, so a step without output says how to pin it. */
export const NO_PIN_YET = "Set test output or generate a sample to pin it.";

@Component({
  selector: "weave-canvas-view",
  standalone: true,
  imports: [
    FFlowModule,
    Icon,
    Modal,
    RowMenu,
    NodeTile,
    EdgeLayer,
    SubNodeRow,
    CanvasTools,
    SelectionToolbar,
    NotesLayer,
  ],
  // A drag on empty canvas draws a selection box; Space, Ctrl or Command
  // with a drag, or the middle button, pans; the canvas handles the wheel.
  providers: [provideFFlow(withControlScheme(F_SCROLL_PAN_CONTROL_SCHEME))],
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  host: { "(document:keydown.escape)": "escapeOutside($event)" },
  templateUrl: "./canvas-view.html",
  styleUrl: "./canvas.css",
})
export class CanvasView implements OnInit, DoCheck {
  /** The editor: it owns the workflow and runs every command. */
  host = input.required<CanvasHost>();
  private readonly root = viewChild.required<ElementRef<HTMLElement>>("root");
  /** The flow library keeps a selection of its own, filled only by the selection box. */
  private readonly flow = viewChild(FFlowComponent);
  private readonly cdr = inject(ChangeDetectorRef);
  private readonly injector = inject(Injector);
  /** The step kinds are registered: tiles draw their roles and icons. */
  ready = false;
  /** Zoom and pan: screen = canvas × zoom + pan. */
  view: View = { zoom: 1, pan: { x: 0, y: 0 } };
  /** The edge under the pointer: its "+" shows. */
  hoveredEdge = "";
  /** The step under the pointer, and the one with focus: the hover toolbar's step. */
  hoveredTile = "";
  focusTile = "";
  /** A press on an output handle, a "+" or a step, until it is a click or a drag. */
  private press: {
    kind: "handle" | "tile";
    key: string;
    start: Point;
    pointer: number;
    target: Element;
    dragging: boolean;
  } | null = null;
  /** The edge drawn from a handle while it is dragged, in canvas units. */
  band: { key: string; from: Point; to: Point } | null = null;
  /** The insertion a dropped edge opened Add a step for: its edge stays drawn meanwhile. */
  private bandFor: Insertion | null = null;
  /** "Ctrl+V", or "Cmd+V" on macOS, for the empty canvas's hint. */
  readonly pasteKeys = formatCombo("Mod+V", keyPlatform());
  /** References pasted steps hold that their place can't read, until they are fixed. */
  private readonly pasteGaps = new Map<string, readonly Unreadable[]>();
  /** Bumped when pasteGaps changes, so the tiles draw again. */
  private pasteGapsVersion = 0;
  /** The note whose text is edited, and the note "Move…" places, or "". */
  editingNote = "";
  placingNote = "";
  private cachedNotes: NoteView[] | null = null;
  /** A coarse pointer: the toolbars' buttons keep 44 px on screen instead of 24. */
  private readonly coarse =
    typeof matchMedia === "function" ? matchMedia("(pointer: coarse)") : null;
  /** The step dragged to a new place, or "". */
  draggingStep = "";
  /** The click that ends a drag does nothing else. */
  private suppressClick = false;
  /** The selected steps and the one with focus. */
  selection: Selection = NO_SELECTION;
  /** The editor's selected step when the canvas last looked. */
  private seenSelected = "";
  /** Space is held: a drag pans. */
  private space = false;
  private spacePanned = false;
  private panPress: {
    pointer: number;
    start: Point;
    pan: Point;
    dragging: boolean;
    space: boolean;
  } | null = null;
  private viewDragging = false;
  /** A selection box being drawn, and whether it adds to the selection. */
  private box: { additive: boolean; ids: string[] } | null = null;
  private boxAdditive = false;
  /** Where the last press began, to tell a click from a drag. */
  private downAt: Point | null = null;
  /** "Show minimap" is on. */
  minimapOn = minimapPinned();
  readonly platform = keyPlatform();
  /** The "?" sheet: what works everywhere and on the canvas. */
  readonly sheet = canvasSheet(this.platform);
  sheetOpen = false;
  /** The pointer whose drag Escape ended: letting go of it does nothing. */
  private cancelledPointer: number | null = null;
  /** Browser storage refused "Show minimap" once: said once. */
  private minimapNoticeShown = false;
  /** The view is moving: the minimap shows. */
  panning = false;
  private panningTimer: ReturnType<typeof setTimeout> | undefined;
  /** Steps whose toolbar Escape hid, until the pointer or focus reaches another step. */
  private dismissed: readonly string[] = [];
  /** The workflow the view was last fitted to (StructuredCanvasAdapter.opened). */
  private fitted = -1;
  private fitPending = false;
  private revealed = "";
  /**
   * The step a pointer press on its card just selected. It is in view where
   * the pointer is, so selecting it pans only when part of it is hidden:
   * otherwise the second click of a double-click would miss it.
   */
  private pointed = "";
  private readonly positions = new WeakMap<LtrTile, Point>();

  private readonly lifetime = inject(DestroyRef);

  constructor() {
    this.lifetime.onDestroy(() => clearTimeout(this.panningTimer));
    inject(FControlSchemeController).setScheme({
      nodeMove: () => false,
      createConnection: () => false,
      reassignConnection: () => false,
      nodeResize: () => false,
      nodeRotate: () => false,
      zoom: () => false,
      scrollPan: false,
      // Handle pan here too, so a tile's drag blocker never claims it.
      canvasMove: () => false,
      selection: (event) => this.boxTrigger(event),
    });
  }
  /** A press on empty canvas, without Space, Ctrl or Command, draws a selection box. */
  readonly boxTrigger = (event: FTriggerEvent): boolean =>
    primaryButtonEventTrigger(event) &&
    !this.space &&
    !event.ctrlKey &&
    !event.metaKey &&
    isOnFlowBackground(event) &&
    !(event.target as Element | null)?.closest?.(
      ".f-minimap, [data-handle], .tile-toolbar, .sticky-note",
    );

  ngOnInit() {
    // A lost chunk still draws: unknown kinds draw as app actions, with
    // the labels Studio knows. The failure is logged, not hidden.
    loadKindRegistrations()
      .catch((error: unknown) =>
        console.error("Could not load the step kinds.", error),
      )
      .finally(() => {
        this.ready = true;
        this.cdr.markForCheck();
      });
  }

  ngDoCheck() {
    if (!this.ready) return;
    const h = this.host();
    if (this.bandFor && !h.isPickerOpenAt(this.bandFor)) {
      this.band = null;
      this.bandFor = null;
    }
    const notes = this.noteViews();
    if (notes !== this.cachedNotes) {
      this.cachedNotes = notes;
      h.model.cacheNotePositions(
        new Map(
          notes
            .filter((note) => note.anchor)
            .map((note) => [note.id, { x: note.x, y: note.y }]),
        ),
      );
    }
    if (this.fitted !== h.model.opened && !this.fitPending) {
      this.fitPending = true;
      afterNextRender(
        () => {
          this.fitPending = false;
          this.fitIfNew();
        },
        { injector: this.injector },
      );
    }
    this.syncSelection();
    const selected = h.model.selected;
    if (selected !== this.revealed) {
      this.revealed = selected;
      const margin = selected && selected === this.pointed ? 0 : undefined;
      this.pointed = "";
      if (selected)
        afterNextRender(() => this.revealTile(selected, margin), {
          injector: this.injector,
        });
    }
  }
  /**
   * The editor's selected step (the one step details show) is the
   * selection's focus: a step selected elsewhere (Outline, an insert, Undo)
   * becomes the selection, and steps that no longer exist leave it.
   */
  private syncSelection() {
    const selected = this.host().model.selected;
    if (selected !== this.seenSelected) {
      this.seenSelected = selected;
      if (!selected) this.selection = NO_SELECTION;
      else if (!this.selection.ids.includes(selected))
        this.selection = only(selected);
      else if (this.selection.focus !== selected)
        this.selection = { ...this.selection, focus: selected };
    }
    const places = this.places();
    if (this.selection.ids.some((id) => !places.has(id)))
      this.selection = selectionOf(
        this.selection.ids.filter((id) => places.has(id)),
        this.selection.focus,
      );
  }

  // ------------------------------------------------------------ layout

  private readonly layoutMemo = memo(
    (
      _tick: number,
      workflow: Workflow,
      _ready: boolean,
      canvas: CanvasSidecar,
    ): LtrLayout =>
      layoutLtr(workflow, {
        role: (kind) => ndvRegistry.kind(kind)?.role,
        slots: (kind) =>
          ndvRegistry.subNodes(kind).map((slot) => ({
            id: slot.id,
            label: slot.label,
            required: slot.required,
          })),
        triggers: triggerInputs(canvas, workflow.spec.steps.length > 0),
      }),
  );
  layout(): LtrLayout {
    const h = this.host();
    return this.layoutMemo(
      h.tick(),
      h.model.definition,
      this.ready,
      h.model.canvas,
    );
  }
  private readonly placesMemo = memo((layout: LtrLayout) => placesOf(layout));
  places(): Map<string, StepPlace> {
    return this.placesMemo(this.layout());
  }
  /** Nothing can change: a simulation runs, or the source doesn't parse. */
  locked(): boolean {
    const h = this.host();
    return h.editingLocked || h.model.readonly;
  }
  /** The step being placed with "Move to…" or dragged, or "". */
  moving(): string {
    return this.host().connectingNode || this.draggingStep;
  }
  /** Every "+" shows: a step is being placed, or a step kind is dragged in. */
  revealing(): boolean {
    return !!this.moving() || !!this.host().dragPreview;
  }
  level() {
    return levelOfDetail(this.view.zoom);
  }
  /** Text under tiles draws this much larger, so names stay readable. */
  labelScale() {
    return labelScale(this.view.zoom);
  }
  /** Path labels and "All branches done" draw this much larger, never below 10 px. */
  pathLabelScale() {
    return pathLabelScale(this.view.zoom);
  }
  focusWidth() {
    return Math.ceil(2 / this.view.zoom);
  }
  /** The tile that takes Tab: the selection's focus, else the first tile. */
  active(): string {
    const layout = this.layout();
    const focus = this.selection.focus;
    return focus && layout.tiles.some((tile) => tile.id === focus)
      ? focus
      : (layout.tiles[0]?.id ?? "");
  }
  isFitted(): boolean {
    return this.fitted === this.host().model.opened;
  }

  // ------------------------------------------------------------- tiles

  private readonly tilesMemo = memo(
    (
      layout: LtrLayout,
      facts: ReadonlyMap<string, StepFacts>,
      selection: Selection,
      active: string,
      run: CanvasRun | null,
      canvas: CanvasSidecar,
      _gapsVersion: number,
      pins: ReadonlyMap<string, { stale: string | null }>,
      _runStamp: unknown,
    ): TileView[] => {
      const ctx = this.host().kindContext();
      // A selected group shows every step inside it as selected.
      const chosen = covered(selection, this.places());
      return layout.tiles.map((tile) => {
        const state = tile.step ? tileRun(tile.id, run) : null;
        return this.tileView(
          tile,
          ctx,
          this.withPasteGaps(tile, facts.get(tile.id) ?? NO_FACTS),
          {
            selected: chosen.has(tile.id),
            active: tile.id === active,
            run: state,
            pulse: pulses(state, run),
            // The trigger's note is the one its step details keep.
            note: tile.step
              ? stepNoteLine(canvas, tile.id)
              : tile.kind === "trigger"
                ? stepNoteLine(canvas, triggerNoteKey(tile.id, canvas))
                : "",
            pin: tile.step ? pins.get(tile.id) : undefined,
            simulated: run?.mode === "simulated",
          },
        );
      });
    },
  );
  tiles(): TileView[] {
    const h = this.host();
    return this.tilesMemo(
      this.layout(),
      h.canvasFacts(),
      this.selection,
      this.active(),
      h.canvasRun(),
      h.model.canvas,
      this.pasteGapsVersion,
      h.pinStates(),
      h.runStamp?.() ?? null,
    );
  }
  private position(tile: LtrTile): Point {
    let point = this.positions.get(tile);
    if (!point) {
      point = { x: tile.x, y: tile.y };
      this.positions.set(tile, point);
    }
    return point;
  }
  private tileView(
    tile: LtrTile,
    ctx: KindContext,
    facts: StepFacts,
    state: {
      selected: boolean;
      active: boolean;
      run: TileRun;
      pulse: boolean;
      /** The first line of the step's note when it shows on the canvas. */
      note: string;
      /** Its pinned output, and why it may be out of date. */
      pin?: { stale: string | null };
      /** The run drawn is a simulation Studio played back. */
      simulated?: boolean;
    },
  ): TileView {
    const h = this.host();
    const common = {
      id: tile.id,
      kind: tile.kind,
      shape: tile.shape,
      position: this.position(tile),
      width: tile.width,
      height: tile.height,
      labelTop: tile.labelY - tile.y,
      paintMargin: tilePaintMargin(tile),
      tabIndex: state.active ? (0 as const) : (-1 as const),
      pulse: state.pulse,
      note: state.note,
      output: "",
    };
    if (!tile.step) {
      const trigger =
        tile.kind === "trigger" ? this.triggerText(tile.id) : null;
      return {
        ...common,
        step: false,
        icon: trigger?.icon ?? "",
        title: trigger ? trigger.title : "End",
        subtitle: trigger?.subtitle ?? "",
        name: trigger
          ? `${trigger.title} trigger, ${trigger.subtitle}`
          : "End, workflow result",
        tooltip: trigger
          ? trigger.manual
            ? "Open the workflow inputs"
            : `${trigger.title} · ${trigger.subtitle}`
          : "Open the workflow result",
        badge: null,
        borders: NO_BORDERS,
        selected: false,
      };
    }
    const descriptor = ndvRegistry.kind(tile.kind);
    const kindLabel = descriptor?.label ?? h.label(tile.kind);
    const summary = descriptor?.summary(tile.step, ctx) ?? "";
    const shown = {
      ...facts,
      run: state.run,
      failure: state.run === "failed" ? (h.runFailure?.(tile.id) ?? {}) : null,
      stale: state.pin?.stale ?? null,
      pinned: !!state.pin,
      selected: state.selected,
      simulated: !!state.simulated,
    };
    const badge = tileBadge(shown);
    // Where the simulated run stopped: what it needs ("Step check needs test data.").
    const note = state.run ? (h.runNote?.(tile.id) ?? "") : "";
    return {
      ...common,
      step: true,
      icon: descriptor?.icon ?? "action",
      title: tile.id,
      subtitle: summary || kindLabel,
      output: h.summaryOf?.(tile.id) ?? "",
      name: tileName({
        id: tile.id,
        kindLabel,
        summary,
        status: note || runText(state.run, tile.kind) || badge?.text || "",
        index: tile.index,
        count: tile.count,
        place: tile.place,
      }),
      tooltip: [`${tile.id}, ${kindLabel}`, note || badge?.text]
        .filter(Boolean)
        .join(". "),
      badge,
      borders: tileBorders(shown),
      selected: state.selected,
    };
  }
  private intentOf(tileId: string): TriggerIntent | null {
    return (
      this.host().model.canvas.triggers?.find(
        (item) => `$trigger:${item.id}` === tileId,
      ) ?? null
    );
  }
  /** A trigger tile's icon and subtitle; the implicit input form has no choice behind it. */
  private triggerText(tileId: string) {
    const kind = this.intentOf(tileId)?.kind ?? "manual";
    return {
      ...triggerTile(kind, this.host().model.definition),
      manual: kind === "manual",
    };
  }
  /** Double-click or Enter on a trigger: the input form opens its details; other triggers are set up when you activate. */
  private openTrigger(tileId: string) {
    const h = this.host();
    const intent = this.intentOf(tileId);
    if (!intent || intent.kind === "manual")
      return void h.openWorkflowSection("spec/inputSchema");
    h.notify(
      `${TRIGGER_LABELS[intent.kind]} is set up when you activate this workflow.`,
    );
  }

  // -------------------------------------------------- insertion targets

  private readonly targetsMemo = memo(
    (
      layout: LtrLayout,
      places: Map<string, StepPlace>,
      moving: string,
      active: string,
    ) => {
      /** While a step moves: only where it can go, named for the move. */
      const offer = (target: InsertView): InsertView | null => {
        if (!moving) return target;
        if (places.has(moving) && !moveAllowed(places, moving, target.insert))
          return null;
        return { ...target, name: moveName(target.name, moving), moving: true };
      };
      const tab = (tile: string): 0 | -1 => (tile === active ? 0 : -1);
      const pluses = layout.handles.flatMap((handle) => {
        if (!handle.plus) return [];
        const view = offer({
          key: handle.key,
          tile: handle.tile,
          x: handle.plus.x - 12,
          y: handle.plus.y - 12,
          name: handle.name,
          insert: handle.insert,
          moving: false,
          tabIndex: tab(handle.tile),
        });
        return view ? [view] : [];
      });
      const slots = layout.slots.flatMap((slot) => {
        const view = offer({
          key: slot.owner,
          tile: slot.tile,
          x: slot.x,
          y: slot.y,
          name: slot.name,
          insert: slot.insert,
          moving: false,
          tabIndex: tab(slot.tile),
        });
        return view ? [view] : [];
      });
      const onEdges = new Map<string, InsertView | null>();
      for (const edge of layout.edges) {
        if (!edge.insert || !edge.name) continue;
        const mid = midpoint(edge);
        onEdges.set(
          edge.key,
          offer({
            key: edge.key,
            tile: edge.tile,
            x: mid.x - 12,
            y: mid.y - 12,
            name: edge.name,
            insert: edge.insert,
            moving: false,
            tabIndex: tab(edge.tile),
          }),
        );
      }
      return { pluses, slots, onEdges };
    },
  );
  private targets() {
    return this.targetsMemo(
      this.layout(),
      this.places(),
      this.moving(),
      this.active(),
    );
  }
  pluses(): InsertView[] {
    return this.locked() ? [] : this.targets().pluses;
  }
  slots(): InsertView[] {
    return this.targets().slots;
  }
  private readonly edgesMemo = memo(
    (
      layout: LtrLayout,
      onEdges: ReadonlyMap<string, InsertView | null>,
      hovered: string,
      locked: boolean,
      run: CanvasRun | null,
      places: Map<string, StepPlace>,
    ): EdgeView[] => {
      // A run records a decision or parallel step only when it finishes, so
      // the way into one counts once the run is at a step inside it.
      const inside = groupsEntered(run, (id) => places.get(id)?.parent ?? null);
      return layout.edges.map((edge) => ({
        edge,
        d: edgePath(edge),
        state: edgeRun(edge, run, inside),
        shown: hovered === edge.key,
        plus: locked ? null : (onEdges.get(edge.key) ?? null),
      }));
    },
  );
  edges(): EdgeView[] {
    return this.edgesMemo(
      this.layout(),
      this.targets().onEdges,
      this.hoveredEdge,
      this.locked(),
      this.host().canvasRun(),
      this.places(),
    );
  }
  private readonly chipsMemo = memo((layout: LtrLayout): SubNodeView[] => {
    if (!layout.subNodes.length) return [];
    const ctx = this.host().kindContext();
    return layout.subNodes.map((row) => {
      const tile = layout.tiles.find((item) => item.id === row.step)!;
      const slots = ndvRegistry.subNodes(tile.kind);
      return {
        step: row.step,
        chips: row.chips.map((chip) => {
          const data = slots
            .find((slot) => slot.id === chip.id)
            ?.chip(tile.step!, ctx) ?? { text: "", state: "ok" as const };
          const warn = data.state === "missing" && chip.required;
          return {
            ...chip,
            icon: warn ? "connections" : chipIcon(chip.id),
            state: data.state,
            warn,
            name: `${chip.label.replace(/\*$/, "")}${chip.required ? ", required" : ""}, ${data.state === "missing" ? "not set" : data.text}`,
          };
        }),
      };
    });
  });
  chips(): SubNodeView[] {
    return this.chipsMemo(this.layout());
  }

  // ----------------------------------------------------------- pointer

  click(event: MouseEvent) {
    if (this.suppressClick) {
      this.suppressClick = false;
      return;
    }
    if (this.placingNote) {
      const target = event.target as Element;
      if (
        target.closest(
          "[data-action], .sticky-note, .tile-node, .canvas-v2-tools, [role='menu']",
        )
      )
        return;
      const id = this.placingNote;
      this.placingNote = "";
      const note = this.noteViews().find((item) => item.id === id);
      const point = this.toWorld(event.clientX, event.clientY);
      if (note)
        this.noteEdit({
          kind: "box",
          id,
          x: point.x,
          y: point.y,
          width: note.width,
          height: note.height,
        });
      return;
    }
    const control = (event.target as Element).closest<HTMLElement>(
      "[data-action]",
    );
    if (!control) {
      // A click on empty canvas, not the end of a drag, clears the selection.
      const still =
        !!this.downAt &&
        Math.hypot(
          event.clientX - this.downAt.x,
          event.clientY - this.downAt.y,
        ) < 4;
      if (
        still &&
        !(event.target as Element).closest(
          "button, a, [role=menu], [data-handle], [data-tile], .canvas-v2-tools, .selection-toolbar, .tile-toolbar, .sticky-note, f-minimap",
        )
      )
        this.clearSelection();
      return;
    }
    const action = control.dataset["action"];
    if (action === "issues") {
      const id = control.closest<HTMLElement>("[data-tile]")?.dataset["tile"];
      if (id && event.detail < 2) void this.host().openStep(id, "issues");
    } else if (action === "tile") {
      const id = control.closest<HTMLElement>("[data-tile]")?.dataset["tile"];
      if (id) void this.openTile(id, event);
    } else if (action === "insert") this.insertAt(control);
    else if (action === "templates") this.host().showTemplates = true;
    else if (action === "execute-workflow") {
      if (control.getAttribute("aria-disabled") !== "true")
        this.host().executeWorkflow?.();
    } else if (action === "remove-trigger") {
      const id = control.dataset["target"];
      if (id && control.getAttribute("aria-disabled") !== "true")
        void this.host().removeTrigger(id);
    } else if (action === "delete") {
      const id = control.dataset["target"];
      if (id && control.getAttribute("aria-disabled") !== "true")
        void this.host().removeSteps([id]);
    }
  }
  doubleClick(event: MouseEvent) {
    if (event.shiftKey || event.ctrlKey || event.metaKey || this.suppressClick)
      return;
    if ((event.target as Element).closest(".tile-issue")) return;
    const tile = (event.target as Element)
      .closest(".tile-body")
      ?.closest("[data-tile]");
    const id = tile?.getAttribute("data-tile");
    if (!id) return;
    if (id.startsWith("$trigger:")) this.openTrigger(id);
    else if (id === "$end") void this.host().openWorkflowSection("spec/output");
    else void this.host().openStep(id, "details");
  }
  /**
   * A click selects; with Shift, Ctrl or Command it adds the step to the
   * selection, or takes it out. Opening belongs to double-click and keys.
   */
  private async openTile(id: string, event: MouseEvent) {
    const h = this.host();
    if (id.startsWith("$trigger:") || id === "$end") return;
    if (event.shiftKey || event.ctrlKey || event.metaKey) {
      this.selection = toggledCovering(this.selection, id, this.places());
      this.seenSelected = this.selection.focus ?? "";
      this.pointed = this.selection.focus === id ? id : "";
      if (this.selection.focus) await h.selectStep(this.selection.focus, false);
      else await h.deselect();
      return;
    }
    this.selection = only(id);
    this.seenSelected = id;
    this.pointed = id;
    await h.selectStep(id, false);
  }
  private insertAt(control: HTMLElement) {
    const h = this.host();
    const insert = this.insertOf(control);
    if (!insert || control.getAttribute("aria-disabled") === "true") return;
    const moving = this.moving();
    if (moving) return h.moveStep(moving, insert);
    if (h.isPickerOpenAt(insert)) h.closePicker();
    else
      h.openPicker(
        insert,
        control.getAttribute("aria-label") ?? "Add a step",
        control,
      );
  }
  private insertOf(control: Element): Insertion | null {
    const owner = control.getAttribute("data-insert-owner");
    const index = Number(control.getAttribute("data-insert-index"));
    return owner && Number.isInteger(index) ? { owner, index } : null;
  }
  hover(event: PointerEvent) {
    const target = event.target as Element;
    const edge =
      target.closest?.("[data-edge]")?.getAttribute("data-edge") ?? "";
    if (edge !== this.hoveredEdge) this.hoveredEdge = edge;
    const tile = this.toolbarStep(target);
    if (tile !== this.hoveredTile) {
      this.hoveredTile = tile;
      this.reached(tile);
    }
  }
  unhover() {
    this.hoveredEdge = "";
    this.hoveredTile = "";
  }
  /** Focus on a step, or in its toolbar, shows that step's toolbar. */
  focusIn(event: FocusEvent) {
    this.focusTile = this.toolbarStep(event.target as Element);
    this.reached(this.focusTile);
  }
  /** Focus left the canvas: only the pointer shows a toolbar now, and Space no longer pans. */
  focusOut(event: FocusEvent) {
    const next = event.relatedTarget;
    if (!(next instanceof Node && this.root().nativeElement.contains(next))) {
      this.focusTile = "";
      this.space = false;
    }
  }
  /** The pointer or focus reached a step: a toolbar Escape hid elsewhere may show again. */
  private reached(id: string) {
    if (id && this.dismissed.length && !this.dismissed.includes(id))
      this.dismissed = [];
  }
  /** The step an element belongs to, as a step or in its toolbar; else "". */
  private toolbarStep(element: Element): string {
    const holder = element.closest?.(
      "[data-step], [data-trigger], [data-toolbar-for]",
    );
    return (
      holder?.getAttribute("data-step") ??
      holder?.getAttribute("data-trigger") ??
      holder?.getAttribute("data-toolbar-for") ??
      ""
    );
  }

  // ------------------------------------------------------ hover toolbar

  private readonly toolbarsMemo = memo(
    (
      layout: LtrLayout,
      focused: string,
      hovered: string,
      locked: boolean,
      dismissed: readonly string[],
      canvas: CanvasSidecar,
      zoom: number,
      coarse: boolean,
      execute: boolean,
    ) =>
      [...new Set([focused, hovered])].flatMap((id): ToolbarView[] => {
        const tile =
          id && !dismissed.includes(id)
            ? layout.tiles.find((item) => item.id === id)
            : undefined;
        if (!tile || tile.kind === "end") return [];
        const tabIndex = id === focused ? (0 as const) : (-1 as const);
        if (!tile.step) {
          const intent = canvas.triggers?.find(
            (item) => `$trigger:${item.id}` === tile.id,
          );
          // The trigger runs the workflow: Execute workflow on hover and focus.
          if (!intent && !execute) return [];
          const label = TRIGGER_LABELS[intent?.kind ?? "manual"];
          const spot = toolbarSpot(tile, layout, zoom, {
            buttons: intent && execute ? 2 : 1,
            coarse,
          });
          return [
            {
              id: tile.id,
              name: `the ${label} trigger`,
              x: spot.x,
              y: spot.y,
              scale: spot.scale,
              clear: spot.clear,
              gap: spot.gap,
              locked,
              tabIndex,
              menu: [],
              trigger: { intent: intent?.id ?? null, label, execute },
            },
          ];
        }
        const spot = toolbarSpot(tile, layout, zoom, { coarse });
        return [
          {
            id: tile.id,
            name: tile.id,
            x: spot.x,
            y: spot.y,
            scale: spot.scale,
            clear: spot.clear,
            gap: spot.gap,
            locked,
            tabIndex,
            menu: this.menuFor(tile, locked),
            trigger: null,
          },
        ];
      }),
  );
  /**
   * Delete and More for the step with focus and the one under the pointer.
   * Each keeps its own toolbar, so an open menu never changes step when the
   * pointer moves over another one.
   */
  toolbars() {
    if (this.dragging() || this.box || this.viewDragging) return [];
    return this.toolbarsMemo(
      this.layout(),
      this.focusTile,
      this.hoveredTile,
      this.locked(),
      this.dismissed,
      this.host().model.canvas,
      this.view.zoom,
      this.coarse?.matches ?? false,
      !!this.host().executeWorkflow,
    );
  }
  /**
   * Hides the toolbars on screen, which appear on hover and focus, until
   * the pointer or focus reaches another step; false when none shows.
   * Focus inside a toolbar returns to its step.
   */
  private hideToolbars(target: Element): boolean {
    const shown = this.toolbars().map((bar) => bar.id);
    if (!shown.length) return false;
    this.dismissed = shown;
    this.cdr.markForCheck();
    const inside = target.closest?.("[data-toolbar-for]");
    if (inside)
      this.root()
        .nativeElement.querySelector<HTMLElement>(
          `[data-tile="${CSS.escape(inside.getAttribute("data-toolbar-for")!)}"] .tile-body`,
        )
        ?.focus();
    return true;
  }
  /**
   * Escape with focus outside the canvas (keys inside it go through key()),
   * for example on the page while the pointer drags: it ends the drag, or
   * else hides the toolbars the pointer shows, and goes no further. A
   * field, a dialog or an open menu takes Escape first; "Move to…" and
   * everything else go on to the editor.
   */
  escapeOutside(event: Event) {
    const h = this.host();
    const target = event.target as Element;
    if (
      event.defaultPrevented ||
      this.root().nativeElement.contains(target) ||
      target.closest?.("input, textarea, select, [role=dialog], .modal-panel")
    )
      return;
    const done = this.dragging()
      ? this.endGesture()
      : !h.connectingNode && !h.dragPreview && this.hideToolbars(target);
    if (done) {
      event.preventDefault();
      event.stopPropagation();
    }
  }
  /** A pasted reference its place can't read is setup the step needs, until it is fixed. */
  private withPasteGaps(tile: LtrTile, facts: StepFacts): StepFacts {
    const gaps = tile.step ? this.pasteGaps.get(tile.id) : undefined;
    if (!gaps || !tile.step) return facts;
    const definition = this.host().model.definition;
    const open = gaps.filter((gap) =>
      stillUnreadable(definition, tile.step!, gap),
    );
    if (open.length) this.pasteGaps.set(tile.id, open);
    else this.pasteGaps.delete(tile.id);
    return open.length
      ? {
          ...facts,
          setup: [
            ...facts.setup,
            ...open.map((gap) => `replace the reference to ${gap.target}`),
          ],
        }
      : facts;
  }
  /** Pin output or Unpin in a step's menu; the canvas never makes data, so without output it says how. */
  private pinItem(id: string): RowMenuItem {
    const h = this.host();
    const pinned = h.pinStates().has(id);
    const blocked = pinned
      ? null
      : !this.pinnable(id)
        ? "This step's output can't be pinned."
        : NO_PIN_YET;
    return {
      label: pinned ? "Unpin" : "Pin output",
      disabled: !!blocked,
      ...(blocked ? { detail: blocked } : {}),
      run: () => h.togglePins([id]),
    };
  }
  /** Whether a step's output can be pinned (actions, AI steps; not what is computed from the input). */
  private pinnable(id: string): boolean {
    const step =
      this.host()
        .model.nodes()
        .find((node) => node.step.id === id)?.step ?? null;
    const descriptor = step ? ndvRegistry.kind(step.kind) : undefined;
    if (!step || !descriptor) return false;
    return typeof descriptor.pinnable === "function"
      ? descriptor.pinnable(step)
      : descriptor.pinnable;
  }
  private menuFor(tile: LtrTile, locked: boolean): RowMenuItem[] {
    const h = this.host();
    const id = tile.id;
    return [
      { label: "Open", run: () => void h.openStep(id, "details") },
      {
        label: "Rename",
        disabled: locked,
        run: () => void h.openStep(id, "rename"),
      },
      { label: "Copy", run: () => void this.copyIds([id], false) },
      this.pinItem(id),
      {
        label: "Duplicate",
        disabled: locked,
        run: () => void h.duplicateSteps([id]),
      },
      { label: "Move to…", disabled: locked, run: () => h.startMove(id) },
      ...(tile.kind === "humanTask"
        ? [
            {
              label: "Add paths for answers",
              disabled: locked,
              run: () => void h.branchOnDecision(id),
            },
          ]
        : []),
      {
        label: "Delete",
        danger: true,
        disabled: locked,
        run: () => void h.removeSteps([id]),
      },
    ];
  }
  /** Right-click on a step opens its More menu, also after Escape hid its toolbar. */
  contextMenu(event: MouseEvent) {
    const id = (event.target as Element)
      .closest?.("[data-step]")
      ?.getAttribute("data-step");
    if (!id) return;
    event.preventDefault();
    this.hoveredTile = id;
    this.dismissed = this.dismissed.filter((step) => step !== id);
    this.cdr.markForCheck();
    afterNextRender(
      () =>
        this.root()
          .nativeElement.querySelector<HTMLElement>(
            `[data-toolbar-for="${CSS.escape(id)}"] .row-menu-toggle`,
          )
          ?.click(),
      { injector: this.injector },
    );
  }

  // ------------------------------------------------- drags on the canvas

  /** A press on a handle or "+" may draw an edge; on a step, may move it. */
  pointerDown(event: PointerEvent) {
    this.downAt = { x: event.clientX, y: event.clientY };
    this.cancelledPointer = null;
    this.boxAdditive = event.shiftKey;
    const target = event.target as Element;
    if (
      (event.button === 1 ||
        (event.button === 0 &&
          (this.space || event.ctrlKey || event.metaKey))) &&
      !target.closest(
        "f-minimap, .canvas-v2-tools, .canvas-v2-tools-menu, .selection-toolbar, .tile-toolbar",
      )
    ) {
      this.panPress = {
        pointer: event.pointerId,
        start: { x: event.clientX, y: event.clientY },
        pan: { ...this.view.pan },
        dragging: false,
        space: this.space,
      };
      event.preventDefault();
      return;
    }
    if (event.button !== 0 || this.locked()) return;
    const port = target.closest?.("[data-handle]");
    const key = port?.getAttribute("data-handle") ?? "";
    const start = { x: event.clientX, y: event.clientY };
    if (
      port &&
      !this.moving() &&
      this.layout().handles.some((item) => item.key === key)
    ) {
      this.press = {
        kind: "handle",
        key,
        start,
        pointer: event.pointerId,
        target: port,
        dragging: false,
      };
      return;
    }
    const body = target.closest?.(".tile-body");
    const step = body?.closest("[data-step]")?.getAttribute("data-step");
    if (body && step)
      this.press = {
        kind: "tile",
        key: step,
        start,
        pointer: event.pointerId,
        target: body,
        dragging: false,
      };
  }
  pointerMove(event: PointerEvent) {
    const pan = this.panPress;
    if (pan && event.pointerId === pan.pointer) {
      const dx = event.clientX - pan.start.x;
      const dy = event.clientY - pan.start.y;
      if (!pan.dragging) {
        if (Math.hypot(dx, dy) < 4) return;
        pan.dragging = true;
        this.root().nativeElement.setPointerCapture(event.pointerId);
        this.panning = true;
        clearTimeout(this.panningTimer);
        if (pan.space) this.spacePanned = true;
      }
      this.setView({
        zoom: this.view.zoom,
        pan: { x: pan.pan.x + dx, y: pan.pan.y + dy },
      });
      return;
    }
    const press = this.press;
    if (!press || event.pointerId !== press.pointer) return;
    if (!press.dragging) {
      if (
        Math.hypot(
          event.clientX - press.start.x,
          event.clientY - press.start.y,
        ) < 4
      )
        return;
      press.dragging = true;
      (press.target as HTMLElement).setPointerCapture?.(event.pointerId);
      // A drag replaces a "Move to…" still waiting for its "+".
      if (this.host().connectingNode) this.host().cancelGesture();
    }
    if (press.kind === "handle") {
      const port = this.layout().handles.find((item) => item.key === press.key);
      if (!port) return this.cancelPress();
      this.band = {
        key: press.key,
        from: { x: port.x, y: port.y },
        to: this.toWorld(event.clientX, event.clientY),
      };
    } else this.draggingStep = press.key;
    this.cdr.markForCheck();
  }
  pointerUp(event: PointerEvent) {
    if (event.pointerId === this.cancelledPointer) {
      // Escape ended this drag: letting go drops nothing and clicks nothing.
      this.cancelledPointer = null;
      this.suppressClick = true;
      setTimeout(() => (this.suppressClick = false));
      return;
    }
    const pan = this.panPress;
    if (pan && event.pointerId === pan.pointer) {
      this.panPress = null;
      if (pan.dragging) {
        this.suppressClick = true;
        setTimeout(() => (this.suppressClick = false));
        this.showWhilePanning();
      }
      return;
    }
    const press = this.press;
    if (!press || event.pointerId !== press.pointer) return;
    this.press = null;
    if (!press.dragging) return;
    this.suppressClick = true;
    setTimeout(() => (this.suppressClick = false));
    const h = this.host();
    if (press.kind === "handle") {
      const band = this.band;
      this.band = null;
      const control = press.target as HTMLElement;
      const hit = control.getBoundingClientRect();
      if (
        control.dataset["action"] === "insert" &&
        event.clientX >= hit.left &&
        event.clientX <= hit.right &&
        event.clientY >= hit.top &&
        event.clientY <= hit.bottom
      ) {
        this.insertAt(control);
        this.cdr.markForCheck();
        return;
      }
      const port = this.layout().handles.find((item) => item.key === press.key);
      const box = this.root().nativeElement.getBoundingClientRect();
      const inside =
        event.clientX >= box.left &&
        event.clientX <= box.right &&
        event.clientY >= box.top &&
        event.clientY <= box.bottom;
      // Let go outside the canvas: nothing happens.
      if (port && inside) {
        const point = this.toWorld(event.clientX, event.clientY);
        if (dropOutcome(point, this.layout(), this.view.zoom) === "refused")
          h.notify(DROP_REFUSED);
        else {
          h.openPicker(port.insert, port.name, {
            left: event.clientX,
            top: event.clientY,
            width: 0,
            height: 0,
          });
          // The edge stays drawn to where it was dropped while Add a step is open there.
          if (band && h.isPickerOpenAt(port.insert)) {
            this.band = band;
            this.bandFor = port.insert;
          }
        }
      }
    } else {
      this.draggingStep = "";
      const over = document
        .elementFromPoint(event.clientX, event.clientY)
        ?.closest("[data-insert]");
      const insert = over ? this.insertOf(over) : null;
      const places = this.places();
      if (
        insert &&
        (!places.has(press.key) || moveAllowed(places, press.key, insert))
      )
        h.moveStep(press.key, insert);
      else h.notify("Drop on a highlighted + to move this step.");
    }
    this.cdr.markForCheck();
  }
  /** A handle or a step is being dragged. */
  private dragging(): boolean {
    return (
      !!this.panPress?.dragging ||
      !!this.press?.dragging ||
      !!this.band ||
      !!this.draggingStep
    );
  }
  cancelPress() {
    const panned = this.panPress?.dragging;
    this.panPress = null;
    this.press = null;
    this.band = null;
    this.draggingStep = "";
    if (panned) this.showWhilePanning();
    this.cdr.markForCheck();
  }
  /** Screen pixels to canvas units. */
  private toWorld(x: number, y: number): Point {
    const box = this.root().nativeElement.getBoundingClientRect();
    return {
      x: (x - box.left - this.view.pan.x) / this.view.zoom,
      y: (y - box.top - this.view.pan.y) / this.view.zoom,
    };
  }

  // ------------------------------------------- step kinds dropped on a "+"

  /** A step kind dragged in from the editor may drop here, while editing is open. */
  dragOver(event: DragEvent) {
    if (
      !this.locked() &&
      event.dataTransfer?.types.includes("application/weave-step")
    )
      this.host().allowDrop(event);
  }
  async dropOn(event: DragEvent) {
    if (!event.dataTransfer?.types.includes("application/weave-step")) return;
    event.preventDefault();
    const over = (event.target as Element).closest?.("[data-insert]");
    const insert = over ? this.insertOf(over) : null;
    if (insert) await this.host().dropStep(event, insert);
    else this.host().notify("Drop the step on a + to place it.");
  }

  // ---------------------------------------------------------- viewport

  wheel(event: WheelEvent) {
    event.preventDefault();
    this.showWhilePanning();
    const box = this.root().nativeElement.getBoundingClientRect();
    if (event.ctrlKey || event.metaKey)
      this.setView(
        zoomAt(this.view, wheelFactor(event.deltaY), {
          x: event.clientX - box.left,
          y: event.clientY - box.top,
        }),
      );
    else {
      const sideways = event.shiftKey && !event.deltaX;
      this.setView({
        zoom: this.view.zoom,
        pan: {
          x: this.view.pan.x - (sideways ? event.deltaY : event.deltaX),
          y: this.view.pan.y - (sideways ? 0 : event.deltaY),
        },
      });
    }
  }
  /** Focus scrolls the canvas to show a tile; that scroll becomes pan. */
  scrolled(event: Event) {
    const element = event.target as HTMLElement;
    const dx = element.scrollLeft;
    const dy = element.scrollTop;
    if (!dx && !dy) return;
    element.scrollLeft = 0;
    element.scrollTop = 0;
    this.setView({
      zoom: this.view.zoom,
      pan: { x: this.view.pan.x - dx, y: this.view.pan.y - dy },
    });
  }
  zoomBy(factor: number) {
    const root = this.root().nativeElement;
    this.setView(
      zoomAt(this.view, factor, {
        x: root.clientWidth / 2,
        y: root.clientHeight / 2,
      }),
    );
  }
  /**
   * Fit view, also the view a workflow opens with: the whole workflow when
   * it fits at 50% or more, else 50% from the trigger (openView).
   */
  fit(): boolean {
    const root = this.root().nativeElement;
    if (!root.clientWidth || !root.clientHeight) return false;
    this.setView(
      openView(this.fitBounds(), {
        width: root.clientWidth,
        height: root.clientHeight,
      }),
    );
    return true;
  }
  /** What Fit view shows: the steps and every note. */
  private fitBounds(): LtrBounds {
    const bounds = this.layout().bounds;
    const notes = this.noteViews();
    if (!notes.length) return bounds;
    const steps = this.layout().tiles.length > 0;
    return {
      minX: Math.min(
        ...(steps ? [bounds.minX] : []),
        ...notes.map((note) => note.x),
      ),
      minY: Math.min(
        ...(steps ? [bounds.minY] : []),
        ...notes.map((note) => note.y),
      ),
      maxX: Math.max(
        ...(steps ? [bounds.maxX] : []),
        ...notes.map((note) => note.x + note.width),
      ),
      maxY: Math.max(
        ...(steps ? [bounds.maxY] : []),
        ...notes.map((note) => note.y + note.height),
      ),
    };
  }
  /** Opens a workflow the canvas hasn't shown yet with Fit view. */
  fitIfNew() {
    const opened = this.host().model.opened;
    if (this.ready && this.fitted !== opened && this.fit())
      this.fitted = opened;
  }
  /** Pans so a tile and its label sit at least `margin` px inside the canvas. */
  revealTile(id: string, margin = 64) {
    const tile = this.layout().tiles.find((item) => item.id === id);
    const root = this.root().nativeElement;
    if (!tile || !root.clientWidth) return;
    const pan = reveal(
      this.view,
      {
        x: tile.x,
        y: tile.y,
        width: tile.width,
        height: tile.labelY + LTR.labelHeight - tile.y,
      },
      { width: root.clientWidth, height: root.clientHeight },
      margin,
    );
    if (pan.x !== this.view.pan.x || pan.y !== this.view.pan.y)
      this.setView({ zoom: this.view.zoom, pan });
  }

  /** Returns from a dialog to a current tile, revealing it after any resize. */
  restoreFocus(target: string, current: () => boolean) {
    const id =
      target === "$trigger"
        ? this.layout().tiles.find((tile) => tile.id.startsWith("$trigger:"))
            ?.id
        : target;
    if (!id || this.lifetime.destroyed || !current()) return;
    this.revealTile(id);
    this.cdr.markForCheck();
    afterNextRender(
      () =>
        setTimeout(() => {
          if (this.lifetime.destroyed || !current()) return;
          const tile = this.root().nativeElement.querySelector<HTMLElement>(
            `[data-tile="${CSS.escape(id)}"] .tile-body`,
          );
          if (tile?.isConnected) tile.focus({ preventScroll: true });
        }),
      { injector: this.injector },
    );
  }

  // ---------------------------------------- selection, box and minimap

  clearSelection() {
    if (!this.selection.ids.length) return;
    this.selection = NO_SELECTION;
    this.seenSelected = "";
    void this.host().deselect();
  }
  /** How many steps the selection toolbar counts: the top-level ones. */
  selectedCount(): number {
    return topLevel(this.selection, this.places()).length;
  }
  duplicateBlocked(): string | null {
    return blockedReason("duplicate", this.selection, this.places());
  }
  /** Why nothing can change now, or null. */
  lockedReason(): string | null {
    const h = this.host();
    if (h.editingLocked) return "Editing is paused while the simulation runs.";
    if (h.model.readonly) return "Fix the source before changing steps.";
    return null;
  }
  async duplicateSelection() {
    const reason = this.lockedReason() ?? this.duplicateBlocked();
    if (reason) return this.host().notify(reason);
    await this.host().duplicateSteps(topLevel(this.selection, this.places()));
  }
  async removeSelection() {
    const reason = this.lockedReason();
    if (reason) return this.host().notify(reason);
    await this.host().removeSteps(topLevel(this.selection, this.places()));
  }
  /** Space held: a drag pans. */
  trackSpace(event: KeyboardEvent) {
    if (event.key !== " " || typingIn(event.target)) return;
    const target = event.target as HTMLElement;
    const tile = target.classList.contains("tile-body");
    const wasHeld = this.space;
    this.space = event.type === "keydown";
    if (this.space) {
      if (!event.repeat) this.spacePanned = false;
      if (tile) event.preventDefault();
    } else {
      const panned = this.spacePanned;
      this.spacePanned = false;
      if (panned || tile) event.preventDefault();
      if (
        wasHeld &&
        !panned &&
        tile &&
        !event.shiftKey &&
        !event.ctrlKey &&
        !event.metaKey &&
        !event.altKey
      )
        this.run("openStepDetails", target);
    }
  }
  dragStarted(event: FDragStartedEvent) {
    if (event.kind === "selection-area")
      this.box = { additive: this.boxAdditive, ids: [] };
    else {
      this.viewDragging = true;
      this.panning = true;
      clearTimeout(this.panningTimer);
    }
    this.cdr.markForCheck();
  }
  boxSelected(event: FSelectionChangeEvent) {
    if (this.box) this.box.ids = event.nodeIds;
  }
  /**
   * The boxed steps arrive right after the drag ends: apply them then. A
   * box drawn with Shift adds to the selection. Like a click, it never
   * lists a group together with a step inside it.
   */
  dragEnded() {
    if (this.viewDragging) {
      this.viewDragging = false;
      this.showWhilePanning();
    }
    const box = this.box;
    if (!box) return;
    queueMicrotask(() => {
      if (this.box !== box) return;
      this.box = null;
      const places = this.places();
      const ids = box.ids.filter((id) => places.has(id));
      const boxed = box.additive
        ? selectionOf(
            [...this.selection.ids, ...ids],
            ids[0] ?? this.selection.focus,
          )
        : selectionOf(ids);
      const next = selectionOf(topLevel(boxed, places), boxed.focus);
      // The next box starts from nothing: what it adds comes from this
      // selection, not from steps the library still holds.
      this.flow()?.clearSelection();
      this.selection = next;
      this.seenSelected = next.focus ?? "";
      const h = this.host();
      if (next.focus) void h.selectStep(next.focus, false);
      else void h.deselect();
      this.cdr.markForCheck();
    });
  }
  /** The view moved by a drag or the minimap. */
  canvasMoved(event: FCanvasChangeEvent) {
    const { x, y } = event.position;
    if (
      event.scale === this.view.zoom &&
      x === this.view.pan.x &&
      y === this.view.pan.y
    )
      return;
    this.setView({ zoom: event.scale, pan: { x, y } });
    this.showWhilePanning();
  }
  /** "Show minimap": kept for this viewer, or for this session when browser storage refuses. */
  toggleMinimap() {
    this.minimapOn = !this.minimapOn;
    if (!setMinimapPinned(this.minimapOn) && !this.minimapNoticeShown) {
      this.minimapNoticeShown = true;
      this.host().notify(CHOICE_NOT_KEPT);
    }
  }
  /** The minimap shows while the view moves, then fades. */
  private showWhilePanning() {
    this.panning = true;
    clearTimeout(this.panningTimer);
    this.panningTimer = setTimeout(() => {
      this.panning = false;
      this.cdr.markForCheck();
    }, 1200);
  }

  // ----------------------------------------------------------- keyboard

  /**
   * Keys on the canvas: the canvas rows of the shortcut table. Commands
   * about steps act from a step or the canvas itself; on a "+", a tool, a
   * toolbar or a menu, keys act on that control.
   */
  key(event: KeyboardEvent) {
    const into = event.target as HTMLElement;
    const noteId =
      into.closest?.("[data-note]")?.getAttribute("data-note") ?? "";
    // A note's text box keeps its keys, its own undo and the paste included.
    if (noteId && into.matches("textarea")) return;
    if (noteId && !into.closest("[role=menu], .sticky-note-toolbar")) {
      const command = noteCommand(event, this.platform);
      if (command && this.runNote(command, noteId, event.shiftKey)) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
    }
    this.trackSpace(event);
    const target = event.target as HTMLElement;
    // A control that handled the key, or an open menu, owns it.
    if (event.defaultPrevented || target.closest("[role=menu]")) return;
    const typing = typingIn(target);
    const onStep =
      target === this.root().nativeElement ||
      target.classList.contains("tile-body");
    const command = canvasCommand(event, {
      platform: this.platform,
      typing,
      onStep,
    });
    if (!command || !this.run(command, target)) return;
    event.preventDefault();
    event.stopPropagation();
  }
  /** Runs a command; false when it had nothing to do, so the key goes on. */
  run(command: KeymapCommand, target: HTMLElement): boolean {
    const h = this.host();
    const places = this.places();
    // Focus on a tile acts on it; focus on the canvas, on the selection's focus.
    const focus =
      target.closest("[data-tile]")?.getAttribute("data-tile") ??
      this.selection.focus ??
      "";
    const step = places.has(focus) ? focus : "";
    switch (command) {
      case "zoomIn":
        this.zoomBy(1.2);
        return true;
      case "zoomOut":
        this.zoomBy(1 / 1.2);
        return true;
      case "zoomReset":
        this.zoomBy(1 / this.view.zoom);
        return true;
      case "fitView":
        this.fit();
        return true;
      case "openStepDetails":
        if (focus.startsWith("$trigger:")) this.openTrigger(focus);
        else if (focus === "$end") void h.openWorkflowSection("spec/output");
        else if (step) void h.openStep(step, "details");
        else return false;
        return true;
      case "renameStep":
        if (!step) return false;
        void h.openStep(step, "rename");
        return true;
      case "openAddStep":
      case "searchAddStep":
        this.addStep(target, focus);
        return true;
      case "selectAll":
        return this.selectAll(step);
      case "togglePin": {
        const chosen = this.selection.ids.length
          ? [...this.selection.ids]
          : step
            ? [step]
            : [];
        if (!chosen.length) return false;
        const pinnable = chosen.filter(
          (id) => places.has(id) && this.pinnable(id),
        );
        if (pinnable.length) h.togglePins(pinnable);
        else h.notify("This step's output can't be pinned.");
        return true;
      }
      case "duplicate": {
        const chosen = this.keyTargets(target, step);
        const reason =
          this.lockedReason() ?? blockedReason("duplicate", chosen, places);
        if (reason) h.notify(reason);
        else void h.duplicateSteps(topLevel(chosen, places));
        return true;
      }
      case "deleteSelection": {
        const chosen = focus.startsWith("$trigger:")
          ? this.intentOf(focus)
          : null;
        if (chosen && target.closest("[data-trigger]")) {
          const reason = this.lockedReason();
          if (reason) h.notify(reason);
          else void h.removeTrigger(chosen.id);
          return true;
        }
        const ids = topLevel(this.keyTargets(target, step), places);
        if (!ids.length) return false;
        const reason = this.lockedReason();
        if (reason) h.notify(reason);
        else void h.removeSteps(ids);
        return true;
      }
      case "undo":
        h.undo();
        return true;
      case "redo":
        h.redo();
        return true;
      case "previousStep":
        return this.moveFocus(focus, "previous");
      case "nextStep":
        return this.moveFocus(focus, "next");
      case "laneAbove":
        return this.moveFocus(focus, "above");
      case "laneBelow":
        return this.moveFocus(focus, "below");
      case "extendUpstream":
      case "extendDownstream":
        return this.extend(
          step,
          command === "extendUpstream" ? "upstream" : "downstream",
        );
      case "copy":
      case "cut":
        void this.copySelection(
          command === "cut",
          this.keyTargets(target, step),
        );
        return true;
      case "paste":
        // The browser's paste event brings the clipboard's text: pasteEvent.
        return false;
      case "tidyLayout":
        this.tidy();
        return true;
      case "addStickyNote":
        this.addNote();
        return true;
      case "save":
        h.saveShortcut();
        return true;
      case "escape":
        return this.escape(target);
      case "showShortcuts":
        this.sheetOpen = true;
        return true;
      case "executeWorkflow":
        if (!h.executeWorkflow) return false;
        h.executeWorkflow();
        return true;
      default:
        return false;
    }
  }
  /**
   * The steps a key acts on: the selection when focus is on the canvas or
   * on a step the selection covers, else the step with focus alone. The
   * trigger and End are not steps.
   */
  private keyTargets(target: HTMLElement, step: string): Selection {
    if (!target.closest("[data-tile]")) return this.selection;
    if (!step) return NO_SELECTION;
    return covered(this.selection, this.places()).has(step)
      ? this.selection
      : only(step);
  }
  /** Makes a selection the canvas's, and its focus the editor's selected step. */
  private choose(next: Selection) {
    this.selection = next;
    this.seenSelected = next.focus ?? "";
    const h = this.host();
    if (next.focus) void h.selectStep(next.focus, false);
    else void h.deselect();
  }
  /** Arrow keys: focus (and select) the neighbor in that direction. */
  private moveFocus(from: string, direction: Direction): boolean {
    const next = neighbor(this.layout(), from || this.active(), direction);
    if (!next) return true;
    if (this.places().has(next)) this.choose(only(next));
    this.focusTileLater(next);
    return true;
  }
  /** Shift+← and Shift+→: add the step before or after the focused one, and focus it. */
  private extend(step: string, direction: "upstream" | "downstream"): boolean {
    if (!step) return false;
    const base = this.selection.ids.includes(step)
      ? { ...this.selection, focus: step }
      : only(step);
    const next = extended(base, this.places(), direction);
    this.choose(next);
    this.focusTileLater(next.focus ?? step);
    return true;
  }
  /**
   * Ctrl/Cmd+A: every step of the main sequence, which covers the steps
   * inside its groups. The focused step, or the group holding it, keeps focus.
   */
  private selectAll(step: string): boolean {
    const places = this.places();
    const ids = [...places.values()]
      .filter((place) => place.owner === "root")
      .map((place) => place.id);
    if (!ids.length) return false;
    let top = step;
    while (places.get(top)?.parent) top = places.get(top)!.parent!;
    this.choose(selectionOf(ids, top || ids[0]));
    return true;
  }
  private focusTileLater(id: string) {
    this.cdr.markForCheck();
    afterNextRender(
      () =>
        this.root()
          .nativeElement.querySelector<HTMLElement>(
            `[data-tile="${CSS.escape(id)}"] .tile-body`,
          )
          ?.focus(),
      { injector: this.injector },
    );
  }
  /**
   * N and /: today's step picker for the "+" with focus, after the step
   * with focus, or for the first step.
   */
  private addStep(target: HTMLElement, focus: string) {
    const h = this.host();
    const reason = this.lockedReason();
    if (reason) return h.notify(reason);
    const control = target.closest<HTMLElement>("[data-insert]");
    if (control) return this.insertAt(control);
    const root = this.root().nativeElement;
    if (!h.model.definition.spec.steps.length) {
      h.openPicker(
        { owner: "root", index: 0 },
        "Add first step",
        root.querySelector<HTMLElement>(".empty-trigger") ?? root,
      );
      return;
    }
    const tile = this.layout().tiles.find((item) => item.id === focus);
    const insert = tile?.after ?? {
      owner: "root",
      index: h.model.definition.spec.steps.length,
    };
    const label = !tile?.after
      ? "Add a step at the end"
      : tile.kind === "trigger"
        ? "Add a step at the start"
        : `Add a step after ${tile.id}`;
    const anchor =
      (tile &&
        root.querySelector<HTMLElement>(
          `[data-tile="${CSS.escape(tile.id)}"] .tile-body`,
        )) ||
      root;
    h.openPicker(insert, label, anchor);
  }
  /**
   * Escape on the canvas, one meaning per press: end a drag or "Move to…",
   * else hide the toolbars on screen, else clear the selection. A field, a
   * dialog or an open menu takes Escape first.
   */
  private escape(target: HTMLElement): boolean {
    if (this.placingNote) {
      this.placingNote = "";
      this.cdr.markForCheck();
      return true;
    }
    if (this.endGesture() || this.hideToolbars(target)) return true;
    if (!this.selection.ids.length) return false;
    const focus = this.selection.focus;
    this.clearSelection();
    // The selection toolbar goes with the selection: focus stays on the canvas.
    if (focus && target.closest(".selection-toolbar"))
      this.focusTileLater(focus);
    return true;
  }
  /** Ends a drag, or a step "Move to…" is placing; false when none is under way. */
  private endGesture(): boolean {
    const h = this.host();
    if (this.dragging()) {
      // Letting go of the pointer then drops nothing and clicks nothing.
      this.cancelledPointer =
        this.panPress?.pointer ?? this.press?.pointer ?? null;
      this.cancelPress();
      return true;
    }
    if (!h.connectingNode && !h.dragPreview) return false;
    h.cancelMove();
    return true;
  }
  // ------------------------------------------------------------- notes

  private readonly notesMemo = memo(
    (layout: LtrLayout, notes: StickyNote[] | undefined): NoteView[] =>
      (notes ?? []).map((note) => {
        const tile = note.anchor
          ? layout.tiles.find((item) => item.id === note.anchor!.stepId)
          : undefined;
        return {
          id: note.id,
          text: note.text,
          color: note.color,
          x: tile ? tile.x + note.anchor!.dx : note.x,
          y: tile ? tile.y + note.anchor!.dy : note.y,
          width: note.width,
          height: note.height,
          anchor: tile?.id ?? "",
          title: noteTitle(note.text),
        };
      }),
  );
  noteViews(): NoteView[] {
    return this.notesMemo(this.layout(), this.host().model.canvas.notes);
  }
  /**
   * Shift+S and the tools' note: under the selected step (anchored), else at
   * the view's center; then the nearest free spot. A step's note keeps clear
   * of every step, name, path label, "+" and other note at this zoom; a free
   * note keeps clear of other notes (Tidy layout moves it off any step it
   * covers), so a second Shift+S never stacks on the first.
   */
  addNote() {
    const h = this.host();
    const reason = this.lockedReason();
    if (reason)
      return h.notify(
        h.model.readonly ? "Fix the source before adding notes." : reason,
      );
    const focus = this.selection.focus ?? h.model.selected;
    const tile = this.layout().tiles.find(
      (item) => item.id === focus && item.step,
    );
    const below = LTR.tile + LTR.labelGap + LTR.labelHeight + 16;
    const box = this.root().nativeElement.getBoundingClientRect();
    const center = this.toWorld(
      box.left + box.width / 2,
      box.top + box.height / 2,
    );
    const notes: Box[] = this.noteViews();
    const spot = freeSpot(
      {
        ...(tile
          ? { x: tile.x, y: tile.y + below }
          : {
              x: center.x - NOTE_SIZES.small.width / 2,
              y: center.y - NOTE_SIZES.small.height / 2,
            }),
        ...NOTE_SIZES.small,
      },
      tile
        ? [...noteObstacles(this.layout(), this.view.zoom), ...notes]
        : notes,
    );
    let added: { canvas: CanvasSidecar; id: string };
    try {
      added = addNote(
        h.model.canvas,
        spot,
        tile
          ? { stepId: tile.id, dx: spot.x - tile.x, dy: spot.y - tile.y }
          : null,
      );
    } catch (error) {
      return h.notify(error instanceof Error ? error.message : String(error));
    }
    if (!h.changeCanvas(added.canvas)) return;
    this.editingNote = added.id;
    this.cdr.markForCheck();
  }
  noteEdit(edit: NoteEdit) {
    const h = this.host();
    if (edit.kind === "place") {
      this.placingNote = edit.id;
      this.cdr.markForCheck();
      return;
    }
    if (edit.kind === "remove") {
      if (!h.changeCanvas(removeNote(h.model.canvas, edit.id))) return;
      const revision = h.model.revision;
      h.notify("Deleted the note.", {
        label: "Undo",
        run: () =>
          h.model.revision === revision
            ? h.undo()
            : h.notify("The workflow changed since. Use Undo in the toolbar."),
      });
      return;
    }
    const patch: NotePatch =
      edit.kind === "text"
        ? { text: edit.text }
        : edit.kind === "color"
          ? { color: edit.color }
          : this.boxPatch(edit);
    let next: CanvasSidecar;
    try {
      next = updateNote(h.model.canvas, edit.id, patch);
    } catch (error) {
      return h.notify(error instanceof Error ? error.message : String(error));
    }
    if (next !== h.model.canvas) h.changeCanvas(next);
  } /** Tidy layout: legacy positions go, free notes leave the steps they cover, then Fit view. */
  tidy() {
    const h = this.host();
    const moves = tidyNotes(
      this.noteViews().map((note) => ({ ...note, anchored: !!note.anchor })),
      tileBoxes(this.layout()),
    );
    const canvas = h.model.canvas;
    const next = moves.size
      ? {
          ...canvas,
          notes: (canvas.notes ?? []).map((note) => {
            const to = moves.get(note.id);
            return to ? { ...note, x: to.x, y: to.y } : note;
          }),
        }
      : null;
    if (next && canvasBytes(next) > CANVAS_MAX_BYTES)
      return h.notify(CANVAS_TOO_BIG);
    if (h.tidyCanvas(next)) this.fit();
  }

  /** A moved anchored note keeps following its step from its new offset. */
  private boxPatch(edit: {
    id: string;
    x: number;
    y: number;
    width: number;
    height: number;
  }): NotePatch {
    const note = this.host().model.canvas.notes?.find(
      (item) => item.id === edit.id,
    );
    const tile = note?.anchor
      ? this.layout().tiles.find((item) => item.id === note.anchor!.stepId)
      : undefined;
    return {
      x: edit.x,
      y: edit.y,
      width: edit.width,
      height: edit.height,
      ...(note?.anchor && tile
        ? {
            anchor: {
              ...note.anchor,
              dx: edit.x - tile.x,
              dy: edit.y - tile.y,
            },
          }
        : {}),
    };
  }
  /** A focused note's keys: Enter edits, Delete deletes, arrows move it (Shift: farther). */
  private runNote(command: KeymapCommand, id: string, far: boolean): boolean {
    const note = this.noteViews().find((item) => item.id === id);
    if (!note || this.locked()) return false;
    const by = far ? 32 : 8;
    const shift = (dx: number, dy: number) =>
      this.noteEdit({
        kind: "box",
        id,
        x: note.x + dx,
        y: note.y + dy,
        width: note.width,
        height: note.height,
      });
    switch (command) {
      case "editNote":
        this.editingNote = id;
        this.cdr.markForCheck();
        return true;
      case "deleteNote":
        this.noteEdit({ kind: "remove", id });
        this.root().nativeElement.focus();
        return true;
      case "nudgeNoteLeft":
        shift(-by, 0);
        return true;
      case "nudgeNoteRight":
        shift(by, 0);
        return true;
      case "nudgeNoteUp":
        shift(0, -by);
        return true;
      case "nudgeNoteDown":
        shift(0, by);
        return true;
      default:
        return false;
    }
  }

  // ------------------------------------------------------ copy and paste

  copyBlocked(): string | null {
    return blockedReason("copy", this.selection, this.places());
  }
  /**
   * Ctrl/Cmd+C or X and the selection toolbar's Copy: the steps a key acts
   * on (the selection, or the focused step outside it), else the selection.
   */
  async copySelection(cut: boolean, chosen: Selection = this.selection) {
    const places = this.places();
    const reason =
      (cut ? this.lockedReason() : null) ??
      blockedReason(cut ? "cut" : "copy", chosen, places);
    if (reason) return this.host().notify(reason);
    await this.copyIds(topLevel(chosen, places), cut);
  }
  async copyIds(ids: string[], cut: boolean) {
    const h = this.host();
    // A cut that can't delete says why and leaves the clipboard as it was.
    const blocked = cut ? h.model.removalBlocked(ids) : null;
    if (blocked) return h.notify(blocked);
    const copied = copySteps(
      h.model.definition,
      ids,
      h.pinsForCopy(),
      ownedActionsOf(h.model.canvas),
    );
    if (!(await this.writeClipboard(copied)))
      return h.notify("Studio couldn't write to the clipboard. Try again.");
    if (cut) await h.removeSteps(ids);
    else
      h.notify(`Copied ${ids.length === 1 ? ids[0] : `${ids.length} steps`}.`);
  }
  /** YAML as text, and the same document as JSON in the web custom format where the browser has it. */
  private async writeClipboard(copied: Copied): Promise<boolean> {
    try {
      const item = globalThis.ClipboardItem as
        | (typeof ClipboardItem & { supports?: (type: string) => boolean })
        | undefined;
      if (item?.supports?.(CLIPBOARD_WEB_FORMAT))
        await navigator.clipboard.write([
          new item({
            "text/plain": new Blob([copied.yaml], { type: "text/plain" }),
            [CLIPBOARD_WEB_FORMAT]: new Blob([copied.json], {
              type: CLIPBOARD_WEB_FORMAT,
            }),
          }),
        ]);
      else await navigator.clipboard.writeText(copied.yaml);
      return true;
    } catch {
      return false;
    }
  }
  /** Ctrl/Cmd+V on the canvas: the browser's paste event, read without a permission prompt. */
  pasteEvent(event: ClipboardEvent) {
    const target = event.target as HTMLElement;
    if (typingIn(target)) return;
    event.preventDefault();
    // Only the text: a paste event doesn't expose the web custom format copy
    // also writes, and the text holds the same document.
    void this.paste(
      event.clipboardData?.getData("text/plain") ?? "",
      this.pastePlace(target),
    );
  }
  /** At the focused "+", else after the focused or selected step, else at the end. */
  private pastePlace(target: HTMLElement): Insertion {
    const control = target.closest?.("[data-insert]");
    const insert = control ? this.insertOf(control) : null;
    if (insert) return insert;
    const id =
      target.closest?.("[data-tile]")?.getAttribute("data-tile") ??
      this.selection.focus ??
      this.host().model.selected;
    const tile = this.layout().tiles.find((item) => item.id === id);
    return (
      tile?.after ?? {
        owner: "root",
        index: this.host().model.definition.spec.steps.length,
      }
    );
  }
  async paste(text: string, at: Insertion) {
    const h = this.host();
    const reason = this.lockedReason();
    if (reason)
      return h.notify(
        h.model.readonly ? "Fix the source before pasting steps." : reason,
      );
    const reading = readClipboard(text);
    if (!reading.ok) return h.notify(reading.reason);
    const plan = await h.pasteSteps(reading.content, at);
    if (!plan) return;
    for (const gap of plan.unreadable)
      this.pasteGaps.set(gap.stepId, [
        ...(this.pasteGaps.get(gap.stepId) ?? []),
        gap,
      ]);
    this.pasteGapsVersion++;
    if (plan.unreadable.length) {
      const first = plan.unreadable[0].stepId;
      h.notify(unreadableMessage(plan.unreadable), {
        label: "Review",
        run: () => void h.selectStep(first, true),
      });
    }
    this.selection = selectionOf(plan.ids, plan.ids[0]);
    this.seenSelected = plan.ids[0];
    this.cdr.markForCheck();
  }

  private setView(view: View) {
    this.view = view;
    this.cdr.markForCheck();
  }
}
