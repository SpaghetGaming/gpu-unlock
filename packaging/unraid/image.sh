#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# gpu-unlock: install into the Unraid boot images (bzroot + bzfirmware).
#
# Why this is needed on Unraid: there is no initramfs. A module that must run
# before nvidia binds a card has to be inside the rootfs that /init unpacks
# from bzroot. /boot/extra and /boot/config/modprobe.d are both applied later,
# in the plugin pass, after nvidia already owns the cards -- the unlock is then
# refused with -16 EBUSY on every boot, and rebooting never helps because the
# ordering is deterministic.
#
# Image formats, none of which is what the name suggests:
#   bzroot      microcode newc cpio + padding, then a zstd frame holding the
#               real rootfs cpio. Neither a squashfs nor a bare cpio.
#   bzfirmware  plain squashfs; it is the lowerdir of the /lib/firmware
#               overlay, so request_firmware() blobs must live here.
#   bzmodules   kernel *build* tree only (config, System.map, *.patch) and zero
#               .ko files -- it is what build.sh compiles against. Patching it
#               does nothing.
#
# Unraid ships no squashfs-tools and no python3, so the repack runs in a
# throwaway container, exactly as gpu-unlock-firmware already does.
#
# usage (as root, Docker running, Unraid 7.3+, after 'make install-deb' or
# packaging/unraid/build.sh has produced the .ko):
#   packaging/unraid/image.sh                      patch both images
#   packaging/unraid/image.sh --ko path/gpu_unlock.ko
#   packaging/unraid/image.sh --revert             restore the originals
#   packaging/unraid/image.sh --selftest           rebuild into $STAGE, leaving
#                                                  the real images untouched
set -eu

BOOT=${BOOT:-/boot}
NV=${NV:-610.43.03}
KREL=$(uname -r)
IMG=${IMG:-debian:13}
BKDIR=$BOOT/config/gpu-unlock-images
HERE=$(cd "$(dirname "$0")" && pwd)

KO=${KO:-}
FW=${FW:-$BOOT/config/plugins/gpu-unlock/firmware/$NV}
TARGETS=${TARGETS:-all}
MODE=patch
NEXT=
for a in "$@"; do
  case "$a" in
    --revert) MODE=revert ;;
    --selftest) MODE=selftest ;;
    --ko=*) KO=${a#--ko=} ;;
    --ko) NEXT=1 ;;
    # *) must come last: bash tries case patterns in source order.
    *) if [ -n "${NEXT:-}" ]; then KO=$a; NEXT=; else die "unknown argument: $a"; fi ;;
  esac
done

say() { echo "gpu-unlock: $*"; }
die() { say "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"

# ---- revert -------------------------------------------------------------
if [ "$MODE" = revert ]; then
  [ -d "$BKDIR" ] || die "no backup at $BKDIR"
  for f in bzroot bzroot.sha256 bzfirmware bzfirmware.sha256; do
    if [ -f "$BKDIR/$f" ]; then
      cp -f "$BKDIR/$f" "$BOOT/$f"
      say "restored $f"
    fi
  done
  say "power cycle (not reboot) to drop the unlock"
  exit 0
fi

# ---- preconditions ------------------------------------------------------
command -v docker >/dev/null || die "docker is required"
docker info >/dev/null 2>&1 || die "start Docker, then run image.sh"
[ -f "$BOOT/bzroot" ] || die "no $BOOT/bzroot"
[ -f "$HERE/image-core.py" ] || die "image-core.py not found next to image.sh"
case "$KREL" in *-Unraid) ;; *) die "not an Unraid kernel: $KREL" ;; esac

# find the module: an explicit path, or the package the plugin/build.sh made
if [ -z "$KO" ]; then
  for c in /boot/extra/gpu-unlock-*.txz /usr/src/gpu-unlock-*/gpu_unlock.ko; do
    [ -e "$c" ] && KO=$c && break
  done
fi
[ -n "$KO" ] && [ -e "$KO" ] || die "cannot find gpu_unlock.ko; pass --ko=<path or .txz>"

# --selftest must not disturb the real images, so it gets its own stage dir.
STAGE=${STAGE:-/boot/gpu-unlock-stage}
[ "$MODE" = selftest ] && STAGE=${STAGE}-selftest
# `docker -v` sources are resolved on the Docker host. When this runs on the
# Unraid host these equal the paths above; they only differ when the script is
# driven from inside a container that sees /boot under another name.
STAGE_HOST=${STAGE_HOST:-$STAGE}
BOOT_HOST=${BOOT_HOST:-$BOOT}
rm -rf "$STAGE"; mkdir -p "$STAGE/pkg" "$STAGE/in"

