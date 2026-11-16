"""A stand-in for the model in tests: no network, no cost."""

import json


class FakeModel:
    """Picks the first option offered, with a reason that has no numbers. Counts its calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *, tier, system, prompt, schema):
        self.calls += 1
        first = schema["properties"]["option"]["enum"][0]
        return json.dumps({"option": first, "reason": "It stays on all night."}), 1000, 100
