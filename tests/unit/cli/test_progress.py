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

"""Progress is optional, bounded, and never mixes with machine output."""

import io
import threading

import pytest


def test_disabled_progress_is_silent(monkeypatch):
    from firefly_weave.cli.progress import progress

    stream = io.StringIO()
    monkeypatch.setattr("firefly_weave.cli.progress.sys.stderr", stream)
    with progress("Setup", enabled=False) as update:
        update("Installing")
    assert stream.getvalue() == ""


def test_redirected_progress_has_stages_without_control_characters(monkeypatch):
    from firefly_weave.cli.progress import progress

    stream = io.StringIO()
    monkeypatch.setattr("firefly_weave.cli.progress.sys.stderr", stream)
    with progress("Setup") as update:
        update("Installing")
    assert "Setup" in stream.getvalue() and "Installing" in stream.getvalue()
    assert "\r" not in stream.getvalue() and "\x1b" not in stream.getvalue()


def test_progress_stops_owned_thread_on_error(monkeypatch):
    from firefly_weave.cli.progress import progress

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    stream = Terminal()
    monkeypatch.setattr("firefly_weave.cli.progress.sys.stderr", stream)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("WEAVE_NO_ANIMATION", raising=False)
    with pytest.raises(ValueError), progress("Setup"):
        raise ValueError("private-error-detail")
    assert not any(t.name == "weave-progress" for t in threading.enumerate())
    assert "Stopped" in stream.getvalue()
    assert "private-error-detail" not in stream.getvalue()
