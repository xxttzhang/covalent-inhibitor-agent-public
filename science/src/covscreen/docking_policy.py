"""Project method policy. Historical reads remain independent of launch policy."""
import json
from pathlib import Path

POLICY = json.loads(Path(__file__).with_name('docking-engine-policy.json').read_text())


def require_engine_enabled(engine, *, route=None):
    if engine in POLICY.get('enabled_engines_by_route', {}).get(route, []):
        return
    if engine in POLICY['disabled_engines']:
        raise ValueError('docking_engine_disabled: ' + POLICY['reason'])
