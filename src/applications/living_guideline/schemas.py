from pydantic import BaseModel, ConfigDict, Field


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=200_000)


class GuidelineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    documents: list[Document] = Field(min_length=1, max_length=20)


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    quote: str = Field(min_length=1, max_length=4000)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quotes: list[Quote] = Field(min_length=1, max_length=100)
