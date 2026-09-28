"""
Interactive profile setup:  python -m app.autofill.setup

Prompts for each field the resolver knows about, in order of how many real
application fields it answers, so stopping early still leaves a useful profile.

Exists instead of "edit this JSON file" for three reasons: nested JSON is easy
to break and a syntax error is a silent empty profile; the ordering carries real
information the file cannot show; and a re-run has to preserve what is already
there rather than starting over.
"""
import os
import sys
from typing import Dict, List, Optional, Tuple

from app.autofill.memory import (
    DEFAULT_PROFILE_PATH,
    PROFILE_FIELDS,
    Memory,
    load_memory,
    save_memory,
)

#: Extra guidance for fields where the format actually matters.
NOTES: Dict[str, str] = {
    "graduation_date": "month and year, e.g. 'May 2028'",
    "gpa": "bare number, no scale: '3.8' not '3.8/4.0'",
    "resume_file": "full path to the PDF",
    "enrollment_status": "e.g. 'Currently enrolled'",
    "internship_term": "'Summer' or 'Winter'",
    "engineering_track": "e.g. 'Backend', 'Machine Learning', 'Full Stack'",
    "work_authorization": "'Yes' if you can work without sponsorship",
    "needs_sponsorship": "'No' if you are a citizen or permanent resident",
    "age_18_plus": "'Yes' or 'No'",
    "heard_about": "how you usually find roles, e.g. 'LinkedIn'",
    "postal_code": "your home zip / postal code",
    "links_combined": "one line for forms that ask for any of your links",
}

#: Suggested answers for the protected questions. Any phrasing works — the
#: matcher treats every way of declining as equivalent, so "Prefer not to say"
#: is understood by forms that word it "I don't wish to answer".
PROTECTED_CHOICES: Dict[str, List[str]] = {
    "veteran_status": ["I am not a protected veteran",
                       "I identify as one or more classifications of protected veteran",
                       "Prefer not to say"],
    "disability_status": ["No, I do not have a disability",
                          "Yes, I have a disability",
                          "Prefer not to say"],
    "race_ethnicity": ["Asian", "White", "Black or African American",
                       "Hispanic or Latino", "Two or More Races", "Prefer not to say"],
    "gender": ["Male", "Female", "Prefer not to say"],
}


def _ordered_fields() -> List[Tuple[int, str, str, str]]:
    """(count, section, key, example), most valuable first."""
    rows = []

    for section, entries in PROFILE_FIELDS.items():
        for name, example, *rest in entries:
            rows.append((rest[0] if rest else 0, section, name, example))

    return sorted(rows, key=lambda row: -row[0])


def _section(memory: Memory, name: str) -> Dict[str, str]:
    return getattr(memory, name)


def _ask(prompt: str) -> Optional[str]:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def main(path: Optional[str] = None) -> int:
    target = path or DEFAULT_PROFILE_PATH
    memory = load_memory(target)
    rows = _ordered_fields()

    print()
    print("FirstPlay profile setup")
    print("-" * 64)
    print("Enter a value, or press Enter to skip / keep what is there.")
    print("Type  q  at any prompt to save and quit.")
    print()
    print("Each field shows how many real application fields it answers,")
    print("measured across 57 live SWE-intern postings.")
    print()
    print("Saving to: {}".format(target))
    print("-" * 64)

    answered = 0

    for count, section, key, example in rows:
        store = _section(memory, section)
        current = store.get(key)

        label = "{}.{}".format(section, key)
        note = NOTES.get(key)

        print()
        print("  {}   (answers {} field{})".format(label, count, "" if count == 1 else "s"))
        if note:
            print("    format: {}".format(note))
        if key in PROTECTED_CHOICES:
            print("    common answers:")
            for index, option in enumerate(PROTECTED_CHOICES[key], 1):
                print("      {}) {}".format(index, option))
            print("    enter a number, or type your own wording")
        elif example:
            print("    example: {}".format(example))
        if current:
            print("    current: {}".format(current))

        reply = _ask("    > ")

        if reply is None:
            print("\n  interrupted")
            break

        reply = reply.strip()

        if reply.lower() == "q":
            break

        if not reply:
            continue

        # A bare number against a protected question selects from the menu, so
        # nobody has to retype "I am not a protected veteran".
        if key in PROTECTED_CHOICES and reply.isdigit():
            choices = PROTECTED_CHOICES[key]
            index = int(reply)
            if 1 <= index <= len(choices):
                reply = choices[index - 1]
            else:
                print("    no option {}, storing as typed".format(index))

        store[key] = reply
        answered += 1

    written = save_memory(memory, target)

    filled = sum(
        1 for _count, section, key, _example in rows if _section(memory, section).get(key)
    )
    covered = sum(
        count for count, section, key, _example in rows if _section(memory, section).get(key)
    )
    total = sum(count for count, _s, _k, _e in rows)

    print()
    print("-" * 64)
    print("saved {}".format(written))
    print("  set this run     : {}".format(answered))
    print("  filled in total  : {}/{} fields".format(filled, len(rows)))
    print("  answers about    : {}/{} field instances in the intern corpus".format(
        covered, total))
    if filled < len(rows):
        print()
        print("  re-run any time to fill in the rest; existing values are kept.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
