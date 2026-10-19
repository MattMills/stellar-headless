#!/bin/bash
# winshot.sh BINARY WORKTREE SIZE "x y wait"...: the windowed renderer under Xvfb :79 at SIZE,
# a left click per step ("r x y wait" a right click), a screenshot after each step in
# /root/ngi/winshot/sN.jpg, the log in /root/ngi/winshot/err.txt
set -u
B=$1; WT=$2; SZ=$3; shift 3
O=/root/ngi/winshot
mkdir -p "$O" && rm -f -- "${O:?}"/*
pkill -f "Xvfb :79"; sleep 1
Xvfb :79 -screen 0 "${SZ}x24" >/dev/null 2>&1 &
sleep 2
export DISPLAY=:79
cd "$WT/sim/render" || exit 1
"$B" --game /opt/stellaris --size "$SZ" > "$O/out.txt" 2> "$O/err.txt" &
RP=$!
W=$(xdotool search --sync --onlyvisible --pid $RP | head -1)
sleep 8; xdotool windowmove "$W" 0 0; sleep 1
shot() { xwd -root -silent -out "$O/t.xwd"; python3 - "$O/t.xwd" "$O/$1.jpg" <<'PY'
import struct,sys
from PIL import Image
d=open(sys.argv[1],"rb").read();h=struct.unpack(">25I",d[:100]);w,ht,bpl,nc=h[4],h[5],h[12],h[19];off=h[0]+nc*12
Image.frombuffer("RGBX",(w,ht),d[off:off+bpl*ht],"raw","BGRX",bpl,1).convert("RGB").save(sys.argv[2])
PY
}
shot s0; i=1
for st in "$@"; do
  set -- $st
  btn=1; if [ "$1" = r ]; then btn=3; shift; fi
  xdotool mousemove --window "$W" 5 5; sleep 0.3; xdotool mousemove --window "$W" "$1" "$2"; sleep 0.3; xdotool click $btn
  sleep "${3:-2}"; shot "s$i"; i=$((i+1))
done
kill $RP; wait $RP 2>/dev/null; pkill -f "Xvfb :79"
