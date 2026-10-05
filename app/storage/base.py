from abc import ABC, abstractmethod


class StorageBackend(ABC):
    name: str

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    def presigned_url(self, key: str, filename: str | None = None) -> str | None:
        """A short-lived direct URL, or None if the backend streams through the API."""
        return None
