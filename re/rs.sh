#!/bin/sh
# rs.sh ADDR... -- the C strings (or floats with -f) at addresses of the stellaris binary
f=s; [ "$1" = -f ] && { f=f; shift; }
args=""; for a in "$@"; do args="$args -ex \"x/$f 0x$a\""; done
eval gdb -batch -nx $args /opt/stellaris-headless/game/stellaris 2>/dev/null
