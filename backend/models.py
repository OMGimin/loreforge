"""Public contracts. Model output never controls document metadata or decisions."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class Evidence(Strict):
    document_ref: str
    passage_id: str

class Passage(Strict):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1)

class Entity(Strict):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["person", "group", "place", "event", "rule"]
    name: str
    description: str
    basis: Literal["explicit", "inferred"]
    evidence: list[Evidence]

class Relation(Strict):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    source: str
    target: str
    type: Literal["belongs_to", "appears_in", "uses", "constrained_by", "precedes", "related_to"]
    label: str
    condition: str
    basis: Literal["explicit", "inferred"]
    evidence: list[Evidence]

class Fact(Strict):
    id: str
    text: str
    kind: str
    basis: Literal["explicit", "inferred"]
    evidence: list[Evidence]

class StoryOutput(Strict):
    title: str = Field(min_length=1, max_length=200)
    passages: list[Passage] = Field(min_length=1)
    # Graph fragments deliberately validate independently after body parsing.
    entities: Any
    relations: Any

    @field_validator("passages")
    @classmethod
    def unique_passages(cls, value):
        if len({p.id for p in value}) != len(value):
            raise ValueError("문단 ID가 중복됩니다")
        return value

class AnalysisOutput(Strict):
    facts: list[Fact]
    entities: Any
    relations: Any

class Issue(Strict):
    id: str
    kind: Literal["conflict_candidate", "internal_conflict", "missing_explanation", "intent_question"]
    reason: str
    suggestion: str
    evidence: list[Evidence]
    status: Literal["open", "resolved"]

class ReviewOutput(Strict):
    issues: list[Issue]

# Concrete schema types for Structured Outputs. Graph validation remains separate
# when a provider/custom fixture supplies a malformed graph fragment.
class StructuredStory(StoryOutput):
    entities: list[Entity]
    relations: list[Relation]

class StructuredAnalysis(AnalysisOutput):
    entities: list[Entity]
    relations: list[Relation]

class SourceInput(Strict):
    text: str = Field(min_length=1, max_length=20000)
    title: str = Field(default="입력 재료", min_length=1, max_length=200)

    @field_validator("text")
    @classmethod
    def not_blank(cls, text):
        if not text.strip():
            raise ValueError("입력 재료를 적어주세요")
        return text

class AnalyzeInput(Strict):
    request_id: str = Field(min_length=1, max_length=120)
    trace: bool = False

class RunInput(AnalyzeInput):
    analysis_id: str | None = None
    accepted_run_id: str | None = None
    constraints: list[str] = Field(default_factory=list, max_length=100)
    request: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def one_origin(self):
        if bool(self.analysis_id) == bool(self.accepted_run_id):
            raise ValueError("analysis_id와 accepted_run_id 중 하나를 지정해주세요")
        if any(not c.strip() for c in self.constraints):
            raise ValueError("빈 조건은 제외해주세요")
        return self

class DecisionInput(Strict):
    status: Literal["accepted", "deferred"]
    reason: str = Field(max_length=2000)
