#USAGE (from root): python3 ./src/caza_wizard.py <path to unstable query (.smt2)>

import argparse
import logging
import os
import subprocess
import shutil

from debugger.factory import get_debugger
from debugger.options import DebugOptions
from debugger.strainer import DebugStatus

def clean_up(proj_name):
    for path in [
        f"data/projs/{proj_name}",
        f"data/projs/{proj_name}.filtered",
        f"data/dbs/{proj_name}",
        f"data/dbs/{proj_name}.filtered",
        f"gen/{proj_name}",
        f"gen/{proj_name}.filtered",
    ]:
        if os.path.exists(path):
            shutil.rmtree(path)  

def main():
    print("Parsing...")

    #parse query_path
    p = argparse.ArgumentParser(
        description="Given unstable query path, runs cazamariposa workflow"
    )
    p.add_argument("query_path", help="path to unstable query")
    args = p.parse_args()

    print("Starting Cazamariposas")
    #set Caza options
    options = DebugOptions()
    options.verbose = True
    options.is_verus = True
    options.retry_failed = True

    #for some tasks the default 30 sec per proof and 120 total sec is not enough
    options.per_proof_time_sec = 90
    options.total_proof_time_sec = 1800

    #Mutate query until we reach a failure trace and a proof object
    dbg = get_debugger(args.query_path, options)

    print("Found failure trace and proof object")

    #If we could not produce a proof object, Caza cannot find fixes
    if dbg.status == DebugStatus.NO_PROOF:
        logging.error(":( could not get any mutant to produce a proof object")
        return
    
    proj_name = dbg.proj_name
    clean_up(proj_name)
    
    #produces candidate smt2 files at data/projs/<name>/base.z3/{edit_id}.smt2
    #dbg.tracker.edit_infos gets populated with ___
    ranked_ids = dbg.create_project()

    print("Produced candidate smt2 files")
    
    project_dir = f"data/projs/{proj_name}/base.z3"

    #check if proof is broken to fail fast
    os.system(f"./src/exper_wizard.py multiple -e verify -i {project_dir} --clear")
    
    print("Checked for broken queries")

    #pick out the ones that are not broken
    os.system(f"./src/analysis_wizard.py filter -i {project_dir}")

    print("Filtered out broken queries")

    #do another fast mariposa round to filter out unstable fixes
    filter_dir = project_dir.replace("/base.z3", ".filtered/base.z3")
    os.system(f"./src/exper_wizard.py multiple -e filter -i {filter_dir} --clear")
    os.system(f"./src/analysis_wizard.py carve -e filter -i {filter_dir}")

    #run Mariposa on each candidate to determine if the fix repaired stability
    os.system(f"./src/exper_wizard.py multiple -e default -i {filter_dir} --clear")

    print("Ran mariposa on each candidate")

    #get a list of the fixed queries that are now stable
    out = subprocess.run(["./src/analysis_wizard.py", "basic", "-i", filter_dir, "-e", "default", "--category", "stable", "-qv", "1"], capture_output=True, text=True,).stdout

    print("Got list of fixes")

    #collect stable ids in a list
    stable_ids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("query path:"):
            path = line.split("query path:", 1)[1].strip()
            edit_id = os.path.splitext(os.path.basename(path))[0]
            stable_ids.append(edit_id)

    #if stable_ids is empty, then no fixes were found
    if not stable_ids:
        print("No fixes were found :(")
        return
    
    print(f"Found {len(stable_ids)} fix(es):")
    for edit_id in stable_ids:
        edit = dbg.tracker.look_up_edit_with_id(edit_id)
        qname, action = edit.get_singleton_edit()
        print(f"    {edit_id}: {action.value} {qname} -> {edit.query_path}")


if __name__ == "__main__":
    main()