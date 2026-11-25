"""Compatibility adapter for historical Library candidate delivery manifests."""
from covscreen.md_candidate_delivery import freeze_md_candidate_delivery as _freeze


def freeze_md_candidate_delivery(candidate_table, candidate_table_sha256,
                                 covdock_manifest, covdock_manifest_sha256,
                                 task_id, output):
    return _freeze(candidate_table, candidate_table_sha256, covdock_manifest,
                   covdock_manifest_sha256, task_id, output,
                   route="library-first", legacy_library=True)
