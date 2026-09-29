"""Lumi's terms (lumi/terms.py): what's in force for a build, accepting them, the machine policy that accepts
them for an organization, LUMI_ACCEPT_TERMS, and the gate on every turn path, which rides on organization
oversight's (lumi/oversight.py)."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import shutil
import time
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
        data["documents"]["eula"]["published"] = "2026-01-01"
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


# ── The PR #104 review: what it found, as tests of the fixed behavior ──────────


def _warmed_backend():
    """A select_backend state whose backend records its warm-ups."""
    warmed = []
    backend = SimpleNamespace(name="ollama", model="stub-model", warm_up=lambda: warmed.append(True))
    state = SimpleNamespace(project=SimpleNamespace(current_session=None), backend=None, _first_message_sent=True)

    def create_backend(kind, model, **kwargs):
        state.backend = backend

    state.create_backend = create_backend
    state.get_init_data = lambda refresh_only=False: {"event": "init"}
    return state, warmed


class TestModelRequestsWaitForTheTerms:
    """Warm-ups sent a model request ("hi", or an EXO tool call) before the terms were accepted. Now nothing
    underneath a turn path can: dlp.guarded, dlp.check_request, Lumi's own HTTP requests and auxiliary requests
    ask the terms too (tests/test_dlp.py lists every use with its gate)."""

    def test_a_guarded_request_waits_even_under_the_permit(self, pending):
        from lumi import dlp
        from tests.test_dlp import ARGUMENTS
        from tests.test_dlp import Backend as GuardedBackend

        backend = GuardedBackend()
        for call in (lambda: backend.classify("hi"), lambda: dlp.send(backend.stream, **ARGUMENTS)):
            with dlp.permit(), pytest.raises(dlp.Blocked) as refused:  # fixed text, such as a warm-up
                call()
            assert refused.value.code == REFUSED and "terms" in refused.value.message
        with pytest.raises(dlp.Blocked) as refused:
            dlp.check_request(dict(ARGUMENTS), purpose="title")
        assert refused.value.code == REFUSED
        # A feedback report isn't a model request: the terms don't hold its DLP check back.
        assert dlp.check_request({"user_msg": "It crashed"}, purpose="feedback").request["user_msg"] == "It crashed"
        assert backend.requests == []
        assert terms.accept(_versions(), "app")
        with dlp.permit():
            assert backend.classify("hi") == "SIMPLE"
        assert list(dlp.send(backend.stream, **ARGUMENTS))

    def test_ollamas_own_http_requests_wait(self, pending, monkeypatch):
        from lumi import backends, dlp

        posted = []

        class Answer:
            status_code = 200

            def __init__(self, url):
                self.url = url

            def json(self):  # /api/show that says nothing about tools, so detection would probe
                return {"template": "", "capabilities": []}

        def post(url, **kwargs):
            posted.append(url)
            return Answer(url)

        monkeypatch.setattr(backends.httpx, "post", post)
        ollama = backends.OllamaBackend("http://127.0.0.1:9", "unknown-model:7b")
        ollama.warm_up()
        assert posted == []
        with pytest.raises(dlp.Blocked) as refused:
            ollama._detect_tool_support()
        assert refused.value.code == REFUSED and posted == ["http://127.0.0.1:9/api/show"]  # metadata, no probe
        assert terms.accept(_versions(), "app")
        ollama.warm_up()
        assert posted[-1] == "http://127.0.0.1:9/api/chat"

    def test_exo_places_no_model_and_sends_nothing_before_the_terms(self, pending, monkeypatch):
        from lumi import backends

        exo = backends.ExoBackend.__new__(backends.ExoBackend)
        placed = []
        exo._ensure_instance = lambda event: placed.append(event)
        exo.warm_up()
        assert placed == []

    def test_auxiliary_requests_answer_with_the_refusal(self, pending):
        from lumi.engine.request_purpose import auxiliary_stream
        from tests.test_oversight import NeverCalled as Unreachable

        events = list(auxiliary_stream(Unreachable(), "title", user_msg="hello", conversation_history=[],
                                       instructions="", tools=[]))
        assert events == [("error", {"message": terms.refusal("request"), "code": REFUSED})]

    def test_choosing_a_model_warms_it_only_once_the_terms_are_accepted(self, pending):
        state, warmed = _warmed_backend()
        answer = _command("select_backend", state, backend="ollama", model="stub-model")
        assert [event.get("event") for event in answer] == ["init", "status_msg"] and warmed == []
        assert terms.accept(_versions(), "app")
        answer = _command("select_backend", state, backend="ollama", model="stub-model")
        assert "model_warmup_started" in [event.get("event") for event in answer]
        for _ in range(100):  # the warm-up runs in its own thread
            if warmed:
                break
            time.sleep(0.02)
        assert warmed == [True]

    def test_the_terminal_ui_asks_before_it_reaches_a_model(self, pending, monkeypatch):
        """The review's probe: the TUI warmed the model up, then asked for the terms and said nothing was sent."""
        from rich.console import Console

        from tests.fixtures import ollama_recorder
        from tests.test_tui import tui

        recorder = ollama_recorder.Recorder()
        server, url = ollama_recorder.start(recorder)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.setattr(tui, "console", Console(file=io.StringIO(), width=120, color_system=None))
        monkeypatch.setattr(tui, "_history_path", lambda: Path(state_home()) / "tui_history")
        answers = ["no"]

        def prompt(message, **kwargs):
            if "Type yes to accept" in str(getattr(message, "value", message)):
                return answers.pop(0)
            raise EOFError  # the first message prompt: Ctrl+D

        monkeypatch.setattr(tui, "pt_prompt", prompt)
        accept = terms.accept

        def accepting(*args, **kwargs):
            recorder.mark("terms accepted")
            return accept(*args, **kwargs)

        monkeypatch.setattr(terms, "accept", accepting)
        try:
            tui.main(["--ollama-url", url, "--model", ollama_recorder.MODEL])
            assert recorder.requests == [] and terms.pending()  # declined: not even a model list
            answers.append("yes")
            tui.main(["--ollama-url", url, "--model", ollama_recorder.MODEL])
        finally:
            server.shutdown()
        paths = [item["path"] for item in recorder.requests]
        assert paths[0] == "terms accepted" and "/api/chat" in paths  # the warm-up, after the acceptance
        assert terms.pending() == []


