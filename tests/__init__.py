"""Offline test suite for CST_AI.

Standard library only: no third-party test runner is required and none is
assumed.  Run it from the repository root

    python -m unittest discover -s tests -v

or from anywhere with an explicit top-level directory

    python -m unittest discover -s <repo>/tests -t <repo> -v

using the interpreter bundled with CST Studio Suite, because `cst_ai_plugin`
imports the `cst` package.  Nothing here connects to CST, solves, or writes into
the repository: the suite asserts exactly that.
"""
