"""Public practice bodies have no caller identity; authentication remains a server concern."""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, field_validator

from .common import Contract, FalseFlag, Id, PositiveInt, Text
from .runtime import Citation

# Match the existing JavaScript trim/split behavior without changing saved text.
JS_WHITESPACE = "\t\v\f \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000\ufeff\n\r\u2028\u2029"
UuidString = Annotated[str, Field(pattern=r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def split_answer_lines(text: str) -> list[str]:
    return re.split(r"\r?\n", text)


def validate_answer_text(text: str) -> str:
    if not text.strip(JS_WHITESPACE) or utf16_length(text) > 2000 or len(split_answer_lines(text)) > 40:
        raise ValueError("invalid_answer")
    return text


AnswerText = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(validate_answer_text)]


class CreateTarget(Contract):
    role_package_id: Literal["agent-intern"]
    level: Literal["intern"]
    focus: Annotated[str, Field(max_length=120)] = ""


class CreatePractice(Contract):
    target_id: UuidString
    question_id: Id


class SubmitAnswer(Contract):
    expected_version: PositiveInt
    text: AnswerText


class EmptyCommand(Contract):
    pass


class QuestionSource(Contract):
    id: Id
    version: Id
    title: Text
    url: Annotated[str, Field(pattern="^https://")]
    locator: Text
    lines: list[Text]


class PracticeQuestion(Contract):
    id: Id
    text: Text
    topic: Id
    version: Id
    review: Literal["source_checked_draft"]
    semantic_scoring_eligible: FalseFlag


class QuestionSnapshot(PracticeQuestion):
    rubric: list[Text]
    sources: list[QuestionSource]


class FeedbackSource(Contract):
    title: Text
    url: Annotated[str, Field(pattern="^https://")]
    locator: Text
    summary_kind: Literal["authored_paraphrase"]


class FeedbackCitation(Citation):
    source: FeedbackSource | None


class PracticeFeedback(Contract):
    summary: Text
    feedback: Text
    semantic_scoring_eligible: FalseFlag
    origin: Text
    provider: Text | None
    citations: list[FeedbackCitation]


class SavedAnswer(Contract):
    id: Id
    text: str
    origin: Literal["user_submitted"]


class PracticeOperation(Contract):
    id: Id
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "WAITING_CREDENTIALS"]
    error_code: Text | None


class PracticeSessionView(Contract):
    id: Id
    version: PositiveInt
    status: Literal["WAITING_FOR_ANSWER", "EVALUATING", "COMPLETED", "CANCELLED", "FAILED"]
    mode: Literal["practice"]
    context_mode: Literal["role_only"]
    target_id: Id
    question: PracticeQuestion
    created_at: str
    answer: SavedAnswer | None
    operation: PracticeOperation | None
    feedback: PracticeFeedback | None

    @field_validator("created_at")
    @classmethod
    def timestamp_string(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if "T" not in value or parsed.tzinfo is None:
            raise ValueError("timezone_timestamp_required")
        return value
