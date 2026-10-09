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
// is open, one editing controller per opened workflow, and the documents of
// the workflow's owned actions kept current on every change.
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
import { StepDetails } from "./step-details";
import { StepDetailsController } from "./step-details-controller";
import type { StepDetailsHost } from "./step-details-host";
import { StepDetailsService } from "./step-details-service";
import { loadKindRegistrations } from "./kinds";

@Component({
  selector: "weave-step-details-mount",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [StepDetails],
  template: `@if (ready() && details.request(); as request) {
    <weave-step-details
      [host]="host()"
      [request]="request"
      [controller]="controller()"
      (closed)="closed($event)"
    />
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

  constructor() {
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
      untracked(() => this.ownedDocuments(host).sync());
      if (!this.ready() || !this.details.request()) return;
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
      this.made = { host, opened, controller: new StepDetailsController(host) };
    }
    return this.made.controller;
  }
  private ownedDocuments(host: StepDetailsHost): OwnedDocuments {
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
  /** The built documents' problems, for the fields that caused them (issues read it). */
  ownedProblems() {
    return this.ownedDocuments(this.host()).problems;
  }
  closed(target: string) {
    this.details.close();
    const host = this.host();
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
