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
// Puts step details on screen for the designer: the dialog while a request
// is open, workflow settings while they are open, the dialog that publishes
// the workflow's own actions while Publish… or Simulate asks for it, one
// editing controller per opened workflow, and the documents of the
// workflow's owned actions kept current on every change.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  effect,
  inject,
  input,
  signal,
  untracked,
} from "@angular/core";
import type { CanvasView } from "../canvas/canvas-view";
import { HttpActionClient } from "../../integrations/http-action-client";
import { ConnectorActions } from "./owned/connector-actions";
import { OwnedDocuments } from "./owned/owned-documents";
import { OwnedPublishDialog } from "./owned/owned-publish-dialog";
import { StepDetails } from "./step-details";
import { StepDetailsController } from "./step-details-controller";
import { FormSession } from "./params/form-session";
import type { StepDetailsHost } from "./step-details-host";
import {
  StepDetailsService,
  type HeldFact,
  type OwnedPublishOutcome,
} from "./step-details-service";
import { loadKindRegistrations } from "./kinds";
import { WorkflowSettingsDialog } from "./workflow-settings-dialog";
import { formulaPreviews } from "../formula/preview";

const NONE_HELD: ReadonlyMap<string, readonly HeldFact[]> = new Map();

@Component({
  selector: "weave-step-details-mount",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [StepDetails, WorkflowSettingsDialog, OwnedPublishDialog],
  template: `@if (ready() && details.request(); as request) {
      <weave-step-details
        [host]="host()"
        [request]="request"
        [controller]="controller()"
        (closed)="closed($event)"
      />
    }
    @if (ready() && details.workflowSettings()) {
      <weave-workflow-settings-dialog
        [host]="host()"
        [controller]="controller()"
        (closed)="details.closeWorkflowSettings()"
      />
    }
    @if (ready() && details.ownedPublish(); as publishing) {
      @defer (on immediate) {
        <weave-owned-publish-dialog
          [host]="host()"
          [request]="publishing.request"
          [documents]="documentsFor(host())"
          (closed)="ownedClosed($event)"
        />
      }
    }`,
})
export class StepDetailsMount {
  host = input.required<StepDetailsHost>();
  canvas = input<CanvasView>();
  readonly details = inject(StepDetailsService);
  readonly ready = signal(false);
  private readonly lifetime = inject(DestroyRef);
  private made: {
    host: StepDetailsHost;
    opened: number;
    controller: StepDetailsController;
  } | null = null;
  private documents: {
    host: StepDetailsHost;
    documents: OwnedDocuments;
  } | null = null;
  /** The workflow on screen when the designer last changed. */
  private workflow: { model: object; opened: number } | null = null;

  constructor() {
    this.details.mounted++;
    this.lifetime.onDestroy(() => {
      this.details.mounted--;
      // A designer that goes away takes its dialogs with it; the command stops.
      this.ownedClosed({ done: false, published: 0 });
      this.closeAll();
    });
    void loadKindRegistrations().then(
      () => {
        if (this.lifetime.destroyed) return;
        this.ready.set(true);
        this.host().refreshView();
      },
      (error: unknown) => console.error("Step kinds failed to load", error),
    );
    effect(() => {
      const host = this.host();
      host.tick();
      const ready = this.ready();
      untracked(() => {
        // Step details belong to the workflow they opened on.
        const workflow = { model: host.model, opened: host.model.opened };
        if (
          this.workflow &&
          (this.workflow.model !== workflow.model ||
            this.workflow.opened !== workflow.opened)
        )
          this.closeAll();
        this.workflow = workflow;
        this.documentsFor(host).sync();
        const controller = this.made?.controller;
        // A step an Undo or Redo renamed keeps its entries under its name.
        for (const { from, to } of this.details.takeRenames())
          controller?.renamePending(from, to);
        // Entries typed for a step that is gone never reach a step made
        // again with its name; the open step keeps its own while step
        // details follow a rename back.
        controller?.prunePending(this.details.request()?.target);
        // What is left, checked against its fields, for the canvas.
        const held =
          ready && controller
            ? FormSession.heldByStep(host, controller)
            : NONE_HELD;
        if (held.size || this.details.held().size) this.details.held.set(held);
      });
      if (
        !this.ready() ||
        (!this.details.request() && !this.details.workflowSettings())
      )
        return;
      const controller = this.controller();
      const model = host.model;
      const opened = model.opened;
      void controller.loadFeatures().then(() => {
        if (
          !this.lifetime.destroyed &&
          this.made?.controller === controller &&
          this.host() === host &&
          host.model === model &&
          model.opened === opened
        )
          host.refreshView();
      });
    });
  }

  controller(): StepDetailsController {
    const host = this.host();
    const opened = host.model.opened;
    if (!this.made || this.made.host !== host || this.made.opened !== opened) {
      const controller: StepDetailsController = new StepDetailsController(
        host,
        () => Date.now(),
        formulaPreviews(() => controller.previewData()),
      );
      controller.ownedProblems = () => this.documentsFor(host).problems();
      this.made = { host, opened, controller };
    }
    return this.made.controller;
  }
  /** The documents of the workflow's own actions, kept for this host. */
  documentsFor(host: StepDetailsHost): OwnedDocuments {
    if (this.documents?.host !== host) {
      const client = new HttpActionClient(host.api);
      this.documents = {
        host,
        documents: new OwnedDocuments(
          host,
          (request) => client.build(request),
          new ConnectorActions((path) => host.api.request(path)),
        ),
      };
    }
    return this.documents.documents;
  }
  /** Closes step details and workflow settings. */
  private closeAll() {
    this.details.close();
    this.details.closeWorkflowSettings();
  }
  /** The publish dialog closed: the command waiting for it goes on, or stops. */
  ownedClosed(outcome: OwnedPublishOutcome) {
    const open = this.details.ownedPublish();
    this.details.ownedPublish.set(null);
    open?.settle(outcome);
  }
  closed(target: string) {
    this.details.close();
    const host = this.host();
    // A platform draft saves what changed in step details at once.
    void host.flushAutosave?.();
    const model = host.model;
    const opened = model.opened;
    const opening = this.details.opening();
    const selected = model.selected;
    const current = () =>
      !this.lifetime.destroyed &&
      this.host() === host &&
      host.model === model &&
      model.opened === opened &&
      model.selected === selected &&
      this.details.opening() === opening &&
      !this.details.isOpen();
    const canvas = this.canvas();
    if (canvas) {
      canvas.restoreFocus(target, () => current() && this.canvas() === canvas);
      return;
    }
    const selector =
      target === "$trigger"
        ? ".start-node button"
        : `[data-outline="${CSS.escape(target)}"]`;
    const opener = document.querySelector<HTMLElement>(selector);
    if (opener?.getClientRects().length) {
      setTimeout(() => {
        if (!current()) return;
        const element = document.querySelector<HTMLElement>(selector);
        if (element?.isConnected && element.getClientRects().length)
          element.focus();
      });
    } else if (!target.startsWith("$") && current()) host.focusStep(target);
  }
}
