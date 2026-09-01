from __future__ import annotations

import pytest

from app.workers.runner import main


def test_worker_help_does_not_initialize_runtime(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    assert "durable Music Agent media worker" in capsys.readouterr().out
