#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Message and thread helpers, copied from the utilities package so
asfSearchAndDownload does not depend on it.

Part of the asfSearchAndDownload package.
"""
import os
import sys
import time
from datetime import datetime


def myerror(message, myLogger=None):
    """ print error and exit with a FAILING status (1).

    Was a bare sys.exit(), which exits 0 -- so every fatal error here looked
    like success to subprocess.run().returncode, csh $status and && chains,
    and silently vanished from cron summaries.

    Note: inside a worker thread this raises SystemExit in that thread only;
    the process exit code is unaffected either way.
    """
    print('\n\t\033[1;31m *** ', message, ' *** \033[0m\n')
    if myLogger is not None:
        myLogger.logError(message)
    sys.exit(1)


def mywarning(message):
    """ print warning """
    print(f'\n\t\033[1;43m ---- {message} ----- \033[0m\n')


def myalert(message):
    """ print alert """
    print(f'\n\t\033[1;46m ++++ {message} ++++ \033[0m\n')


def runMyThreads(threads, maxThreads, message, delay=0.2, prompt=False,
                 quiet=False):
    """ Loop through a list of  -- threads -- starting each one.
    Allow -- maxThreads -- running at once.
    In each loop iteration check status and print number running along with
    -- message.
    """
    #
    # Optional prompt
    #
    if prompt:
        while(1):
            ans = input(
                f'\n\033[1mRun these {str(len(threads))} jobs [y/n] \033[0m\n')
            if ans.lower() == 'y':
                break
            if ans.lower() == 'n':
                myerror("User prompted abort")
    notDone = True
    nRun = count = 0
    running = []
    # make sure always calling from home directory
    home = os.getcwd()
    # delay
    if delay < 0.2:
        delay = 0.2
    # format codes
    bs = '\033[1m'
    norm = '\033[0m'
    grs = '\033[1;42m'
    bls = '\033[1;44m'
    # time for counter
    start = datetime.now()
    # loop counter
    n = 0
    while notDone:
        #
        # Start a thread is < maxThreads
        #
        if n % 1 == 0 and not quiet:
            timeElapsed = datetime.now() - start
            print(grs, message, '(', maxThreads, ')', norm, ': nRunning ', bs,
                  f'{nRun:5}', norm, ' nStarted ', bs, f'{count:5}',
                  norm, 'nToGo ', bs, f'{len(threads)-count:5}', '  ', bls,
                  timeElapsed, norm, '     '.ljust(maxThreads+8), end='\r')
            print(grs, message, '(', maxThreads, ')', norm, ': nRunning ', bs,
                  f'{nRun:5}', norm, 'nStarted ', bs, f'{count:5}',
                  norm, 'nToGo ', bs, f'{len(threads)-count:5}', '  ', bls,
                  timeElapsed, norm, '     ', end='')
            sys.stdout.flush()
        #
        # Run as many threads as can be started
        while count < len(threads) and nRun < maxThreads:
            # run thread and always make sure to return to current directory
            os.chdir(home)
            if not quiet:
                print('.', end='')
            sys.stdout.flush()
            threads[count].start()
            running.append(threads[count])
            nRun += 1
            count += 1
            time.sleep(delay)
            #
            # check status of threads
            #
        toRemove = []
        #
        # loop through running thread to find threads that are done
        for t in running:
            if not t.is_alive():
                toRemove.append(t)
        #
        # update list of running
        for t in toRemove:
            nRun -= 1
            running.remove(t)
        time.sleep(1)
        #
        if not quiet:
            print('', end='\r')
        n += 1
        if nRun == 0 and count >= len(threads):
            notDone = False
    if not quiet:
        print('--\n')
        myalert('Threads Done')
    return
