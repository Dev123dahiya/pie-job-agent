"""Settings saves change only the edited keys and keep the file's comments."""

import pytest
import yaml

from backend.core.config import REPO_ROOT
from backend.core.yaml_edit import YamlEditError, set_value

DOC = """# Top comment
search:
  # terms comment
  terms:
    - A
    - B
  # aliases comment
  term_aliases:
    A: [AA]
  locations:
  - Gurugram
  - Remote

daily_limits:
  total: 20   # inline
  naukri: 15

# trailing section
preferences:
  verified_answers: {}
  excluded_companies: []
"""


def test_replaces_a_list_and_keeps_every_comment() -> None:
    out = set_value(DOC, ["search", "terms"], ["Python Developer", "Backend Developer"])
    assert yaml.safe_load(out)["search"]["terms"] == ["Python Developer", "Backend Developer"]
    for comment in ("# Top comment", "# aliases comment", "# trailing section"):
        assert comment in out
    assert yaml.safe_load(out)["search"]["term_aliases"] == {"A": ["AA"]}


def test_list_items_at_the_keys_own_indent() -> None:
    out = set_value(DOC, ["search", "locations"], ["Noida"])
    data = yaml.safe_load(out)
    assert data["search"]["locations"] == ["Noida"] and data["daily_limits"]["total"] == 20


def test_scalars_and_new_keys() -> None:
    out = set_value(DOC, ["daily_limits", "naukri"], 12)
    assert yaml.safe_load(out)["daily_limits"] == {"total": 20, "naukri": 12}
    assert "# inline" in out  # the untouched line keeps its comment
    out = set_value(out, ["daily_limits", "linkedin"], 4)
    assert yaml.safe_load(out)["daily_limits"]["linkedin"] == 4
    out = set_value(out, ["preferences", "verified_answers", "notice_period"], "Immediate")
    assert yaml.safe_load(out)["preferences"]["verified_answers"] == {"notice_period": "Immediate"}
    out = set_value(out, ["min_match_score"], 70)
    assert yaml.safe_load(out)["min_match_score"] == 70


def test_values_that_look_special_stay_strings() -> None:
    out = set_value(DOC, ["preferences", "excluded_companies"], ["Yes", "No: really", "#1 Corp"])
    assert yaml.safe_load(out)["preferences"]["excluded_companies"] == [
        "Yes",
        "No: really",
        "#1 Corp",
    ]


def test_broken_yaml_is_refused() -> None:
    with pytest.raises(YamlEditError):
        set_value("a: [1, 2", ["a"], 1)


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [
        ("job_sources.yaml", ["search", "terms"], ["Python Developer"]),
        ("job_sources.yaml", ["search", "locations"], ["Noida", "Remote"]),
        ("automation.yaml", ["daily_limits", "naukri"], 12),
        ("shortlist_rules.yaml", ["min_match_score"], 75),
        ("user_profile.example.yaml", ["preferences", "excluded_companies"], ["X"]),
        ("user_profile.example.yaml", ["verified_profile", "verified_answers", "current_ctc"], "0"),
    ],
)
def test_the_shipped_config_files_can_be_edited(name: str, path: list[str], value: object) -> None:
    text = (REPO_ROOT / "config" / name).read_text(encoding="utf-8")
    out = set_value(text, path, value)
    before, after = yaml.safe_load(text), yaml.safe_load(out)
    node = after
    for key in path:
        node = node[key]
    assert node == value
    # Nothing else changed.
    assert {k: v for k, v in after.items() if k != path[0]} == {
        k: v for k, v in before.items() if k != path[0]
    }
    assert out.count("#") >= text.count("#") - 3  # comments kept (bar the replaced block's)
