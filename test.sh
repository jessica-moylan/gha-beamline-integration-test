#!/bin/bash

if grep -qw "${1}" configs/post_data_security.txt; then
    echo "Beamline ${1} is allowed to access the data."
else
    echo "Beamline ${1} is NOT allowed to access the data."
fi
