#!/usr/bin/env bash

rm -rf test.m4v test.m4a
cat *init.m4v *.m4v > test.m4v
cat *init.m4a *.m4a > test.m4a
