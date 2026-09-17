from __future__ import annotations

from dataclasses import dataclass


ACADEMY_CATEGORY_ID = "academy-program"
ACADEMY_SERVICE_ID = "academy-enrollment-fee"
ACADEMY_ENROLLMENT_FEE_CENTS = 10_000


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


ACADEMY_TIERS = (
    AcademyTier(
        key="foundation",
        name="Foundation",
        tuition_usd_cents=50_000,
        summary="Recorded modules, weekly text Q&A, and core lab access.",
        target_student="Beginners building a disciplined technical foundation.",
    ),
    AcademyTier(
        key="operator",
        name="Operator",
        tuition_usd_cents=150_000,
        summary="Foundation plus weekly live sessions, mentoring, the full lab suite, and scenario-based exercises.",
        target_student="Serious learners ready for sustained practical work.",
    ),
    AcademyTier(
        key="black",
        name="Black Tier",
        tuition_usd_cents=500_000,
        summary="Operator plus private mentoring, advanced secure-development study, and apprenticeship consideration.",
        target_student="Advanced students pursuing professional specialization.",
    ),
)

ACADEMY_TIERS_BY_KEY = {tier.key: tier for tier in ACADEMY_TIERS}


def get_academy_tier(key: str) -> AcademyTier | None:
    return ACADEMY_TIERS_BY_KEY.get(key)
