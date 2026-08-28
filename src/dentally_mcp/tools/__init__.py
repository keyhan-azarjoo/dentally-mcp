"""Tool registration.

Split by the job being done rather than by Dentally resource, because that is how a
practice thinks about it: the front desk asks about the diary, the manager asks about
money, and neither wants the other's tools in their context window.
"""
from __future__ import annotations

from . import appointments, finance, patients, practice


def register_all(mcp) -> None:
    patients.register(mcp)
    appointments.register(mcp)
    finance.register(mcp)
    practice.register(mcp)


__all__ = ["register_all", "patients", "appointments", "finance", "practice"]
