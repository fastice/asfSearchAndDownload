#!/usr/bin/env python3

import argparse
import zipfile
import os
import tempfile
import shutil
import subprocess

# This code was partially generated with chatGPT

def remove_files_from_zip(zip_path, pattern):
    """Remove files containing 'pattern' from a ZIP archive."""
    # List files in the ZIP
    with zipfile.ZipFile(zip_path, 'r') as z:
        files_to_remove = [f for f in z.namelist() if pattern in f]

    if not files_to_remove:
        print(f"No files matching '{pattern}' found in {zip_path}")
        return

    # Use zip CLI if available
    if shutil.which("zip"):
        cmd = ["zip", "-d", zip_path] + files_to_remove
        subprocess.run(cmd, check=True)
    else:
        # Fallback: stream copy to new ZIP
        print('using slower python rather since zip not present')
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip", dir=os.path.dirname(zip_path))
        os.close(tmp_fd)
        with zipfile.ZipFile(zip_path, "r") as zin, zipfile.ZipFile(tmp_name, "w") as zout:
            for item in zin.infolist():
                if item.filename in files_to_remove:
                    continue
                with zin.open(item) as src, zout.open(item.filename, 'w') as dst:
                    shutil.copyfileobj(src, dst, length=1024*1024*32)  # 32 MB chunks
        os.replace(tmp_name, zip_path)
    print(f"Removed {len(files_to_remove)} file(s) from {zip_path}")
    

def main():
    parser = argparse.ArgumentParser(description="Remove files from ZIP archives by pattern.")
    parser.add_argument("zipfile", nargs="?", help="Path to a single ZIP file")
    parser.add_argument("--pattern", default="hv",
                        help="Pattern to match filenames to  [hv]")
    parser.add_argument("--directory", 
                        help="Directory containing ZIP files to process")
    parser.add_argument("--suffix", default="zip",
                        help="Suffix of files to process in directory [zip]")
    args = parser.parse_args()

    # Get listing of files
    if args.directory:
        # Process all files in the directory matching suffix
        for f in os.listdir(args.directory):
            if f.endswith("." + args.suffix):
                remove_files_from_zip(os.path.join(args.directory, f),
                                      args.pattern)
    elif args.zipfile:
        remove_files_from_zip(args.zipfile, args.pattern)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
    