class TestAcceptanceIsTiedToTheText:
    def test_a_changed_text_asks_again_even_with_the_same_version(self, pending, monkeypatch, tmp_path):
        """The review's probe: the record kept the text's SHA-256, but nothing compared it."""
        assert terms.accept(_versions(), "app") and terms.pending() == []
        accepted_hash = terms.accepted()["eula"]["sha256"]
        legal = tmp_path / "legal"
        shutil.copytree(terms.legal_dir(), legal)
        eula = legal / "EULA.md"
        eula.write_text(eula.read_text(encoding="utf-8").replace("Luminary Analytics, LLC",
                                                                  "Luminary Holdings, Inc."), encoding="utf-8")
        monkeypatch.setattr(terms, "legal_dir", lambda: legal)
        assert terms.text_sha256("eula") != accepted_hash
        assert [doc.id for doc in terms.pending()] == ["eula"]
        status = terms.status()
        eula_status = status["required"][0]
        assert status["pending"] and eula_status["changed"] and not eula_status["accepted"]
        # Accepting the text now shown records its hash, and nothing waits any more.
        assert terms.accept({"eula": terms.document("eula").version}, "app")
        assert terms.accepted()["eula"]["sha256"] == terms.text_sha256("eula") and terms.pending() == []

    def test_a_record_without_the_texts_hash_counts_for_nothing(self, pending):
        record = {"kind": terms.RECORD_KIND, "people": {oversight.os_user(): {
            doc.id: {"version": doc.version, "accepted_at": "2026-09-29T12:00:00Z"} for doc in pending}}}
        terms.record_path().parent.mkdir(parents=True, exist_ok=True)
        terms.record_path().write_text(json.dumps(record), encoding="utf-8")
        assert [doc.id for doc in terms.pending()] == [doc.id for doc in pending]


