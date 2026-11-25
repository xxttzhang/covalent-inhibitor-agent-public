"""Prepare a bounded fresh Library-first request from verified frozen sites."""
import hashlib
import json
from typing import Literal
from pathlib import Path

from pydantic import Field, model_validator
import yaml

from .library_site_handoff import LibrarySitePlanInput, verified_selection
from .jobs import JobSubmissionRequest, ResourceBudget, sha256_file
from .layout import library_root


class PilotInput(LibrarySitePlanInput):
    canonical_position: int = Field(ge=1)
    record_selection: Literal["file_order_prefix", "reservoir"] = "reservoir"
    max_rows: int = Field(default=1000, ge=10, le=10000)
    top_n: int = Field(default=10, ge=1, le=100)

    @model_validator(mode='after')
    def bounded(self):
        if self.top_n > self.max_rows:
            raise ValueError('top_n exceeds source row limit')
        return self


def prepare_pilot(root: Path, arguments: dict) -> dict:
    args = PilotInput.model_validate(arguments)
    root = root.resolve()
    binding = args.model_dump(include={'site_selection_artifact_id', 'site_selection_manifest_sha256'})
    _, selection, parent = verified_selection(root, binding)
    site = next((s for s in selection['selected_sites'] if s['canonical_position']==args.canonical_position), None)
    if site is None:
        raise ValueError('Pilot site must be frozen in the supplied selection')
    # Chemistry/reference policies must be curated assets, never model-invented
    # SMILES or a claim that another site's pocket is the C797 pocket.
    templates=[]
    for path in sorted((library_root(Path(__file__).resolve().parents[2])/'covindb-configs').glob('*.yaml')):
        value=yaml.safe_load(path.read_text())
        if value.get('target_id')==selection['target']['gene'] and value.get('residue_number')==args.canonical_position:
            templates.append((path,value))
    if len(templates)!=1:
        return {'status':'BLOCKED','reason':'A unique curated library chemistry/reference policy is required for this site', 'calculation_submitted':False}
    template,policy=templates[0]
    from .execution import JobManager
    manager=JobManager(root)
    source=Path(manager.config.get('covindb',{}).get('virtual_screening_library',''))
    if not source.is_file() or source.is_symlink():
        raise ValueError('Registered virtual screening library unavailable')
    source_hash=sha256_file(source)
    contract={'schema':'library-pilot-input-v1','selection':binding,'target':selection['target'],
        'structure':selection['structure'],'site':site,'prediction_source':parent,
        'template_sha256':sha256_file(template),'source':{'path':str(source),'sha256':source_hash},
        'limits':{'max_rows':args.max_rows,'top_n':args.top_n},
        'record_selection':args.record_selection,'sampling_seed':20260725,
        'scope':'new prescreen, then separately planned docking/scoring; no MD or parameterization'}
    raw=(json.dumps(contract,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n').encode()
    identifier=hashlib.sha256(raw).hexdigest()[:32]
    directory=root/'state/library_pilot'/identifier
    # Remove all historical paths and inferred offsets. Prescreen uses chemical
    # policy only; downstream docking must bind the actual acquired structure.
    policy={k:v for k,v in policy.items() if k not in {'structure','cysteine_sequence_index','stage_budgets'}}
    policy.update(protein_chain=site['author_chain'],residue_number=args.canonical_position,
        structure=selection['structure'],site_selection=binding)
    policy_raw=yaml.safe_dump(policy,sort_keys=True).encode()
    for name,content in [('pilot-input.json',raw),('target.yaml',policy_raw)]:
        path=directory/name
        if path.exists():
            if path.is_symlink() or path.read_bytes()!=content:
                raise ValueError('Existing pilot input differs')
        else:
            directory.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content)
    request=JobSubmissionRequest(request_id='library-pilot-'+identifier,
        project_id='library-pilot-'+identifier, route='library-first',engine='covindb-prescreen',
        parameters={'target_config':str((directory/'target.yaml').relative_to(root)),
            'pilot_manifest':str((directory/'pilot-input.json').relative_to(root)),
            'record_selection':args.record_selection,'source_sha256':source_hash,'max_rows':args.max_rows,'top_n':args.top_n,'chunk_size':500,'seed':20260725},
        resource_budget=ResourceBudget(max_runtime_minutes=20,cpu_count=2,memory_gb=8,max_candidates=args.top_n))
    spec=manager.build_spec(request)
    return {'status':'PLANNED','artifact_id':'library-pilot-'+identifier,
        'manifest_sha256':sha256_file(directory/'pilot-input.json'),'input':contract,
        'request':request.model_dump(mode='json'),'spec':spec.model_dump(mode='json'),
        'approval_required':True,'calculation_submitted':False,
        'next_step':{'tool':'covalent_submit_job','instruction':'Use this exact request, set execution_mode=execute and approval_note to trigger native approval; do not self-approve.'},
        'limitations':['Reservoir mode scans the full CSV but performs chemistry on at most max_rows sampled records; prefix mode is not representative of the library.',
            'Prescreen scores are chemical prioritization, not docking scores or evidence of activity.',
            'Docking and structure scoring require separate plans and native approvals.']}
