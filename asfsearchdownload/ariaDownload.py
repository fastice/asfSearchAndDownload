#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Oct  7 15:51:50 2021

@author: ian
"""
import argparse
import sys
import os
from subprocess import call
import shutil
from datetime import datetime


def myerror(message):
    """ print error and exit """
    print(f'\n\t\033[1;31m *** {message} *** \033[0m\n')
    sys.exit()


def ariaArgs():
    ''' Handle command line args'''
    parser = argparse.ArgumentParser(
        description='\n\n\033[1mDownload with aria2c from a list in a file'
        '\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.')

    parser.add_argument('--overWrite', action='store_true', default=False,
                        help='Overwrite existing [False]')
    parser.add_argument('downloadLinks', metavar='isceUNW', type=str, nargs=1,
                        help='File with download links')
    parser.add_argument('--xferDir', type=str,
                        default='*',
                        help='First check this directory for .zip or .zip.1;'
                        'use * for all /Volumes/insar*/ian/xfer')
    parser.add_argument('--noRename',  action='store_true', default=False,
                        help='Do not rename/move existing zip.1 file')
    #
    args = parser.parse_args()
    xferDirs = ['.']
    if args.xferDir == '*':
        for n in [1, 3, 6, 7, 8, 9, 10, 11]:
            if os.path.exists(f'/Volumes/insar{n}/ian/xfer'):
                xferDirs.append(f'/Volumes/insar{n}/ian/xfer')
    else:
        xferDirs = [args.xferDir]
    return args.downloadLinks[0], args.overWrite, xferDirs, not args.noRename


def getX():
    '''
    Return aria2c connection count based on time of day and day of week.
      Weekday 07:00-18:00 -> 1  (polite during office hours)
      Weekend 07:00-18:00 -> 4  (office is empty, use more bandwidth)
      All other times     -> 10
    '''
    now = datetime.now()
    t = now.time()
    start_time = t.replace(hour=7, minute=0, second=0, microsecond=0)
    end_time   = t.replace(hour=18, minute=0, second=0, microsecond=0)
    if start_time <= t <= end_time:
        return 4 if now.weekday() >= 5 else 1  # 5=Sat, 6=Sun
    return 10


def main():
    ''' Download files with aria2c '''
    downloadLinks, overwrite, xferDirs, rename = ariaArgs()
    if not os.path.exists(downloadLinks):
        myerror(f'{downloadLinks} files does not exist')
    # Now download list
    with open(downloadLinks) as fp:
        for link in fp:
            myFile = link.split('/')[-1].strip()
            found = False
            if len(myFile) < 1:
                continue
            # Mv the file if already us as indicated by ...zip.1
            if not os.path.exists(myFile):
                for xferDir in xferDirs:
                    if os.path.exists(f'{xferDir}/{myFile}.1'):
                        if rename:
                            print(f'Moving {xferDir}/{myFile}.1')
                            shutil.move(f'{xferDir}/{myFile}.1', myFile)
                        found = True
                        break
                    # Copy file if not already used indicated by .zip
                    if os.path.exists(f'{xferDir}/{myFile}'):
                        if not rename:
                            print(f'Copying {xferDir}/{myFile}')
                            shutil.copyfile(f'{xferDir}/{myFile}', myFile)
                        found = True
                        break
            if found:
                continue
            if os.path.exists(myFile) and not overwrite:
                print(f'skipping existing {myFile}')
                continue
            x = getX()
            call(f'aria2c -x {x} {link.strip()}', shell=True, executable='/bin/csh')


if __name__ == '__main__':
    main()
