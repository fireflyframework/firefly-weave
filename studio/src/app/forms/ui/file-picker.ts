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
import {
  ChangeDetectorRef,
  Component,
  OnChanges,
  OnDestroy,
  SimpleChanges,
  inject,
  input,
  output,
} from "@angular/core";
import {
  fileReference,
  type FileAccess,
  type FileReference,
  type FileUpload,
} from "../core/file-reference";
import { uploadFile, downloadFile } from "../core/file-transfer";
import { describeError } from "../../errors";

@Component({
  selector: "weave-file-picker",
  standalone: true,
  template: `<div class="file-picker">
    @if (file; as value) {
      <div class="file-selected">
        <span
          >{{ value.filename }} <small>{{ size(value.sizeBytes) }}</small></span
        >
        @if (access()?.canRead) {
          <button type="button" [disabled]="busy" (click)="download(value)">
            Download {{ label() }}
          </button>
        }
        @if (!readOnly()) {
          <button
            type="button"
            [disabled]="busy"
            [attr.aria-label]="'Remove ' + label()"
            (click)="remove()"
          >
            Remove
          </button>
        }
      </div>
    }
    @if (access(); as permissions) {
      @if (permissions.canManage && !readOnly()) {
        <label class="file-upload"
          >Upload {{ label()
          }}<input
            type="file"
            [attr.aria-label]="'Upload ' + label()"
            [disabled]="busy"
            (change)="upload($event)"
        /></label>
      }
      @if (permissions.canRead && !permissions.task && !readOnly()) {
        <button type="button" [disabled]="busy" (click)="list()">
          Choose uploaded file
        </button>
      }
      @if (!permissions.canRead && !permissions.canManage) {
        <p class="hint">Ask an administrator for file access.</p>
      }
    } @else {
      <p class="hint">
        File upload preview · files are chosen when starting a run or answering
        a task.
      </p>
    }
    @if (listing) {
      <div
        class="file-options"
        role="group"
        [attr.aria-label]="'Uploaded files for ' + label()"
      >
        @for (item of choices; track item.id) {
          <button type="button" (click)="choose(item)">
            {{ item.filename }} · {{ size(item.sizeBytes) }}
          </button>
        }
        @if (!choices.length && !busy) {
          <p class="hint">No uploaded files are available.</p>
        }
        @if (cursor) {
          <button type="button" [disabled]="busy" (click)="list(true)">
            Load more files
          </button>
        }
        <button type="button" (click)="listing = false">Close file list</button>
      </div>
    }
    @if (busy) {
      <p role="status">{{ progress }}</p>
    }
    @if (error) {
      <p class="field-error" role="alert">{{ error }}</p>
    }
  </div>`,
  styles: [
    `
      .file-picker {
        display: grid;
        gap: 8px;
        min-width: 0;
      }
      .file-upload {
        display: grid;
        gap: 6px;
      }
      .file-upload input {
        max-width: 100%;
        min-width: 0;
        width: 100%;
      }
      .file-selected {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        align-items: center;
      }
      .file-selected span {
        overflow-wrap: anywhere;
        min-width: 0;
      }
      .file-selected small {
        white-space: nowrap;
      }
      .file-options {
        display: grid;
        gap: 6px;
        max-height: 240px;
        overflow: auto;
      }
      .file-options button {
        text-align: left;
        overflow-wrap: anywhere;
      }
      .hint {
        margin: 0;
      }
    `,
  ],
})
export class FilePicker implements OnChanges, OnDestroy {
  readOnly = input(false);
  value = input<unknown>(undefined);
  access = input<FileAccess | null>(null);
  label = input("file");
  valueChange = output<FileReference | undefined>();
  validityChange = output<boolean>();
  private cdr = inject(ChangeDetectorRef);
  private generation = 0;
  busy = false;
  error = "";
  progress = "";
  listing = false;
  choices: FileReference[] = [];
  cursor: string | null = null;
  get file() {
    return fileReference(this.value());
  }
  ngOnChanges(changes: SimpleChanges) {
    if (changes["access"]) {
      this.generation++;
      this.busy = false;
      this.error = "";
      this.choices = [];
      this.listing = false;
    }
  }
  ngOnDestroy() {
    this.generation++;
  }
  size(bytes: number) {
    return bytes < 1024
      ? `${bytes} B`
      : bytes < 1048576
        ? `${Math.ceil(bytes / 1024)} KiB`
        : `${(bytes / 1048576).toFixed(1)} MiB`;
  }
  remove() {
    this.error = "";
    this.valueChange.emit(undefined);
    this.validityChange.emit(true);
  }
  choose(file: FileReference) {
    if (!this.access()?.active()) return;
    this.valueChange.emit(file);
    this.validityChange.emit(true);
    this.listing = false;
    this.error = "";
  }
  private async transfer(
    run: (access: FileAccess, alive: () => boolean) => Promise<void>,
  ) {
    const access = this.access();
    if (!access || this.busy) return;
    const generation = ++this.generation;
    const alive = () =>
      generation === this.generation && access === this.access();
    this.busy = true;
    this.error = "";
    this.validityChange.emit(false);
    try {
      await run(access, alive);
      if (alive()) this.validityChange.emit(true);
    } catch (error) {
      if (alive()) {
        this.error = describeError(error).message;
        this.validityChange.emit(!!this.file);
      }
    } finally {
      if (alive()) {
        this.busy = false;
        this.cdr.markForCheck();
      }
    }
  }
  async upload(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    await this.transfer(async (access, alive) => {
      this.progress = "Preparing upload…";
      const reference = await uploadFile(
        access,
        file,
        (percent) => {
          this.progress = `Uploading ${percent}%…`;
          this.cdr.markForCheck();
        },
        alive,
      );
      if (alive()) this.valueChange.emit(reference);
    });
    input.value = "";
  }
  async list(more = false) {
    await this.transfer(async (access, alive) => {
      this.listing = true;
      this.progress = "Loading files…";
      const suffix =
        more && this.cursor ? `?cursor=${encodeURIComponent(this.cursor)}` : "";
      const result = await access.api.request<{
        items: FileUpload[];
        next_cursor: string | null;
      }>(`${access.api.environment}/files${suffix}`);
      if (!alive() || !access.active()) return;
      const files = result.items
        .filter((item) => item.state === "ready")
        .map((item) => item.file);
      this.choices = more ? [...this.choices, ...files] : files;
      this.cursor = result.next_cursor;
    });
  }
  async download(file: FileReference) {
    await this.transfer(async (access, alive) => {
      this.progress = "Preparing download…";
      const result = await downloadFile(access, file, alive);
      if (!alive()) return;
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = result.file.filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
}
