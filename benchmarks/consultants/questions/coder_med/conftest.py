"""pytest config for the coder_med oracle suite (AUTO-GENERATED).

Registers the ``constraint`` marker so the two-axis grading in
coder_bench treats ``test_source_present`` as instruction-following
(does NOT gate ``passes_algorithm``) while the parametrized ``test_io``
cases decide correctness.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "constraint: structural check; does NOT gate trial.passes_algorithm",
    )
