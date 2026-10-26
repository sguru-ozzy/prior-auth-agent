"""Reviewed equivalent labels match without broadening their subject or meaning."""

import pytest

from prior_auth_agent.agent.matching import name_pattern


@pytest.mark.parametrize("label", ["First name", "Given name", "  GIVEN   NAME  "])
def test_reviewed_equivalent_accessible_names_match(label: str) -> None:
    assert name_pattern("First name", ["Given name"]).fullmatch(label)


@pytest.mark.parametrize("label", ["Prescriber given name", "Subscriber first name", "Nickname", "First"])
def test_unreviewed_subjects_and_partial_names_do_not_match(label: str) -> None:
    assert not name_pattern("First name", ["Given name"]).fullmatch(label)
