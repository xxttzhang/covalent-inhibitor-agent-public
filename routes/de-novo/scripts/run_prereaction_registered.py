"""Entry point from the immutable route package, never from task-owned code."""
import os
from pathlib import Path
import sys

for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '1'
root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root/'routes/de-novo/src'))
sys.path.insert(0, str(root/'science'/'src'))
from covscreen_de_novo.prereaction_runner import main

if __name__ == '__main__':
    main()
