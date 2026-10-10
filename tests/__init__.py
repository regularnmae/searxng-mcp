# Marks `tests` as a package so test modules can import shared helpers
# (`tests.fakes`, `tests.minimal_pdf`) with the repo root on sys.path,
# which pyproject sets via pytest's `pythonpath` option.
