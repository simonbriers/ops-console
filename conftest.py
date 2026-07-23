# Presence of this file at the repo root puts the root on sys.path during test
# collection, so `from backend import ...` resolves whether tests are run as
# `pytest` or `python -m pytest`. (Bare `pytest backend/tests/x.py` doesn't add
# the CWD to sys.path; `python -m pytest` does — this makes both work.)
