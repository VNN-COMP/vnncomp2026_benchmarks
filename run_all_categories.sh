#!/bin/bash
# run measurements for all categories for a single tool (passed on command line)
# eight args: 'v1' (version string), tool_scripts_folder, vnncomp_folder, result_csv_file, counterexamples_folder, categories, all|different|first, vnnlib_version
#
# for example ./run_all_categories.sh v1 ~/repositories/simple_adversarial_generator/vnncomp_scripts . ./out.csv ./counterexamples "test acasxu" all 1.0

VERSION_STRING=v1
SCRIPT_PATH=$(dirname $(realpath $0))

MAX_CATEGORY_TIMEOUT=6*60*60*1000
MIN_CATEGORY_TIMEOUT=3*60*60*0

# if this is "true", will only report total timeout (and not run anything)
TOTAL_TIMEOUT_ONLY="false"
TOTAL_TIMEOUT=0
TIMEOUT_OF_EXECUTED_INSTANCES=0

# if "true", measure overhead after each category
MEASURE_OVERHEAD="true"

# check arguments
if [ "$#" -ne 8 ]; then
    echo "Expected 8 arguments (got $#): '$VERSION_STRING' (version string), tool_scripts_folder, vnncomp_folder, result_csv_file, counterexamples_folder, categories, run_which_networks (all|different|first), vnnlib_version"
    exit 1
fi

if [ "$1" != ${VERSION_STRING} ]; then
	echo "Expected first argument (version string) '$VERSION_STRING', got '$1'"
	exit 1
fi

TOOL_FOLDER=$2
VNNCOMP_FOLDER=$3
RESULT_CSV_FILE=$4
COUNTEREXAMPLES_FOLDER=$5
# list of benchmark category names seperated by spaces
CATEGORY_LIST=$6
RUN_WHICH_NETWORKS=$7
VNNLIB_VERSION=$8

VALID_OPTIONS=("all" "different" "first")
if [[ ! "${VALID_OPTIONS[*]}" =~ $RUN_WHICH_NETWORKS ]]; then
    echo "run all|different|first networks per benchmark"
    exit 1
fi

if [[ $RESULT_CSV_FILE != *csv ]]; then
    echo "result csv file '$RESULT_CSV_FILE' should end in .csv"
    exit 1
fi

if [ ! -d $VNNCOMP_FOLDER ] 
then
    echo "VNNCOMP directory does not exist: '$VNNCOMP_FOLDER'" 
    echo "errored" > $RESULT_CSV_FILE
    exit 0
fi

if [ ! -d $TOOL_FOLDER ] 
then
    echo "Tool scripts directory does not exist: '$TOOL_FOLDER'" 
    echo "errored" > $RESULT_CSV_FILE
    exit 0
fi

echo "Running measurements with vnncomp folder '$VNNCOMP_FOLDER' for tool scripts in '$TOOL_FOLDER' and saving results to '$RESULT_CSV_FILE'."

emit_instance_rows() {
    python3 - "$1" "$2" <<'PY'
import ast
import csv
import os
import sys
from pathlib import Path

csv_path = sys.argv[1]
benchmark_base_path = sys.argv[2]

def prefixed_path(path):
    path = str(path).strip()
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(benchmark_base_path, path))

def onnx_value_and_stem(value):
    value = value.strip()

    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return prefixed_path(value), Path(value).stem

    if not isinstance(parsed, list):
        return prefixed_path(value), Path(value).stem

    prefixed_entries = []
    tuple_names = []

    for entry in parsed:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise SystemExit(f"expected ONNX list entries to be 2-tuples, got: {entry!r}")

        name, onnx_path = entry
        tuple_names.append(str(name))
        prefixed_entries.append((name, prefixed_path(onnx_path)))

    return repr(prefixed_entries), "_".join(tuple_names)

with open(csv_path, newline="") as f:
    for line_number, row in enumerate(csv.reader(f), 1):
        if not row or all(not cell.strip() for cell in row):
            continue

        if len(row) != 3:
            raise SystemExit(f"{csv_path}:{line_number}: expected 3 columns, got {len(row)}: {row}")

        onnx, vnnlib, timeout = (cell.strip() for cell in row)
        onnx_path, onnx_stem = onnx_value_and_stem(onnx)
        vnnlib_path = prefixed_path(vnnlib)
        vnnlib_stem = Path(vnnlib).stem

        print("\t".join([onnx_path, vnnlib_path, timeout, onnx_stem, vnnlib_stem]))
PY
}

# clear file
echo -n "" > $RESULT_CSV_FILE