class TestOnlyHardenedMachineSourcesAccept:
    """``legal.accepted_by_organization`` counts only from sources only an administrator can write (the machine
    policy PR #109 hardened): the HKLM Group Policy key or the file its PolicyFile names, a configuration
    profile, or the machine policy file where only administrators can change it."""

    DOCUMENT = {"schema": policy.SCHEMA, "organization": "Acme", "legal": {"accepted_by_organization": "Acme"}}

    @pytest.fixture
    def machine(self, tmp_path, monkeypatch):
        root = tmp_path / "ProgramData"
        (root / "Lumi").mkdir(parents=True)
        monkeypatch.setattr(policy, "machine_policy_file", lambda: root / "Lumi" / "policy.json")
        monkeypatch.setattr(policy, "_program_data", lambda: str(root))
        monkeypatch.setattr(policy, "_registry_values", lambda: {})
        monkeypatch.setattr(policy, "MAC_MANAGED_PREFERENCES", tmp_path / "no-managed-preferences")
        monkeypatch.delenv("LUMI_POLICY_FILE", raising=False)
        yield root / "Lumi"
        policy.set_for_tests(None)
        from lumi import admin_files

        admin_files.set_for_tests(None)

    def test_a_policy_file_a_person_planted_under_programdata_accepts_nothing(self, pending, machine):
        """The review's probe: any standard user can create C:\\ProgramData\\Lumi and a policy.json in it."""
        from lumi import admin_files

        (machine / "policy.json").write_text(json.dumps({**self.DOCUMENT, "organization": "Anyone"}),
                                             encoding="utf-8")
        admin_files.set_for_tests(lambda path, root: admin_files.Trust(
            False, f"{path} is owned by DESKTOP\\ana, not by Administrators, SYSTEM or TrustedInstaller"))
        state = policy.load(force=True)
        assert state.policy is None and not state.from_machine and [item.kind for item in state.ignored] == ["policy"]
        assert policy.terms_accepted_by() == ("", "") and terms.pending() and not terms.record_path().exists()
        # The same file, only an administrator's, accepts.
        admin_files.set_for_tests(lambda path, root: admin_files.Trust(True, admin_owned=True))
        policy.load(force=True)
        assert policy.terms_accepted_by()[0] == "Acme" and terms.pending() == []

    def test_the_machine_file_counts_even_when_lumi_policy_file_names_it_too(self, pending, machine, monkeypatch):
        """The review's probe: the path comparison made the machine file stop counting once LUMI_POLICY_FILE
        named it. Which source found the policy decides now."""
        (machine / "policy.json").write_text(json.dumps(self.DOCUMENT), encoding="utf-8")
        monkeypatch.setenv("LUMI_POLICY_FILE", str(machine / "policy.json"))
        state = policy.load(force=True)
        assert state.source == str(machine / "policy.json") and state.from_machine
        assert policy.terms_accepted_by() == ("Acme", str(machine / "policy.json")) and terms.pending() == []
        # The same document where only LUMI_POLICY_FILE finds it doesn't.
        mine = machine.parent.parent / "mine.json"
        mine.write_text(json.dumps(self.DOCUMENT), encoding="utf-8")
        (machine / "policy.json").unlink()
        monkeypatch.setenv("LUMI_POLICY_FILE", str(mine))
        state = policy.load(force=True)
        assert state.policy.terms_accepted_by == "Acme" and not state.from_machine
        assert policy.terms_accepted_by() == ("", "") and terms.pending()

    def test_group_policy_in_hklm_accepts_and_an_unreadable_policy_file_accepts_nothing(self, pending, machine,
                                                                                        monkeypatch, tmp_path):
        monkeypatch.setattr(policy, "_registry_values", lambda: {"Policy": json.dumps(self.DOCUMENT)})
        state = policy.load(force=True)
        assert state.from_machine and policy.terms_accepted_by()[0] == "Acme" and terms.pending() == []
        named = tmp_path / "share" / "lumi-policy.json"
        named.parent.mkdir()
        named.write_text(json.dumps(self.DOCUMENT), encoding="utf-8")
        monkeypatch.setattr(policy, "_registry_values", lambda: {"PolicyFile": str(named)})
        policy.load(force=True)
        assert policy.terms_accepted_by() == ("Acme", f"{named} (set by Group Policy)")
        # A PolicyFile Lumi can't read fails closed (PR #109): no policy, so no acceptance, and no requests.
        missing = tmp_path / "share" / "missing.json"
        monkeypatch.setattr(policy, "_registry_values", lambda: {"PolicyFile": str(missing)})
        monkeypatch.setenv("LUMI_POLICY_FILE", str(named))  # never stands in for it
        state = policy.load(force=True)
        assert state.policy is None and "couldn't be read" in state.error and policy.blocked_reason()
        assert policy.terms_accepted_by() == ("", "") and terms.pending()

    def test_a_planted_key_file_and_a_self_signed_policy_neither_replace_group_policy_nor_accept(
            self, pending, machine, monkeypatch):
        """The review's second probe (pre-existing, fixed by PR #109): a person's policy-keys.json under
        ProgramData let their self-signed Lumi Cloud policy replace Group Policy."""
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        from lumi import admin_files

        gpo = {"schema": policy.SCHEMA, "organization": "Acme", "permissions": {"allowed_modes": ["ask"]},
               "cloud": {"url": "https://cloud.example"}}
        monkeypatch.setattr(policy, "_registry_values", lambda: {"Policy": json.dumps(gpo)})
        admin_files.set_for_tests(lambda path, root: admin_files.Trust(
            False, f"{path} is owned by DESKTOP\\ana, not by Administrators, SYSTEM or TrustedInstaller"))
        private = Ed25519PrivateKey.generate()
        public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        (machine / "policy-keys.json").write_text(json.dumps({"mine": public}), encoding="utf-8")
        mine = {"schema": policy.SCHEMA, "organization": "Acme",
                "permissions": {"allowed_modes": ["ask", "auto-edit", "plan", "bypass"]},
                "legal": {"accepted_by_organization": "Acme"}, "expires_at": "2099-01-01T00:00:00Z"}
        signature = base64.b64encode(private.sign(policy.canonical(mine))).decode()
        policy.cloud_policy_path().parent.mkdir(parents=True, exist_ok=True)
        policy.cloud_policy_path().write_text(json.dumps({"policy": mine, "signature": signature, "key_id": "mine"}),
                                              encoding="utf-8")
        state = policy.load(force=True)
        assert not state.cloud and not state.policy.mode_allowed("bypass")
        assert policy.terms_accepted_by() == ("", "") and terms.pending()


