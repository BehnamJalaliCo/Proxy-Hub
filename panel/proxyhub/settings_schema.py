"""Typed owner settings stored in the ``settings`` table.

Budget cap and node cap have no default: until the owner sets them, nothing is created
(decision O3). The kill switch starts ON, so a fresh panel does nothing until the owner
deliberately turns it off.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from proxyhub.safety.gates import OperatorLimits


class FleetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kill_switch: bool = True
    dry_run: bool = True
    budget_cap_eur: Decimal | None = Field(default=None, gt=0)
    node_cap_total: int | None = Field(default=None, ge=0, le=200)
    creations_per_hour_cap: int = Field(default=3, ge=0, le=20)
    target_slots: int = Field(default=9, ge=0, le=200)  # decision D14: 9 active + 1 spare
    allowed_server_types: list[str] = Field(default_factory=lambda: ["cpx02"])
    locations: list[str] = Field(default_factory=lambda: ["fsn1", "nbg1", "hel1"])

    def limits(self) -> OperatorLimits:
        return OperatorLimits(
            kill_switch=self.kill_switch,
            budget_cap_eur=self.budget_cap_eur,
            node_cap_total=self.node_cap_total,
            creations_per_hour_cap=self.creations_per_hour_cap,
        )


SETTINGS_KEYS: dict[str, type[BaseModel]] = {"fleet": FleetSettings}
