#!/bin/sh
set -eu

# A local launcher is optional and supplied outside the public build context.
if [ -n "${RECORDWEB_LOCAL_LAUNCHER:-}" ] && [ "$#" -ge 2 ]; then
    case "$1:$2" in
        python:recordWEB.py|python3:recordWEB.py)
            shift 2
            set -- python "$RECORDWEB_LOCAL_LAUNCHER" "$@"
            ;;
    esac
fi

# Preserve the existing default-data and recording symlink initialization.
exec /app/docker-entrypoint.sh python /opt/recordweb-docker/docker_support/supervisor.py -- "$@"