case "$KO" in
  *.txz) tar -xJf "$KO" -C "$STAGE/pkg"
         KO=$STAGE/pkg/lib/modules/$KREL/extra/gpu_unlock.ko ;;
esac
[ -f "$KO" ] || die "gpu_unlock.ko not found ($KO)"
modinfo -F vermagic "$KO" | grep -q "^$KREL " ||
  die "vermagic of $KO does not match the running kernel $KREL"
cp -f "$KO" "$STAGE/gpu_unlock.ko"

[ -d "$FW" ] || die "no firmware in $FW; run gpu-unlock-firmware first"
cp -a "$FW" "$STAGE/$NV"

# Prefer the conf shipped with the repo; generate one only when running from a
# bare checkout. Keeps the packaged file and the fallback in agreement.
if [ -f "$HERE/modprobe-gpu-unlock.conf" ]; then
  sed "s/@TARGETS@/$TARGETS/" "$HERE/modprobe-gpu-unlock.conf" > "$STAGE/gpu-unlock.conf"
else
  cat > "$STAGE/gpu-unlock.conf" <<EOF
# gpu-unlock: shipped in the rootfs so the module exists before nvidia binds.
# softdep makes kmod load gpu_unlock first, while the cards are still unbound.
# No blacklist nvidia here: on Unraid the driver is loaded by an explicit
# \`modprobe nvidia\` from the Nvidia Driver plugin, which blacklist does not
# block, and nvidia.ko is absent from the rootfs at coldplug time anyway.
softdep nvidia pre: gpu_unlock
options gpu_unlock targets=$TARGETS
EOF
fi
cp -f "$HERE/image-core.py" "$STAGE/"

# ---- back up, then rebuild from pristine so reruns are idempotent -------
if [ "$MODE" = patch ]; then
  mkdir -p "$BKDIR"
  for f in bzroot bzroot.sha256 bzfirmware bzfirmware.sha256; do
    [ -e "$BOOT/$f" ] && [ ! -e "$BKDIR/$f" ] && cp -a "$BOOT/$f" "$BKDIR/$f"
  done
  say "originals kept in $BKDIR (revert: image.sh --revert)"
  TARGET=$BOOT
else
  TARGET=$STAGE/testboot; mkdir -p "$TARGET"  # inside $STAGE, which is kept
  SRCDIR=$BOOT
  [ -f "$BKDIR/bzroot" ] && SRCDIR=$BKDIR
  for f in bzroot bzfirmware; do
    src="$SRCDIR/$f"
    [ -f "$src" ] || src="$BOOT/$f"
    cp -f "$src" "$TARGET/$f"
  done
  # Name the directory the inputs actually came from: on a box patched by hand
  # there is no backup dir, so these are the live (already patched) images.
  say "selftest: inputs from $SRCDIR, output in $TARGET"
fi

# Free space has to cover the scratch copy plus every image as both .new and
# final, so the images are counted twice, plus a margin.
need=$(( $(stat -c %s "$TARGET/bzroot") * 2 + $(stat -c %s "$TARGET/bzfirmware") * 2 + 67108864 ))
free=$(( $(df -k "$TARGET" | awk 'NR==2{print $4}') * 1024 ))
[ "$free" -gt "$need" ] || die "need ~$((need/1048576)) MB free on $TARGET, have $((free/1048576)) MB"

echo "gpu-unlock: patching $TARGET/bzroot and $TARGET/bzfirmware"
docker run --rm \
  -v "$STAGE_HOST:/in:ro" \
  -v "$( [ "$MODE" = patch ] && echo "$BOOT_HOST" || echo "$STAGE_HOST/testboot" ):/out" \
  "$IMG" bash -euo pipefail -c '
    apt-get -qq update -q >/dev/null 2>&1
    apt-get -qq install -y -q --no-install-recommends \
      squashfs-tools cpio zstd kmod python3 xz-utils >/dev/null 2>&1
    python3 /in/image-core.py \
      --boot /out \
      --ko /in/gpu_unlock.ko \
      --conf /in/gpu-unlock.conf \
      --krel '"$KREL"' \
      --fwdir /in/'"$NV"' --nv '"$NV"

# the patched bzroot must be re-read by /init at boot, and rc.S verifies the
# checksum, so the image has to sit at the flash root -- /out is already it.
if [ "$MODE" = selftest ]; then
  say "selftest OK; patched images kept in $TARGET"
  say "nothing under $BOOT was modified"
  say "inputs came from $SRCDIR"
  [ -f "$BKDIR/bzroot" ] ||
    say "note: no $BKDIR, so this run re-patched the live images rather than
         starting from unpatched ones -- idempotency, not a clean patch"
  # keep the stage: the artifacts just reported live inside it
  exit 0
fi

say "images patched; power cycle (not reboot) to apply"
rm -rf "$STAGE"