# run on each benchmark category
for CATEGORY in $CATEGORY_LIST
do
    CATEGORY_PATH="${CATEGORY}/${VNNLIB_VERSION}"

    if [ ! -f "${VNNCOMP_FOLDER}/benchmarks/${CATEGORY_PATH}/instances.csv" ]; then
        CATEGORY_PATH="${CATEGORY}"
    fi

    INSTANCES_CSV_PATH="${VNNCOMP_FOLDER}/benchmarks/${CATEGORY_PATH}/instances.csv"
    BENCHMARK_BASE_PATH="${VNNCOMP_FOLDER}/benchmarks/${CATEGORY_PATH}"
    echo "Running $CATEGORY category from $INSTANCES_CSV_PATH"
    
    # loop through csv file and run on each instance in category
    if [ ! -f $INSTANCES_CSV_PATH ]
    then
	    echo "$INSTANCES_CSV_PATH file not found"
	    
	    echo "errored" > $RESULT_CSV_FILE
	    exit 0
    fi
    
    SUM_TIMEOUT=$(emit_instance_rows "$INSTANCES_CSV_PATH" "$BENCHMARK_BASE_PATH" | awk -F '\t' '{x+=$3}END{print x}')
    echo "Category '$CATEGORY' timeout sum: $SUM_TIMEOUT seconds"
    TOTAL_TIMEOUT=$(( $TOTAL_TIMEOUT + $SUM_TIMEOUT ))
   
    if (( $(echo "$SUM_TIMEOUT < $MIN_CATEGORY_TIMEOUT || $SUM_TIMEOUT > $MAX_CATEGORY_TIMEOUT" |bc -l) )); then
    
	# to compare more closely with last year, ignore runtime threshold for this one
	if [[ $CATEGORY != "cifar2020" && $CATEGORY != "test" ]]; then
	    echo "$CATEGORY sum timeout ($SUM_TIMEOUT) not in valid range [$MIN_CATEGORY_TIMEOUT, $MAX_CATEGORY_TIMEOUT]"
	    
	    echo "errored" > $RESULT_CSV_FILE
	    exit 0
	else
	    echo "Ignoring out of bounds timeout for category $CATEGORY"
	fi
    fi
    
    if [[ $TOTAL_TIMEOUT_ONLY == "true" ]]; then
	continue
    fi
	
    PREV_ONNX_PATHS=()
    while IFS=$'\t' read -r ONNX_PATH VNNLIB_PATH TIMEOUT ONNX_FILENAME VNNLIB_FILENAME || [[ $ONNX_PATH ]]
    do
        if [[ $RUN_WHICH_NETWORKS == "different" && "${PREV_ONNX_PATHS[*]}" =~ "${ONNX_PATH}" && $CATEGORY != "test" ]]; then
            continue
        fi
        PREV_ONNX_PATHS+=("$ONNX_PATH")
        
        # remove carriage return from timeout
        TIMEOUT=$(echo $TIMEOUT | sed -e 's/\r//g')

        mkdir -p ${COUNTEREXAMPLES_FOLDER}/${CATEGORY}
        COUNTEREXAMPLE_FILE=${COUNTEREXAMPLES_FOLDER}/${CATEGORY}/${ONNX_FILENAME}_${VNNLIB_FILENAME}.counterexample
        # If the benchmark is the safenlp benchmark and the vnnlib file contains "ruarobot", prepend ruarobot_ to the filname
        if [[ $CATEGORY == "safenlp" && $VNNLIB_PATH == *"ruarobot"* ]]; then
            COUNTEREXAMPLE_FILE=${COUNTEREXAMPLES_FOLDER}/${CATEGORY}/ruarobot_${ONNX_FILENAME}_${VNNLIB_FILENAME}.counterexample
        fi
        if [[ $CATEGORY == "safenlp" && $VNNLIB_PATH == *"medical"* ]]; then
            COUNTEREXAMPLE_FILE=${COUNTEREXAMPLES_FOLDER}/${CATEGORY}/medical_${ONNX_FILENAME}_${VNNLIB_FILENAME}.counterexample
        fi
        $SCRIPT_PATH/run_single_instance.sh v1 "$TOOL_FOLDER" "$CATEGORY" "$ONNX_PATH" "$VNNLIB_PATH" "$TIMEOUT" "$RESULT_CSV_FILE" "${COUNTEREXAMPLE_FILE}"

        TIMEOUT_OF_EXECUTED_INSTANCES=$(python3 -c "print($TIMEOUT_OF_EXECUTED_INSTANCES + $TIMEOUT)")
        
        if [[ $RUN_WHICH_NETWORKS == "first" && $CATEGORY != "test" ]]; then
           break
        fi

		
    done < <(emit_instance_rows "$INSTANCES_CSV_PATH" "$BENCHMARK_BASE_PATH")
	
    if [[ $MEASURE_OVERHEAD == "true" && $RUN_WHICH_NETWORKS == "all" ]]; then
	# measure overhead at end (hardcoded model)
	ONNX_PATH="${VNNCOMP_FOLDER}/benchmarks/test/onnx/test_nano.onnx"
	VNNLIB_PATH="${VNNCOMP_FOLDER}/benchmarks/test/vnnlib/test_nano.vnnlib"
	TIMEOUT=120
	$SCRIPT_PATH/run_single_instance.sh v1 "$TOOL_FOLDER" "$CATEGORY" "$ONNX_PATH" "$VNNLIB_PATH" "$TIMEOUT" "$RESULT_CSV_FILE"
    fi

done

if [[ $TOTAL_TIMEOUT_ONLY == "true" ]]; then
    echo "Total Timeout of all benchmarks: $TOTAL_TIMEOUT"
fi

echo "Timeout of executed instances: $TIMEOUT_OF_EXECUTED_INSTANCES sec"
