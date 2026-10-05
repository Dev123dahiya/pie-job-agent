"""Which platform a job belongs to, and how it is applied to.

platform: naukri | linkedin | company_site | instahyre (daily limits are counted per platform).
apply type:
- company_form: an employer's own application form PIE can fill (Greenhouse, Lever...)
- naukri_direct / linkedin_easy_apply / instahyre_direct: applied on the platform itself
- external_link: a listing that only links elsewhere (aggregator APIs); PIE follows the link
"""

from typing import Literal

from database.models import Job

Platform = Literal["naukri", "linkedin", "company_site", "instahyre"]
PLATFORMS: tuple[Platform, ...] = ("naukri", "linkedin", "company_site", "instahyre")
# Job sources that are job platforms themselves (applied to on the platform, with the profile
# saved there: no tailored resume, no employer form).
PLATFORM_SOURCES: tuple[str, ...] = ("naukri", "linkedin", "instahyre")

ApplyType = Literal[
    "company_form", "naukri_direct", "linkedin_easy_apply", "instahyre_direct", "external_link"
]
APPLY_TYPES: tuple[ApplyType, ...] = (
    "company_form",
    "naukri_direct",
    "linkedin_easy_apply",
    "instahyre_direct",
    "external_link",
)

# Search-only sources: no applying through them (foundit: its listings are copies of other
# sites' jobs). A shortlisted job from here goes straight to the Manual apply list.
MANUAL_ONLY_SOURCES = frozenset({"foundit", "indeed"})  # Indeed: until its apply is built

# Sources whose postings are the employer's own form on a supported ATS (or the test site).
FORM_SOURCES = frozenset({"greenhouse", "lever", "ashby", "smartrecruiters", "mock"})


def platform_of(job: Job) -> Platform:
    source = (job.source or "").lower()
    if source == "naukri":
        return "naukri"
    if source == "linkedin":
        return "linkedin"
    if source == "instahyre":
        return "instahyre"
    return "company_site"  # Greenhouse, Lever, Ashby... and every other employer site


def apply_type_of(job: Job) -> ApplyType:
    source = (job.source or "").lower()
    if source == "naukri":
        return "naukri_direct"
    if source == "linkedin":
        return "linkedin_easy_apply"
    if source == "instahyre":
        return "instahyre_direct"
    return "company_form" if source in FORM_SOURCES else "external_link"


def sources_for_apply_type(apply_type: ApplyType, known_sources: list[str]) -> list[str]:
    """The job sources (as stored) whose jobs have this apply type."""
    return [s for s in known_sources if apply_type_of(Job(source=s)) == apply_type]
