"""
Freeze a corpus of live SWE-intern postings:  python -m app.autofill.freeze

Snapshots raw Greenhouse payloads to ~/.config/firstplay/corpus so every later
measurement scores identical inputs. Live postings change under you, so a number
measured against them cannot be compared with one measured tomorrow.
"""
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from app.autofill.evaluate import CORPUS_DIR, freeze_corpus

BOARDS = ["stripe", "databricks", "figma", "discord", "robinhood", "plaid", "ramp",
          "brex", "scaleai", "coinbase", "doordash", "lyft", "reddit", "instacart",
          "anthropic", "samsara", "verkada", "duolingo", "snap", "pinterest"]

INTERN = re.compile(r"\b(intern|internship|co-?op)\b", re.I)
SWE = re.compile(r"\b(software|engineer|engineering|swe|developer|backend|frontend|"
                 r"full ?stack|machine learning|infrastructure|platform|security|data)\b", re.I)
SKIP = re.compile(r"\b(recruiter|recruiting|electrical|mechanical|hardware|financial|"
                  r"accounting|marketing|sales|legal|design)\b", re.I)


def _jobs(board):
    try:
        url = "https://boards-api.greenhouse.io/v1/boards/{}/jobs".format(board)
        data = json.load(urllib.request.urlopen(url, timeout=15))
        return [(board, j["id"], j["title"]) for j in data.get("jobs", [])]
    except Exception:
        return []


def _questions(target):
    board, job_id, _title = target
    try:
        url = ("https://boards-api.greenhouse.io/v1/boards/{}/jobs/{}"
               "?questions=true".format(board, job_id))
        return json.load(urllib.request.urlopen(url, timeout=15))
    except Exception:
        return None


def main() -> int:
    with ThreadPoolExecutor(20) as pool:
        listings = [j for batch in pool.map(_jobs, BOARDS) for j in batch]

    targets = [t for t in listings
               if INTERN.search(t[2]) and SWE.search(t[2]) and not SKIP.search(t[2])]

    print("scanned {} boards, {} postings, {} SWE-intern matches".format(
        len(BOARDS), len(listings), len(targets)))

    with ThreadPoolExecutor(16) as pool:
        payloads = [p for p in pool.map(_questions, targets) if p]

    written = freeze_corpus(payloads)
    print("froze {} postings to {}".format(written, CORPUS_DIR))

    return 0


if __name__ == "__main__":
    sys.exit(main())
