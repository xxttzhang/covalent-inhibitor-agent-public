"""CovDock launcher with a distinct progress channel for nested ligand strain.

Schrodinger 2021-2 uses EXECUTE_FUNCTION_PROGRESS for both CovDock candidates
and its in-process job-control descendants. LigandStrain pose indices can exceed
the candidate count and crash PrimeJobDJ. Only the CovDock subclass channel is
changed; vendor chemistry, scoring, job control and licensing remain intact.
"""
import sys

PROGRESS_CHANNEL = 'COVSCREEN_COVDOCK_PROGRESS_V1'


def configure_progress(covdock_class, execute_function_class):
    if not issubclass(covdock_class, execute_function_class):
        raise RuntimeError('Unsupported CovDock progress implementation')
    if execute_function_class.JOBCONTROL_FLAG != 'EXECUTE_FUNCTION_PROGRESS':
        raise RuntimeError('Vendor progress contract changed; revalidate compatibility')
    if covdock_class.JOBCONTROL_FLAG not in {'EXECUTE_FUNCTION_PROGRESS', PROGRESS_CHANNEL}:
        raise RuntimeError('CovDock already uses a different progress channel')
    covdock_class.JOBCONTROL_FLAG = PROGRESS_CHANNEL


def main():
    from schrodinger.application.prime.packages import covalent_docking, executeparallel
    configure_progress(covalent_docking.CovalentDocking, executeparallel.ExecuteFunction)
    # Every vendor-launched subjob re-enters this same script and sets the same
    # channel. Other ExecuteFunction subclasses keep the vendor default.
    covalent_docking.CovalentDocking(sys.argv[0]).launch(sys.argv[1:])


if __name__ == '__main__':
    main()
