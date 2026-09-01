# Development and verification

Use Python 3.12–3.14 in a repository-local virtualenv. Production paths are selected only when `MUSIC_AGENT_ENVIRONMENT=production`; tests should use temporary directories and synthetic media. No test should require `/srv`, a real OpenAI key, a network media download, or an existing Navidrome service.

## Windows and macOS

PowerShell:

```powershell
py -3.14 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements/development.lock
python -m pip install --no-build-isolation --no-deps -e .
pytest
```

POSIX:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements/development.lock
python -m pip install --no-build-isolation --no-deps -e .
pytest
```

Install ffmpeg separately for media integration tests; those tests skip cleanly when it is absent. Filesystem publication semantics and all deployment scripts must receive final validation on Linux/WSL2 because Windows rename, ACL, executable-bit, and service behavior differ.

## Checks

```bash
pytest
ruff check app tests scripts/sqlite-maintenance.py
mypy app
python -m pip check
python -m pip_audit --require-hashes -r requirements/production.lock
find scripts -name '*.sh' -print0 | xargs -0 -n1 bash -n
find scripts -name '*.sh' -print0 | xargs -0 shellcheck
```

On Ubuntu 26.04 also run `sudo scripts/validate.sh --services` and `systemd-analyze verify` after deployment.

## Dependency changes

Edit exact direct pins in `pyproject.toml` and the corresponding `.in` file together, run tests on Python 3.12 and 3.14, then regenerate transitive pins. Artifact hash locks must be generated on Ubuntu 26.04/Python 3.14:

```bash
PYTHON_BIN=/usr/bin/python3 bash scripts/lock-requirements.sh
```

The helper creates a temporary venv with pinned `pip-tools==7.5.0`, uses `--generate-hashes`, and omits machine-specific headers. Review the diff and commit both locks. Never invent or hand-copy package hashes. Tool binary pins are independent in `requirements/tool-pins.env`; verify them against the official release checksum assets.
