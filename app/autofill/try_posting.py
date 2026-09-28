"""
Try the resolver against any real posting:

    python -m app.autofill.try_posting <url or board/job-id>

Examples:

    python -m app.autofill.try_posting https://job-boards.greenhouse.io/samsara/jobs/8082091
    python -m app.autofill.try_posting samsara/8082091
    python -m app.autofill.try_posting https://www.samsara.com/...?gh_jid=8082091 --board samsara

Exists so the resolver can be exercised without Chrome. The extension is a thin
DOM layer over exactly this, so anything that looks wrong here looks wrong there
too — and this is far quicker to iterate against.
"""
import argparse
import json
import re
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()

from app.autofill.binder import resolve_form  # noqa: E402
from app.autofill.greenhouse import parse_greenhouse_job  # noqa: E402
from app.autofill.jev_binder import JevBinder, OptionCache, ThemeCache  # noqa: E402
from app.autofill.memory import DEFAULT_PROFILE_PATH, load_memory  # noqa: E402

BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true"

#: Greenhouse is reachable three ways, and only the first is obvious:
#:   job-boards.greenhouse.io/{board}/jobs/{id}   hosted
#:   boards.greenhouse.io/{board}/jobs/{id}       older hosted
#:   any-company.com/...?gh_jid={id}              embedded on the employer's site
#: The third is common enough that ignoring it would miss a large share of real
#: postings — Samsara's own careers page is one.
_HOSTED = re.compile(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)")
_EMBEDDED = re.compile(r"[?&]gh_jid=(\d+)")
_SHORTHAND = re.compile(r"^([A-Za-z0-9_-]+)/(\d+)$")


def resolve_target(target, board_hint=None):
    """(board, job_id) from a URL or shorthand.

    Raises:
        SystemExit: with a message naming what was missing, rather than a
            traceback about a None.
    """
    hosted = _HOSTED.search(target)
    if hosted:
        return hosted.group(1), hosted.group(2)

    shorthand = _SHORTHAND.match(target.strip())
    if shorthand:
        return shorthand.group(1), shorthand.group(2)

    embedded = _EMBEDDED.search(target)
    if embedded:
        if not board_hint:
            raise SystemExit(
                "That is an embedded posting, which carries the job id "
                "({}) but not the board. Re-run with --board <name>, e.g. "
                "--board samsara.".format(embedded.group(1))
            )
        return board_hint, embedded.group(1)

    raise SystemExit(
        "Could not find a Greenhouse board and job id in {!r}.\n"
        "Pass a job-boards.greenhouse.io URL, a 'board/job-id' pair, or an "
        "embedded URL with --board.".format(target)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.autofill.try_posting")
    parser.add_argument("target", help="posting URL, or board/job-id")
    parser.add_argument("--board", help="board name, for an embedded posting")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--no-model", action="store_true",
                        help="deterministic only; nothing leaves this machine")
    parser.add_argument("--json", action="store_true", help="print the raw plan")
    args = parser.parse_args(argv)

    board, job = resolve_target(args.target, args.board)
    payload = json.load(urllib.request.urlopen(
        BOARD_API.format(board=board, job=job), timeout=30))
    form = parse_greenhouse_job(payload, board=board)
    memory = load_memory(args.profile)

    binder = None
    if not args.no_model:
        try:
            binder = JevBinder(cache=ThemeCache(), option_cache=OptionCache())
        except Exception as e:
            print("model unavailable ({}), continuing deterministically\n".format(e))

    plan = resolve_form(form, memory, binder=binder)

    if args.json:
        print(plan.model_dump_json(indent=2))
        return 0

    summary = plan.summary()
    print("{} — {}".format(form.company or board, form.title or job))
    print("{}\n".format(form.apply_url or ""))
    print("  {} fields: {} filled, {} answered by a sibling, {} skipped, "
          "{} need you".format(summary["total"], summary["filled"],
                               summary["satisfied"], summary["skipped"],
                               summary["review"]))
    if binder is not None:
        print("  {} model call(s), ${:.6f}".format(binder.calls, binder.cost_usd))
    print()

    for entry in plan.entries:
        if entry.skipped:
            state = "skip "
        elif entry.satisfied_by:
            state = "sib  "
        elif (entry.value is not None or entry.values) and not entry.needs_review:
            state = "FILL "
        else:
            state = "  >> "
        value = " + ".join(entry.values) if entry.values else (entry.value or "")
        print("  {} {:44} {}".format(state, entry.label[:44], str(value)[:36]))

    print()
    print("  >> marks what needs you. Nothing is ever submitted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
