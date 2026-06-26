import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Callable, List, Optional

from pydantic import BaseModel, JsonValue

from registry import ProviderVersion, Registry

REGISTRY = "registry.opentofu.org"
INDEX_URL = "https://api.opentofu.org/registry/docs/providers/index.json"
INDEX_CACHE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "index.json")


class _Addr(BaseModel):
    namespace: str
    name: str


class _Version(BaseModel):
    id: str
    published: str


class _Provider(BaseModel):
    addr: _Addr
    is_blocked: bool
    versions: Optional[List[_Version]] = None


class _Index(BaseModel):
    providers: List[_Provider]


class OpenTofuRegistry(Registry):
    def __init__(self):
        self.__fetched_at: Optional[str] = None

    def load(self, registry_state: JsonValue):
        if isinstance(registry_state, dict):
            fetched = registry_state.get("fetched_at")
            self.__fetched_at = fetched if isinstance(fetched, str) else None

    def generate(self, add: Callable[[ProviderVersion], None]) -> bool:
        index = _Index.model_validate_json(self.__load_index())
        for provider in index.providers:
            if provider.is_blocked or not provider.versions:
                continue
            for version in provider.versions:
                add(ProviderVersion(
                    registry=REGISTRY,
                    org=provider.addr.namespace,
                    provider=provider.addr.name,
                    version=version.id,
                ))
        self.__fetched_at = datetime.now(timezone.utc).isoformat()
        return False

    def store(self) -> JsonValue:
        return {"index_url": INDEX_URL, "fetched_at": self.__fetched_at}

    def __load_index(self) -> bytes:
        try:
            request = urllib.request.Request(INDEX_URL, headers={"User-Agent": "terraform-provider-schema-archive"})
            with urllib.request.urlopen(request) as resp:
                data = resp.read()
            with open(INDEX_CACHE, "wb") as f:
                f.write(data)
            return data
        except Exception:
            if os.path.exists(INDEX_CACHE):
                with open(INDEX_CACHE, "rb") as f:
                    return f.read()
            raise
