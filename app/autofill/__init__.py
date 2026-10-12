"""Job-application autofill: form extraction, field binding, narrative drafting.

Layered deliberately, because only one of the four layers wants a model:

    A. extraction  which controls exist          deterministic (this package)
    B. binding     which stored fact goes here   Jev typed decision
    C. formatting  phone/date/option shape       deterministic
    D. narrative   essays                        Jev -> LLM -> Jev

Layer A is built first and on its own because it is the part most likely to
sink the project, and because every later layer is scored against the corpus
it produces.
"""


def engine_fingerprint() -> str:
    """A short hash of this package's source, so a client can tell when the
    engine changed. The extension keys its session plan cache on it: a plan is
    a pure function of posting, profile *and* engine, and after §40 a cached
    pre-widening plan was served for twelve minutes on the board that was
    meant to test the widening."""
    import hashlib
    import os

    digest = hashlib.sha1()
    here = os.path.dirname(__file__)
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            with open(os.path.join(here, name), "rb") as handle:
                digest.update(name.encode())
                digest.update(handle.read())
    return digest.hexdigest()[:12]


ENGINE = engine_fingerprint()
