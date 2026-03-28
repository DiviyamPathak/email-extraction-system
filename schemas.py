from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

try:
    from pydantic import BaseModel, ConfigDict, field_validator

    class ShipmentExtraction(BaseModel):
        model_config = ConfigDict(extra="forbid")

        id: str
        product_line: Optional[str]
        origin_port_code: Optional[str]
        origin_port_name: Optional[str]
        destination_port_code: Optional[str]
        destination_port_name: Optional[str]
        incoterm: Optional[str]
        cargo_weight_kg: Optional[float]
        cargo_cbm: Optional[float]
        is_dangerous: Optional[bool]

        @field_validator("cargo_weight_kg", "cargo_cbm")
        @classmethod
        def round_numeric_fields(cls, value: Optional[float]) -> Optional[float]:
            if value is None:
                return None
            return round(value, 2)

except ModuleNotFoundError:
    @dataclass
    class ShipmentExtraction:
        id: str
        product_line: Optional[str]
        origin_port_code: Optional[str]
        origin_port_name: Optional[str]
        destination_port_code: Optional[str]
        destination_port_name: Optional[str]
        incoterm: Optional[str]
        cargo_weight_kg: Optional[float]
        cargo_cbm: Optional[float]
        is_dangerous: Optional[bool]

        def __post_init__(self) -> None:
            if self.cargo_weight_kg is not None:
                self.cargo_weight_kg = round(float(self.cargo_weight_kg), 2)
            if self.cargo_cbm is not None:
                self.cargo_cbm = round(float(self.cargo_cbm), 2)

        def model_dump(self) -> dict[str, Any]:
            return asdict(self)

        @classmethod
        def model_validate(cls, payload: dict[str, Any]) -> "ShipmentExtraction":
            return cls(**payload)
