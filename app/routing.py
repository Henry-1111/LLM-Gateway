from dataclasses import dataclass
from typing import Any, Dict, Iterable, Protocol, Tuple

from app.exceptions import ModelNotAvailableError


class ChatProvider(Protocol):
    async def create_chat_completion(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...


@dataclass(frozen=True)
class ProviderRoute:
    name: str
    provider: ChatProvider
    models: Tuple[str, ...]
    priority: int = 100
    enabled: bool = True

    def supports(self, model: str) -> bool:
        return "*" in self.models or model in self.models


class ProviderRouter:
    """Selects one provider deterministically by model support and priority."""

    def __init__(self, routes: Iterable[ProviderRoute]) -> None:
        route_list = list(routes)
        names = [route.name for route in route_list]
        if len(names) != len(set(names)):
            raise ValueError("Provider route names must be unique")
        self._routes = tuple(
            sorted(
                (route for route in route_list if route.enabled),
                key=lambda route: (route.priority, route.name),
            )
        )

    def select(self, model: str) -> ProviderRoute:
        return self.candidates(model)[0]

    def candidates(self, model: str) -> Tuple[ProviderRoute, ...]:
        candidates = tuple(route for route in self._routes if route.supports(model))
        if not candidates:
            raise ModelNotAvailableError(model)
        return candidates

    @property
    def provider_names(self) -> Tuple[str, ...]:
        return tuple(route.name for route in self._routes)
