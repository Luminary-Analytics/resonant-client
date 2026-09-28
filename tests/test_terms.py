"""Lumi's terms (lumi/terms.py): what's in force for a build, accepting them, the machine policy that accepts
them for an organization, LUMI_ACCEPT_TERMS, and the gate on every turn path, which rides on organization
oversight's (lumi/oversight.py)."""

from __future__ import annotations

import asyncio
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import oversight, policy, terms
from lumi.paths import state_home
from tests.streaming_stub import StreamingBackend, done, text_delta
from tests.test_oversight import NeverCalled, _command, _session

REFUSED = terms.REFUSAL_CODE


@pytest.fixture
def pending(monkeypatch):
    """The real gate with nothing accepted: no record, no machine policy, no LUMI_ACCEPT_TERMS."""
    monkeypatch.delenv(terms.ENVIRONMENT, raising=False)
    terms.set_for_tests(None)
    return terms.required()


def _versions(documents=None):
    return {doc.id: doc.version for doc in (terms.required() if documents is None else documents)}


def _refused(events):
    return [event for event in events if event.get("event") == "error" and event.get("code") == REFUSED]


def _with_version(monkeypatch, doc_id, version):
    """terms.json as if a release changed a document's version."""
    data = copy.deepcopy(terms.facts())
    data["documents"][doc_id]["version"] = version
    monkeypatch.setattr(terms, "facts", lambda: data)


class TestWhatIsInForce:
    @pytest.mark.parametrize("version, prerelease", [
        ("0.20.0", False), ("1.2.3", False), ("0.20.0.post1", False), ("0.20.0+build7", False),
        ("0.21.0-beta.1", True), ("0.21.0-alpha.2", True), ("0.21.0-rc.1", True), ("0.20.0a1", True),
        ("0.20.0b2", True), ("0.20.0rc1", True), ("0.19.2.dev11", True), ("0.20.0.dev0", True),
    ])
    def test_a_pre_release_build_adds_the_test_terms(self, version, prerelease):
        assert terms.is_prerelease(version) is prerelease
        ids = [doc.id for doc in terms.required(version)]
        assert ids == (["eula", "alpha_terms"] if prerelease else ["eula"])

    def test_the_acceptance_value_names_each_document_and_version(self):
        eula, alpha = terms.document("eula"), terms.document("alpha_terms")
        assert terms.acceptance_value(terms.required("0.20.0")) == f"eula-{eula.version}"
        value = terms.acceptance_value(terms.required("0.21.0-beta.1"))
        assert value == f"eula-{eula.version},alpha-terms-{alpha.version}"
        assert terms.parse_value(value) == {"eula": eula.version, "alpha_terms": alpha.version}
        assert terms.parse_value(" EULA-2.0  alpha-terms-1.1 ") == {"eula": "2.0", "alpha_terms": "1.1"}
        for wrong in ("", "yes", "eula", "eula-", "privacy-1.0", "1.0"):
            with pytest.raises(terms.TermsError):
                terms.parse_value(wrong)

    def test_the_texts_ship_with_lumi(self):
        for doc_id in terms.READABLE_DOCUMENTS:
            doc = terms.document(doc_id)
            text = terms.text(doc_id)
            assert text.startswith(f"# {doc.title}\n") and f"Version {doc.version}" in text
            assert "<!--" not in text and "\r" not in text
        assert terms.long_date("2026-09-28") == "September 28, 2026"


