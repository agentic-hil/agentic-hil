"""The bench tier is a package so its conftest does not take over the name.

``tests/conftest.py`` is imported as the top-level module ``conftest`` and the
suite's other modules import their helpers from it by that name. A second
``conftest.py`` in a directory that is not a package is imported under the same
name and replaces it, and every module that had imported the first one then
fails to collect. The ``__init__.py`` is what keeps this one at
``bench.conftest`` instead.
"""
