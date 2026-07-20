DropGain: place FFmpeg here
===========================

build.py copies ffmpeg.exe and ffprobe.exe into release/DropGain/bin/.

Required:
  bin/ffmpeg.exe
  bin/ffprobe.exe
  LICENSE (for the release licenses/ folder)

Not needed:
  bin/ffplay.exe
  doc/
  presets/

Accepted layouts (either works):

  A) Essentials zip extracted here (gyan.dev style), trimmed to the required files above
  B) Flat copy:
       third_party/ffmpeg/ffmpeg.exe
       third_party/ffmpeg/ffprobe.exe
       plus LICENSE / COPYING files

Confirm LGPL (not nonfree):
  ffmpeg -version

Optional: VERSION.txt with the exact version for the GitHub Release source link.

This file is DropGain's note. FFmpeg's README.txt sit beside it.