class TestAccepting:
    def test_nothing_reaches_a_model_until_the_terms_are_accepted(self, pending, tmp_path):
        session = _session(tmp_path, NeverCalled())
        [refusal] = _refused(list(session.run("hello")))
        assert "accept its terms" in refusal["message"] and "Review terms" in refusal["message"]
        assert terms.accept(_versions(), "app") is True
        session = _session(tmp_path, StreamingBackend(events=[text_delta("Ran."), done()]))
        assert not _refused(list(session.run("hello")))
        record = json.loads(terms.record_path().read_text(encoding="utf-8"))
        assert record["kind"] == terms.RECORD_KIND
        entry = record["people"][oversight.os_user()]["eula"]
        assert entry["version"] == terms.document("eula").version and entry["surface"] == "app"
        assert entry["sha256"] == terms.text_sha256("eula") and entry["accepted_at"].endswith("Z")
        assert [item["document"] for item in record["history"]] == [doc.id for doc in pending]

    def test_only_the_terms_in_force_can_be_accepted(self, pending):
        stale = {doc.id: "0.9" for doc in pending}
        assert terms.accept(stale, "app") is False and terms.pending()
        assert terms.accept({"eula": terms.document("eula").version}, "app") is False  # the test terms are missing
        assert not terms.record_path().exists()
        with pytest.raises(ValueError):
            terms.accept(_versions(), "a status push")
        with pytest.raises(terms.TermsError, match="isn't the version in force"):
            terms.accept_value("eula-0.9,alpha-terms-0.9", "command")
        with pytest.raises(terms.TermsError, match=terms.acceptance_value()):
            terms.accept_value("please", "command")
        assert terms.accept_value(terms.acceptance_value(), "command") == "" and terms.pending() == []

    def test_a_new_version_asks_again_and_a_new_date_doesnt(self, pending, monkeypatch, tmp_path):
        assert terms.accept(_versions(), "app")
        data = copy.deepcopy(terms.facts())
        data["documents"]["eula"]["effective"] = "2027-01-01"
        monkeypatch.setattr(terms, "facts", lambda: data)
        assert terms.pending() == []
        _with_version(monkeypatch, "eula", "2.0")
        assert [doc.id for doc in terms.pending()] == ["eula"]
        status = terms.status()
        eula = status["required"][0]
        assert (eula["accepted"], eula["previous_version"], eula["version"]) == (False, "1.0", "2.0")
        assert _refused(list(_session(tmp_path, NeverCalled()).run("hello")))
        # The test terms stay accepted; only the new agreement is asked for.
        assert terms.accept({"eula": "2.0"}, "app") and terms.pending() == []

    def test_each_computer_user_accepts_for_themselves(self, pending, monkeypatch):
        assert terms.accept(_versions(), "app")
        monkeypatch.setattr(oversight, "os_user", lambda: "someone-else")
        assert terms.pending()

    def test_a_record_written_by_hand_in_another_shape_counts_for_nothing(self, pending):
        terms.record_path().parent.mkdir(parents=True, exist_ok=True)
        terms.record_path().write_text(json.dumps({"eula": "1.0", "accepted": True}), encoding="utf-8")
        assert terms.pending()

    def test_a_broken_terms_file_refuses_rather_than_admits(self, pending, monkeypatch, tmp_path):
        def broken():
            raise OSError("terms.json is missing")

        monkeypatch.setattr(terms, "facts", broken)
        assert "couldn't read its terms" in terms.refusal()
        # No organization policy asks for oversight, and still nothing is admitted.
        admission = oversight.admit(_session(tmp_path, NeverCalled()))
        assert admission.refusal and admission.code == REFUSED
        assert terms.status()["pending"] is True and "couldn't read its terms" in terms.status()["error"]


