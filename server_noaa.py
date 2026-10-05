#!/usr/bin/env python3
"""Thin launcher for the noaa_receiver package.

Kept under this filename so existing startup scripts and process
matching (pkill -f server_noaa) continue to work; all logic lives in
the noaa_receiver/ package.
"""
from noaa_receiver import main

if __name__ == '__main__':
    main()
