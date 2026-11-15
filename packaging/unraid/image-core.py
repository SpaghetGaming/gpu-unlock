#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Rebuild bzroot and bzfirmware with gpu_unlock baked in.

Runs inside the container (needs: cpio, zstd, squashfs-tools, kmod, md5sum).
Everything is written to <boot>/*.new and verified BEFORE replacing the real
images, so a failure leaves the system exactly as it was.

bzroot = microcode newc cpio (kept byte-verbatim) + zstd frame -> rootfs cpio.
bzfirmware = plain squashfs, lowerdir of the /lib/firmware overlay.
"""
import argparse, hashlib, os, subprocess, sys, tempfile

ZSTD = b"\x28\xb5\x2f\xfd"


def sh(cmd, **kw):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                       text=True, **kw)
    if r.returncode:
        sys.exit(f"FAILED: {cmd}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def cpio_scan(buf, want=None):
    """Yield (name, mode, filesize, data_offset) for a newc stream.

    newc header is 110 bytes: magic(6) + 13 contiguous 8-hex fields.
    filesize is field 6, namesize field 11 -- field 12 is chksum."""
    off = 0
    while buf[off:off + 6] == b"070701":
        h = buf[off + 6:off + 110]
        if len(h) < 104:
            return
        v = [int(h[i * 8:(i + 1) * 8], 16) for i in range(13)]
        mode, fs, ns = v[1], v[6], v[11]
        name = buf[off + 110:off + 110 + ns - 1].decode("ascii", "replace")
        nh = off + 110 + ns
        nh += (-nh) % 4
        if name == "TRAILER!!!":
            yield (name, mode, 0, off, nh)
            return
        yield (name, mode, fs, off, nh)
        off = nh + fs + ((-fs) % 4)


def cpio_end(buf):
    last = None
    for name, mode, fs, off, nh in cpio_scan(buf):
        last = (name, off)
    if not last or last[0] != "TRAILER!!!":
        sys.exit("not a complete newc cpio stream")
    name, off = last
    h = buf[off + 6:off + 110]
    ns = int(h[11 * 8:12 * 8], 16)
    end = off + 110 + ns
    return end + (-end) % 4


def split_bzroot(path):
    d = open(path, "rb").read()
    z = cpio_end(d)
    while d[z:z + 4] != ZSTD:
        z += 1
        if z > len(d):
            sys.exit("no zstd frame follows the microcode cpio")
    return d[:z], d[z:]


def repack_bzroot(boot, ko, conf, krel):
    bz = os.path.join(boot, "bzroot")
    prefix, frame = split_bzroot(bz)
    print(f"  bzroot: prefix {len(prefix)} bytes (kept verbatim), "
          f"zstd frame {len(frame)}")

    td = tempfile.mkdtemp()
    open(f"{td}/r.zst", "wb").write(frame)
    sh(f"zstd -qt {td}/r.zst")
    sh(f"zstd -qdc {td}/r.zst > {td}/rootfs.cpio")
    orig_size = os.path.getsize(f"{td}/rootfs.cpio")

    os.makedirs(f"{td}/rt")
    sh(f"cpio -idm --no-absolute-filenames -D {td}/rt < {td}/rootfs.cpio")

    md = f"{td}/rt/lib/modules/{krel}"
    if not os.path.isdir(f"{md}/kernel"):
        sys.exit(f"extracted rootfs has no {md}/kernel - wrong image?")

    # snapshot the shipped modules.dep so we can prove the change is additive
    buf = open(f"{td}/rootfs.cpio", "rb").read()
    dep_before = None
    for name, mode, fs, off, nh in cpio_scan(buf):
        if name == f"lib/modules/{krel}/modules.dep":
            dep_before = set(l for l in buf[nh:nh + fs].decode(errors="replace")
                             .split("\n") if l.strip())
    if dep_before is None:
        sys.exit("shipped rootfs has no modules.dep")

    sh(f"install -D -m 0644 {ko} {md}/extra/gpu_unlock.ko")
    # install, not cp: /etc/modprobe.d must stay world-readable or kmod may
    # skip the file when the images are unpacked as a non-root context.
    sh(f"mkdir -p {td}/rt/etc/modprobe.d && "
       f"install -D -m 0644 {conf} {td}/rt/etc/modprobe.d/gpu-unlock.conf")
    sh(f"depmod -b {td}/rt -a {krel}")

    dep_after = set(l for l in open(f"{md}/modules.dep").read().split("\n") if l.strip())
    removed, added = dep_before - dep_after, dep_after - dep_before
    print(f"  modules.dep: {len(dep_before)} -> {len(dep_after)} entries, "
          f"added={sorted(added)} removed={sorted(removed)}")
    if removed:
        sys.exit(f"depmod dropped {len(removed)} shipped dependencies - aborting")
    # The safety property is "nothing shipped was lost". Membership is checked in
    # the result rather than in `added`, so re-running against an image that
    # already carries the module is a normal rerun instead of an abort.
    if "extra/gpu_unlock.ko:" not in dep_after:
        sys.exit("gpu_unlock.ko is not in the regenerated modules.dep")
    if "extra/gpu_unlock.ko:" in dep_before:
        print("  note: gpu_unlock.ko was already listed in modules.dep; this "
              "image has been patched before")

    sh(f"cd {td}/rt && find . | cpio -o -H newc --quiet --owner=0:0 > {td}/new.cpio")
    sh(f"zstd -q -19 --ultra -22 --long=27 -T0 -c {td}/new.cpio > {td}/new.cpio.zst")
    out = bz + ".new"
    with open(out, "wb") as f:
        f.write(prefix)
        f.write(open(f"{td}/new.cpio.zst", "rb").read())

    # verify the candidate independently of what we just wrote
    p2, f2 = split_bzroot(out)
    if p2 != prefix:
        sys.exit("candidate: microcode prefix is not byte-identical")
    open(f"{td}/v.zst", "wb").write(f2)
    sh(f"zstd -qdf {td}/v.zst -o {td}/v.cpio")
    names = [n for n, *_ in cpio_scan(open(f"{td}/v.cpio", "rb").read())]
    need = [f"lib/modules/{krel}/extra/gpu_unlock.ko",
            "etc/modprobe.d/gpu-unlock.conf",
            f"lib/modules/{krel}/modules.dep", "sbin/init"]
    for n in need:
        if n not in names:
            sys.exit(f"candidate rootfs is missing {n}")
    print(f"  bzroot.new {os.path.getsize(out)} bytes (was {os.path.getsize(bz)}), "
          f"{len(names)} members, all required files present")
    return out


def repack_bzfirmware(boot, fwdir, nv):
    bz = os.path.join(boot, "bzfirmware")
    if not os.path.exists(bz):
        print("  no bzfirmware, skipped")
        return None
    sup = sh(f"unsquashfs -s {bz}")
    comp = next((l.split()[1] for l in sup.split("\n") if "Compression" in l), "zstd")
    bsize = next((l.split()[2] for l in sup.split("\n") if "Block size" in l), "131072")
    print(f"  bzfirmware: comp={comp} block={bsize}")

    td = tempfile.mkdtemp()
    sh(f"unsquashfs -q -no-xattrs -d {td}/src {bz}")
    src = f"{td}/src"
    # with -d, unsquashfs writes the archive root DIRECTLY into src (the
    # 'squashfs-root/' shown by -l is synthetic and must not be preserved)
    if os.path.isdir(f"{src}/squashfs-root"):
        for e in os.listdir(f"{src}/squashfs-root"):
            os.replace(f"{src}/squashfs-root/{e}", f"{src}/{e}")
        os.rmdir(f"{src}/squashfs-root")
    if not os.path.isdir(f"{src}/nvidia"):
        sys.exit("extracted bzfirmware has no nvidia/ - wrong layout")

    before = int(sh(f"unsquashfs -l {bz} | grep -c /").strip())
    sh(f"mkdir -p {src}/gpu-unlock && cp -a {fwdir} {src}/gpu-unlock/")
    out = bz + ".new"
    # NB: -processors must be >= 1; omitting it already means "all CPUs".
    sh(f"mksquashfs {src} {out} -comp {comp} -b {bsize} -noappend")

    got = sh(f"unsquashfs -l {out}").split("\n")
    n_gu = len([l for l in got if f"gpu-unlock/{nv}" in l])
    n_nv = len([l for l in got if "/nvidia/" in l])
    if n_gu < 10:
        sys.exit(f"new bzfirmware has only {n_gu} gpu-unlock entries")
    if n_nv < before // 4:
        sys.exit(f"new bzfirmware lost nvidia firmware ({n_nv} entries)")
    print(f"  bzfirmware.new {os.path.getsize(out)} bytes (was {os.path.getsize(bz)}), "
          f"{n_gu} gpu-unlock + {n_nv} nvidia entries")
    return out


def commit(boot, news):
    for f in news:
        if f:
            os.replace(f, f[:-4])
            d = hashlib.sha256(open(os.path.join(boot, os.path.basename(f)[:-4]), "rb").read()).hexdigest()
            open(os.path.join(boot, os.path.basename(f).replace(".new", ".sha256")), "w").write(d + "\n")
    for f in news:
        if f:
            base = os.path.basename(f)[:-4]
            got = sh(f"sha256sum {os.path.join(boot, base)}").split()[0]
            want = open(os.path.join(boot, base + ".sha256")).read().strip()
            assert got == want, f"{base}: sha256 file mismatch"
            n = os.path.getsize(os.path.join(boot, base + ".sha256"))
            if n != 65:
                sys.exit(f"{base}.sha256 is {n} bytes, rc.S requires exactly 65")
            print(f"  committed {base} ({os.path.getsize(os.path.join(boot, base))} B), "
                  f"sha256 file {n} bytes, verified")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", default="/boot")
    ap.add_argument("--ko", required=True)
    ap.add_argument("--conf", required=True)
    ap.add_argument("--krel", required=True)
    ap.add_argument("--fwdir", required=True)
    ap.add_argument("--nv", default="610.43.03")
    a = ap.parse_args()

    print(f"gpu-unlock: patching boot images for {a.krel}")
    news = [repack_bzroot(a.boot, a.ko, a.conf, a.krel),
            repack_bzfirmware(a.boot, a.fwdir, a.nv)]
    commit(a.boot, news)
    print("gpu-unlock: done")


if __name__ == "__main__":
    main()
