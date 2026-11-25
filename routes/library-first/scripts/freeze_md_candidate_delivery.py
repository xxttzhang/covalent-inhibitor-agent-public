#!/usr/bin/env python3
"""Freeze a completed Library-first Pareto result into an MD candidate delivery."""
import argparse
import json

from covscreen_library_first.md_candidate_delivery import freeze_md_candidate_delivery


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("candidate-table","candidate-table-sha256","covdock-manifest",
                 "covdock-manifest-sha256","task-id","output"):
        parser.add_argument("--"+name,required=True)
    args=parser.parse_args()
    result=freeze_md_candidate_delivery(args.candidate_table,args.candidate_table_sha256,
        args.covdock_manifest,args.covdock_manifest_sha256,args.task_id,args.output)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=="__main__": main()
