from enum import Enum
from pydantic import BaseModel, model_validator


class Action(str, Enum):
    DELETE = "DELETE"
    KEEP_PHONE = "KEEP_PHONE"
    KEEP_PC = "KEEP_PC"
    UNDECIDED = "UNDECIDED"


class PhotoItem(BaseModel):
    id: str
    taken_date: str = ""
    width: int = 0
    height: int = 0
    location: str = ""
    thumbnail_bytes: bytes = b""


class PhotoResult(BaseModel):
    id: str
    action: Action
    confidence: float
    reason: str

    @model_validator(mode="after")
    def check_confidence(self) -> "PhotoResult":
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {self.confidence}")
        return self


class ClassificationResponse(BaseModel):
    results: list[PhotoResult]

    @model_validator(mode="after")
    def check_all_photos(self) -> "ClassificationResponse":
        ids = [r.id for r in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate photo IDs in results")
        return self
