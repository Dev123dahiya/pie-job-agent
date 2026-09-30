"""The agent's per-step decision contract."""

import json

import pytest

from agent.decision import DECISION_SCHEMA, DecisionError, parse_decision


def test_valid_decisions() -> None:
    d = parse_decision(
        {
            "summary": "Look up jobs",
            "action": "call_tool",
            "tool": "search_jobs",
            "arguments_json": '{"limit": 5}',
            "message": "",
        }
    )
    assert d.arguments() == {"limit": 5}
    done = parse_decision(
        json.dumps(
            {
                "summary": "Done",
                "action": "complete",
                "tool": "",
                "arguments_json": "{}",
                "message": "3 jobs reviewed",
            }
        )
    )
    assert done.action == "complete"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "JSON object"),
        (
            {
                "summary": "x",
                "action": "call_tool",
                "tool": "",
                "arguments_json": "{}",
                "message": "",
            },
            "needs a tool name",
        ),
        (
            {
                "summary": "x",
                "action": "call_tool",
                "tool": "get_job",
                "arguments_json": "{oops",
                "message": "",
            },
            "arguments_json",
        ),
        (
            {
                "summary": "x",
                "action": "call_tool",
                "tool": "get_job",
                "arguments_json": "[1]",
                "message": "",
            },
            "JSON object",
        ),
        (
            {
                "summary": "x",
                "action": "complete",
                "tool": "",
                "arguments_json": "{}",
                "message": "",
            },
            "needs a message",
        ),
        (
            {
                "summary": "x",
                "action": "run_shell",
                "tool": "",
                "arguments_json": "{}",
                "message": "m",
            },
            "action",
        ),
        (
            {
                "summary": "x",
                "action": "complete",
                "tool": "",
                "arguments_json": "{}",
                "message": "m",
                "thoughts": "...",
            },
            "thoughts",
        ),
        (
            {
                "summary": "",
                "action": "complete",
                "tool": "",
                "arguments_json": "{}",
                "message": "m",
            },
            "summary",
        ),
    ],
)
def test_malformed_decisions_rejected(payload: object, message: str) -> None:
    with pytest.raises(DecisionError, match=message):
        parse_decision(payload)  # type: ignore[arg-type]


def test_schema_offers_no_free_form_execution() -> None:
    assert DECISION_SCHEMA["properties"]["action"]["enum"] == [
        "call_tool",
        "complete",
        "need_user_input",
    ]
    assert DECISION_SCHEMA["additionalProperties"] is False
