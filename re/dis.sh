#!/bin/sh
# dis.sh <hex start> <hex end>  -- disassemble a range of the stellaris binary
objdump -d -C --no-show-raw-insn --start-address=0x$1 --stop-address=0x$2 /opt/stellaris-headless/game/stellaris | tail -n +7
