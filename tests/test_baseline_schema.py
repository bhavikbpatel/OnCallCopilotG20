"""C1's structured response is a JSON schema OpenAI accepts (no "oneOf") and still parses every allow-listed action.

Live E1 rehearsal, 5 Nov: all five C1 runs failed before diagnosing with "Invalid schema for response_format
'SingleAgentResult': ... 'oneOf' is not permitted" (the discriminated Action union)."""
import json

import pytest

from copilot.agents.baselines import SingleAgentResult
from copilot.agents.diagnosis import HypothesisResult
from copilot.state import RestartService, Rollback, ScaleService, SetFlag


@pytest.mark.parametrize("model", [SingleAgentResult, HypothesisResult])
def test_structured_responses_have_no_oneof(model):
    assert "oneOf" not in json.dumps(model.model_json_schema())


@pytest.mark.parametrize("action, cls", [
    ({"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}, SetFlag),
    ({"tool": "restart_service", "service": "email"}, RestartService),
    ({"tool": "scale_service", "service": "ad", "replicas": 2}, ScaleService),
    ({"tool": "rollback", "service": "cart", "to_version": "1.0"}, Rollback),
])
def test_every_action_still_parses_to_its_model(action, cls):
    r = SingleAgentResult.model_validate({"service": "payment", "fault_type": "paymentFailure", "action": action})
    assert isinstance(r.action, cls)
    assert SingleAgentResult.model_validate({}).action is None