class TestOrganizationsAndAutomation:
    def _machine_policy(self, monkeypatch, tmp_path, document):
        path = tmp_path / "machine" / "policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr(policy, "_registry_policy", lambda: None)
        monkeypatch.setattr(policy, "_macos_managed_policy", lambda: None)
        monkeypatch.setattr(policy, "machine_policy_file", lambda: path)
        monkeypatch.delenv("LUMI_POLICY_FILE", raising=False)
        return path

    def test_a_machine_policy_accepts_for_everyone_on_the_computer(self, pending, monkeypatch, tmp_path):
        self._machine_policy(monkeypatch, tmp_path, {"schema": policy.SCHEMA, "organization": "Acme",
                                                     "legal": {"accepted_by_organization": "  Acme   Corp "}})
        policy.load(force=True)
        assert policy.terms_accepted_by()[0] == "Acme Corp" and terms.pending() == []
        status = terms.status()
        assert (status["pending"], status["organization"]) == (False, "Acme Corp")
        assert oversight.gate("app") == ("", "")
        assert not _refused(list(_session(tmp_path, StreamingBackend(events=[text_delta("Ran."), done()]))
                                 .run("hello")))

    def test_only_a_machine_policy_can_accept(self, pending, monkeypatch, tmp_path):
        document = {"schema": policy.SCHEMA, "organization": "Acme", "legal": {"accepted_by_organization": "Acme"}}
        # LUMI_POLICY_FILE, which a person can set, is read as policy but can't accept for anyone.
        mine = tmp_path / "mine.json"
        mine.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr(policy, "_registry_policy", lambda: None)
        monkeypatch.setattr(policy, "_macos_managed_policy", lambda: None)
        monkeypatch.setattr(policy, "_machine_file_policy", lambda: None)
        monkeypatch.setenv("LUMI_POLICY_FILE", str(mine))
        state = policy.load(force=True)
        assert state.policy.terms_accepted_by == "Acme" and not state.from_machine
        assert policy.terms_accepted_by() == ("", "") and terms.pending()
        # Nor can a Lumi Cloud policy that a person brought by joining an organization.
        parsed = policy.parse(document, source="Lumi Cloud: Acme (joined in this app)")
        monkeypatch.setattr(policy, "load", lambda force=False: policy.PolicyState(policy=parsed, cloud=True))
        assert policy.terms_accepted_by() == ("", "") and terms.pending()
        # A machine policy that set up that Lumi Cloud does, while its policy is in force.
        machine = policy.parse(document, source="machine")
        monkeypatch.setattr(policy, "load", lambda force=False: policy.PolicyState(
            policy=parsed, cloud=True, machine=machine, from_machine=True))
        assert policy.terms_accepted_by() == ("Acme", "machine") and terms.pending() == []

    @pytest.mark.parametrize("value", ["", "   ", 7, True, ["Acme"], "x" * 201])
    def test_a_value_that_isnt_a_name_makes_the_policy_invalid(self, value):
        with pytest.raises(policy.PolicyError, match="accepted_by_organization"):
            policy.parse({"schema": policy.SCHEMA, "legal": {"accepted_by_organization": value}}, source="t")
        with pytest.raises(policy.PolicyError, match="legal must be an object"):
            policy.parse({"schema": policy.SCHEMA, "legal": "yes"}, source="t")

    def test_lumi_accept_terms_counts_outside_the_app(self, pending, monkeypatch):
        monkeypatch.setenv(terms.ENVIRONMENT, terms.acceptance_value())
        assert terms.pending() == [] and not terms.record_path().exists()
        # A value naming older terms, or nothing Lumi reads, doesn't.
        monkeypatch.setenv(terms.ENVIRONMENT, "eula-0.9,alpha-terms-0.9")
        assert terms.pending()
        monkeypatch.setenv(terms.ENVIRONMENT, "I agree")
        assert terms.pending()
        # The app always asks its person, or relies on the machine policy.
        monkeypatch.setenv(terms.ENVIRONMENT, terms.acceptance_value())
        terms.mark_app_process()
        assert terms.pending()


