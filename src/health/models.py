from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


@dataclass
class ComponentHealth:
    name: str
    status: HealthStatus
    message: str
    checked_at: Optional[datetime] = None

    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY


@dataclass
class SystemHealth:
    status: HealthStatus
    components: dict[str, ComponentHealth] = field(default_factory=dict)
    checked_at: Optional[datetime] = None

    def add(self, component: ComponentHealth):
        self.components[component.name] = component

    def get(self, name: str) -> Optional[ComponentHealth]:
        return self.components.get(name)
