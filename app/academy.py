from __future__ import annotations

from dataclasses import dataclass


ACADEMY_CATEGORY_ID = "academy-program"
ACADEMY_SERVICE_ID = "academy-enrollment-fee"
ACADEMY_FIRST_MONTH_CENTS = 10_000


@dataclass(frozen=True)
class AcademyTier:
    key: str
    name: str
    tuition_usd_cents: int
    summary: str
    target_student: str

    @property
    def installments_usd_cents(self) -> tuple[int, int, int]:
        base, remainder = divmod(self.tuition_usd_cents, 3)
        return tuple(base + (1 if index < remainder else 0) for index in range(3))


ACADEMY_PROGRAM = AcademyTier(
    key="operator",
    name="Academy Program",
    tuition_usd_cents=30_000,
    summary="Twelve weeks of guided study, practical labs, feedback, and a final capstone assessment.",
    target_student="Committed learners who want practical, professional cybersecurity skills.",
)

ACADEMY_TIERS = (
    ACADEMY_PROGRAM,
)

ACADEMY_TIERS_BY_KEY = {tier.key: tier for tier in ACADEMY_TIERS}


def get_academy_tier(key: str) -> AcademyTier | None:
    return ACADEMY_TIERS_BY_KEY.get(key)
