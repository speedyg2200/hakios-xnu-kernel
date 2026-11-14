#!/usr/bin/env python3
"""
Pack a device's UEFI (with the hook that starts the XNU loader) and an optional payload
into an Android boot image: for `fastboot boot` on redwood, for flashing with Odin/Heimdall on
Samsung Exynos phones (a51), whose bootloader has no fastboot.

  redwood: kernel = gzip(BootShim + UEFI FD + ["XNUP" container]) + DTB, header v1
  a51:     kernel = BootShim + UEFI FD + ["XNUP" container], DTB in its own section, header v2

The boot shim copies the FD to its base and the payload to PAYLOAD_BASE.

The UEFI pieces are taken from a Mu-Silicium tree built here (Mu-Silicium/, see uefi/README.md)
or, when there is none, from devices/redwood/prebuilt/.

usage: mkxnuimg.py [--device redwood|a51] [-p payload.bin] [-o out.img]
"""
import argparse, gzip, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MU = os.path.join(HERE, 'Mu-Silicium')
PAYLOAD_MAX = 0x04000000
# per device: where the UEFI runs and the payload goes (both must match the device's UEFI platform), and how
# its bootloader wants the boot image
DEVICES = {
    'redwood': dict(fd_base=0x9FD00000, fd_size=0x200000, payload_base=0xF0000000, gzip=True, append_dtb=True,
                    mkbootimg=['--header_version', '1']),
    # Samsung S-Boot (A515FXXU8HWI1): the layout of the phone's own boot.img; payload window reserved in a51Pkg's
    # memory map (0xF0000000 is the modem's carve-out there)
    'a51': dict(fd_base=0xA0000000, fd_size=0x200000, payload_base=0xC1000000, gzip=False, append_dtb=False,
                mkbootimg=['--header_version', '2', '--base', '0x10000000', '--kernel_offset', '0x00008000',
                           '--ramdisk_offset', '0x01000000', '--tags_offset', '0x00000100', '--dtb_offset', '0x0',
                           '--os_version', '13.0.0', '--os_patch_level', '2023-08']),
}

ap = argparse.ArgumentParser()
ap.add_argument('--device', default='redwood', choices=sorted(DEVICES))
ap.add_argument('-p', '--payload')
ap.add_argument('-o', '--output')
ap.add_argument('--mode', default='RELEASE')
args = ap.parse_args()
DEV = DEVICES[args.device]
FD_BASE, FD_SIZE, PAYLOAD_BASE = DEV['fd_base'], DEV['fd_size'], DEV['payload_base']
assert PAYLOAD_BASE < 1 << 32 and PAYLOAD_BASE & 0xFFFF == 0, 'the shim loads PAYLOAD_BASE with one movz'
args.output = args.output or os.path.join(HERE, f'xnu-{args.device}.img')
PREBUILT = os.path.join(HERE, 'devices', args.device, 'prebuilt')

fd_path = os.path.join(MU, f'Build/{args.device}Pkg/{args.mode}_CLANGPDB/FV/SILICIUM_UEFI.fd')
if os.path.isfile(fd_path):
    shim_dir = os.path.join(MU, 'BootShim')
    for f in ('BootShim.elf', 'BootShim.bin'):
        try: os.unlink(os.path.join(shim_dir, f))
        except FileNotFoundError: pass
    subprocess.check_call(['make', 'REQUIRES_KERNEL_HEADER=1', f'FD_BASE={FD_BASE:#x}', f'FD_SIZE={FD_SIZE:#x}',
                           f'CFLAGS=--target=aarch64-linux-gnu -DPAYLOAD_BASE={PAYLOAD_BASE:#x}'],
                          cwd=shim_dir, stdout=subprocess.DEVNULL)
    parts = {'shim': os.path.join(shim_dir, 'BootShim.bin'), 'fd': fd_path,
             'dtb': os.path.join(MU, f'Resources/DTBs/{args.device}.dtb'), 'ramdisk': os.path.join(MU, 'Resources/ramdisk'),
             'mkbootimg': os.path.join(MU, 'Resources/Scripts/mkbootimg.py')}
else:
    parts = {'shim': os.path.join(PREBUILT, 'BootShim.bin'), 'fd': os.path.join(PREBUILT, 'SILICIUM_UEFI.fd'),
             'dtb': os.path.join(PREBUILT, 'device-tree.dtb'), 'ramdisk': os.path.join(PREBUILT, 'ramdisk'),
             'mkbootimg': os.path.join(PREBUILT, 'mkbootimg.py')}
for name, path in parts.items():
    if not os.path.isfile(path):
        hint = ''
        if name == 'fd':
            hint = ("\n  The UEFI image is not in the repository: it carries the phone vendor's own drivers, which are not\n"
                    "  redistributed here. Build it from your own phone's firmware: uefi/README.md")
        sys.exit(f'mkxnuimg.py: {path} not found{hint}')
shim = open(parts['shim'], 'rb').read()
fd = open(parts['fd'], 'rb').read()
assert len(fd) == FD_SIZE, hex(len(fd))

blob = shim + fd
if args.payload:
    data = open(args.payload, 'rb').read()
    # the payload file is a complete "XNUP" container (see xnuloader/mkpayload.py)
    assert data[:4] == b'XNUP' and len(data) <= PAYLOAD_MAX - 0x1000, 'bad or oversized payload'
    blob += data
    blob += b'\0' * (-len(blob) % 16)
    print(f'payload: {len(data)} bytes, bytesum={sum(data) & 0xffffffff:08x}')

dtb = open(parts['dtb'], 'rb').read()
kernel = args.output + '.kernel~'
body = gzip.compress(blob, 6) if DEV['gzip'] else blob
open(kernel, 'wb').write(body + dtb if DEV['append_dtb'] else body)
extra = [] if DEV['append_dtb'] else ['--dtb', parts['dtb']]
try:
    subprocess.check_call([sys.executable, parts['mkbootimg'], '--kernel', kernel, '--ramdisk', parts['ramdisk'],
                           *DEV['mkbootimg'], *extra, '-o', args.output])
finally:
    os.unlink(kernel)
print(f'{args.output}: {os.path.getsize(args.output)} bytes (shim {len(shim)}, uncompressed {len(blob)})')
