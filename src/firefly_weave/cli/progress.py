# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Owned terminal progress with plain redirected output and a silent JSON mode."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


@contextmanager
def progress(label: str, *, enabled: bool = True) -> Iterator[Callable[[str], None]]:
    """Yield a stage updater and always stop the animation before returning.

    A spinner signals activity, not a fabricated completion percentage. The
    calling operation owns its timeout; this helper never launches product work.
    """
    if not enabled:
        yield lambda message: None
        return
    stream = sys.stderr
    animated = stream.isatty() and os.environ.get("TERM") != "dumb" and not os.environ.get("WEAVE_NO_ANIMATION")
    stopped = threading.Event()
    lock = threading.Lock()
    stage = label
    started = time.monotonic()

    def line(value: str) -> str:
        # Keep a stage to one terminal row and strip caller-supplied controls.
        clean = "".join(c if c.isprintable() else " " for c in value)
        return clean[: max(20, shutil.get_terminal_size((80, 24)).columns - 18)]

    def update(message: str) -> None:
        nonlocal stage
        with lock:
            if animated:
                stream.write("\r\x1b[2K")
            stage = line(message)
            stream.write(stage + "...\n")
            stream.flush()

    def animate() -> None:
        frames = "|/-\\"
        index = 0
        while not stopped.wait(0.12):
            with lock:
                elapsed = int(time.monotonic() - started)
                stream.write(f"\r\x1b[2K{frames[index % len(frames)]} {stage} ({elapsed}s)")
                stream.flush()
                index += 1

    update(label)
    thread = threading.Thread(target=animate, name="weave-progress", daemon=True) if animated else None
    if thread is not None:
        thread.start()
    completed = False
    try:
        yield update
        completed = True
    finally:
        stopped.set()
        if thread is not None:
            thread.join()
        with lock:
            if animated:
                stream.write("\r\x1b[2K")
            stream.write(("Done" if completed else "Stopped") + f" ({int(time.monotonic() - started)}s).\n")
            stream.flush()