class TestNothingWaitsInTheWrongPlace:
    def test_tasks_from_chat_wait_in_lumi_cloud_until_the_terms_are_accepted(self, pending, tmp_path):
        """The review's probe: each queued chat request was claimed, refused and reported failed."""
        from lumi.remote_tasks import IDLE_SECONDS, RemoteTasks

        class Settings:
            def get(self, section, key=None, default=None):
                data = {"cloud": {"remote_tasks": True, "remote_tasks_project": str(tmp_path),
                                  "remote_tasks_mode": "ask"}}
                value = data.get(section, default)
                return value if key is None else (value or {}).get(key, default)

        class Cloud:
            def __init__(self):
                self.calls = []
                self.queue = [{"id": "t1", "prompt": "fix the bug"}, {"id": "t2", "prompt": "and this"}]

            def device(self):
                return {"id": "dev", "how": "personal"}

            def device_call(self, method, path, json=None):
                self.calls.append((method, path, json))
                if path.endswith("/claim"):
                    return self.queue.pop(0) if self.queue else {}
                return {}

        def factory(project, mode, task_id):
            session = _session(tmp_path, StreamingBackend(events=[text_delta("Fixed."), done()]))
            session.audit_session_id = f"chat-task:{task_id}"
            return session

        cloud = Cloud()
        tasks = RemoteTasks(Settings(), cloud, session_factory=factory, sleep=lambda s: None)
        assert tasks.blocked() == terms.refusal("chat_task") and "Lumi app" in tasks.blocked()
        assert tasks.status()["blocked"] == tasks.blocked()
        assert tasks.step() == IDLE_SECONDS and tasks.step() == IDLE_SECONDS
        assert cloud.calls == [] and len(cloud.queue) == 2  # nothing claimed: both wait
        assert terms.accept(_versions(), "app")
        assert tasks.blocked() == ""
        tasks.step()
        assert [path for _method, path, _body in cloud.calls][:1] == ["/api/v1/devices/tasks/claim"]
        assert len(cloud.queue) == 1

    def test_engram_commands_ask_the_gate(self, pending):
        remembered = []
        engram = SimpleNamespace(enabled=True, recall=lambda query: remembered.append(("recall", query)) or [],
                                 remember=lambda text: remembered.append(("remember", text)))
        state = SimpleNamespace(engram=engram)
        for command, field in (("engram_recall", {"query": "auth"}), ("engram_remember", {"text": "note"})):
            answer = _command(command, state, **field)
            assert answer[0]["event"] == "terms_status" and answer[-1]["code"] == REFUSED, command
        assert remembered == []
        assert terms.accept(_versions(), "app")
        assert _command("engram_recall", state, query="auth") == [{"event": "engram_recall", "memories": []}]
        assert remembered == [("recall", "auth")]

    def test_a_refused_message_says_no_turn_started(self, pending, monkeypatch):
        """The page ends its running state and gives the text back only for ``refused`` (PR #105's contract)."""
        from lumi.gui import app as gui

        answer = _command("message", text="hello", message_id="m1")
        assert answer[-1] == {"event": "error", "message": terms.refusal("app"), "code": REFUSED, "refused": True,
                              "message_id": "m1"}
        sent = []

        class WS:
            async def send_json(self, payload):
                sent.append(payload)

        state_home().mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(gui.state, "session", _session(Path(state_home()), NeverCalled()))
        asyncio.run(gui._process_chat_message(WS(), {"command": "message", "text": "hello", "message_id": "q2"}))
        assert sent[-1]["refused"] is True and sent[-1]["message_id"] == "q2" and sent[-1]["code"] == REFUSED

    def test_a_policy_refusal_is_said_before_the_message_is_saved(self, pending, monkeypatch):
        """The PR #109 author saw the app stay "running" after a policy refusal: the turn started, and the
        engine's refusal came without a session.end. The app now asks the policy first."""
        from lumi.gui import app as gui

        assert terms.accept(_versions(), "app")
        policy.set_for_tests(None, error="The organization policy at GPO is invalid: a mistake")
        sent = []

        class WS:
            async def send_json(self, payload):
                sent.append(payload)

        state_home().mkdir(parents=True, exist_ok=True)
        session = _session(Path(state_home()), NeverCalled())
        monkeypatch.setattr(gui.state, "session", session)
        asyncio.run(gui._process_chat_message(WS(), {"command": "message", "text": "hello"}))
        assert [event["event"] for event in sent] == ["error"]  # nothing saved, titled or listed
        assert sent[0]["refused"] is True and sent[0]["code"] == "policy_blocked"
        assert sent[0]["message"] == session.policy_refusal() != ""
        # The engine says so too, with the code the page ends its running state on.
        [refusal] = [event for event in session.run("hello") if event.get("event") == "error"]
        assert refusal["code"] == "policy_blocked"

    def test_accepting_in_one_window_unlocks_the_others(self, pending):
        from lumi.gui import ws_commands

        heard = {"this": [], "other": []}

        class WS:
            def __init__(self, name):
                self.name = name

            async def send_json(self, payload):
                heard[self.name].append(payload)

        this, other = WS("this"), WS("other")
        state = SimpleNamespace(_navigation_viewers={this, other}, settings=None)
        ctx = ws_commands.CommandContext(ws=this, state=state, msg={"command": "terms_accept",
                                                                     "documents": _versions()})
        asyncio.run(ws_commands.HANDLERS["terms_accept"](ctx))
        assert [event["event"] for event in heard["this"]] == ["terms_status"]
        assert [event["event"] for event in heard["other"]] == ["terms_status"]
        assert heard["other"][0]["data"]["pending"] is False
        # A refused acceptance (older terms) tells only its own window.
        heard["other"].clear()
        terms.record_path().unlink()
        stale = ws_commands.CommandContext(ws=this, state=state, msg={"command": "terms_accept",
                                                                       "documents": {"eula": "0.9"}})
        asyncio.run(ws_commands.HANDLERS["terms_accept"](stale))
        assert heard["other"] == []

    def test_about_on_a_stable_build_offers_no_test_terms(self, pending):
        stable = terms.status("0.20.0")
        assert stable["prerelease"] is False and stable["readable"] == ["eula", "privacy"]
        assert terms.status("0.21.0-beta.1")["readable"] == ["eula", "alpha_terms", "privacy"]
