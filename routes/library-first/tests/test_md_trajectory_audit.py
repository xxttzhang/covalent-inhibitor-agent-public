import json,struct
from pathlib import Path
import pytest
from covscreen_library_first.md_trajectory_audit import inspect_dcd

def record(data): return struct.pack('<i',len(data))+data+struct.pack('<i',len(data))
def test_rejects_declared_frame_mismatch(tmp_path):
    header=b'CORD'+struct.pack('<20i',2,1,1,2,*([0]*16)); title=struct.pack('<i',0)
    path=tmp_path/'bad.dcd'; path.write_bytes(record(header)+record(title)+record(struct.pack('<i',2)))
    with pytest.raises(ValueError,match='frame/step counts'):
        inspect_dcd(path,2,[1,2])
