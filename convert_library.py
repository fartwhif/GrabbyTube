#!/usr/bin/env python3
"""Convert all GrabbyTube MP3s to 160kbps 44.1kHz stereo CBR.
Backs up originals as .mp3.bak before overwriting.
Tolerant of missing .json / .webp sidecar files.
If .bak already exists: deletes current .mp3 and re-converts from .bak."""

import subprocess
import os
import sys

CACHE_DIR = "/home/user/projects/drivepod/cache"
BITRATE = "160k"
SAMPLE_RATE = "44100"
CHANNELS = 2

def convert_all():
    total = 0
    converted = 0
    failed = 0

    for entry in sorted(os.listdir(CACHE_DIR)):
        mp3_path = os.path.join(CACHE_DIR, entry, f"{entry}.mp3")
        bak_path = mp3_path + ".bak"
        if not os.path.isfile(mp3_path) and not os.path.isfile(bak_path):
            continue
        total += 1

        if os.path.isfile(bak_path):
            # Already has backup — delete current .mp3 and re-convert from .bak
            if os.path.isfile(mp3_path):
                os.remove(mp3_path)
                print(f"  removed existing .mp3: {entry}")
        else:
            # No backup yet — create one from original
            os.rename(mp3_path, bak_path)

        # Convert: 160k CBR, 44.1kHz, stereo (write directly to target)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", bak_path,
                    "-codec:a", "libmp3lame",
                    "-b:a", BITRATE,
                    "-ar", str(SAMPLE_RATE),
                    "-ac", str(CHANNELS),
                    "-map_metadata", "0",
                    mp3_path,
                ],
                capture_output=True, text=True, timeout=300,
            )
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
                converted += 1
                print(f"  [{converted}/{total}] {entry} -> {BITRATE} {SAMPLE_RATE}Hz stereo")
            else:
                print(f"  FAILED (empty output): {entry}", file=sys.stderr)
                failed += 1
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT: {entry}", file=sys.stderr)
            failed += 1
        except Exception as e:
            print(f"  FAILED: {entry} -> {e}", file=sys.stderr)
            failed += 1

    print(f"\nDone: {converted} converted, {failed} failed (of {total} total)")

if __name__ == "__main__":
    convert_all()
