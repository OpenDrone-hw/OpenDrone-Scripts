#!/bin/sh
# Link the OpenDrone multiboard plugin into KiCad's user plugin directory.
# Re-run after `git pull`; a symlink means there is nothing to re-copy.
#   sh kicad/multiboard/install.sh          # KiCad 10
#   sh kicad/multiboard/install.sh 9.0      # another major version
set -e
VER="${1:-10.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$(uname -s)" in
  Darwin) DIR="$HOME/Library/Application Support/kicad/$VER/scripting/plugins" ;;
  Linux)  DIR="$HOME/.local/share/kicad/$VER/scripting/plugins" ;;
  *)      echo "Windows: copy $HERE/opendrone_multiboard to %APPDATA%\\kicad\\$VER\\scripting\\plugins\\ by hand"; exit 1 ;;
esac
mkdir -p "$DIR"
rm -rf "$DIR/opendrone_multiboard"
ln -s "$HERE/opendrone_multiboard" "$DIR/opendrone_multiboard"
echo "linked $DIR/opendrone_multiboard -> $HERE/opendrone_multiboard"
echo "KiCad PCB editor: Tools > External Plugins > Refresh Plugins, then 'Multi-Board Manager'"
