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
