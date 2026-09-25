"""The first-run checklist's server side: the sample project and the first finished task (lumi/gui/onboarding.py)."""
from __future__ import annotations

import subprocess
import sys

from lumi.gui import onboarding
from lumi.gui.settings import SettingsManager


def test_the_sample_project_has_a_bug_to_find(tmp_path):
    project = onboarding.create_sample_project(tmp_path)
    assert project == tmp_path / "lumi-sample"
    assert sorted(p.name for p in project.iterdir()) == ["README.md", "app.py"]
    assert onboarding.SAMPLE_TASK in (project / "README.md").read_text(encoding="utf-8")
    # The planted bug: average() of an empty list raises instead of returning 0.0.
    probe = ("import app; assert app.greet(' ') == 'Hello, there!'\n"
             "try:\n    app.average([])\nexcept ZeroDivisionError:\n    print('bug')\n")
    result = subprocess.run([sys.executable, "-c", probe], cwd=project, capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "bug"


def test_an_existing_sample_is_never_overwritten(tmp_path):
    project = onboarding.create_sample_project(tmp_path)
    (project / "app.py").write_text("# my changes\n", encoding="utf-8")
    assert onboarding.create_sample_project(tmp_path) == project
    assert (project / "app.py").read_text(encoding="utf-8") == "# my changes\n"


def test_a_finished_turn_is_an_answer_without_errors():
    assert onboarding.turn_finished([{"event": "user"}, {"event": "text.done"}, {"event": "session.end"}])
    assert not onboarding.turn_finished([{"event": "text.done"}, {"event": "error", "message": "x"}])
    assert not onboarding.turn_finished([{"event": "user"}])


def test_settings_and_socket_commands(tmp_path, monkeypatch):
    from tests.test_connections import _command

    settings = SettingsManager(tmp_path / "settings.json")
    assert settings.get("onboarding") == {"dismissed": False, "first_task_done": False}
    sent = _command(settings, "update_settings", section="onboarding", key="dismissed", value=True)
    assert settings.get("onboarding", "dismissed") is True and sent[0]["event"] == "settings"
    refused = _command(settings, "update_settings", section="onboarding", key="dismissed", value="yes")
    assert refused[0]["event"] == "error"
    # The page can't mark the task done itself; only a finished turn does.
    refused = _command(settings, "update_settings", section="onboarding", key="first_task_done", value=True)
    assert refused[0]["event"] == "error" and settings.get("onboarding", "first_task_done") is False

    monkeypatch.setattr(onboarding, "sample_project_path", lambda projects_root=None: tmp_path / "lumi-sample")
    [reply] = _command(settings, "create_sample_project")
    assert reply == {"event": "sample_project", "path": str(tmp_path / "lumi-sample"), "task": onboarding.SAMPLE_TASK}
    assert (tmp_path / "lumi-sample" / "app.py").exists()
