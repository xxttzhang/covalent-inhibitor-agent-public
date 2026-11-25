from covscreen.covindb import source_records
import pytest


def test_reservoir_preserves_identity_and_is_reproducible():
    rows=[{'id':i} for i in range(10000)]
    audit={'records_scanned':0}
    result=list(source_records(iter(rows),100,'reservoir',17,audit))
    assert audit['records_scanned']==10000
    assert len(result)==len({i for i,r in result})==100
    assert all(r['id']==i for i,r in result)
    assert any(i>9000 for i,r in result)
    assert result==list(source_records(iter(rows),100,'reservoir',17,{'records_scanned':0}))
    assert result!=list(source_records(iter(rows),100,'reservoir',18,{'records_scanned':0}))


def test_prefix_never_reads_outside_approved_boundary():
    def rows():
        yield 'a'
        yield 'b'
        raise AssertionError('out of scope read')
    assert list(source_records(rows(),2,'file_order_prefix',17,{'records_scanned':0}))==[(0,'a'),(1,'b')]


def test_small_library_and_invalid_policy():
    assert list(source_records(iter(['a']),5,'reservoir',1,{'records_scanned':0}))==[(0,'a')]
    for n,policy in [(None,'reservoir'),(0,'reservoir'),(10,'unknown')]:
        with pytest.raises(ValueError):list(source_records(iter([]),n,policy,1,{'records_scanned':0}))


def test_reservoir_manifest_distinguishes_scan_from_chemistry(tmp_path):
    import csv,yaml
    from covscreen.covindb import run_prescreen
    source=tmp_path/'source.csv'
    with source.open('w') as f:
        writer=csv.writer(f);writer.writerow(['','Source','CompoundType','SMILES','InChI_Key'])
        writer.writerows([(str(i),'fixture','covalent','C=CC(=O)Nc1ccccc1',str(i)) for i in range(30)])
    target=tmp_path/'target.yaml'
    target.write_text(yaml.safe_dump({'target_id':'TEST','site_id':'TEST_C1','protein_chain':'A',
        'residue_name':'CYS','residue_number':1,'reaction_type':'Michael Addition','warhead_class':'acrylamide',
        'warhead_smarts':'[C:1]=[C:2][C:3](=[O:4])[N:5]','lead_like':{'min_mw':0}}))
    result=run_prescreen(source,target,tmp_path/'out',max_rows=5,top_n=1,record_selection='reservoir',seed=17)
    assert result['source']['records_scanned']==30
    assert result['source']['records_read']==result['counts']['input_rows']==5
    assert result['source']['record_selection']=='reservoir'
    assert result['source']['sampling_seed']==17
