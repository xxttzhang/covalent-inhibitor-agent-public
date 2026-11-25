from covscreen.adapters.carsidock import CarsiDockAdapter


def test_carsidock_missing_checkpoint_is_unavailable(tmp_path):
    a = CarsiDockAdapter(str(tmp_path), str(tmp_path / "missing.ckpt"))
    report = a.check_availability()
    assert report["ready"] is False
    assert report["checkpoint_exists"] is False


def test_carsidock_command_preserves_explicit_gpu_index(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checkpoint = tmp_path / "carsidock.ckpt"
    adapter = CarsiDockAdapter(str(repo), str(checkpoint))
    command = adapter.build_command(
        "protein.pdb",
        "reference.sdf",
        "ligands.sdf",
        "A:CYS:481",
        "output",
        cuda_device_index=1,
    )
    assert command[command.index("--cuda_device_index") + 1] == "1"
    assert "-rtms_rescoring" not in command
    assert "--rtms_ckpt_path" not in command


def test_carsidock_command_preserves_explicit_reaction_type(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    adapter = CarsiDockAdapter(str(repo), str(tmp_path / "carsidock.ckpt"))
    command = adapter.build_command(
        "protein.pdb",
        "reference.sdf",
        "ligands.sdf",
        "A:CYS:797",
        "output",
        reaction_type="Nucleophilic Substitution",
    )
    assert command[command.index("-rectype") + 1] == "Nucleophilic Substitution"
