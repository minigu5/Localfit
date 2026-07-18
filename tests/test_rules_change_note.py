from omm import rules as rules_mod


def test_refresh_rules_with_change_note_flags_new_data_as_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(rules_mod, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(rules_mod, "fetch_rules", lambda url: [{"name": "a"}])

    fetched, changed = rules_mod.refresh_rules_with_change_note("http://example.com/rules.json")

    assert changed is True
    assert fetched == [{"name": "a"}]


def test_refresh_rules_with_change_note_flags_identical_refetch_as_unchanged(monkeypatch, tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text('[{"name": "a"}]')
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)
    monkeypatch.setattr(rules_mod, "fetch_rules", lambda url: [{"name": "a"}])

    fetched, changed = rules_mod.refresh_rules_with_change_note("http://example.com/rules.json")

    assert changed is False
    assert fetched == [{"name": "a"}]
