"""Person 5: LangSmith tracing masks secrets and PII before anything leaves the machine (copilot/tracing.py)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from copilot import tracing

ROOT = Path(__file__).resolve().parent.parent

# A trace-shaped payload: an LLM message, a gated tool call with its approval token, an HTTP header.
TRACE = {
    "messages": [{
        "content": "Checkout errors at 12%; paymentFailure changed 3 minutes ago. Contact jane.doe@example.com",
        "tool_calls": [{"name": "set_flag",
                        "args": {"flag": "paymentFailure", "variant": "off",
                                 "approval_token": "1730900000.9f2c4a1be3d877c0aa21"}}],
    }],
    "headers": {"Authorization": "Bearer abcdefghijklmnop123456"},
    "note": "key sk-abcdefghijklmnopqrstu was rotated",
}


def _anonymize(payload):
    from langsmith.anonymizer import create_anonymizer
    return create_anonymizer(tracing.mask)(payload)


def test_credentials_are_masked_by_key_wherever_they_sit():
    out = _anonymize(TRACE)
    assert out["messages"][0]["tool_calls"][0]["args"]["approval_token"] == "[REDACTED]"
    assert out["headers"]["Authorization"] == "[REDACTED]"


def test_secrets_and_pii_in_free_text_are_redacted_and_the_evidence_is_kept():
    out = _anonymize(TRACE)
    text = out["messages"][0]["content"]
    assert "jane.doe@example.com" not in text and "[REDACTED-EMAIL]" in text
    assert "sk-abcdefghijklmnopqrstu" not in out["note"]
    assert "Checkout errors at 12%" in text and "paymentFailure" in text          # the diagnosis evidence stays
    args = out["messages"][0]["tool_calls"][0]["args"]
    assert args["flag"] == "paymentFailure" and args["variant"] == "off"


def test_metric_values_and_identifiers_stay_readable():
    payload = {
        "content": '[{"service": "checkout", "error_rate": 0.4123456789012345, "latency_p95_ms": 739.1234567890123456}]',
        "values": "[[1793974830.159, 0.4123456789012345]]",
        "id": "chatcmpl-AbC123dEf456GhI789jKl012MnO",
        "run_id": "P5-TRACE-D-001-r1-20261106T141506Z",
        "incident_id": "INC-7f3a9c2e1b4d4e8fa1c2d3e4f5a6b7c8",
    }
    out = _anonymize(payload)
    assert "0.4123456789012345" in out["content"] and "739.1234567890123456" in out["content"]
    assert "0.4123456789012345" in out["values"]
    assert out["id"] == payload["id"]
    assert out["run_id"] == payload["run_id"] and out["incident_id"] == payload["incident_id"]


def test_card_numbers_are_still_masked_next_to_decimals():
    out = tracing.mask("card 4111111111111111 paid 12.50, rate 0.4123456789012345")
    assert "4111111111111111" not in out and "[REDACTED-NUMBER]" in out
    assert "12.50" in out and "0.4123456789012345" in out


def test_tracing_is_off_in_tests_and_setup_does_nothing():
    assert os.environ["LANGSMITH_TRACING"] == "false"          # tests/conftest.py
    assert tracing.enabled() is False
    assert tracing.setup() is False


def test_setup_makes_the_masking_client_the_default_for_every_thread():
    # In a child process, so the process-wide LangSmith client never leaks into other tests. Nothing is sent:
    # creating the client and configuring it make no network calls.
    script = """
import json, threading
from copilot import tracing
assert tracing.setup() is True
from langchain_core.tracers.context import _get_tracer_kwargs
from langchain_core.tracers.langchain import LangChainTracer
seen = {}
def worker():
    tracer = LangChainTracer(**_get_tracer_kwargs())
    seen["inputs"] = tracer.client._hide_run_inputs({"args": {"approval_token": "1730900000.9f2c4a1b"},
                                                     "text": "mail jane.doe@example.com"})
t = threading.Thread(target=worker); t.start(); t.join()
print(json.dumps(seen["inputs"]))
"""
    env = dict(os.environ, LANGSMITH_TRACING="true", LANGSMITH_API_KEY="lsv2-test-not-real",
               LANGSMITH_PROJECT="test-project")
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    inputs = json.loads(done.stdout.strip().splitlines()[-1])
    assert inputs["args"]["approval_token"] == "[REDACTED]"
    assert "jane.doe@example.com" not in inputs["text"]


@pytest.mark.parametrize("path", ["copilot/run.py", "copilot/runtime.py", "copilot/reflexion.py"])
def test_every_entry_point_sets_up_tracing_after_loading_env(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    load = text.index('load_dotenv(ROOT / ".env")')
    assert "tracing.setup()" in text[load:load + 200], path