class TestEveryPath:
    """While the terms aren't accepted, no entry point reaches a model: the oversight gate asks them first."""

    def test_session_run_refuses_every_surface(self, pending, tmp_path):
        for key, trigger, unattended in (("conv-1", "", False), ("headless:r1", "", True), ("tui:t1", "", False),
                                         ("chat-task:c1", "", False), ("gateway:42", "", False),
                                         ("", "plan", False), ("", "mission", False), ("", "team", False)):
            session = _session(tmp_path, NeverCalled())
            session.audit_session_id, session.oversight_trigger = key, trigger
            session.oversight_unattended = unattended
            [refusal] = _refused(list(session.run("hello")))
            assert "terms" in refusal["message"], key
        assert "lumi terms accept" in oversight.refusal("terminal")
        assert "--accept-terms" in oversight.refusal("headless") and "--accept-terms" in oversight.refusal("schedule")
        assert oversight.gate("mission") == (terms.refusal("app"), REFUSED)

    def test_the_gate_checks_before_every_model_request(self, pending, monkeypatch, tmp_path):
        # Accepted when the turn starts, then no longer (say, the organization's acceptance was withdrawn):
        # the turn stops before its model request.
        backend = StreamingBackend(events=[text_delta("First."), done()])
        session = _session(tmp_path, backend)
        asked = []

        def pending_after_the_first_check(version=None, use_environment=True):
            asked.append(version)
            return [] if len(asked) == 1 else terms.required()

        monkeypatch.setattr(terms, "pending", pending_after_the_first_check)
        events = list(session.run("hello"))
        assert len(asked) >= 2 and _refused(events) and backend.stream_calls == []

    def test_the_app_and_its_socket_refuse(self, pending, monkeypatch):
        from lumi.gui import app as gui

        sent = []

        class WS:
            async def send_json(self, payload):
                sent.append(payload)

        state_home().mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(gui.state, "session", _session(Path(state_home()), NeverCalled()))
        asyncio.run(gui._process_chat_message(WS(), {"command": "message", "text": "hello"}))
        assert sent[0]["event"] == "terms_status" and sent[0]["data"]["pending"]
        assert sent[1]["code"] == REFUSED
        for command, msg, event in (("message", {"text": "hello"}, "error"),
                                    ("voice_transcribe", {"audio": "", "request_id": "r1"}, "voice.error"),
                                    ("evaluation_start", {}, "error"),
                                    ("schedule_change", {"id": "s1", "action": "run"}, "schedule_error")):
            answer = _command(command, **msg)
            assert answer[0]["event"] == "terms_status" and answer[-1]["event"] == event, command
            assert "terms" in answer[-1]["message"], command
        assert _command("message", text="hello")[-1]["code"] == REFUSED

    def test_plans_missions_team_and_tasks_from_chat_refuse(self, pending, monkeypatch, tmp_path):
        from lumi.engine.swarming.organization import TeamGovernance
        from lumi.engine.swarming.service import policy_refusal
        from lumi.engine.tools import AGENT_TOOLS
        from lumi.gui.autonomous_session import start_autonomous_mission
        from lumi.orchestration import LocalSpecialistRunner, NodeStatus, PlanGraph, PlanNode, new_node_id
        from lumi.orchestration.intent_service import IntentService
        from lumi.remote_tasks import RemoteTasks
        from tests.test_autonomous_session import _SPEC_MD, _StubAppState, _StubProject

        project = tmp_path / "project"
        project.mkdir()
        service = IntentService(project_path=str(project), backend=NeverCalled(), all_tools=list(AGENT_TOOLS))
        for trigger in ("plan", "mission"):
            with pytest.raises(ValueError, match="terms"):
                service.start_intent("Build the counter", trigger=trigger)
        runner = LocalSpecialistRunner(backend=NeverCalled(), project_path=str(project), all_tools=list(AGENT_TOOLS),
                                       oversight_trigger="mission")
        graph = PlanGraph.new("look")
        node = PlanNode(id=new_node_id(), intent_id=graph.intent_id, goal="look")
        graph.add_node(node)
        result = runner(node, graph)
        assert result.status == NodeStatus.BLOCKED and "terms" in result.summary
        with pytest.raises(ValueError, match="terms"):
            start_autonomous_mission(state=_StubAppState(project=_StubProject(str(project))), intent_id="auto-1",
                                     feature="counter", spec_markdown=_SPEC_MD, on_event=lambda event: None)
        assert "terms" in policy_refusal("start", personal=True, team=lambda: "")
        governance = TeamGovernance(None, run_id="run-1", project=str(tmp_path), models=[("ollama", "m")])
        assert "terms" in governance.dispatch_refusal()
        results = []

        class Cloud:
            def device(self):
                return {"id": "dev-1", "how": "joined"}

            def device_call(self, method, path, **kwargs):
                if path.endswith("/result"):
                    results.append(kwargs["json"])
                return {}

        def factory(project, mode, task_id):
            session = _session(tmp_path, NeverCalled())
            session.audit_session_id = f"chat-task:{task_id}"
            return session

        settings = SimpleNamespace(get=lambda section, key=None, default=None: default)
        RemoteTasks(settings, Cloud(), session_factory=factory).run({"id": "t1", "prompt": "hello"})
        assert results[0]["status"] == "failed" and "terms are accepted in the Lumi app" in results[0]["text"]
        # Accepted: the same entry points go on.
        assert terms.accept(_versions(), "app")
        assert policy_refusal("start", personal=True, team=lambda: "") == ""
        assert "terms" not in governance.dispatch_refusal()

    def test_dictation_waits_for_the_terms(self, pending):
        from lumi import voice

        settings = SimpleNamespace(get=lambda section, key=None, default=None: default)
        state = voice.status(settings)
        assert state["browser"] is False and "accept its terms" in state["reason"]
        with pytest.raises(voice.VoiceError, match="terms"):
            voice.transcribe(settings, b"audio", "audio/webm")


