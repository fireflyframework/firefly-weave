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

"""Step enablement for acceptance journeys (tests/acceptance/journeys.toml) and pinned versions.

A step such as J0.3, or a check inside it such as J0.3/owner-roles, runs only when every
requirement holds. A requirement names a milestone ("O3") or alternatives ("A3|O3"). A
step that does not run reports skipped with its missing milestones, never passed.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILES = ("pr", "ai", "cluster", "k3d", "nightly", "release", "docs-shots")
JOURNEYS = tuple(f"J{number}" for number in range(16))
MILESTONES = (
    *(f"brand-PR{number}" for number in range(1, 4)),
    *(f"E-M{number}" for number in range(11)),
    "E-M11a",
    "E-M11b",
    "E-M11c",
    "E-M12",
    *(f"L-M{number}" for number in range(11)),
    *(f"A{number}" for number in range(1, 10)),
    *(f"O{number}" for number in range(9)),
    *(f"S6-M{number}" for number in range(5)),
)
STEP = re.compile(r"(J(?:[0-9]|1[0-5]))\.([1-9][0-9]?)")
CHECK = re.compile(r"(J(?:[0-9]|1[0-5])\.[1-9][0-9]?)/[a-z0-9]+(?:-[a-z0-9]+)*")
CRITERION = re.compile(r"SC[1-7]")
IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")
_TABLES = {"milestones", "profiles", "criteria", "steps", "step_profiles"}
_REQUIRED = {"milestones", "profiles", "criteria", "steps"}


class JourneysInvalid(ValueError):
    """journeys.toml or versions.toml breaks a rule; the message names the entry."""


@dataclass(frozen=True)
class Enablement:
    milestones: dict[str, bool]
    steps: dict[str, tuple[str, ...]]
    profiles: dict[str, tuple[str, ...]]
    step_profiles: dict[str, tuple[str, ...]]
    criteria: dict[str, tuple[str, ...]]

    @classmethod
    def load(cls, path: Path) -> Enablement:
        return cls.parse(path.read_text(encoding="utf-8"))

    @classmethod
    def parse(cls, text: str) -> Enablement:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise JourneysInvalid(f"journeys.toml is not valid TOML: {error}") from None
        if set(data) - _TABLES or _REQUIRED - set(data):
            raise JourneysInvalid(
                "journeys.toml has the tables milestones, profiles, criteria, steps and step_profiles"
            )
        milestones = data["milestones"]
        if set(milestones) != set(MILESTONES) or any(type(value) is not bool for value in milestones.values()):
            raise JourneysInvalid("[milestones] lists every milestone ID once with true or false")
        steps: dict[str, tuple[str, ...]] = {}
        for key, requirements in data["steps"].items():
            if STEP.fullmatch(key) is None and CHECK.fullmatch(key) is None:
                raise JourneysInvalid(f"{key} is not a step (J3.5) or a check (J3.5/name)")
            if not isinstance(requirements, list) or not requirements:
                raise JourneysInvalid(f"{key} needs at least one milestone")
            for requirement in requirements:
                if not isinstance(requirement, str) or any(part not in milestones for part in requirement.split("|")):
                    raise JourneysInvalid(f"{key} names an unknown milestone: {requirement}")
            steps[key] = tuple(requirements)
        for key in steps:
            parent = cls.parent(key)
            if parent is not None and parent not in steps:
                raise JourneysInvalid(f"{key} belongs to {parent}, which is not declared")
        profiles = data["profiles"]
        if set(profiles) != set(PROFILES) or any(
            not isinstance(value, list) or set(value) - set(JOURNEYS) for value in profiles.values()
        ):
            raise JourneysInvalid("[profiles] maps every profile to known journeys")
        step_profiles = data.get("step_profiles", {})
        for key, names in step_profiles.items():
            if key not in steps or not isinstance(names, list) or not names or set(names) - set(PROFILES):
                raise JourneysInvalid(f"[step_profiles] {key} must be a declared step with known profiles")
        criteria = data["criteria"]
        if set(criteria) != set(JOURNEYS) or any(
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or CRITERION.fullmatch(item) is None for item in value)
            for value in criteria.values()
        ):
            raise JourneysInvalid("[criteria] maps every journey to success criteria SC1 to SC7")
        return cls(
            dict(milestones),
            steps,
            {key: tuple(value) for key, value in profiles.items()},
            {key: tuple(value) for key, value in step_profiles.items()},
            {key: tuple(value) for key, value in criteria.items()},
        )

    @staticmethod
    def parent(step_id: str) -> str | None:
        match = CHECK.fullmatch(step_id)
        return match[1] if match else None

    def missing(self, step_id: str) -> tuple[str, ...]:
        """Requirements that do not hold yet, the parent step's first for a check."""
        if step_id not in self.steps:
            raise KeyError(f"{step_id} is not declared in journeys.toml")
        parent = self.parent(step_id)
        inherited = self.missing(parent) if parent else ()
        own = tuple(
            requirement
            for requirement in self.steps[step_id]
            if not any(self.milestones[part] for part in requirement.split("|"))
        )
        return tuple(dict.fromkeys((*inherited, *own)))

    def applies(self, step_id: str, profile: str) -> bool:
        parent = self.parent(step_id)
        if parent is not None and not self.applies(parent, profile):
            return False
        journey = (parent or step_id).split(".")[0]
        if journey not in self.profiles[profile]:
            return False
        limited = self.step_profiles.get(step_id)
        return limited is None or profile in limited

    def checks(self, step_id: str) -> tuple[str, ...]:
        return tuple(key for key in self.steps if self.parent(key) == step_id)

    def journey_steps(self, journey: str) -> tuple[str, ...]:
        found = [key for key in self.steps if (match := STEP.fullmatch(key)) and match[1] == journey]
        return tuple(sorted(found, key=lambda key: int(key.split(".")[1])))


def load_versions(path: Path) -> dict[str, Any]:
    """Pinned tools and images (versions.toml); every image is pinned by tag and digest."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or set(data) != {"version", "tools", "images"}:
        raise JourneysInvalid("versions.toml has version = 1 and the tables tools and images")
    for name, ref in data["images"].items():
        if not isinstance(ref, str) or IMAGE.fullmatch(ref) is None:
            raise JourneysInvalid(f"versions.toml image {name} must be pinned as name:tag@sha256:digest")
    if any(not isinstance(value, str) for value in data["tools"].values()):
        raise JourneysInvalid("versions.toml tool versions are strings")
    return data
