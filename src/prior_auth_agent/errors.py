"""Safe, stable descriptions for human handoff."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    field: str
    code: str
    problem: str
    suggested_action: str


class InputProblem(Exception):
    def __init__(self, issue: Issue) -> None:
        self.issue = issue
        super().__init__(issue.problem)
