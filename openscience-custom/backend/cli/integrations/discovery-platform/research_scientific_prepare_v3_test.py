"""Small synthetic geometry tests; never a scientific preparation result."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('preparation_v3',Path(__file__).with_name('research-scientific-prepare-v3.py'))
prep=importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


class StandardTemplateTest(unittest.TestCase):
    def test_standard_l_centers_and_cys_ile_thr_exceptions(self):
        try:
            import rdkit
        except ImportError:
            self.skipTest('Run this test in the pinned RDKit environment')
        for residue in prep.RESIDUES:
            if residue=='GLY':
                continue
            value=prep.template_sign(residue,'CA')
            self.assertEqual(value['expected_cip'],'R' if residue=='CYS' else 'S')
            self.assertEqual(value['neighbor_names'],['C','CB','N'])
            self.assertIn(value['sign'],[-1,1])
        self.assertEqual(prep.template_sign('ILE','CB')['expected_cip'],'S')
        self.assertEqual(prep.template_sign('ILE','CB')['neighbor_names'],['CA','CG1','CG2'])
        self.assertEqual(prep.template_sign('THR','CB')['expected_cip'],'R')
        self.assertEqual(prep.template_sign('THR','CB')['neighbor_names'],['CA','CG2','OG1'])

    def test_actual_openmm_signed_volume_force_corrects_inversion_and_fixes_anchors(self):
        try:
            import openmm as mm
            from openmm import unit
        except ImportError:
            self.skipTest('Run this test in the pinned OpenMM environment')
        for sign in [-1,1]:
            system=mm.System()
            for mass in [0,0,0,12]:
                system.addParticle(mass)
            system.addForce(prep.chiral_force([{'particles':[0,1,2,3],'sign':sign,'movable':True}],1e8,0.0015))
            integrator=mm.VerletIntegrator(0.001)
            context=mm.Context(system,integrator,mm.Platform.getPlatformByName('CPU'),{'Threads':'1'})
            coords=[[0,0,0],[0.15,0,0],[0,0.15,0],[0,0,-sign*0.15]]
            context.setPositions(coords)
            before=context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
            self.assertGreater(before,100)
            mm.LocalEnergyMinimizer.minimize(context,0.001,100)
            state=context.getState(getEnergy=True,getPositions=True)
            values=state.getPositions().value_in_unit(unit.nanometer)
            self.assertLess(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole),1e-6)
            self.assertGreater(values[3][2]*sign,0)
            for index in range(3):
                self.assertEqual(list(values[index]),coords[index])
            del context,integrator,system


if __name__=='__main__':
    unittest.main()
