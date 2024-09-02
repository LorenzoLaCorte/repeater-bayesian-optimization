#!/bin/bash

SCRIPT="gp_asymmetric.py"
PY_ALIAS="python3.10"

# Define what simulation you want to run {True, False}
GP=True

# Define the general result directory
GENERAL_RESULT_DIR="./results"

# Define parameters as tuples of t_coh, p_gen, p_swap, w0, nodes, max_dists
PARAMETER_SETS=(
    # "360000  0.000000096 0.85 0.36  2 1"
    "720000  0.000015    0.85 0.9   3  1" 
    # "3600000 0.0026      0.85 0.958 10 1"
)

# -----------------------------------------
# GP: Parameter Set Testing with Various Optimizers and Spaces
# -----------------------------------------
# This section of the script is responsible for testing different parameter sets
# using various optimizers and search spaces. The goal is to evaluate the performance
# and effectiveness of each combination
#
# Follow these steps to run the script:
# 1. Define the parameter sets to be tested (above).
# 2. Specify the optimizers and search spaces to be used for testing (below).
#
# Note: Ensure that all necessary dependencies and environment variables are set
# before running this section of the script, and to choose the Python alias of your environment.
# -----------------------------------------

if [ "$GP" = "True" ]; then
    # Define the optimizers and spaces to test
    OPTIMIZER_COMBS=(
        "gp"
    )

    for PARAMETERS in "${PARAMETER_SETS[@]}"; do
        IFS=' ' read -r -a PARAM_ARRAY <<< "$PARAMETERS"
        
        T_COH="${PARAM_ARRAY[0]}"
        P_GEN="${PARAM_ARRAY[1]}"
        P_SWAP="${PARAM_ARRAY[2]}"
        W0="${PARAM_ARRAY[3]}"
        NODES="${PARAM_ARRAY[4]}"
        MAX_DISTS="${PARAM_ARRAY[5]}"
        
        for TUPLE in "${OPTIMIZER_COMBS[@]}"; do
            OPTIMIZER=$(echo $TUPLE | awk '{print $1}')
            FILENAME="output.txt"
            TMPFILE=$(mktemp)
            
            echo "Running distillation with optimizer $OPTIMIZER..."

            # Run the Python script with the specified parameters and append the output to TMPFILE
            { time $PY_ALIAS $SCRIPT \
                --nodes="$NODES" \
                --max_dists="$MAX_DISTS" \
                --optimizer="$OPTIMIZER" \
                --filename="$FILENAME" \
                --t_coh="$T_COH" \
                --p_gen="$P_GEN" \
                --p_swap="$P_SWAP" \
                --w0="$W0" \
            ; } 2>&1 | tee -a "$TMPFILE"

            # Extract the time taken and append it to the output file
            echo "Time taken:" >> "$FILENAME"
            tail -n 3 "$TMPFILE" >> "$FILENAME"
            rm "$TMPFILE"

            # Create a folder for the results if it doesn't exist
            RESULT_DIR="$GENERAL_RESULT_DIR/results_${OPTIMIZER}_tcoh${T_COH}_pgen${P_GEN}_pswap${P_SWAP}_w0${W0}_nodes${NODES}_maxdists${MAX_DISTS}"
            mkdir -p "$RESULT_DIR"

            # Move the output file and the plots to the results folder
            mv "$FILENAME" "$RESULT_DIR/"
            if ls *_${OPTIMIZER}.png 1> /dev/null 2>&1; then
                mv *_${OPTIMIZER}.png "$RESULT_DIR/"
            else
                echo "No plots yielded for optimizer $OPTIMIZER"
            fi
        done
    done
fi