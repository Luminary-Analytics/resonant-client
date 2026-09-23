from resonant_client.engine.repair_progress import RepairProgress


def edit(progress, before, after, path="tests.js", error=False):
    return progress.observe("file_edit", {"path": path, "old_text": before, "new_text": after}, is_error=error)


def test_repeated_successful_reversals_trigger_once_not_on_first_revert():
    p = RepairProgress()
    assert not edit(p, "A", "B")
    assert not edit(p, "B", "A")
    assert not edit(p, "A", "B")
    assert edit(p, "B", "A") == "tests.js"
    assert not edit(p, "A", "B")
    assert not edit(p, "B", "A")


def test_progress_failed_edits_other_files_and_reads_do_not_trigger():
    p = RepairProgress()
    for i in range(20):
        assert not edit(p, str(i), str(i + 1))
        assert not edit(p, "A", "B", path=f"file-{i}.js")
        assert not edit(p, "B", "A", error=True)
        assert not p.observe("file_read", {"path": "tests.js"}, is_error=False)


def test_new_turn_tracker_does_not_inherit_previous_reversals():
    p = RepairProgress()
    for before, after in [("A", "B"), ("B", "A"), ("A", "B")]:
        assert not edit(p, before, after)
    assert not edit(RepairProgress(), "B", "A")
