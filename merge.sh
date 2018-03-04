#!/usr/bin/env bash

dir=$1
result="result"
if [ -z "$dir" ]
then
    dir="."
fi

dir="$dir/"
rm -rf ${dir}${result}.m4v ${dir}${result}.m4a
cat ${dir}*init.m4v ${dir}*.m4v > ${dir}${result}.m4v
cat ${dir}*init.m4a ${dir}*.m4a > ${dir}${result}.m4a
