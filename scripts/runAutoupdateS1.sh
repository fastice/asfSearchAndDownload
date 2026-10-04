#!/bin/bash
#
# Cron entry point for autoupdateS1.
#
# cron runs with an almost-empty environment, so this wrapper activates the
# conda env that provides autoupdateS1, searchASF, ariaDownload and reduces1
# (plus aria2c / zip on the system PATH), sources the Gamma environment the
# assemble stage needs, then runs the update for one project directory. It lives
# in the package so it is versioned with the code; reference it from crontab by
# its absolute path.
#
# The Gamma part is not optional once `assemble: true` is set in autoupdate.yaml:
# setupTrack shells out to S1_TOPS_preproc and friends by bare name, and those
# come from ~/.cshrc, which cron never sources. See scripts/gammaEnv.sh.
#
# Usage (from cron, by absolute path):
#   runAutoupdateS1.sh <projectDir> [extra autoupdateS1 args]
# e.g.
#   runAutoupdateS1.sh /Volumes/insar9/ian/Sentinel1/GreenlandS1
#   runAutoupdateS1.sh /Volumes/.../GreenlandS1 --maxDownloads 1
#
# Environment overrides (defaults suit the GrIMP workstation):
#   CONDA_BASE  conda/miniforge install root [/home/ian/miniforge3]
#   GAMMA_ROOT  Gamma install root [/home/ian/gammaISP/GAMMA_SOFTWARE-20160611]
#
# Note: no `set -u` here -- conda's own activate/deactivate hooks reference
# unbound variables, so `set -u` would abort activation. The explicit guards
# below cover the missing-argument and unmounted-tree cases instead.

CONDA_BASE="${CONDA_BASE:-/home/ian/miniforge3}"

if [ ! -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
    echo "$(date): cannot find conda at $CONDA_BASE (set CONDA_BASE)" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate base

#
# After conda, so conda's bin stays ahead of the Gamma and GrIMP directories on
# PATH and the console scripts keep resolving to the conda python
#
GAMMAENV="$(dirname "$(readlink -f "$0")")/gammaEnv.sh"
if [ ! -f "$GAMMAENV" ]; then
    echo "$(date): cannot find gammaEnv.sh beside $0" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$GAMMAENV" || exit 1

PROJ="${1:-}"
if [ -z "$PROJ" ]; then
    echo "usage: $0 <projectDir> [extra autoupdateS1 args]" >&2
    exit 2
fi
shift

# Bail cleanly if the (NFS) project tree is not mounted / ready, so a failed
# mount does not turn into a spurious "no data" run.
if [ ! -f "$PROJ/autoupdate.yaml" ]; then
    echo "$(date): no autoupdate.yaml in $PROJ (mount not ready?)" >&2
    exit 1
fi

cd "$PROJ" || exit 1
# autoupdateS1 writes a timestamped per-session log under logs/; the crontab
# line also redirects into logs/cron.log to capture subprocess (searchASF /
# aria2c) output. The shell opens that redirect BEFORE anything runs, so the
# directory must exist up front on a fresh project. Harmless if already present.
mkdir -p logs
exec autoupdateS1 "$@" autoupdate.yaml