class _Terminal(io.StringIO):
    def isatty(self):
        return True


def _lumi_run(monkeypatch, tmp_path, backend, *args, answer=None):
    from lumi import headless
    from tests.test_headless import MODEL

    monkeypatch.setattr(headless, "build_spec", lambda settings, provider, model, project: SimpleNamespace(
        create_backend=lambda settings: backend, permission_mode=""))
    # Whether anyone is at a terminal comes only from the streams this test passes.
    monkeypatch.setattr(headless, "_terminal_attached", lambda *streams: answer is not None)
    out, err = io.StringIO(), (_Terminal() if answer is not None else io.StringIO())
    stdin = _Terminal(answer) if answer is not None else io.StringIO("")
    code = headless.main(["--provider", "anthropic", "--model", MODEL, "--project", str(tmp_path), *args],
                         stdin=stdin, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TestCommandLine:
    def test_lumi_run_says_how_to_accept_and_stops(self, pending, monkeypatch, tmp_path):
        code, out, err = _lumi_run(monkeypatch, tmp_path, NeverCalled(), "Summarize", "--mode", "bypass")
        value = terms.acceptance_value()
        assert code == 2 and out == ""
        assert f"--accept-terms {value}" in err and f"LUMI_ACCEPT_TERMS={value}" in err and "lumi terms show" in err

    def test_the_flag_and_the_variable_accept_and_are_recorded(self, pending, monkeypatch, tmp_path):
        from tests.test_headless import call, scripted

        code, _out, err = _lumi_run(monkeypatch, tmp_path, NeverCalled(), "Summarize", "--accept-terms",
                                    "eula-0.1")
        assert code == 2 and "isn't the version in force" in err and terms.acceptance_value() in err
        code, out, _err = _lumi_run(monkeypatch, tmp_path, scripted(call(0.01, text_delta("Done."))), "Summarize",
                                    "--mode", "bypass", "--accept-terms", terms.acceptance_value())
        assert code == 0 and json.loads(out)["status"]
        assert terms.accepted()["eula"]["surface"] == "flag"
        # A variable naming the terms in force is recorded too, the first time something needs it.
        terms.record_path().unlink()
        monkeypatch.setenv(terms.ENVIRONMENT, terms.acceptance_value())
        code, _out, _err = _lumi_run(monkeypatch, tmp_path, scripted(call(0.01, text_delta("Done."))), "Summarize",
                                     "--mode", "bypass")
        assert code == 0 and terms.accepted()["eula"]["surface"] == "environment"

    def test_someone_at_a_terminal_can_type_yes(self, pending, monkeypatch, tmp_path):
        from tests.test_headless import call, scripted

        code, out, err = _lumi_run(monkeypatch, tmp_path, NeverCalled(), "Summarize", answer="no\n")
        assert code == 3 and "type yes to accept" in err and "nothing was sent" in err and terms.pending()
        code, out, err = _lumi_run(monkeypatch, tmp_path, scripted(call(0.01, text_delta("Done."))), "Summarize",
                                   "--mode", "bypass", answer="yes\n")
        assert code == 0 and "Lumi End User License Agreement, version" in err
        assert terms.accepted()["eula"]["surface"] == "terminal"

    def test_a_scheduled_run_never_asks(self, pending, monkeypatch, tmp_path):
        from lumi import headless
        from tests.test_headless import MODEL

        monkeypatch.setattr(headless, "build_spec", lambda settings, provider, model, project: SimpleNamespace(
            create_backend=lambda settings: NeverCalled(), permission_mode=""))
        err = _Terminal()
        code = headless.main(["--provider", "anthropic", "--model", MODEL, "--project", str(tmp_path), "Summarize"],
                             stdin=_Terminal("yes\n"), stdout=io.StringIO(), stderr=err, trigger="schedule")
        assert code == 2 and "haven't been accepted" in err.getvalue() and terms.pending()

    def test_lumi_terms(self, pending, capsys):
        assert terms.main(["status"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["pending"] is True and status["acceptance_value"] == terms.acceptance_value()
        assert terms.main(["show", "eula"]) == 0
        assert capsys.readouterr().out.startswith("# Lumi End User License Agreement")
        assert terms.main(["show", "alpha-terms"]) == 0 and "Alpha and Beta Test Terms" in capsys.readouterr().out
        assert terms.main(["show", "nothing"]) == 1
        assert terms.main(["accept", "eula-0.1"]) == 1 and terms.pending()
        capsys.readouterr()
        assert terms.main(["accept", terms.acceptance_value()]) == 0
        assert "Accepted the Lumi End User License Agreement" in capsys.readouterr().out
        assert terms.accepted()["eula"]["surface"] == "command" and terms.pending() == []
        assert terms.main(["accept"]) == 2

    def test_the_terminal_ui_asks_for_yes(self, pending, monkeypatch):
        from rich.console import Console

        from tests.test_tui import tui

        monkeypatch.setattr(tui, "console", Console(file=io.StringIO(), width=100, color_system=None))
        monkeypatch.setattr(tui, "pt_prompt", lambda *args, **kwargs: "no")
        assert tui.confirm_terms() is False and terms.pending()
        monkeypatch.setattr(tui, "pt_prompt", lambda *args, **kwargs: "yes")
        assert tui.confirm_terms() is True and terms.accepted()["eula"]["surface"] == "terminal"
        assert tui.confirm_terms() is True  # nothing more to accept

    def test_the_gateway_asks_before_it_starts(self, pending):
        from lumi.gateway.cli import accept_terms

        err = io.StringIO()
        assert accept_terms("", stdin=io.StringIO(""), stderr=err) is False
        assert "--accept-terms" in err.getvalue() and terms.pending()
        err = _Terminal()
        assert accept_terms("", stdin=_Terminal("nope\n"), stderr=err) is False and "not started" in err.getvalue()
        assert accept_terms("", stdin=_Terminal("yes\n"), stderr=_Terminal()) is True
        assert terms.accepted()["eula"]["surface"] == "terminal"


class TestSocket:
    def test_the_dialog_accepts_only_the_terms_it_showed(self, pending):
        [status] = _command("terms_status")
        assert status["data"]["pending"] and [doc["id"] for doc in status["data"]["required"]] == \
            [doc.id for doc in pending]
        stale = _command("terms_accept", documents={doc.id: "0.9" for doc in pending})
        assert stale[-1]["event"] == "terms_status" and stale[-1]["data"]["pending"]
        answer = _command("terms_accept", documents=_versions())
        assert answer[0]["event"] == "terms_status" and answer[0]["data"]["pending"] is False
        assert terms.accepted()["eula"]["surface"] == "app"

    def test_the_texts_are_readable_from_the_app(self):
        [eula] = _command("legal_document", id="eula")
        assert eula["data"]["format"] == "markdown" and eula["data"]["text"].startswith("# Lumi End User License")
        [privacy] = _command("legal_document", id="privacy")
        assert privacy["data"]["version"] == terms.document("privacy").version
        # Running from source there are no third-party notices; an installed copy has them.
        [notices] = _command("legal_document", id="notices")
        assert notices["data"]["error"] and notices["data"]["text"] == ""
        [unknown] = _command("legal_document", id="../../settings.json")
        assert unknown["data"] == {"id": "../../settings.json", "error": "There's no such document."}

    def test_about_says_who_accepted(self, pending, monkeypatch):
        [about] = _command("about_info")
        assert about["data"]["terms"]["pending"] is True
        policy.set_for_tests(policy.parse({"schema": policy.SCHEMA, "organization": "Acme",
                                           "legal": {"accepted_by_organization": "Acme Corp"}}, source="GPO"),
                             machine=True)
        [about] = _command("about_info")
        assert (about["data"]["terms"]["pending"], about["data"]["terms"]["organization"]) == (False, "Acme Corp")
