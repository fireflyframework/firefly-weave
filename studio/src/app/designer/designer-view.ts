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
// The workflow designer: the editor bar, the step palette, the canvas (or
// the source and outline), the inspector, the docked simulation and the
// diagnostics strip. Loaded lazily (@defer) when a workflow opens, so the
// initial bundle stays small; the shell passes itself as the host and keeps
// owning the workflow and every command.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { FFlowModule } from "@foblex/flow";
import { Icon } from "../icon";
import { StepDetailsMount } from "../editor/ndv/step-details-mount";
import { ModalSheet } from "../modal-sheet";
import { RowMenu } from "../row-menu";
import { StepPropertyGrid } from "../property-grid";
import { PaletteIntegrations } from "../integrations/palette-integrations";
import { ConnectionSlotList } from "../integrations/connection-slot-list";
import { ActionInspector } from "./action-inspector";
import { DecisionInspector } from "./decision-inspector";
import { LlmInspector } from "./llm-inspector";
import { HumanInspector } from "./human-inspector";
import { PathInspector } from "./path-inspector";
import { DiagnosticsList } from "./diagnostics-list";
import { SimulationPanel } from "./simulation/simulation-panel";
import { CanvasView } from "../editor/canvas/canvas-view";
import type { App } from "../app";

@Component({
  selector: "weave-designer",
  // A view of the shell's state: checked whenever the shell renders.
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [
    Icon,
    StepDetailsMount,
    FFlowModule,
    ModalSheet,
    RowMenu,
    StepPropertyGrid,
    PaletteIntegrations,
    ActionInspector,
    DecisionInspector,
    LlmInspector,
    HumanInspector,
    PathInspector,
    ConnectionSlotList,
    DiagnosticsList,
    SimulationPanel,
    CanvasView,
  ],
  // No box of its own: the bar, the grid and the strip lay out in the
  // shell's main column.
  styles: [":host { display: contents; }"],
  template: `@let h = host();
    <!-- Identity and views above an aligned command row. -->
    <div class="editor-bar">
      <div class="editor-heading">
        <div class="editor-identity">
          <button
            type="button"
            class="back-link"
            (click)="h.navigate('workflows')"
          >
            Workflows /
          </button>
          <h1 [attr.title]="h.model.definition.metadata.name">
            {{ h.model.definition.metadata.name }}
          </h1>
          <span class="tag version-badge">{{
            h.model.definition.metadata.version
          }}</span>
          @if (h.statusChip; as chip) {
            <span
              class="pill status-chip"
              [attr.data-tone]="chip.tone || null"
              [attr.title]="chip.text"
              ><span class="status-text">{{ chip.text }}</span></span
            >
          }
        </div>
        <ol class="workflow-progress" aria-label="Workflow progress">
          <li
            [attr.aria-current]="
              h.lifecycle !== 'published' && h.lifecycle !== 'active'
                ? 'step'
                : null
            "
          >
            Draft
          </li>
          <li [attr.aria-current]="h.lifecycle === 'published' ? 'step' : null">
            Published
          </li>
          <li [attr.aria-current]="h.lifecycle === 'active' ? 'step' : null">
            Active
          </li>
        </ol>
      </div>
      <div
        class="editor-views"
        role="tablist"
        aria-label="Workflow views"
        (keydown)="h.viewKey($event)"
      >
        @for (t of h.editorViews; track t) {
          <button
            type="button"
            role="tab"
            [id]="'view-tab-' + t.toLowerCase()"
            aria-controls="editor-view"
            [attr.aria-selected]="h.tab === t"
            [attr.tabindex]="h.tab === t ? 0 : -1"
            (click)="h.selectTab(t)"
          >
            {{ t }}
          </button>
        }
      </div>
      <div class="editor-toolbar" role="toolbar" aria-label="Workflow commands">
        <div class="toolbar-group" role="group" aria-label="Edit workflow">
          <button
            type="button"
            class="insert-popover-button"
            aria-label="Insert step"
            [attr.aria-expanded]="h.showPalette"
            [attr.aria-disabled]="h.editingLocked || null"
            (click)="!h.editingLocked && (h.showPalette = !h.showPalette)"
          >
            <weave-icon name="plus" /><span class="insert-label"
              >Insert step</span
            >
          </button>
          <button
            type="button"
            class="icon-button wide-only"
            aria-label="Undo"
            title="Undo (⌘/Ctrl Z)"
            [disabled]="!h.model.canUndo || h.editingLocked"
            (click)="h.undo()"
          >
            <weave-icon name="undo" /></button
          ><button
            type="button"
            class="icon-button wide-only"
            aria-label="Redo"
            title="Redo (⌘/Ctrl Shift Z)"
            [disabled]="!h.model.canRedo || h.editingLocked"
            (click)="h.redo()"
          >
            <weave-icon name="redo" />
          </button>
        </div>
        <div class="toolbar-group" role="group" aria-label="Check workflow">
          <button
            type="button"
            [attr.aria-disabled]="h.busy !== '' || null"
            [attr.aria-label]="
              h.busy === 'validate' ? 'Validating…' : 'Validate'
            "
            title="Validate"
            (click)="h.runCommand('validate')"
          >
            <weave-icon name="check" /><span class="validate-label">{{
              h.busy === "validate" ? "Validating…" : "Validate"
            }}</span>
          </button>
          <button
            type="button"
            class="wide-only"
            [attr.aria-disabled]="!!h.blocker('simulate') || null"
            [attr.aria-describedby]="
              h.blocker('simulate') ? 'simulate-blocked' : null
            "
            (click)="h.runCommand('simulate')"
          >
            {{ h.busy === "simulate" ? "Simulating…" : "Simulate" }}
          </button>
        </div>
        @if (!h.profile) {
          <details
            #localHelp
            class="toolbar-help"
            (keydown.escape)="closeHelp(localHelp, $event)"
          >
            <summary
              aria-label="About local authoring"
              title="About local authoring"
            >
              i
            </summary>
            <div class="toolbar-help-content">
              <p>
                Your draft is kept on this computer. Save to file downloads a
                copy. Connect to a platform to publish, activate and run it.
              </p>
              <button
                type="button"
                class="text-link"
                (click)="h.menuChoice('connect')"
              >
                Connect to a platform
              </button>
            </div>
          </details>
        }
        <div
          class="toolbar-group toolbar-lifecycle"
          role="group"
          aria-label="Save and run"
        >
          @if (h.profile) {
            <button
              type="button"
              class="primary"
              [attr.aria-disabled]="h.primaryBlocker ? true : null"
              [attr.aria-describedby]="
                h.primaryBlocker ? 'primary-blocked' : null
              "
              (click)="h.runCommand(h.primaryCommand)"
            >
              {{ h.primaryLabel }}
            </button>
          } @else {
            <button
              type="button"
              class="primary"
              title="Save to file (⌘/Ctrl S)"
              aria-label="Save to file"
              (click)="h.runCommand('export')"
            >
              <weave-icon name="download" /><span class="save-label"
                >Save to file</span
              ><span class="compact-save-label">Save</span>
            </button>
          }
          @if (h.moreItems.length) {
            <weave-row-menu label="More" text="More" [items]="h.moreItems" />
          }
          <button
            type="button"
            class="icon-button inspector-toggle wide-only"
            aria-controls="inspector"
            [attr.aria-label]="
              h.showInspector ? 'Hide inspector' : 'Show inspector'
            "
            [attr.title]="h.showInspector ? 'Hide inspector' : 'Show inspector'"
            [attr.aria-pressed]="h.showInspector"
            [attr.aria-expanded]="h.showInspector"
            (click)="h.toggleInspector()"
          >
            <weave-icon name="panel" />
          </button>
        </div>
      </div>
    </div>

    <!-- Why a command the person pressed can't run. -->
    <span class="sr-only" id="simulate-blocked">{{
      h.blocker("simulate")
    }}</span>
    <span class="sr-only" id="primary-blocked">{{ h.primaryBlocker }}</span>
    <p class="command-note" role="status">{{ h.commandNote }}</p>
    <div
      class="editor-grid"
      [class.side-open]="
        (h.showInspector || !!h.simulationSession) && h.tab !== 'Source'
      "
      [class.source-mode]="h.tab === 'Source'"
      [class.simulating]="!!h.simulationSession"
      [style.--palette-w.px]="h.paletteWidth"
      [style.--inspector-w.px]="h.inspectorWidth"
    >
      <!-- A section, not an aside: as a modal sheet it takes role="dialog". -->
      <section
        class="palette"
        role="complementary"
        aria-labelledby="palette-title"
        [class.popover-visible]="h.showPalette"
        [attr.inert]="h.editingLocked || null"
        [weaveModalSheet]="h.showPalette"
        [sheetWhen]="h.sheetWhen.palette"
        sheetInitialFocus=".palette .search-field input"
        (sheetDismiss)="h.showPalette = false"
      >
        <button
          class="pane-resizer"
          role="separator"
          aria-label="Resize step palette"
          aria-orientation="vertical"
          [attr.aria-valuenow]="h.paletteWidth"
          aria-valuemin="176"
          aria-valuemax="256"
          (pointerdown)="h.paneResize($event, 'palette')"
          (pointermove)="h.paneMove($event)"
          (pointerup)="h.resizing = null"
          (keydown)="h.paneKey($event, 'palette')"
        ></button>
        <header class="palette-header">
          <div class="palette-heading-row">
            <h2 id="palette-title">Steps</h2>
            <details
              #paletteHelp
              class="palette-help"
              (keydown.escape)="closeHelp(paletteHelp, $event)"
            >
              <summary aria-label="How to add steps" title="How to add steps">
                i
              </summary>
              <p class="pane-help">
                Click to add after the selected step, or drag onto a + on the
                canvas.
              </p>
            </details>
          </div>
          <label class="search-field"
            ><weave-icon name="search" /><input
              aria-label="Search steps"
              placeholder="Search steps"
              [value]="h.paletteQuery"
              (input)="h.paletteQuery = h.value($event)"
          /></label>
        </header>
        @for (group of h.palette; track group.label) {
          <div
            class="palette-group"
            role="group"
            [attr.aria-labelledby]="
              'palette-group-' + group.label.toLowerCase()
            "
          >
            <h3
              class="palette-heading"
              [id]="'palette-group-' + group.label.toLowerCase()"
            >
              {{ group.label }}
            </h3>
            @for (kind of group.kinds; track kind) {
              <button
                class="palette-step"
                [draggable]="!h.model.readonly"
                [attr.title]="h.description(kind)"
                [attr.aria-description]="h.description(kind)"
                (dragstart)="h.dragPalette($event, kind)"
                (dragend)="h.dragPreview = null"
                (click)="h.insert(kind)"
                [disabled]="h.model.readonly"
              >
                <span class="step-icon"><weave-icon [name]="kind" /></span
                ><span class="palette-label">{{ h.label(kind) }}</span
                ><span class="drag-grip" aria-hidden="true"
                  ><weave-icon name="menu" [size]="16"
                /></span>
              </button>
            }
            @if (group.label === "Actions") {
              @defer (on immediate) {
                <weave-palette-integrations
                  [host]="h"
                  (connect)="h.showPalette = false; h.menuChoice('connect')"
                  [query]="h.paletteQuery"
                />
              }
            }
          </div>
        }
      </section>
      <section
        class="canvas-panel"
        id="editor-view"
        role="tabpanel"
        [attr.aria-labelledby]="'view-tab-' + h.tab.toLowerCase()"
      >
        @if (h.simulationSession) {
          <div class="editing-paused" role="status">
            <weave-icon name="lock" [size]="16" /><span
              >Simulating. Editing is paused.</span
            ><button type="button" class="sm" (click)="h.closeSimulation()">
              Stop simulation
            </button>
          </div>
        }
        @if (h.tab === "Source") {
          <div class="source-heading">
            <div>
              <h2>Workflow source</h2>
              <p>
                {{
                  h.model.readonly
                    ? "Read-only until the source is fixed. The last valid graph is kept."
                    : "YAML and graph share one workflow definition."
                }}
              </p>
            </div>
            <select
              aria-label="Source format"
              [value]="h.model.format"
              (change)="h.changeFormat($event)"
            >
              <option value="yaml">YAML</option>
              <option value="json">JSON</option></select
            ><button
              class="primary"
              [attr.aria-disabled]="h.editingLocked || null"
              (click)="!h.editingLocked && h.updateSource()"
            >
              Apply changes
            </button>
          </div>
          <textarea
            class="source-editor"
            spellcheck="false"
            wrap="off"
            aria-label="Workflow source"
            [readOnly]="h.editingLocked"
            [value]="h.sourceBuffer"
            (input)="h.sourceInput($event)"
          ></textarea>
          @if (h.model.error) {
            <div class="source-error" role="alert">{{ h.model.error }}</div>
          }
        } @else if (h.tab === "Outline") {
          <div class="outline-heading">
            <h2>Outline</h2>
            <p>
              Arrow keys move through the steps. Select one to edit it in the
              inspector.
            </p>
          </div>
          @if (h.outlineNotice) {
            <div class="notice outline-notice" data-tone="info">
              <p>Editing on the canvas works best on a wider screen.</p>
              <button
                type="button"
                class="sm"
                (click)="h.selectTab('Designer')"
              >
                Show canvas
              </button>
            </div>
          }
          <div class="outline" role="tree" aria-label="Workflow steps">
            @for (node of h.nodes; track node.step.id) {
              <button
                role="treeitem"
                [attr.data-outline]="node.step.id"
                [attr.aria-level]="node.depth + 1"
                [attr.aria-selected]="h.model.selected === node.step.id"
                [attr.tabindex]="h.outlineStop(node.step.id, $index)"
                [style.margin-left.px]="node.depth * 24"
                (click)="h.select(node)"
                (keydown)="h.outlineKey($event, $index)"
              >
                <weave-icon [name]="node.step.kind" /><strong>{{
                  node.step.id
                }}</strong
                ><span>{{ h.outlineOwner(node) }}</span>
              </button>
            }
            @if (!h.nodes.length) {
              <p>No steps yet. Insert a step from the palette.</p>
            }
          </div>
        } @else if (h.editorNext) {
          @defer (on immediate) {
            <weave-canvas-view [host]="h" />
          } @placeholder {
            <p class="connecting-status designer-loading" role="status">
              <span class="loading-spinner small"></span>Opening the canvas…
            </p>
          }
        } @else {
          <div
            class="canvas"
            role="group"
            aria-roledescription="workflow canvas"
            aria-label="Workflow canvas"
            aria-describedby="canvas-help"
            tabindex="0"
            [class.overview]="h.overview"
            [class.tiny-overview]="h.zoom < 0.4"
            [class.moving]="!!h.connectingNode"
            [class.simulating]="!!h.simulationSession"
            (wheel)="h.panCanvas($event)"
            (scroll)="h.scrollToPan($event)"
            (dragover)="h.allowDrop($event)"
            (drop)="h.drop($event)"
            (pointerdown)="h.canvasPointerDown($event)"
            (pointerup)="h.canvasPointerUp($event)"
          >
            <div class="canvas-context">
              <span class="canvas-chip"
                ><weave-icon name="workflows" [size]="16" />{{
                  h.nodes.length
                }}
                {{ h.nodes.length === 1 ? "step" : "steps" }}</span
              >
              @if (h.model.readonly) {
                <span class="canvas-chip warning"
                  >Read-only until the source is fixed</span
                >
              }
              @if (h.overview) {
                <span class="canvas-chip">{{
                  h.zoom < 0.4
                    ? "Overview · zoom in to insert"
                    : "Overview · zoom in for details"
                }}</span>
              }
              @if (h.connectingNode) {
                <span class="canvas-chip info"
                  >Moving {{ h.connectingNode }}: choose a +</span
                >
              }
            </div>
            <f-flow
              class="graph-flow"
              [style.--zoom]="h.zoom"
              (fFullRendered)="h.fitAfterRender()"
              (scroll)="h.scrollToPan($event)"
              ><f-canvas [position]="h.pan" [scale]="h.zoom"
                ><div fNodes>
                  <svg
                    class="edges"
                    [attr.width]="h.graphWidth()"
                    [attr.height]="h.graphHeight()"
                    aria-hidden="true"
                  >
                    <defs>
                      <marker
                        id="arrow"
                        viewBox="0 0 10 10"
                        refX="9"
                        refY="5"
                        markerWidth="6"
                        markerHeight="6"
                        orient="auto"
                      >
                        <path class="edge-arrow" d="M0 0L10 5L0 10Z" />
                      </marker>
                      <marker
                        id="arrow-taken"
                        viewBox="0 0 10 10"
                        refX="9"
                        refY="5"
                        markerWidth="6"
                        markerHeight="6"
                        orient="auto"
                      >
                        <path class="edge-arrow taken" d="M0 0L10 5L0 10Z" />
                      </marker>
                    </defs>
                    @for (edge of h.edgePaths; track edge.key) {
                      <path
                        class="edge"
                        [attr.data-from]="edge.key.split('>')[0]"
                        [class.edge-taken]="h.edgeTaken(edge.key)"
                        [attr.d]="edge.d"
                        fill="none"
                        [attr.marker-end]="
                          h.edgeTaken(edge.key)
                            ? 'url(#arrow-taken)'
                            : 'url(#arrow)'
                        "
                      />
                    }
                  </svg>
                  @for (group of h.laneGroups; track group.id) {
                    <div
                      class="lane-group"
                      [style.left.px]="group.point.x"
                      [style.top.px]="group.point.y"
                      [style.width.px]="group.width"
                      [style.height.px]="group.height"
                      aria-hidden="true"
                    ></div>
                    @for (lane of group.lanes; track lane.owner) {
                      <div
                        class="lane-header"
                        [attr.data-owner]="lane.owner"
                        [style.left.px]="lane.point.x"
                        [style.top.px]="lane.point.y"
                        [style.width.px]="lane.width"
                      >
                        {{ lane.label }}
                      </div>
                    }
                  }
                  @for (terminal of h.laneTerminals; track terminal.step) {
                    <span
                      class="lane-terminal"
                      [attr.data-terminal]="terminal.step"
                      [style.left.px]="terminal.point.x - 6"
                      [style.top.px]="terminal.point.y - 6"
                      role="img"
                      aria-label="This path ends here"
                    ></span>
                  }
                  <div
                    fNode
                    fNodeId="$start"
                    [fNodePosition]="h.boundaries.start"
                    class="boundary-node start-node"
                  >
                    <button
                      type="button"
                      aria-label="Start — workflow settings"
                      (dblclick)="h.openStepDetails('$trigger')"
                      (keydown.enter)="h.openTriggerDetails($event)"
                      title="Workflow settings"
                      (click)="h.openWorkflowSettings()"
                    >
                      <weave-icon name="play" />Start
                    </button>
                  </div>
                  <div
                    fNode
                    fNodeId="$end"
                    [fNodePosition]="h.boundaries.end"
                    class="boundary-node end"
                    role="img"
                    aria-label="Workflow end"
                  >
                    <weave-icon name="check" />End
                  </div>
                  @for (node of h.nodes; track node.step.id) {
                    @let info = h.nodeInfo(node);
                    @let sim = h.simState(node.step.id);
                    <div
                      fNode
                      [fNodeId]="node.step.id"
                      [fNodePosition]="h.nodePoint(node)"
                      class="graph-node"
                      [class.selected]="h.model.selected === node.step.id"
                      [class.decision]="node.step.kind === 'switch'"
                      [class.dirty]="h.dirtyStep === node.step.id"
                      [class.has-error]="info.status === 'error'"
                      [class.has-warning]="info.status === 'warning'"
                      [class.incomplete]="info.status === 'incomplete'"
                      [class.is-live]="sim === 'live'"
                      [class.pulse]="sim === 'live'"
                      [class.is-done]="sim === 'done'"
                      [class.is-unreached]="sim === 'unreached'"
                      [class.sim-breakpoint]="
                        h.simNodes.breakpoints.includes(node.step.id)
                      "
                      [attr.data-step]="node.step.id"
                      [class.dragging]="h.dragNode?.id === node.step.id"
                      (contextmenu)="
                        $event.preventDefault(); nodeMenu.toggleMenu()
                      "
                    >
                      <button
                        class="node-body"
                        [attr.title]="h.label(node.step.kind)"
                        (pointerdown)="h.pointerDown($event, node)"
                        (pointermove)="h.pointerMove($event)"
                        (pointerup)="h.pointerUp($event)"
                        (pointercancel)="h.cancelGesture()"
                        (click)="h.nodeClick(node)"
                        (dblclick)="
                          h.openStepDetails(h.model.selected || node.step.id)
                        "
                        (focus)="h.nodeFocused(node)"
                        (keydown)="h.nodeKey($event, node)"
                        [attr.aria-label]="
                          info.label + h.simulationNote(node.step.id)
                        "
                      >
                        <span class="step-icon"
                          ><weave-icon [name]="node.step.kind" /></span
                        ><span class="node-text"
                          ><strong class="node-title">{{ node.step.id }}</strong
                          ><small class="node-summary">{{
                            info.summary || h.label(node.step.kind)
                          }}</small></span
                        >
                        @if (sim === "done") {
                          <span class="node-badge" aria-hidden="true"
                            ><weave-icon name="check" [size]="16"
                          /></span>
                        }
                      </button>
                      <weave-row-menu
                        #nodeMenu
                        class="node-actions"
                        [class.menu-open]="nodeMenu.open()"
                        [label]="'Actions for ' + node.step.id"
                        [items]="h.nodeActions(node)"
                      />
                      @if (
                        node.answerPrompt &&
                        !h.editingLocked &&
                        !h.model.readonly
                      ) {
                        <button
                          type="button"
                          class="answer-branch-prompt"
                          (click)="h.branchOnDecision(node.step.id)"
                        >
                          <weave-icon name="switch" [size]="16" />{{
                            node.answerPrompt
                          }}
                        </button>
                      }
                      @if (info.chip) {
                        <button
                          type="button"
                          class="node-chip"
                          [attr.data-tone]="info.status"
                          [attr.aria-label]="info.chip"
                          [attr.title]="info.chip"
                          (click)="h.openNodeIssue(node)"
                        >
                          <weave-icon
                            [name]="
                              info.status === 'error' ? 'failCircle' : 'warning'
                            "
                            [size]="16"
                          /><span class="node-chip-text" aria-hidden="true">{{
                            info.chip
                          }}</span>
                        </button>
                      }
                      @if (h.connectingNode) {
                        <span class="port input-port" aria-hidden="true"></span
                        ><span
                          class="port output-port"
                          [class.connecting]="h.connectingNode === node.step.id"
                          aria-hidden="true"
                        ></span>
                      }
                    </div>
                  }
                  @if (!h.nodes.length && !h.model.readonly) {
                    <div
                      class="canvas-empty"
                      [style.left.px]="h.boundaries.start.x"
                      [style.top.px]="h.boundaries.start.y + 84"
                    >
                      <button
                        type="button"
                        class="first-step"
                        aria-haspopup="dialog"
                        (dragover)="h.allowDrop($event)"
                        (drop)="h.drop($event, h.targets[0])"
                        [attr.aria-expanded]="h.pickerOpenAt(h.targets[0])"
                        [attr.aria-disabled]="h.editingLocked || null"
                        (click)="h.connectTo(h.targets[0], $event)"
                      >
                        <weave-icon name="plus" />Add your first step
                      </button>
                      <button
                        type="button"
                        class="text-link"
                        (click)="h.showTemplates = true"
                      >
                        Start from a template
                      </button>
                    </div>
                  }
                  @for (
                    target of h.nodes.length && !h.editingLocked
                      ? h.targets
                      : [];
                    track target.owner + ":" + target.index
                  ) {
                    <button
                      class="insertion-target"
                      [class.branch-placeholder]="!!target.empty"
                      [class.drop-active]="
                        h.dragPreview || h.validMoveTarget(target)
                      "
                      [class.drop-hover]="
                        h.dragTarget?.owner === target.owner &&
                        h.dragTarget.index === target.index
                      "
                      [attr.data-owner]="target.owner"
                      [attr.data-index]="target.index"
                      (focus)="h.targetFocused(target)"
                      [style.left.px]="target.point.x"
                      [style.top.px]="target.point.y"
                      [attr.aria-label]="
                        h.connectingNode
                          ? target.label.replace(
                              'Add a step here',
                              'Move ' + h.connectingNode + ' here'
                            )
                          : target.label
                      "
                      [attr.title]="
                        target.empty ? target.empty : 'Add a step here'
                      "
                      [attr.aria-haspopup]="h.connectingNode ? null : 'dialog'"
                      [attr.aria-expanded]="
                        h.connectingNode ? null : h.pickerOpenAt(target)
                      "
                      (dragover)="h.allowDrop($event)"
                      (drop)="h.drop($event, target)"
                      (click)="h.connectTo(target, $event)"
                    >
                      @if (target.empty) {
                        <span class="placeholder-branch">{{
                          h.shortLabel(target.empty)
                        }}</span
                        ><span class="placeholder-hint"
                          ><weave-icon name="plus" [size]="16" />Add a
                          step</span
                        >
                      } @else {
                        <weave-icon name="plus" [size]="16" />
                        @if (target.lane) {
                          <span class="lane-add-label"
                            >Add to {{ target.lane }}</span
                          >
                        }
                      }
                    </button>
                  }
                </div></f-canvas
              ></f-flow
            >
            <div class="canvas-tools" role="toolbar" aria-label="Canvas view">
              <button
                type="button"
                class="icon-button"
                aria-label="Zoom out"
                title="Zoom out (−)"
                (click)="h.zoomBy(1 / 1.2)"
              >
                −</button
              ><!-- The current zoom; a click goes back to 100%. -->
              @let percent = (h.zoom * 100).toFixed(0) + "%";
              <button
                type="button"
                class="zoom-level"
                title="Zoom to 100% (⌘/Ctrl 0)"
                [attr.aria-label]="'Zoom to 100%, now ' + percent"
                (click)="h.zoomReset()"
              >
                {{ percent }}</button
              ><span class="sr-only" aria-live="polite">Zoom {{ percent }}</span
              ><button
                type="button"
                class="icon-button"
                aria-label="Zoom in"
                title="Zoom in (+)"
                (click)="h.zoomBy(1.2)"
              >
                +</button
              ><span class="toolbar-divider"></span
              ><button type="button" title="Fit all (⇧1)" (click)="h.fitAll()">
                <weave-icon name="fit" [size]="16" /><span class="tool-text"
                  >Fit all</span
                ></button
              ><span class="toolbar-divider"></span
              ><button
                type="button"
                title="Put the steps back in order. You can undo this."
                [attr.aria-disabled]="h.editingLocked || null"
                (click)="h.tidyLayout()"
              >
                <weave-icon name="tidy" [size]="16" /><span class="tool-text"
                  >Tidy layout</span
                ></button
              ><button
                type="button"
                class="icon-button"
                aria-label="Keyboard shortcuts"
                title="Keyboard shortcuts (?)"
                aria-haspopup="dialog"
                (click)="h.shortcutsOpen = true"
              >
                <weave-icon name="help" [size]="16" />
              </button>
            </div>
            <p class="canvas-key-help" id="canvas-help">
              Scroll to pan, Ctrl + scroll to zoom. Press ? for shortcuts.
            </p>
          </div>
        }
        @if (h.model.unplaced.length) {
          <div class="unplaced">
            <strong>{{ h.model.unplaced.length }} unplaced step(s)</strong
            ><span>These steps block export and publication.</span>
            @for (step of h.model.unplaced; track step.id) {
              <button (click)="h.startMove(step.id)">
                Place {{ h.label(step.kind) }}
              </button>
            }
          </div>
        }
      </section>
      <section
        class="inspector"
        id="inspector"
        role="complementary"
        [class.visible]="h.showInspector && !h.simulationSession"
        aria-label="Inspector"
        [weaveModalSheet]="h.showInspector && !h.simulationSession"
        [sheetWhen]="h.sheetWhen.inspector"
        sheetInitialFocus=".inspector-header h2"
        [sheetReturnFocus]="h.selectedNodeElement"
        (sheetDismiss)="h.closeInspector()"
      >
        <button
          class="pane-resizer left"
          role="separator"
          aria-label="Resize inspector"
          aria-orientation="vertical"
          [attr.aria-valuenow]="h.inspectorWidth"
          [attr.aria-valuemin]="h.inspectorLimits.min"
          [attr.aria-valuemax]="h.inspectorLimits.max"
          (pointerdown)="h.paneResize($event, 'inspector')"
          (pointermove)="h.paneMove($event)"
          (pointerup)="h.resizing = null"
          (keydown)="h.paneKey($event, 'inspector')"
        ></button>
        <header class="inspector-header">
          <h2 tabindex="-1">
            {{ h.selected ? h.label(h.selected.step.kind) : "Workflow" }}
          </h2>
          @if (h.selected) {
            <button
              type="button"
              class="text-link inspector-settings"
              (click)="h.deselect()"
            >
              Workflow settings
            </button>
          }
          <button
            class="icon-button inspector-close"
            aria-label="Close inspector"
            (click)="h.closeInspector()"
          >
            <weave-icon name="close" />
          </button>
        </header>
        <div
          class="inspector-body"
          (change)="h.inspectorInteraction($event)"
          (focusout)="h.inspectorInteraction($event)"
          (click)="h.inspectorInteraction($event)"
        >
          @if (h.selected) {
            <form
              class="rename-step"
              data-field="id"
              (submit)="$event.preventDefault(); h.commitRename($event)"
            >
              <label for="step-name-input">Step name</label>
              <input
                id="step-name-input"
                autocomplete="off"
                spellcheck="false"
                [attr.data-step-id]="h.selected.step.id"
                [value]="h.renameDraft"
                (input)="h.inputRename($event)"
                [disabled]="h.model.readonly || h.editingLocked"
                [attr.aria-invalid]="!!h.renameError"
                [attr.aria-describedby]="
                  h.renameError ? 'rename-error' : 'rename-hint'
                "
                (blur)="h.commitRename($event)"
                (keydown.enter)="
                  $event.preventDefault(); h.commitRename($event)
                "
              />
              @if (h.renameError) {
                <p id="rename-error" class="field-error" role="alert">
                  <weave-icon name="failCircle" [size]="16" />{{
                    h.renameError
                  }}
                </p>
              } @else {
                <p id="rename-hint" class="hint">
                  {{ h.placeLabel(h.selected.owner) }}. Later steps read this
                  step's output by its name; renaming updates them.
                </p>
              }
            </form>
            @if (h.selected.step.kind === "action") {
              @defer (on immediate) {
                <weave-action-inspector [host]="h" />
              } @placeholder {
                <p class="hint">Loading the action…</p>
              }
            }
            @if (
              h.selected.step.kind !== "action" ||
              h.advancedProperties ||
              !h.inspectorHiddenFields.includes("with")
            ) {
              <details
                class="inspector-section"
                [open]="h.sectionOpen('properties')"
                (toggle)="h.sectionToggled('properties', $event)"
              >
                <summary class="properties-heading">
                  {{ sectionTitle() }}
                </summary>
                @if (!h.advancedProperties) {
                  @for (session of [h.inspectorSession]; track session) {
                    @if (h.selected.step.kind === "switch") {
                      @defer (on immediate) {
                        <weave-path-inspector [host]="h" />
                      }
                    } @else if (h.selected.step.kind === "humanTask") {
                      @defer (on immediate) {
                        <weave-human-inspector [host]="h" />
                      } @placeholder {
                        <p class="hint">Loading the human task…</p>
                      }
                    } @else if (h.selected.step.kind === "decisionTable") {
                      @defer (on immediate) {
                        <weave-decision-inspector [host]="h" />
                      } @placeholder {
                        <p class="hint">Loading the decision table…</p>
                      }
                    } @else if (h.selected.step.kind === "llm") {
                      @defer (on immediate) {
                        <weave-llm-inspector [host]="h" />
                      } @placeholder {
                        <p class="hint">Loading the AI task…</p>
                      }
                    } @else {
                      @defer (on immediate) {
                        <weave-step-property-grid
                          [step]="h.propertyStep || h.selected.step"
                          [readOnly]="h.model.readonly || h.editingLocked"
                          [hiddenFields]="h.inspectorHiddenFields"
                          [expressionSchema]="
                            h.selected.step.kind === 'action'
                              ? h.actionInputSchema()
                              : null
                          "
                          [scope]="h.referenceContext"
                          [inferSchema]="h.inferSchema"
                          (fieldChange)="h.stepEdit($event.value, $event.path)"
                          (fieldValidity)="h.inspectorFieldValidity($event)"
                          (validityChange)="h.propertyValid.set($event)"
                          (input)="h.touchInspector('step')"
                          (change)="h.touchInspector('step')"
                          (click)="h.touchInspector('step', $event)"
                        />
                      } @placeholder {
                        <p class="hint">Loading the properties…</p>
                      }
                    }
                  }
                } @else {
                  <label
                    >Step configuration<textarea
                      class="step-editor"
                      spellcheck="false"
                      [value]="h.inspectorBuffer"
                      (input)="h.editInspectorJson($event)"
                      aria-label="Step configuration JSON"
                    ></textarea>
                  </label>
                  @if (!h.propertyValid()) {
                    <p class="field-error" role="alert">
                      Enter valid step JSON, keeping the step name and kind
                      unchanged.
                    </p>
                  }
                }
                @if (h.selected.step.kind === "parallel") {
                  <section class="branch-management">
                    <h3>Parallel branches</h3>
                    <p class="hint">
                      Every branch runs. Move contained steps before removing a
                      branch.
                    </p>
                    @for (name of h.branchNames(); track name) {
                      <div class="branch-row">
                        <input
                          [value]="name"
                          [disabled]="h.model.readonly || !h.propertyValid()"
                          [attr.aria-label]="'Rename branch ' + name"
                          (change)="h.manageBranch('rename', name, $event)"
                        />
                        <button
                          (click)="h.manageBranch('remove', name)"
                          [disabled]="h.model.readonly || !h.propertyValid()"
                          [attr.aria-label]="'Remove branch ' + name"
                        >
                          Remove
                        </button>
                      </div>
                    }
                    <button
                      (click)="h.manageBranch('add')"
                      [disabled]="h.model.readonly || !h.propertyValid()"
                    >
                      Add branch
                    </button>
                  </section>
                }
              </details>
            }
            <button
              type="button"
              class="text-link property-mode"
              (click)="h.switchPropertyMode()"
            >
              {{ h.advancedProperties ? "Properties table" : "Advanced JSON" }}
            </button>
          } @else {
            @defer (on immediate) {
              <weave-step-property-grid
                [step]="h.workflowProperties"
                [readOnly]="h.model.readonly || h.editingLocked"
                [scope]="h.referenceContext"
                [inferSchema]="h.inferSchema"
                (fieldChange)="h.workflowEdit($event)"
                (fieldValidity)="h.inspectorFieldValidity($event)"
                (validityChange)="h.workflowValid.set($event)"
                (input)="h.touchInspector('workflow')"
                (change)="h.touchInspector('workflow')"
                (click)="h.touchInspector('workflow', $event)"
                ><weave-connection-slots [host]="h"
              /></weave-step-property-grid>
            } @placeholder {
              <p class="hint">Loading the workflow settings…</p>
            }
          }
        </div>
        @if (h.selected) {
          <footer class="inspector-actions">
            <weave-row-menu label="Step actions" [items]="h.stepMenu" />
          </footer>
        }
      </section>
      @if (h.simulation) {
        @defer (on immediate) {
          <weave-simulation-panel
            [artifact]="h.simulation.artifact"
            [session]="h.simulationSession"
            [busy]="h.busy !== ''"
            [error]="h.simulationError"
            [selectedStep]="h.model.selected"
            (start)="h.createDebug($event)"
            (command)="h.debugCommand($event)"
            (nodesChange)="h.simulationNodes($event)"
            (close)="h.closeSimulation()"
          />
        }
      }
    </div>
    <section
      class="diagnostics"
      aria-label="Compiler diagnostics"
      [class.open]="
        h.diagnosticsOpen &&
        (!!h.diagnostics?.diagnostics?.length || !!h.contractIssues.length)
      "
    >
      <div class="diagnostics-title">
        @if (h.diagnosticsIcon; as icon) {
          <weave-icon
            class="diagnostics-icon"
            [attr.data-tone]="h.validationView.tone"
            [name]="icon"
            [size]="16"
          />
        }
        @if (h.diagnostics?.diagnostics?.length || h.contractIssues.length) {
          <button
            type="button"
            class="diagnostics-toggle"
            aria-controls="diagnostic-rows"
            [attr.aria-expanded]="h.diagnosticsOpen"
            (click)="h.diagnosticsOpen = !h.diagnosticsOpen"
          >
            <weave-icon
              name="chevron"
              [size]="16"
              [class.turned]="h.diagnosticsOpen"
            />Diagnostics
          </button>
        } @else {
          <strong>Diagnostics</strong>
        }
        <span
          class="diagnostics-headline"
          [attr.data-tone]="h.validationView.tone"
          >{{ h.statusLine }}</span
        >
      </div>
      @if (h.contractIssues.length && h.diagnosticsOpen) {
        <ul class="contract-gap-list" aria-label="Setup needed">
          @for (
            issue of h.contractIssues;
            track issue.stepId + ":" + issue.label
          ) {
            <li>
              <button
                type="button"
                class="text-link"
                (click)="h.openContractIssue(issue)"
              >
                {{ issue.stepId }} · {{ issue.label }}
              </button>
            </li>
          }
        </ul>
      }
      @if (h.diagnostics?.diagnostics?.length && h.diagnosticsOpen) {
        <div id="diagnostic-rows">
          @defer (on immediate) {
            <weave-diagnostics-list
              [diagnostics]="h.diagnostics!.diagnostics"
              [definition]="h.diagnosticsDefinition"
              [readonly]="h.model.readonly"
              (locate)="h.openDiagnostic($event)"
              (apply)="h.applySuggestion($event)"
            />
          }
        </div>
      }
    </section>
    <weave-step-details-mount [host]="h" />`,
})
export class DesignerView {
  /** The editor shell. */
  host = input.required<App>();
  closeHelp(details: HTMLDetailsElement, event: Event) {
    details.open = false;
    details.querySelector("summary")?.focus();
    event.stopPropagation();
  }
  sectionTitle() {
    const kind = this.host().selected?.step.kind ?? "";
    return (
      (
        {
          action: "Custom input",
          transform: "Transform data",
          wait: "Wait time",
          signal: "Signal settings",
          fail: "Failure details",
          humanTask: "Human task setup",
          llm: "AI task setup",
          decisionTable: "Decision table setup",
          switch: "Paths",
          parallel: "Branches",
        } as Record<string, string>
      )[kind] ?? "Step settings"
    );
  }
}
