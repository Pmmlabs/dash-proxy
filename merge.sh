#!/usr/bin/env bash

dir=$1
result="result"
if [ -z "$dir" ]
then
    dir="."
fi

dir="$dir/"
FFMPEG=${FFMPEG:-"ffmpeg"}
cat ${dir}*init.m4v ${dir}*.m4v > ${dir}${result}.m4v
cat ${dir}*init.m4a ${dir}*.m4a > ${dir}${result}.m4a
${FFMPEG} -i ${dir}${result}.m4v -i ${dir}${result}.m4a -c copy $dir$(date +%x_%X).mp4 && rm -rf ${dir}*.m4v ${dir}*.m4a
