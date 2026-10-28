#!/bin/sh
# Run Narratarr as PUID:PGID (like the other *arr images) so files in your
# library belong to you, not root.
set -e

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$NARRATARR_DATA" "$NARRATARR_LIBRARY"
  chown -R "$PUID:$PGID" "$NARRATARR_DATA"
  # Only take over the library folder itself if Docker just created it.
  if [ "$(stat -c %u "$NARRATARR_LIBRARY")" = "0" ]; then
    chown "$PUID:$PGID" "$NARRATARR_LIBRARY"
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- env HOME="$NARRATARR_DATA" "$@"
fi

exec "$@"
