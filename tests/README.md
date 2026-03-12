# Automated Testing
The following guide covers how to run all tests for auto-southwest-check-in. There are two types
of tests currently implemented: unit tests and integration tests.

## Writing Tests
Writing automated tests for new features or bug fixes is vital to maintaining the reliability of
auto-southwest-check-in. Unit tests should be written/modified when any code changes enough to
trigger test failures or when that code isn't tested (can be seen with a coverage report).

Integration tests should be added whenever a large feature is added or changed. These tests should
test multiple parts of the script rather than just one.

The test naming and formatting conventions can be replicated from the tests that already exist.

## Running Tests
[pytest] is used to run all tests. Both unit tests and integration tests are automatically run
after every pull request and push to the `master` branch using a [GitHub workflow]. Additionally,
unit tests are also run on every push to the `develop` branch.

### Setup
Install all the requirements needed
```shell
pip install -r tests/requirements.txt
```

If you prefer to keep test and lint tools isolated from your global Python install, you can use the
repo-local virtual environment:
```shell
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r tests/requirements.txt
.venv/bin/pip install ruff
```

### Running the Tests
To run all tests
```shell
pytest
```

If you are using the local virtual environment, run:
```shell
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_mock
```

To run only unit tests
```shell
pytest tests/unit
```

To run only integration tests
```shell
pytest tests/integration
```

To run all tests for a specific module
```shell
pytest tests/unit/test_<module name>.py
```
Or multiple
```shell
pytest tests/unit/test_<module1>.py tests/unit/test_<module2>.py
```

To get a coverage report
```shell
pytest --cov
```

To lint the Python files with the local virtual environment, run:
```shell
.venv/bin/ruff check .
```

[pytest]: https://docs.pytest.org
[GitHub workflow]: ../.github/workflows/tests.yml
