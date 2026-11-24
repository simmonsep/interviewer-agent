"""Copy-owned snapshots, server scope and exact delivered-window citations."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from interview_agent.contracts.common import Contract, Id, PositiveInt
from interview_agent.contracts.practice import utf16_length
from interview_agent.contracts.runtime import Citation, FixtureBundle, RunInput, SqlSnapshot

from .types import ScopeError


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ReadWindow(Contract):
    kind: Literal["answer", "evidence"]
    source_id: Id
    version: Id
    start_line: PositiveInt
    end_line: PositiveInt
    lines: Annotated[list[str], Field(min_length=1, max_length=40)]


class ScopedAssessmentStore:
    def __init__(self, data: FixtureBundle | SqlSnapshot | dict):
        raw = data.model_dump(mode="json") if isinstance(data, (FixtureBundle, SqlSnapshot)) else data
        try:
            model = SqlSnapshot if raw.get("schema_version") == "sql-snapshot-v1" else FixtureBundle
            self.__data = model.model_validate(raw).model_copy(deep=True)
        except (ValidationError, AttributeError) as exc:
            raise ScopeError("invalid_snapshot") from exc
        for values in [self.__data.sessions, self.__data.questions, self.__data.answers]:
            if len({v.id for v in values}) != len(values):
                raise ScopeError("duplicate_fixture_id")
        if len({(v.source_id, v.version) for v in self.__data.evidence}) != len(self.__data.evidence):
            raise ScopeError("duplicate_fixture_id")
        self.digest = hashlib.sha256(canonical(self.__data.model_dump(mode="json")).encode()).hexdigest()

    def session(self, run: RunInput):
        session = next((s for s in self.__data.sessions if s.id == run.session_id and s.user_id == run.server_bound_user), None)
        if session is None or session.version != run.session_version or session.question_id != run.question_id:
            raise ScopeError()
        return session.model_copy(deep=True)

    def question(self, run: RunInput, question_id: str):
        self.session(run)
        if question_id != run.question_id:
            raise ScopeError()
        question = next((q for q in self.__data.questions if q.id == question_id and q.target_role == run.target_role), None)
        if question is None:
            raise ScopeError()
        return question.model_copy(deep=True)

    def answer(self, run: RunInput):
        session = self.session(run)
        if session.answer_id is None:
            return None
        answer = next((a for a in self.__data.answers if a.id == session.answer_id and a.user_id == run.server_bound_user
                       and a.session_id == run.session_id and a.question_id == run.question_id), None)
        if answer is None:
            raise ScopeError()
        return answer.model_copy(deep=True)

    def evidence(self, run: RunInput, source_id: str, version: str, start: int, end: int) -> ReadWindow:
        question = self.question(run, run.question_id)
        allowed = lambda refs: any(r.source_id == source_id and r.version == version for r in refs)
        source = next((e for e in self.__data.evidence if e.source_id == source_id and e.version == version
                       and e.owner_user_id in (None, run.server_bound_user)), None)
        if source is None or not allowed(run.context_refs) or not allowed(question.sources):
            raise ScopeError()
        return self.window("evidence", source_id, version, source.lines, start, end)

    def answer_window(self, run: RunInput) -> ReadWindow | None:
        answer = self.answer(run)
        return None if answer is None else self.window("answer", answer.id, answer.version, answer.lines, 1, len(answer.lines))

    @staticmethod
    def window(kind: str, source_id: str, version: str, lines: list[str], start: int, end: int) -> ReadWindow:
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines) or end - start + 1 > 40:
            raise ScopeError("invalid_range")
        selected = lines[start - 1:end]
        if utf16_length("\n".join(selected)) > 8000:
            raise ScopeError("result_too_large")
        return ReadWindow(kind=kind, source_id=source_id, version=version, start_line=start, end_line=end, lines=selected)

    def verify_citation(self, run: RunInput, citation: Citation, reads: list[ReadWindow]) -> None:
        current = (self.evidence(run, citation.source_id, citation.version, citation.start_line, citation.end_line)
                   if citation.kind == "evidence" else self.answer_window(run))
        if current is None or current.source_id != citation.source_id or current.version != citation.version:
            raise ScopeError("invalid_citation")
        if not any(r.kind == citation.kind and r.source_id == citation.source_id and r.version == citation.version
                   and r.start_line <= citation.start_line <= citation.end_line <= r.end_line for r in reads):
            raise ScopeError("invalid_citation")
        if not current.start_line <= citation.start_line <= citation.end_line <= current.end_line:
            raise ScopeError("invalid_citation")
        expected = "\n".join(current.lines[citation.start_line - current.start_line:citation.end_line - current.start_line + 1])
        if citation.quote != expected:
            raise ScopeError("invalid_citation")

    def verify_read(self, run: RunInput, read: ReadWindow) -> None:
        current = (self.evidence(run, read.source_id, read.version, read.start_line, read.end_line)
                   if read.kind == "evidence" else self.answer_window(run))
        if current != read:
            raise ScopeError("invalid_checkpoint")
