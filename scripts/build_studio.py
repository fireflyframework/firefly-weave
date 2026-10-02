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

"""Produce a checksummed optional Studio bundle from the compiled Angular application."""

import argparse
from pathlib import Path

from firefly_weave.studio.assets import build_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("studio/dist/studio/browser"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    archive, digest = build_bundle(args.source, args.output)
    print(f"{digest}  {archive}")


if __name__ == "__main__":
    main()
