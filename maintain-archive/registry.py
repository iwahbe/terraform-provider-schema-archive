from abc import ABC, abstractmethod
from typing import Callable, Iterator
from dataclasses import dataclass

from pydantic import JsonValue

@dataclass(frozen=True)
class ProviderVersion:
    registry: str
    org: str
    provider: str
    version: str

class CrawlState:
    def __init__(self):
        self.__m: set[ProviderVersion] = set()

    def add(self, p: ProviderVersion):
        self.__m.add(p)

    def __contains__(self, p: ProviderVersion) -> bool:
        return p in self.__m

    def __iter__(self) -> Iterator[ProviderVersion]:
        return iter(self.__m)

    def __len__(self) -> int:
        return len(self.__m)

class Registry(ABC):

    @abstractmethod
    def load(self, registry_state: JsonValue):
        "Set the internal state of the registry"
        pass

    @abstractmethod
    def generate(self, add: Callable[[ProviderVersion], None]) -> bool:
        "Add every provider version found to the archive. add is fully idempotent. Returns True if more remains to generate."
        pass

    @abstractmethod
    def store(self) -> JsonValue:
        pass
