"""The migration inventory must describe the real registry without changing it."""
from copy import deepcopy

import pytest

from covscreen.mcp.catalog import catalog
from covscreen.mcp.registry import TOOLS, groups, handlers, tool_catalog


def test_catalog_covers_real_discovery_and_dispatch_without_invoking_handlers(tmp_path):
    before = deepcopy(TOOLS)
    result = tool_catalog()
    assert {row['name'] for row in result['tools']} == set(handlers(tmp_path, {}))
    assert len(result['tools']) == len(TOOLS)
    assert TOOLS == before
    assert not result['visibility_enforced']
    assert not result['scientific_readiness_verified']
    assert list(tmp_path.iterdir()) == []
    values = {row['name']: row for row in result['tools']}
    assert values['covalent_ligand_workflow']['disposition'] == 'research_entry'
    assert values['covalent_prepare_execution']['disposition'] == 'internal_execution'
    assert values['covalent_ligand_first_reuse_result']['disposition'] == 'compatibility'
    assert values['covalent_plan_attachments']['routes'] == ['ligand-first', 'de-novo']


def test_unclassified_added_or_removed_tool_is_not_silently_published():
    extra = deepcopy(TOOLS[0])
    extra['name'] = 'covalent_unreviewed_tool'
    with pytest.raises(ValueError, match='unclassified'):
        catalog([*TOOLS, extra], groups())
    with pytest.raises(ValueError, match='removed'):
        catalog(TOOLS[1:], groups())


def test_duplicate_or_missing_ownership_cannot_create_ambiguous_catalog():
    owners = deepcopy(groups())
    owners[1]['tools'].append(owners[0]['tools'][0])
    with pytest.raises(ValueError, match='Duplicate tool owner'):
        catalog(TOOLS, owners)
    with pytest.raises(ValueError, match='ownership differs'):
        catalog(TOOLS, groups()[1:])


def test_schema_change_is_visible_without_changing_registry_or_dispatch():
    before = tool_catalog()
    changed = deepcopy(TOOLS)
    changed[0]['inputSchema']['description'] = '新增参数说明'
    after = catalog(changed, groups())
    assert before['tools'][0]['input_schema_sha256'] != after['tools'][0]['input_schema_sha256']
    assert before['input_schema_bytes'] != after['input_schema_bytes']
    assert before == tool_catalog()
    profiles = {row['route']: row for row in before['workflow_interfaces']}
    assert set(profiles) == {'library-first', 'ligand-first'}
    names = {row['name'] for row in before['tools']}
    for profile in profiles.values():
        assert profile['entry'] in profile['tools']
        assert set(profile['tools']) <= names
