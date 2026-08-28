"""Role scoping: what each role sees, and what it cannot invoke anyway."""
from __future__ import annotations

import pytest

from dentally_mcp import surfaces


def test_every_surface_can_orient_itself():
    for role in surfaces.SURFACES:
        assert "whoami" in surfaces.visible(role)


def test_nurses_and_clinicians_have_no_financial_tools():
    """A hygienist does not need the practice's revenue in their context window."""
    for role in ("nurse", "clinician"):
        visible = surfaces.visible(role)
        assert "revenue_summary" not in visible
        assert "list_invoices" not in visible
        assert "list_nhs_claims" not in visible


def test_only_reception_can_change_the_diary():
    assert "book_appointment" in surfaces.visible("reception")
    for role in ("nurse", "clinician", "manager"):
        assert "book_appointment" not in surfaces.visible(role)


def test_full_is_the_union_of_every_role():
    union = set().union(*(surfaces.SURFACES[r] for r in surfaces.SURFACES if r != "full"))
    assert surfaces.SURFACES["full"] == union


def test_every_write_tool_is_reachable_from_some_role():
    """A write tool in no surface would be dead code that still looks implemented."""
    for tool in surfaces.WRITE_TOOLS:
        assert any(tool in surfaces.SURFACES[r] for r in surfaces.SURFACES), tool


@pytest.mark.parametrize("alias,expected", [
    ("receptionist", "reception"), ("dentist", "clinician"), ("hygienist", "nurse"),
    ("practice-manager", "manager"), ("MANAGER", "manager"), ("all", "full"),
])
def test_job_titles_map_to_surfaces(alias, expected):
    assert surfaces.normalise(alias) == expected


def test_an_unknown_role_falls_back_to_the_narrowest_useful_surface():
    """Fail closed: an unrecognised header must not widen access."""
    assert surfaces.normalise("chief-dental-wizard") == surfaces.DEFAULT
    assert surfaces.normalise(None) == surfaces.DEFAULT
    assert surfaces.normalise("") == surfaces.DEFAULT
    assert surfaces.DEFAULT != "full"


def test_write_classification_matches_the_tool_names():
    assert surfaces.is_write("cancel_appointment")
    assert not surfaces.is_write("list_appointments")
