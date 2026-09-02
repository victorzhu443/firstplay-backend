"""
Regression tests for sparse-resume parsing.

The payloads here are not hypothetical. The one in
test_parses_production_payload_that_broke_the_pipeline is the exact completion
gpt-4o-mini returned for tests/fixtures/sample_resume.pdf in production, which
PydanticOutputParser rejected with:

    1 validation error for ResumeParsed
    experience.0.duration
      Input should be a valid string [type=string_type, input_value=None]

That rejection failed the parse_resume node and took down the whole pipeline
run, surfacing to the user as an unrelated error from the final node.
"""
import json

from app.schemas import (
    EducationItem,
    ExperienceItem,
    ImprovedResumeParsed,
    ProjectItem,
    ResumeParsed,
)


def test_parses_production_payload_that_broke_the_pipeline():
    """The verbatim completion that returned a 500 from /api/resume/parse."""
    payload = json.loads("""
    {
      "name": "JOHN DOE",
      "email": null,
      "phone": null,
      "skills": ["Python", "JavaScript", "React", "FastAPI", "SQL"],
      "experience": [{
        "company": "Tech Company",
        "title": "Software Developer",
        "duration": null,
        "bullets": ["Built web applications using modern frameworks"]
      }],
      "projects": [],
      "education": []
    }
    """)

    parsed = ResumeParsed(**payload)

    assert parsed.name == "JOHN DOE"
    assert parsed.experience[0].company == "Tech Company"
    # The null becomes "", not None, so downstream consumers still see a string.
    assert parsed.experience[0].duration == ""


def test_null_is_normalised_to_empty_string_not_none():
    """Downstream code does string operations on these without guarding."""
    item = ExperienceItem(company=None, title=None, duration=None, bullets=[])

    for value in (item.company, item.title, item.duration):
        assert value == ""
        assert isinstance(value, str)


def test_missing_keys_fall_back_to_empty():
    """A model that omits a key entirely behaves like one that sends null."""
    assert ExperienceItem().duration == ""
    assert ProjectItem().description == ""
    assert EducationItem().graduation_date == ""
    assert ResumeParsed().experience == []


def test_populated_values_are_untouched():
    """Coercion must only apply to null — real values pass through verbatim."""
    item = ExperienceItem(
        company="Tech Corp",
        title="Software Engineer",
        duration="Jan 2022 - Present",
        bullets=["Built APIs"],
    )

    assert item.company == "Tech Corp"
    assert item.duration == "Jan 2022 - Present"
    assert item.bullets == ["Built APIs"]


def test_improved_resume_survives_null_contact():
    """
    The browser calls .replace() on name and passes contact to jsPDF doc.text(),
    both of which throw on null, so node 5 must not emit one either.
    """
    improved = ImprovedResumeParsed(
        name=None,
        contact=None,
        skills=["Python"],
        experience=[],
        projects=[],
        education=[],
    )

    assert improved.name == ""
    assert improved.contact == ""


def test_education_still_accepts_both_string_and_dict():
    """The union form the frontend already branches on must keep working."""
    improved = ImprovedResumeParsed(
        name="John Doe",
        contact="john@example.com",
        education=[
            "Cornell University, BS Computer Science, May 2027",
            {"institution": "Cornell", "degree": "BS CS", "graduation_date": "May 2027"},
        ],
    )

    assert isinstance(improved.education[0], str)
    assert isinstance(improved.education[1], dict)
