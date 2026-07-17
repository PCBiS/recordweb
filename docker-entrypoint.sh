#!/bin/sh
set -eu

mkdir -p /app/json /recordings/chzzk

for source in /opt/recordweb-defaults/json/*; do
    [ -e "$source" ] || continue
    target="/app/json/$(basename "$source")"
    if [ ! -e "$target" ]; then
        cp -a "$source" "$target"
    fi
done

# The bundled channels use the relative output path "chzzk".
# Redirect it into the persistent recordings volume.
if [ ! -e /app/chzzk ]; then
    ln -s /recordings/chzzk /app/chzzk
fi

exec "$@"
