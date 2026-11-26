# SPDX-License-Identifier: GPL-3.0-or-later
"""Drivers: the hardware of this computer, the driver each part uses, what could be better, and what is installed
for hardware this computer does not have.

Everything here only reads, as the user, and runs in a worker thread (no Qt): lspci, lsusb and sysfs, CUPS (lpstat,
lpoptions), Secure Boot (mokutil or the EFI variable), /proc, modinfo, the rpm database and one zypper process
(backend.run_c). A proposal becomes a change only when the page puts it in the queue.

The rpm database is read so that the window stays light on an older computer: one 'rpm -qa' read line by line, which
keeps the name, size and vendor of every package but the capabilities only of the packages that could go and of the
firmware (read_rpm); then a few rpm -q of those packages: their files, which packages require them (--whatrequires)
and which installed packages provide the few capabilities the rules ask about (--whatprovides). One rpm process at a
time.

What openSUSE Tumbleweed does today (checked on 26/11/2026):
- NVIDIA, Turing and newer (device id from 0x1E00): the open kernel module "nvidia-open-driver-G07-signed-kmp-<flavour>"
  is in openSUSE's main repository (repo-oss), signed with the openSUSE Secure Boot key: it loads with Secure Boot on,
  with nothing to enroll. The rest (nvidia-video-G07, nvidia-gl-G07, nvidia-compute-utils-G07…) is proprietary and
  comes from NVIDIA's repository https://download.nvidia.com/opensuse/tumbleweed (signed with the key of the "NVIDIA
  Linux Driver Team", fingerprint below). "nvidia-open-driver-G07-signed-kmp-meta" and "nvidia-userspace-meta-G07"
  (NVIDIA's repository) keep the two halves at the same version. openSUSE's package
  openSUSE-repos-Tumbleweed-NVIDIA adds the same repository as a service (alias repo-non-free); it turns a repository
  file called NVIDIA.repo, as the wiki told people to add, into that service.
- NVIDIA, Maxwell, Pascal and Volta: the last series for them is 580, "nvidia-driver-G06-kmp-<flavour>" with
  "nvidia-driver-G06-kmp-meta", all from NVIDIA's repository. That kernel module is built on the computer when it is
  installed (it requires gcc, make and kernel-<flavour>-devel) and signed with a key made there; with Secure Boot on,
  the key is enrolled at the next start (MokManager, with the root password; its scripts say "Please enroll it using
  your root password. (It will use US keymap.)").
- NVIDIA, Kepler (470, G05) and Fermi (390, G04): still built in NVIDIA's repository (nvidia-gfxG05-kmp,
  x11-video-nvidiaG05…), the same way as G06, but NVIDIA no longer updates them.
- Which card goes with which series comes from the modalias supplements of NVIDIA's own packages (NV_SERIES).
- Broadcom: "broadcom-wl" and "broadcom-wl-kmp-<flavour>" are in openSUSE's non-free repository (repo-non-oss, since
  November 2025), signed with the openSUSE Secure Boot key. broadcom-wl blacklists b43, b43legacy, bcm43xx, bcma,
  brcm80211, brcmfmac, brcmsmac and ssb: it is only proposed for cards no free driver supports (WL_ONLY), and never when
  another device uses one of those drivers.
"""

import bisect
import fnmatch
import os
import re
import shlex
import subprocess
import threading

from . import backend
from .backend import ENV_C
from .i18n import _, ngettext

SYS = "/sys"
LOCKS_FILE = "/etc/zypp/locks"
REPOS_D = "/etc/zypp/repos.d"
FIRMWARE_DIRS = ("/usr/lib/firmware", "/lib/firmware")
SB_VAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
TIMEOUT = 30

NVIDIA, BROADCOM, INTEL, AMD = "10de", "14e4", "8086", "1002"

# ---------------------------------------------------------------------------------------------- NVIDIA

NVIDIA_KEY_FPR = "2FB03195DECD49492BD1C17AB1D0D788DB27FD5A"  # NVIDIA Linux Driver Team, RSA 4096, 2022
NVIDIA_URL = "https://download.nvidia.com/opensuse/{dist}"
NVIDIA_ALIAS = "NVIDIA"  # the name the wiki gave the repository (openSUSE-repos-*-NVIDIA knows it)
# Device ids of the series before G07, from the modalias() supplements of nvidia-driver-G06-kmp-meta (580),
# nvidia-gfxG05-kmp-default (470) and nvidia-gfxG04-kmp-default (390) in NVIDIA's repository for Tumbleweed
# (26/11/2026). Every card from 0x1E00 on (Turing and newer) is in nvidia-open-driver-G07-signed-kmp-meta.
NV_SERIES = {
    "G06": "1340-1341 1344 1346-1349 134b 134d-134f 137a-137b 1380-1382 1390-1393 1398-139d 13b0-13b4 13b6 13b9-13bc "
           "13c0 13c2 13d7-13da 13f0-13f3 13f8-13fb 1401-1402 1406-1407 1427 1430-1431 1436 15f0 15f7-15f9 1617-161a "
           "1667 174d-174e 179c 17c2 17c8 17f0-17f1 17fd 1b00 1b02 1b06 1b30 1b38 1b80-1b84 1b87 1ba0-1ba2 1bb0-1bb1 "
           "1bb3-1bb9 1bbb 1bc7 1be0-1be1 1c02-1c04 1c06-1c07 1c09 1c20-1c23 1c30-1c31 1c60-1c62 1c81-1c83 1c8c-1c8d "
           "1c8f-1c92 1c94 1c96 1cb1-1cb3 1cb6 1cba-1cbd 1cfa-1cfb 1d01-1d02 1d10-1d13 1d16 1d33-1d34 1d52 1d81 1db1 "
           "1db3-1db8 1dba 1df0 1df2 1df6",
    "G05": "0fc0-0fc2 0fc6 0fc8-0fc9 0fd2 0fe3 0ff3 0ff9-0ffa 0ffd-0fff 1001 1004-1005 1007-1008 100a 100c 103a 103c "
           "1180 1183-1185 1187-118a 118e 1193 1195 1199 11b4 11ba 11c0 11c2-11c6 11c8 11cb 11e3 11fa 1280-1282 1284 "
           "1286-1289 128b 1290 1292 1295 1299 137d",
    "G04": "06c0 06c4 06ca 06cd 06d8-06da 06dc-06dd 0dc0 0dc4-0dc6 0dcd-0dce 0dd1-0dd3 0dd6 0dd8 0dda 0de0-0de5 "
           "0de7-0dfa 0dfc 0e22-0e24 0e30-0e31 0e3a-0e3b 0f00-0f03 1040 1042 1048-104c 1050-1052 1054-105b 107c-107d "
           "1080-1082 1084 1086-1089 108b 109a-109b 1140 1200-1201 1203 1205-1208 1210-1213 1241 1243-1249 124b 124d "
           "1251",
}
TURING = 0x1E00
# what each series installs: (package, from NVIDIA's repository); {flavor} is the kernel's flavour
SERIES = {
    "G07": dict(version="620", compiled=False, legacy=False, flavors=("default", "longterm", "64kb"),
                packages=(("nvidia-open-driver-G07-signed-kmp-meta", True),
                          ("nvidia-open-driver-G07-signed-kmp-{flavor}", False),
                          ("nvidia-userspace-meta-G07", True))),
    "G06": dict(version="580", compiled=True, legacy=False, flavors=("default", "longterm", "64kb"),
                packages=(("nvidia-driver-G06-kmp-meta", True), ("nvidia-driver-G06-kmp-{flavor}", True))),
    "G05": dict(version="470", compiled=True, legacy=True, flavors=("default", "longterm"),
                packages=(("nvidia-gfxG05-kmp-{flavor}", True), ("x11-video-nvidiaG05", True),
                          ("nvidia-glG05", True))),
    "G04": dict(version="390", compiled=True, legacy=True, flavors=("default",),
                packages=(("nvidia-gfxG04-kmp-{flavor}", True), ("x11-video-nvidiaG04", True),
                          ("nvidia-glG04", True))),
}
# the distributions whose NVIDIA repository Zypp Desk knows (Slowroll and MicroOS use Tumbleweed's)
NVIDIA_DISTS = {"opensuse-tumbleweed": "tumbleweed", "opensuse-slowroll": "tumbleweed"}
NVIDIA_KMP = re.compile(r"^(nvidia-open-driver-G0\d(-signed)?(-cuda)?-kmp-|nvidia-driver-G0\d-kmp-|"
                        r"nvidia-gfxG0\d-kmp-)")

# ---------------------------------------------------------------------------------------------- Broadcom

# cards that only Broadcom's wl driver supports (Debian's and Arch's lists of the wl driver agree on these)
WL_ONLY = {"43a0": "BCM4360", "4360": "BCM4360", "43b1": "BCM4352", "4365": "BCM43142"}
# cards wl supports that b43 may also handle: wl only when no driver made a network interface for them
WL_TOO = {"4358": "BCM43227", "4359": "BCM43228"}
# what broadcom-wl's /usr/lib/modprobe.d/broadcom-wl.conf blacklists (b44 Ethernet needs ssb)
WL_BLACKLIST = {"b43", "b43legacy", "bcm43xx", "bcma", "bcma-pci-bridge", "brcm80211", "brcmfmac", "brcmsmac",
                "ssb", "b44"}

# ---------------------------------------------------------------------------------------------- names

PCI_VENDORS = {"8086": "Intel", "1002": "AMD", "1022": "AMD", "10de": "NVIDIA", "10ec": "Realtek",
               "14e4": "Broadcom", "168c": "Qualcomm Atheros", "17cb": "Qualcomm", "14c3": "MediaTek",
               "15ad": "VMware", "1af4": "Red Hat", "1b36": "Red Hat", "80ee": "VirtualBox", "1234": "QEMU",
               "1414": "Microsoft", "102b": "Matrox", "1a03": "ASPEED", "11ab": "Marvell", "1b4b": "Marvell",
               "1969": "Qualcomm Atheros", "1814": "Ralink", "1106": "VIA", "1039": "SiS", "5333": "S3"}
USB_VENDORS = {"8087": "Intel", "8086": "Intel", "0bda": "Realtek", "0e8d": "MediaTek", "0cf3": "Qualcomm Atheros",
               "0a5c": "Broadcom", "13d3": "IMC Networks", "04ca": "Lite-On", "0489": "Foxconn", "148f": "Ralink",
               "2357": "TP-Link", "0846": "Netgear", "7392": "Edimax", "03f0": "HP", "17ef": "Lenovo",
               "413c": "Dell", "046d": "Logitech", "04f2": "Chicony", "04f9": "Brother", "04b8": "Epson",
               "04a9": "Canon", "04e8": "Samsung", "0b0e": "Jabra", "0a12": "Cambridge Silicon Radio"}
# the USB vendor of a Wi-Fi chip's Bluetooth half: one card "Wi-Fi and Bluetooth"
BT_TWIN = {"8086": {"8087", "8086"}, "10ec": {"0bda"}, "14c3": {"0e8d", "13d3", "0489"},
           "17cb": {"0cf3", "0489", "13d3"}, "168c": {"0cf3", "0489", "13d3", "04ca"},
           "14e4": {"0a5c", "0489", "13d3"}}
PRINTER_VENDORS = ("hp", "hewlett", "brother", "epson", "canon", "samsung", "xerox", "kyocera", "ricoh", "lexmark",
                   "oki", "konica", "minolta", "dell", "sharp", "pantum")
USB_PRINTER_VENDORS = {"03f0": "hp", "04f9": "brother", "04b8": "epson", "04a9": "canon", "04e8": "samsung",
                       "0924": "xerox", "0482": "kyocera", "05ca": "ricoh", "043d": "lexmark", "06bc": "oki",
                       "132b": "konica"}
PRINTERISH = re.compile(r"print|scan|mfp|all-in-one|officejet|deskjet|laserjet|envy|pixma|ecotank|workforce|"
                        r"perfection|\bdcp|\bmfc|\bhl-", re.I)

# ---------------------------------------------------------------------------------------------- not needed

# graphics packages and the PCI vendors of the cards they are for (display devices, class 03)
GRAPHICS_FOR = {
    "xf86-video-amdgpu": {AMD}, "xf86-video-ati": {AMD}, "xf86-video-r128": {AMD}, "xf86-video-mach64": {AMD},
    "xf86-video-nouveau": {NVIDIA}, "xf86-video-nv": {NVIDIA}, "xf86-video-intel": {INTEL},
    "xf86-video-vmware": {"15ad"}, "xf86-video-qxl": {"1b36"}, "xf86-video-mga": {"102b"},
    "xf86-video-openchrome": {"1106"}, "xf86-video-savage": {"5333"}, "xf86-video-sis": {"1039"},
    "xf86-video-siliconmotion": {"126f"}, "xf86-video-tdfx": {"121a"}, "xf86-video-voodoo": {"121a"},
    "xf86-video-neomagic": {"10c8"}, "xf86-video-i128": {"105d"}, "xf86-video-chips": {"102c"},
    "xf86-video-ark": {"edd8"}, "xf86-video-ast": {"1a03"},
    "libvulkan_radeon": {AMD}, "libvulkan_intel": {INTEL}, "libvulkan_intel_hasvk": {INTEL},
    "libvulkan_nouveau": {NVIDIA}, "libvulkan_virtio": {"1af4"},
    "libvdpau_radeonsi": {AMD}, "libvdpau_r600": {AMD}, "libvdpau_r300": {AMD}, "libvdpau_nouveau": {NVIDIA},
    "libvdpau_virtio_gpu": {"1af4"},
    "Mesa-libva": {AMD, NVIDIA, "1af4"}, "mesa-libva": {AMD, NVIDIA, "1af4"},
    "Mesa-dri-nouveau": {NVIDIA},
    "intel-media-driver": {INTEL}, "libva-intel-driver": {INTEL}, "libmfx1": {INTEL}, "libmfx-gen1_2": {INTEL},
    "libmfx-gen1": {INTEL}, "libvpl2": {INTEL}, "intel-vaapi-driver": {INTEL},
}
NVIDIA_USERSPACE = re.compile(r"^(nvidia-(video|gl|compute|compute-utils|common|utils|drivers|drivers-minimal|"
                              r"userspace-meta)-?G0\d|nvidia-glG0\d|nvidia-computeG0\d|x11-video-nvidiaG0\d|"
                              r"nvidia-open-driver-G0\d|nvidia-driver-G0\d|nvidia-gfxG0\d|nvidia-libXNVCtrl$|"
                              r"nvidia-settings$|nvidia-persistenced$|nvidia-modprobe$|nvidia-xconfig$|"
                              r"kernel-firmware-nvidia-gspx-G0\d)")
VM_PACKAGES = {
    "open-vm-tools": _("VMware's tools for virtual machines"),
    "open-vm-tools-desktop": _("VMware's tools for virtual machines"),
    "open-vm-tools-sdmp": _("VMware's tools for virtual machines"),
    "virtualbox-guest-tools": _("VirtualBox's tools for virtual machines"),
    "virtualbox-guest-x11": _("VirtualBox's tools for virtual machines"),
    "virtualbox-guest-desktop-icons": _("VirtualBox's tools for virtual machines"),
    "qemu-guest-agent": _("QEMU's agent for virtual machines"),
    "spice-vdagent": _("the SPICE agent of virtual machines"),
    "hyper-v": _("Microsoft Hyper-V's tools for virtual machines"),
    "xf86-input-vmmouse": _("VMware's mouse"),
    "xen-tools-domU": _("Xen's tools for virtual machines"),
}
WIRELESS_PACKAGES = {
    "b43-fwcutter": _("old Broadcom Wi-Fi cards that use the b43 driver"),
    "broadcom-wl": _("Broadcom's own Wi-Fi driver"),
    "ipw-firmware": _("old Intel PRO/Wireless 2100, 2200 and 2915 Wi-Fi cards"),
    "bcm43xx-firmware": _("Broadcom Wi-Fi of small boards like the Raspberry Pi"),
    "atmel-firmware": _("old Atmel Wi-Fi adapters"),
    "zd1211-firmware": _("old ZyDAS Wi-Fi adapters"),
}
# printer and scanner drivers: (vendors they are for, words in a printer's model that show it uses them)
PRINTER_PACKAGES = (
    (re.compile(r"^hplip"), ("hp", "hewlett"), ("hpcups", "hpijs", "hplip", "hpps")),
    (re.compile(r"^gutenprint"), (), ("gutenprint",)),
    (re.compile(r"^splix$"), ("samsung", "xerox", "dell"), ("splix",)),
    (re.compile(r"^foo2zjs"), ("hp", "hewlett", "konica", "minolta", "samsung", "xerox", "oki", "lexmark", "dell"),
     ("foo2",)),
    (re.compile(r"^epson-inkjet-printer-escpr"), ("epson",), ("escpr", "epson inkjet")),
    (re.compile(r"^OpenPrintingPPDs-(ghostscript|hpijs|postscript)$"), (), ("foomatic", "openprinting", "hpijs",
                                                                           "postscript")),
    (re.compile(r"^printer-driver-brlaser$"), ("brother",), ("brlaser",)),
    (re.compile(r"^(iscan|epsonscan2|imagescan)"), ("epson",), ()),
    (re.compile(r"^brscan"), ("brother",), ()),
)
VENDOR_OF_PACKAGER = (("brother", "brother"), ("seiko epson", "epson"), ("canon", "canon"))
GROUP_ORDER = ("firmware", "microcode", "graphics", "wireless", "vm", "later", "printers")
FIRMWARE_ALL = "kernel-firmware-all"


# ============================================================================================== reading

def _run(args, timeout=TIMEOUT):
    """The output of a read-only command, '' when it cannot run (C locale: the output is parsed)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace", env=ENV_C, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _which(name):
    for d in ("/usr/bin", "/usr/sbin", "/bin", "/sbin"):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def collect(settings=None):
    """Everything the page shows, read now (a worker thread): the raw facts, turned into hw by build(). The slow
    ones (rpm, zypper, CUPS, modinfo: 2 to 3 s each) run side by side."""
    from concurrent.futures import ThreadPoolExecutor
    lspci = _run([_which("lspci") or "lspci", "-vmmnnk"])
    modules = loaded_modules()
    uname = os.uname()
    early = build(dict(lspci=lspci, uname=uname))  # enough to know what to ask zypper about
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_rpm = pool.submit(_rpm_facts)
        f_zypper = pool.submit(zypper_info, info_names(early))
        f_printers = pool.submit(printers)
        f_modfw = pool.submit(module_firmware, modules)
        raw = dict(
            lspci=lspci, pci_sys=[] if lspci.strip() else pci_from_sysfs(), lsusb=_run([_which("lsusb") or "lsusb"]),
            usb=usb_devices(), secure_boot=secure_boot(),
            uname=uname, os_release=_read("/etc/os-release"), cpuinfo=_cpu_vendor_line(), virt=detect_virt(),
            aliases=device_aliases(), pci_net=pci_net(), modules=modules, repos=repos(), locks=_read(LOCKS_FILE),
            journal=kernel_firmware_log(), protected=_protected(settings))
        installed, extra, files = f_rpm.result()
        raw.update(rpm=installed, rpm_extra=extra, files=files, provided=(), zypper_info=f_zypper.result(),
                   printers=f_printers.result(), module_firmware=f_modfw.result())
    hw = build(raw)
    hw["provided"] = provided_names(hw)
    return hw


def _rpm_facts():
    """(installed, extra, files of the packages that could go): read_rpm, then one rpm -q of the files of the packages
    that could go (a required file, a kernel module that is loaded), in this thread: one rpm process at a time."""
    installed, extra = read_rpm()
    names = [n for n, p in installed.items() if classify(n, p.get("vendor")) and n not in (FIRMWARE_ALL,)]
    files = _run(["rpm", "-q", "--qf", "@%{NAME}\n[%{FILENAMES}\n]", "--", *names], timeout=60) if names else ""
    return installed, extra, files


def detect_virt():
    """'none' on a real computer, the hypervisor's name in a virtual machine, '' when it cannot be told
    (systemd-detect-virt exits 1 when it prints "none")."""
    exe = _which("systemd-detect-virt")
    if not exe:
        return ""
    try:
        p = subprocess.run([exe], capture_output=True, text=True, env=ENV_C, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip()


def _protected(settings):
    if settings is None:
        return set()
    try:
        keys = settings.get("protected_apps") or []
    except Exception:
        return set()
    return {k.split(":", 1)[1] for k in keys if isinstance(k, str) and k.startswith("rpm:")}


def _cpu_vendor_line():
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("vendor_id"):
                    return line.strip()
    except OSError:
        pass
    return ""


def usb_devices(root=SYS):
    """The USB devices (sysfs): dict(id, vid, pid, cls, product, manufacturer, ifaces=[dict(cls, sub, proto,
    driver, alias, net)])."""
    base = os.path.join(root, "bus/usb/devices")
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for n in names:
        if ":" in n:
            continue
        d = os.path.join(base, n)
        vid = _read(os.path.join(d, "idVendor")).lower()
        if not vid:
            continue
        dev = dict(id=n, vid=vid, pid=_read(os.path.join(d, "idProduct")).lower(),
                   cls=_read(os.path.join(d, "bDeviceClass")).lower(), product=_read(os.path.join(d, "product")),
                   manufacturer=_read(os.path.join(d, "manufacturer")), ifaces=[])
        for i in names:
            if not i.startswith(n + ":"):
                continue
            idir = os.path.join(base, i)
            drv = os.path.basename(os.path.realpath(os.path.join(idir, "driver"))) \
                if os.path.exists(os.path.join(idir, "driver")) else ""
            nets = []
            for nd in _listdir(os.path.join(idir, "net")):
                nets.append(dict(name=nd, wireless=os.path.exists(os.path.join(idir, "net", nd, "wireless"))
                                 or os.path.exists(os.path.join(idir, "net", nd, "phy80211"))))
            dev["ifaces"].append(dict(cls=_read(os.path.join(idir, "bInterfaceClass")).lower(),
                                      sub=_read(os.path.join(idir, "bInterfaceSubClass")).lower(),
                                      proto=_read(os.path.join(idir, "bInterfaceProtocol")).lower(),
                                      driver=drv, alias=_read(os.path.join(idir, "modalias")), net=nets))
        out.append(dev)
    return out


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def pci_from_sysfs(root=SYS):
    """The PCI devices from sysfs, without names, for a computer without lspci (pciutils)."""
    out = []
    base = os.path.join(root, "bus/pci/devices")
    for full in _listdir(base):
        d = os.path.join(base, full)
        cls = _read(os.path.join(d, "class"))[2:]  # 0x030000
        drv = os.path.basename(os.path.realpath(os.path.join(d, "driver"))) \
            if os.path.exists(os.path.join(d, "driver")) else ""
        out.append(dict(slot=full.split(":", 1)[1] if full.count(":") == 2 else full, cls="", cc=cls[:4],
                        prog_if=cls[4:6], vid=_read(os.path.join(d, "vendor"))[2:],
                        did=_read(os.path.join(d, "device"))[2:],
                        svid=_read(os.path.join(d, "subsystem_vendor"))[2:],
                        sdid=_read(os.path.join(d, "subsystem_device"))[2:], vendor="", device="", driver=drv,
                        modules=[]))
    return out


def pci_net(root=SYS):
    """{slot: [dict(name, wireless)]}: the network interfaces each PCI device made ('00:14.3')."""
    out = {}
    base = os.path.join(root, "bus/pci/devices")
    for full in _listdir(base):
        nets = []
        for nd in _listdir(os.path.join(base, full, "net")):
            p = os.path.join(base, full, "net", nd)
            nets.append(dict(name=nd, wireless=os.path.exists(os.path.join(p, "wireless"))
                             or os.path.exists(os.path.join(p, "phy80211"))))
        if nets:
            out[full.split(":", 1)[1] if full.count(":") == 2 else full] = nets
    return out


def printers():
    """The printers CUPS knows: dict(name, uri, model, info). model: printer-make-and-model ('' when it could not be
    read)."""
    lpstat = _run(["lpstat", "-v"])
    names = [m.group(1) for m in re.finditer(r"^device for (.+?): ", lpstat, re.M)]
    return parse_printers(lpstat, {n: _run(["lpoptions", "-p", n]) for n in names})


def parse_printers(lpstat_v, lpoptions):
    """The printers of 'lpstat -v' (C locale), with what 'lpoptions -p NAME' says of each ({name: text})."""
    out = []
    for line in (lpstat_v or "").splitlines():
        m = re.match(r"^device for (.+?): (\S+)", line)
        if not m:
            continue
        p = dict(name=m.group(1), uri=m.group(2), model="", info="")
        try:
            opts = shlex.split((lpoptions or {}).get(p["name"], ""))
        except ValueError:
            opts = []
        for o in opts:
            k, _s, v = o.partition("=")
            if k == "printer-make-and-model":
                p["model"] = v
            elif k == "printer-info":
                p["info"] = v
        out.append(p)
    return out


def secure_boot():
    """True when Secure Boot is on, False when off or on a computer without UEFI, None when it cannot be told."""
    mok = _which("mokutil")
    if mok:
        text = _run([mok, "--sb-state"]).lower()
        if "enabled" in text:
            return True
        if "disabled" in text:
            return False
    if not os.path.isdir("/sys/firmware/efi"):
        return False  # BIOS: no Secure Boot
    try:
        with open(SB_VAR, "rb") as f:
            data = f.read()
        return bool(data[4]) if len(data) >= 5 else None
    except OSError:
        return None


def device_aliases(root=SYS):
    """The modalias of the devices the kernel sees (what zypper matches hardware packages against)."""
    out = set()
    for bus in _listdir(os.path.join(root, "bus")):
        d = os.path.join(root, "bus", bus, "devices")
        for n in _listdir(d):
            a = _read(os.path.join(d, n, "modalias"))
            if a:
                out.add(a)
    for extra in ("devices/system/cpu/modalias", "class/dmi/id/modalias"):
        a = _read(os.path.join(root, extra))
        if a:
            out.add(a)
    return sorted(out)


def loaded_modules():
    out = set()
    try:
        with open("/proc/modules", encoding="utf-8", errors="replace") as f:
            for line in f:
                out.add(line.split(" ", 1)[0])
    except OSError:
        pass
    return out


def module_firmware(modules):
    """{firmware file: module} for the files the loaded modules say they may load (MODULE_FIRMWARE)."""
    modinfo = _which("modinfo")
    out = {}
    if not modinfo:
        return out
    for m in sorted(modules):
        for f in _run([modinfo, "-F", "firmware", m], timeout=10).split():
            out.setdefault(f, m)
    return out


def kernel_firmware_log():
    """The kernel's lines about firmware it could not load, this boot; None when this user cannot read them (on
    openSUSE only root and the groups wheel and systemd-journal can)."""
    text = _run(["journalctl", "-k", "-b", "--no-pager", "-q", "-o", "cat"], timeout=30)
    if not text.strip():
        text = _run(["dmesg"], timeout=10)
    if not text.strip():
        return None
    return [ln for ln in text.splitlines() if "irmware" in ln and re.search(
        r"fail|not found|error -2|missing|unable to load|could not load|can't load", ln, re.I)]


def repos():
    """The repositories of /etc/zypp/repos.d (readable by everyone): dict(alias, name, url, enabled)."""
    import configparser
    out = []
    for n in _listdir(REPOS_D):
        if not n.endswith(".repo"):
            continue
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(os.path.join(REPOS_D, n), encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for sec in cp.sections():
            s = cp[sec]
            out.append(dict(alias=sec, name=s.get("name", sec).strip() or sec,
                            url=(s.get("baseurl", "") or "").strip(),
                            enabled=s.get("enabled", "1").strip() not in ("0", "false", "no")))
    return out


RPM_QF = "@%{NAME}\t%{SIZE}\t%{VENDOR}\n[R %{REQUIRENAME}\n][P %{PROVIDENAME}\n][S %{SUPPLEMENTNAME}\n]"
# the same, with the flags of each requirement (read_rpm)
RPM_STREAM_QF = ("@%{NAME}\t%{SIZE}\t%{VENDOR}\n[R %{REQUIREFLAGS}\t%{REQUIRENAME}\n][P %{PROVIDENAME}\n]"
                 "[S %{SUPPLEMENTNAME}\n]")
# requirements rpm leaves out of its index of requirements, so that --whatrequires does not find them: those only
# for installing (Requires(pre), Requires(post), pretrans, posttrans, rpmlib(…), keyrings), and, to be safe, every
# other one for scriptlets (rpm's flags: RPMSENSE_SCRIPT_PRE, _POST, _PREUN, _POSTUN, _VERIFY, PRETRANS, POSTTRANS,
# PREUNTRANS, POSTUNTRANS, PREREQ, INTERP, RPMLIB, KEYRING)
UNINDEXED = (0x200 | 0x400 | 0x800 | 0x1000 | 0x2000 | 0x80 | 0x20 | 0x100000 | 0x200000 | 0x40 | 0x100 | 0x1000000
             | 0x4000000)
_NONE = ()  # the capabilities of a package the rules never look at (shared: no list for each of them)


def _stream(args, on_line, timeout=TIMEOUT):
    """Each line of a read-only command's output to on_line, as it comes (C locale); False when it cannot run, fails
    or takes too long (what _run's '' means). The lines are split as str.splitlines() splits a whole text."""
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             text=True, errors="replace", env=ENV_C)
    except OSError:
        return False
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    try:
        for chunk in p.stdout:
            for line in chunk.splitlines():
                on_line(line)
    finally:
        timer.cancel()
        p.stdout.close()
        code = p.wait()
    return code == 0


def _query(args, timeout=TIMEOUT):
    """The output of a read-only rpm query whatever its exit code (rpm -q --whatrequires exits 1 when one of the
    capabilities has none); '' when it cannot run."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace", env=ENV_C, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout


def _chunks(items, size=2000, chars=100_000):
    """The items in groups that fit on one command line."""
    group, n = [], 0
    for it in items:
        if group and (len(group) >= size or n + len(it) > chars):
            yield group
            group, n = [], 0
        group.append(it)
        n += len(it) + 1
    if group:
        yield group


def _detailed(name, vendor):
    """Whether the rules look at this package's capabilities: what could go (classify) and the firmware."""
    return name.startswith("kernel-firmware-") or name == "sof-firmware" or bool(classify(name, vendor))


def read_rpm(timeout=120):
    """The installed packages, from one 'rpm -qa' (RPM_STREAM_QF) read line by line: about 10 MB of text, of which
    only this is kept (what parse_rpm would give, but smaller):
    - installed: {name: dict(size, vendor, requires, provides, supplements)}; provides and supplements only of the
      packages _detailed() names (the others: empty), requires of none (needed_by_others asks rpm);
    - extra: dict(unindexed={requirement: [packages]}, paths={package: [provides that are paths]}) of every package.
      unindexed: the requirements rpm -q --whatrequires cannot find: those UNINDEXED flags, and the rich ones like
      '(a if b)' (rpm finds those only by some of the names inside); a few hundred different ones. paths:
      --whatprovides of a path answers with the package that owns the file, not with one that provides it by name.
    ({}, None) when rpm cannot list the packages."""
    installed, unindexed, paths = {}, {}, {}
    state = {"cur": None, "name": "", "keep": False}

    def line_(line):
        if line.startswith("@"):
            name, _t, rest = line[1:].partition("\t")
            size, _t, vendor = rest.partition("\t")
            cur = installed.get(name)
            if cur is None:  # the first copy of a name decides, as its vendor stays (parse_rpm)
                keep = _detailed(name, vendor)
                cur = installed[name] = dict(size=0, vendor=vendor, requires=_NONE, provides=[] if keep else _NONE,
                                             supplements=[] if keep else _NONE)
            state.update(cur=cur, name=name, keep=cur["provides"] is not _NONE)
            cur["size"] = max(cur["size"], int(size) if size.isdigit() else 0)
        elif state["cur"] is not None and len(line) > 2 and line[1] == " ":
            kind, value = line[0], line[2:]
            if kind == "R":
                flags, _t, req = value.partition("\t")
                if req.startswith("(") or (int(flags) if flags.isdigit() else UNINDEXED) & UNINDEXED:
                    unindexed.setdefault(req, []).append(state["name"])
            elif kind == "P":
                if value.startswith(("/", ".")):
                    paths.setdefault(state["name"], []).append(value)
                if state["keep"]:
                    state["cur"]["provides"].append(value)
            elif kind == "S" and state["keep"]:
                state["cur"]["supplements"].append(value)

    if not _stream(["rpm", "-qa", "--qf", RPM_STREAM_QF], line_, timeout):
        return {}, None
    return installed, dict(unindexed=unindexed, paths=paths)


def _supplement_parts(s):
    """(modalias patterns as written, the other names) of one supplement: '(kernel and modalias(pci:…))' →
    (['pci:…'], ['kernel'])."""
    pats = _MODALIAS.findall(s)
    if not pats:
        return pats, []
    rest = _MODALIAS.sub(" ", s).replace("namespace:", " ")
    return pats, [t for t in re.findall(r"[^\s()]+", rest) if t not in _OPS]


def _package_before_alias(p):
    """'xorg-x11-server' for 'xorg-x11-server:pci:…' (a package named before the alias), else ''."""
    head, sep, tail = p.partition(":")
    if sep and head not in BUSES and "*" not in head and ":" in tail and tail.split(":", 1)[0].rstrip("*") in BUSES:
        return head
    return ""


def _who(flag, caps, tag):
    """{package: its values of tag} of the installed packages rpm names for these capabilities (flag:
    --whatrequires or --whatprovides): their names first, then each package once with its values (asked with the
    values, rpm would print a package again for each capability it matches)."""
    names = set()
    for chunk in _chunks(sorted(caps)):
        text = _query(["rpm", "-q", "--qf", "@%{NAME}\n", flag, "--", *chunk], 60)
        names.update(line[1:] for line in text.splitlines() if line.startswith("@"))
    out = {}
    for chunk in _chunks(sorted(names)):
        text = _query(["rpm", "-q", "--qf", "@%{NAME}[\t%{" + tag + "}]\n", "--", *chunk], 60)
        for line in text.splitlines():
            if line.startswith("@"):
                name, *values = line[1:].split("\t")
                if name in names:  # (rpm -q NAME also finds a package whose name-version is NAME)
                    out.setdefault(name, []).extend(values)
    return out


def providers_of(caps, hw):
    """{package: the ones of these capabilities it provides (its provides, not its files)}, for the installed
    packages: rpm -q --whatprovides of those that are not paths, and read_rpm's list for the paths (rpm answers a
    path with the package that owns the file)."""
    out = {}
    paths = (hw.get("rpm_extra") or {}).get("paths") or {}
    for name, provs in paths.items():
        hits = {c for c in provs if c in caps}
        if hits:
            out.setdefault(name, set()).update(hits)
    for name, provs in _who("--whatprovides", [c for c in caps if not c.startswith(("/", "."))],
                            "PROVIDENAME").items():
        hits = {c for c in (p.split(" ", 1)[0] for p in provs) if c in caps}
        if hits:
            out.setdefault(name, set()).update(hits)
    return out


FEW_CAPS = 20


def provided_names(hw):
    """The set hw_patterns() needs as hw["provided"]: of the names the supplements of these packages (installed
    ones with their capabilities read, and the available ones) ask about, those an installed package has as its name
    or among its provides. The same answers as every name and capability of every package, from one rpm -q."""
    asked = set()
    sources = [p["supplements"] for p in hw["installed"].values()] + \
              [p["supplements"] for p in (hw.get("available") or {}).values()]
    for sups in sources:
        for s in sups:
            pats, conds = _supplement_parts(s)
            asked.update(conds)
            for p in pats:
                head = _package_before_alias(_unescape(p))
                if head:
                    asked.add(head)
    out = {n for n in asked if n in hw["installed"]}
    rest = asked - out
    if len(rest) > FEW_CAPS:
        for name, caps in providers_of(rest, hw).items():
            if name in hw["installed"]:
                out |= caps
        return out
    # a few (kernel, kernel-default, xorg-x11-server): one quick rpm -q each, which only says whether a package
    # provides it (providers_of would print every capability of each kernel)
    paths = (hw.get("rpm_extra") or {}).get("paths") or {}
    for cap in sorted(rest):
        if cap.startswith(("/", ".")):
            hit = any(cap in provs for name, provs in paths.items() if name in hw["installed"])
        else:
            text = _query(["rpm", "-q", "--qf", "@%{NAME}\n", "--whatprovides", "--", cap], 60)
            hit = any(line[1:] in hw["installed"] for line in text.splitlines() if line.startswith("@"))
        if hit:
            out.add(cap)
    return out


def zypper_info(names):
    """zypper info of these packages (globs allowed) with their supplements and provides: one zypper process."""
    if not names:
        return ""
    try:
        p = backend.run_c(["--no-refresh", "info", "--supplements", "--provides", "--", *names], timeout=180)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout


# ============================================================================================== parsing

def parse_lspci(text):
    """The PCI devices of 'lspci -vmmnnk' (or 'lspci -nnk'): dict(slot, cls, cc, vid, did, svid, sdid, vendor,
    device, driver, modules)."""
    if re.search(r"^Slot:\t", text, re.M):
        return _parse_lspci_vmm(text)
    out = []
    cur = None
    for line in text.splitlines():
        m = re.match(r"^(\S+) (.+?) \[([0-9a-f]{4})\]: (.+?) \[([0-9a-f]{4}):([0-9a-f]{4})\]", line)
        if m:
            vendor, device = _split_vendor(m.group(4), m.group(5))
            cur = dict(slot=m.group(1), cls=m.group(2), cc=m.group(3), vid=m.group(5), did=m.group(6), svid="",
                       sdid="", vendor=vendor, device=device, driver="", modules=[])
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Subsystem:"):
            m = re.search(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", s)
            if m:
                cur["svid"], cur["sdid"] = m.groups()
        elif s.startswith("Kernel driver in use:"):
            cur["driver"] = s.split(":", 1)[1].strip()
        elif s.startswith("Kernel modules:"):
            cur["modules"] = [x.strip() for x in s.split(":", 1)[1].split(",") if x.strip()]
    return out


def _parse_lspci_vmm(text):
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        f = {}
        for line in block.splitlines():
            k, _s, v = line.partition(":\t")
            if k == "Module":
                f.setdefault("modules", []).append(v.strip())
            else:
                f[k] = v.strip()
        if "Slot" not in f:
            continue

        def split(val):
            m = re.match(r"^(.*?)\s*\[([0-9a-f]{4})\]$", val or "")
            return (m.group(1), m.group(2)) if m else (val or "", "")

        cls, cc = split(f.get("Class"))
        vendor, vid = split(f.get("Vendor"))
        device, did = split(f.get("Device"))
        _sv, svid = split(f.get("SVendor"))
        _sd, sdid = split(f.get("SDevice"))
        slot = f["Slot"]
        out.append(dict(slot=slot.split(":", 1)[1] if slot.count(":") == 2 else slot, cls=cls, cc=cc,
                        prog_if=f.get("ProgIf", ""), vid=vid, did=did, svid=svid, sdid=sdid, vendor=vendor,
                        device=device, driver=f.get("Driver", ""), modules=f.get("modules", [])))
    return out


KNOWN_VENDOR_NAMES = ("Intel Corporation", "NVIDIA Corporation", "Advanced Micro Devices, Inc. [AMD/ATI]",
                      "Advanced Micro Devices, Inc. [AMD]", "Realtek Semiconductor Co., Ltd.",
                      "Broadcom Inc. and subsidiaries", "Broadcom Limited", "Broadcom Corporation",
                      "Qualcomm Atheros", "Qualcomm Technologies, Inc", "Qualcomm", "MEDIATEK Corp.",
                      "MediaTek Inc.", "Samsung Electronics Co Ltd", "VMware", "Red Hat, Inc.",
                      "InnoTek Systemberatung GmbH", "Marvell Technology Group Ltd.", "Atheros Communications Inc.")


def _split_vendor(text, vid):
    """'Intel Corporation WhiskeyLake-U GT2 [UHD Graphics 620]' → ('Intel Corporation', 'WhiskeyLake-U GT2 [UHD …]')
    ('lspci -nnk' prints the two names together)."""
    for v in KNOWN_VENDOR_NAMES:
        if text.startswith(v + " "):
            return v, text[len(v) + 1:]
    return PCI_VENDORS.get(vid, ""), text


def parse_lsusb(text):
    """{'8087:0aaa': 'Intel Corp. Bluetooth 9460/9560 Jefferson Peak (JfP)'}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^Bus \d+ Device \d+: ID ([0-9a-f]{4}:[0-9a-f]{4})\s*(.*)$", line.strip())
        if m:
            out.setdefault(m.group(1), m.group(2).strip())
    return out


def parse_os_release(text):
    out = {}
    for line in (text or "").splitlines():
        k, s, v = line.partition("=")
        if s:
            out[k.strip()] = v.strip().strip("\"'")
    return out


def parse_rpm(text):
    """{name: dict(size, vendor, requires, provides, supplements)} from RPM_QF."""
    out = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("@"):
            name, _t, rest = line[1:].partition("\t")
            size, _t, vendor = rest.partition("\t")
            cur = out.get(name)
            if cur is None:
                cur = out[name] = dict(size=0, vendor=vendor, requires=[], provides=[], supplements=[])
            cur["size"] = max(cur["size"], int(size) if size.isdigit() else 0)
        elif cur is not None and len(line) > 2 and line[1] == " ":
            {"R": cur["requires"], "P": cur["provides"], "S": cur["supplements"]}.get(line[0], []).append(line[2:])
    return out


def parse_zypper_info(text):
    """{name: dict(repo, installed, version, supplements, provides)} of 'zypper info --supplements --provides'."""
    out = {}
    cur = None
    field = None
    for line in (text or "").splitlines():
        m = re.match(r"^Information for (?:package|patch|pattern|product) (\S+):\s*$", line)
        if m:
            cur = out[m.group(1)] = dict(repo="", installed=False, version="", supplements=[], provides=[])
            field = None
            continue
        if cur is None:
            continue
        m = re.match(r"^([A-Z][A-Za-z ]+?)\s*:\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            field = None
            if key == "Repository":
                cur["repo"] = val
            elif key == "Installed":
                cur["installed"] = val.lower().startswith("y")
            elif key == "Version":
                cur["version"] = val
            elif key in ("Supplements", "Provides"):
                field = key.lower()
            continue
        if field and line.startswith((" ", "\t")) and line.strip():
            cur[field].append(line.strip())
    return out


def parse_files(text):
    out = {}
    cur = None
    for line in (text or "").splitlines():
        if line.startswith("@"):
            cur = out.setdefault(line[1:].strip(), [])
        elif cur is not None and line.startswith("/"):
            cur.append(line)
    return out


def parse_locks(text):
    """The package names (or globs) locked in /etc/zypp/locks, for every repository."""
    out = []
    for block in re.split(r"\n\s*\n", text or ""):
        f = {}
        for line in block.splitlines():
            k, s, v = line.partition(":")
            if s:
                f[k.strip()] = v.strip()
        if f.get("solvable_name") and f.get("type", "package") == "package" and not f.get("repo"):
            out.append(f["solvable_name"])
    return out


def _unescape(p):
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), p)


def build(raw):
    """hw: the facts in a form the rules use. raw: what collect() read (tests give their own)."""
    uname = raw.get("uname")
    if isinstance(uname, dict):
        release, machine = uname.get("release", ""), uname.get("machine", "")
    else:
        release, machine = getattr(uname, "release", ""), getattr(uname, "machine", "")
    osr = parse_os_release(raw.get("os_release", ""))
    cpu = (raw.get("cpuinfo") or "").lower()
    installed = parse_rpm(raw.get("rpm", "")) if isinstance(raw.get("rpm"), str) else dict(raw.get("rpm") or {})
    if "provided" in raw:  # collect(): provided_names() after build
        provided = set(raw["provided"] or ())
    else:  # every capability of every package is in installed (tests)
        provided = set()
        for name, p in installed.items():
            provided.add(name)
            for cap in p["provides"]:
                provided.add(cap.split(" ", 1)[0])
    pci = parse_lspci(raw.get("lspci", "")) or [dict(d) for d in raw.get("pci_sys") or ()]
    nets = raw.get("pci_net") or {}
    for d in pci:
        d["net"] = nets.get(d["slot"], [])
        d["alias"] = _pci_alias(d)
    usb = list(raw.get("usb") or [])
    names = parse_lsusb(raw.get("lsusb", ""))
    for u in usb:
        u["lsusb"] = names.get(f"{u['vid']}:{u['pid']}", "")
    aliases = set(raw.get("aliases") or ())
    if not aliases:  # tests: the aliases of the devices they describe
        aliases.update(d["alias"] for d in pci if d["alias"])
        for u in usb:
            aliases.update(i["alias"] for i in u.get("ifaces", ()) if i.get("alias"))
        if "genuineintel" in cpu:
            aliases.add("cpu:type:x86,ven0000fam0006mod008E:feature:,0000")
        elif "authenticamd" in cpu:
            aliases.add("cpu:type:x86,ven0002fam0019mod0021:feature:,0000")
    flavor = release.rsplit("-", 1)[-1] if "-" in release else ""
    printers_ = raw.get("printers")
    if printers_ is None and "lpstat" in raw:
        printers_ = parse_printers(raw["lpstat"], raw.get("lpoptions") or {})
    hw = dict(
        pci=pci, usb=usb, printers=list(printers_ or []), secure_boot=raw.get("secure_boot"),
        kernel=release, flavor=flavor, arch=machine, distro=osr.get("ID", ""), distro_name=osr.get("NAME", ""),
        cpu_vendor="intel" if "genuineintel" in cpu else "amd" if "authenticamd" in cpu else "",
        virt=(raw.get("virt") or "").strip(), aliases=sorted(aliases), modules=set(raw.get("modules") or ()),
        module_firmware=dict(raw.get("module_firmware") or {}), journal=raw.get("journal"),
        installed=installed, provided=provided, repos=list(raw.get("repos") or []),
        locks=parse_locks(raw["locks"]) if isinstance(raw.get("locks"), str) else list(raw.get("locks") or []),
        protected=set(raw.get("protected") or ()),
        available=(parse_zypper_info(raw["zypper_info"]) if isinstance(raw.get("zypper_info"), str)
                   else dict(raw.get("available") or {})),
        candidate_files=(parse_files(raw["files"]) if isinstance(raw.get("files"), str)
                         else dict(raw.get("files") or {})),
        # read_rpm's extra: needed_by_others asks rpm who requires what (None: installed has it all, as in tests)
        rpm_extra=raw.get("rpm_extra"),
    )
    if "fw_plan" in raw:  # tests: a firmware plan of their own (None: no plan)
        hw["fw_plan"] = raw["fw_plan"]
    return hw


def _pci_alias(d):
    if not d.get("vid") or len(d.get("cc", "")) < 4:
        return ""
    return "pci:v0000{v}d0000{d}sv0000{sv}sd0000{sd}bc{c}sc{s}i{i}".format(
        v=d["vid"].upper(), d=d["did"].upper(), sv=(d.get("svid") or "0000").upper(),
        sd=(d.get("sdid") or "0000").upper(), c=d["cc"][:2].upper(), s=d["cc"][2:4].upper(),
        i=(d.get("prog_if") or "00").upper())


def info_names(hw):
    """What the one zypper info asks about: firmware, microcode and the graphics drivers zypper would install for a
    device (to tell when one is missing), and the drivers proposed for the NVIDIA and Broadcom cards here."""
    names = ["kernel-firmware-*", "sof-firmware", "ucode-intel", "ucode-amd", "intel-media-driver",
             "libvulkan_intel", "libvulkan_radeon", "wireless-regdb"]
    fl = hw.get("flavor") or "default"
    if any(d["vid"] == NVIDIA and d["cc"].startswith("03") for d in hw["pci"]):
        names += [f"nvidia-open-driver-G07-signed-kmp-{fl}", "openSUSE-repos-Tumbleweed-NVIDIA"]
    if any(d["vid"] == BROADCOM and d["cc"].startswith("02") for d in hw["pci"]):
        names += ["broadcom-wl", f"broadcom-wl-kmp-{fl}"]
    return names


# ============================================================================================== hardware patterns

BUSES = {"pci", "usb", "acpi", "cpu", "x86cpu", "of", "platform", "hdaudio", "sdio", "dmi", "input", "i2c", "spi",
         "virtio", "scsi", "wmi", "mei", "serio", "pnp", "hid", "mdio", "vmbus", "xen", "soundwire", "ishtp",
         "auxiliary", "ieee1394", "ssb", "bcma", "mhi", "tee", "rpmsg", "mmc", "snd", "ap", "ccw", "css", "nd",
         "cxl", "typec", "thunderbolt", "zorro", "eisa", "sound", "fsl-mc", "amba"}
_MODALIAS = re.compile(r"modalias\(([^()]*)\)")
_OPS = {"and", "or", "if", "else", "unless", "with", "without"}


def hw_patterns(supplements, provided):
    """The modalias glob patterns of a package's supplements whose other conditions hold here (a package named
    before the alias, 'kernel-default:pci:…', or beside it, '(kernel and modalias(…))', must be installed)."""
    out = []
    for s in supplements:
        pats, conds = _supplement_parts(s)
        if not pats:
            continue
        if " or " not in s and any(c not in provided for c in conds):
            continue
        for p in pats:
            p = _unescape(p)
            head = _package_before_alias(p)
            if head:
                if head not in provided:
                    continue  # 'xorg-x11-server:pci:…' without the X server
                p = p.partition(":")[2]
            out.append(p)
    return out


def _literal_prefix(p):
    return re.match(r"[^*?\[\\]*", p).group(0)


def matching(patterns, aliases_sorted):
    """The aliases (sorted list) that match any of these patterns."""
    hits = []
    for p in patterns:
        pre = _literal_prefix(p)
        i = bisect.bisect_left(aliases_sorted, pre)
        while i < len(aliases_sorted) and aliases_sorted[i].startswith(pre):
            if fnmatch.fnmatchcase(aliases_sorted[i], p):
                hits.append(aliases_sorted[i])
            i += 1
    return list(dict.fromkeys(hits))


def nvidia_series(did):
    """'G07', 'G06', 'G05', 'G04' for an NVIDIA device id ('2504'), or '' when NVIDIA has no driver for it."""
    try:
        n = int(did, 16)
    except (TypeError, ValueError):
        return ""
    if n >= TURING:
        return "G07"
    for series, ranges in NV_SERIES.items():
        for r in ranges.split():
            a, _s, b = r.partition("-")
            if int(a, 16) <= n <= int(b or a, 16):
                return series
    return ""


# ============================================================================================== names in words

def short_vendor(vid, name=""):
    if vid in PCI_VENDORS:
        return PCI_VENDORS[vid]
    name = re.sub(r"\s*\[.*?\]", "", name or "")
    name = re.sub(r",?\s*(Inc\.?|Corporation|Corp\.?|Co\.,? Ltd\.?|Ltd\.?|Limited|GmbH|and subsidiaries)$", "", name)
    return name.strip() or vid


def device_name(d):
    """'Intel UHD Graphics 620' for 'Intel Corporation' + 'WhiskeyLake-U GT2 [UHD Graphics 620]'."""
    v = short_vendor(d.get("vid", ""), d.get("vendor", ""))
    dev = d.get("device", "")
    m = re.search(r"\[([^\]]+)\]\s*$", dev)
    name = m.group(1) if m else dev
    if not name:
        return v
    return name if name.lower().startswith(v.lower()) else f"{v} {name}"


_CORP = re.compile(r"^.*?\b(Corp\.?|Corporation|Co\.,? ?Ltd\.?|Inc\.?|Ltd\.?|GmbH|S\.A\.|AG|Netcom)(?=\s)\s*")


def usb_name(u):
    """'LG Camera', 'Realtek RTL8153 Gigabit Ethernet Adapter': the device's own name when it says something, else
    lsusb's without the company's full name."""
    product = (u.get("product") or "").strip()
    if product and len(product) > 3 and not re.match(r"(?i)^usb\b", product):
        return product
    vendor = USB_VENDORS.get(u.get("vid", ""), "")
    text = (u.get("lsusb") or "").strip()
    if text:
        rest = _CORP.sub("", text, count=1) if _CORP.match(text) else text
        if vendor and not rest.lower().startswith(vendor.lower()):
            return f"{vendor} {rest}"
        return rest
    if product:
        return f"{vendor} {product}".strip()
    return f"USB {u.get('vid', '')}:{u.get('pid', '')}"


def join(names, limit=4):
    """'a, b and c' ('a, b, c and 3 more' after limit)."""
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) > limit:
        rest = len(names) - limit
        return ngettext("{list} and {n} more", "{list} and {n} more", rest).format(
            list=", ".join(names[:limit]), n=rest)
    if len(names) == 1:
        return names[0]
    return _("{list} and {last}").format(list=", ".join(names[:-1]), last=names[-1])


def nvidia_installed(hw):
    """(series, open kernel module?) of the NVIDIA driver packages installed, or ('', None)."""
    for name in hw["installed"]:
        m = re.match(r"^nvidia-open-driver-(G0\d)", name)
        if m and "-kmp-" in name:
            return m.group(1), True
    for name in hw["installed"]:
        m = re.match(r"^(?:nvidia-driver-|nvidia-gfx)(G0\d)-kmp-", name)
        if m:
            return m.group(1), False
    return "", None


# ============================================================================================== the cards

def firmware_of(alias_list, hw, only_installed=True):
    """The firmware packages (installed, or available and missing) whose supplements match these device aliases."""
    out = []
    aliases = sorted(alias_list)
    if only_installed:
        source = {n: p["supplements"] for n, p in hw["installed"].items() if n.startswith("kernel-firmware-")
                  or n == "sof-firmware"}
    else:
        source = {n: p["supplements"] for n, p in hw.get("available", {}).items() if not p["installed"]
                  and n not in hw["installed"]}
    for name, sups in sorted(source.items()):
        if matching(hw_patterns(sups, hw["provided"]), aliases):
            out.append(name)
    return out


def _missing_for(aliases, hw):
    """Packages zypper would install for these devices that are not installed: firmware (kernel-firmware-*,
    sof-firmware), microcode and the graphics drivers (VA-API, Vulkan)."""
    return [n for n in firmware_of(aliases, hw, only_installed=False)
            if n.startswith(("kernel-firmware-", "sof-firmware", "ucode-", "intel-media-driver", "libvulkan_"))]


def _missing_text(names):
    fw = [n for n in names if "firmware" in n]
    other = [n for n in names if n not in fw]
    parts = []
    if fw:
        parts.append(ngettext("Its driver can use firmware from {names}, which is not installed.",
                              "Its driver can use firmware from {names}, which are not installed.",
                              len(fw)).format(names=join(fw)))
    if other:
        parts.append(ngettext("{names} is not installed: zypper installs it for this hardware.",
                              "{names} are not installed: zypper installs them for this hardware.",
                              len(other)).format(names=join(other)))
    return " ".join(parts)


def _card(kind, icon, title, detail, status="ok", proposal=None, devices=(), aliases=()):
    return dict(kind=kind, icon=icon, title=title, detail=detail, status=status, proposal=proposal,
                devices=list(devices), aliases=list(aliases))


def _firmware_sentence(pkgs):
    if not pkgs:
        return ""
    return " " + ngettext("With its firmware ({packages}).", "With its firmware ({packages}).",
                          len(pkgs)).format(packages=join(pkgs, 3))


def _add_missing(card, hw):
    """A card whose device needs a package that is not installed says so, with a button to install it."""
    missing = _missing_for(card["aliases"], hw)
    if not missing or card["proposal"] is not None:
        return card
    card["detail"] = (card["detail"] + " " + _missing_text(missing)).strip()
    fw = any("firmware" in n for n in missing)
    card["status"] = "firmware" if fw else "missing"
    card["proposal"] = dict(kind="install", packages=missing, firmware=fw, device=card["title"],
                            actions=[("install", n, dict(package=n)) for n in missing])
    return card


def cards(hw):
    """One card per device worth showing: graphics, network, Bluetooth, sound, webcams, printers, and anything
    with something to propose. dict(kind, icon, title, detail, status, proposal, devices, aliases);
    status: ok, better, optional, firmware, missing, nodriver."""
    out = []
    pci = hw["pci"]
    used = set()
    # ---- graphics
    for d in pci:
        if not d["cc"].startswith("03"):
            continue
        used.add(d["slot"])
        out.append(_add_missing(_graphics_card(d, hw), hw))
    # ---- Wi-Fi, network (and the Bluetooth half of a Wi-Fi chip)
    bt = [u for u in hw["usb"] if any(i["cls"] == "e0" and i["sub"] == "01" and i["proto"] == "01"
                                      for i in u.get("ifaces", ()))]
    bt_used = set()
    for d in pci:
        if not d["cc"].startswith(("02", "0d")):
            continue
        used.add(d["slot"])
        wifi = d["cc"] == "0280" or any(n["wireless"] for n in d.get("net", ())) or \
            d["driver"] in WIFI_DRIVERS
        twin = None
        if wifi:
            twins = [u for u in bt if u["vid"] in BT_TWIN.get(d["vid"], ()) and u["id"] not in bt_used]
            if len(twins) == 1:
                twin = twins[0]
                bt_used.add(twin["id"])
        out.append(_add_missing(_network_card(d, hw, wifi, twin), hw))
    for u in bt:
        if u["id"] in bt_used:
            continue
        drv = _usb_drivers(u)
        aliases = [i["alias"] for i in u["ifaces"] if i.get("alias")]
        fw = firmware_of(aliases, hw)
        detail = (_("Free driver {driver}, part of the kernel.").format(driver=join(drv)) if drv
                  else _("No driver is using it.")) + _firmware_sentence(fw)
        out.append(_add_missing(_card("bluetooth", "preferences-system-bluetooth|bluetooth",
                                      _("Bluetooth: {name}").format(name=usb_name(u)), detail,
                                      "ok" if drv else "nodriver", aliases=aliases), hw))
    # USB network adapters (docks)
    for u in hw["usb"]:
        nets = [n for i in u.get("ifaces", ()) for n in i.get("net", ())]
        if not nets or u["id"] in bt_used:
            continue
        wifi = any(n["wireless"] for n in nets)
        drv = _usb_drivers(u)
        aliases = [i["alias"] for i in u["ifaces"] if i.get("alias")]
        fw = firmware_of(aliases, hw)
        title = (_("Wi-Fi (USB): {name}") if wifi else _("Network (USB): {name}")).format(name=usb_name(u))
        detail = _("Free driver {driver}, part of the kernel.").format(driver=join(drv)) + _firmware_sentence(fw)
        out.append(_add_missing(_card("network", "network-wireless" if wifi else "network-wired", title, detail,
                                      aliases=aliases), hw))
    # ---- sound
    for d in pci:
        if not d["cc"].startswith("04") or d["cc"] == "0480":
            continue
        used.add(d["slot"])
        fw = firmware_of([d["alias"]], hw)
        detail = (_("Free driver {driver}, part of the kernel.").format(driver=d["driver"]) if d["driver"]
                  else _no_driver(d)) + _firmware_sentence(fw)
        out.append(_add_missing(_card("audio", "audio-card", _("Sound: {name}").format(name=device_name(d)),
                                      detail, "ok" if d["driver"] else "nodriver", devices=[d],
                                      aliases=[d["alias"]]), hw))
    usb_audio = [u for u in hw["usb"] if any(i["cls"] == "01" for i in u.get("ifaces", ()))]
    if usb_audio:
        drv = sorted({i["driver"] for u in usb_audio for i in u["ifaces"] if i["cls"] == "01" and i["driver"]})
        out.append(_add_missing(_card(
            "audio", "audio-headset|audio-card", _("USB sound: {names}").format(
                names=join([usb_name(u) for u in usb_audio], 3)),
            _("Free driver {driver}, part of the kernel.").format(driver=join(drv)) if drv
            else _("No driver is using it."), "ok" if drv else "nodriver",
            aliases=[i["alias"] for u in usb_audio for i in u["ifaces"] if i.get("alias")]), hw))
    # ---- webcams
    for u in hw["usb"]:
        if not any(i["cls"] == "0e" for i in u.get("ifaces", ())):
            continue
        drv = sorted({i["driver"] for i in u["ifaces"] if i["cls"] == "0e" and i["driver"]})
        out.append(_add_missing(_card(
            "camera", "camera-web|camera-photo", _("Webcam: {name}").format(name=usb_name(u)),
            _("Free driver {driver}, part of the kernel.").format(driver=join(drv)) if drv
            else _("No driver is using it."), "ok" if drv else "nodriver",
            aliases=[i["alias"] for i in u["ifaces"] if i.get("alias")]), hw))
    for d in pci:
        if d["cc"] == "0480" and re.search(r"image|ipu|camera", d["device"], re.I):
            used.add(d["slot"])
            out.append(_add_missing(_card(
                "camera", "camera-web|camera-photo", _("Camera: {name}").format(name=device_name(d)),
                _("Free driver {driver}, part of the kernel.").format(driver=d["driver"]) if d["driver"]
                else _("No driver is using it."), "ok" if d["driver"] else "nodriver", devices=[d],
                aliases=[d["alias"]]), hw))
    # ---- printers
    for p in hw["printers"]:
        out.append(_printer_card(p, hw))
    # ---- anything else that misses something (a processor without its microcode, a chip on the board…)
    shown = {a for c in out for a in c["aliases"]}
    rest = [a for a in hw["aliases"] if a not in shown]
    by_card = {}
    for name in _missing_for(rest, hw):
        hits = matching(hw_patterns(hw["available"][name]["supplements"], hw["provided"]), rest)
        by_card.setdefault("cpu" if any(a.startswith("cpu:") for a in hits) else "other", []).append(name)
    for key, names in by_card.items():
        title = _("Processor") if key == "cpu" else _("Other hardware")
        detail = (_("{names} is not installed: it has the processor's microcode updates.").format(names=join(names))
                  if key == "cpu" else _("Chips on the board or devices without a card of their own.") + " "
                  + _missing_text(names))
        out.append(dict(kind=key, icon="cpu|computer" if key == "cpu" else "computer", title=title, detail=detail,
                        status="firmware", devices=[], aliases=[],
                        proposal=dict(kind="install", packages=names, firmware=True, device=title,
                                      actions=[("install", n, dict(package=n)) for n in names])))
    # ---- what the kernel said it could not load (only when this user can read the kernel's messages)
    out += _log_cards(hw, out)
    return out


WIFI_DRIVERS = {"iwlwifi", "ath9k", "ath10k_pci", "ath11k_pci", "ath12k", "rtw88_8822ce", "rtw88_8821ce",
                "rtw88_8822be", "rtw89_8852ae", "rtw89_8852be", "rtw89_8852ce", "rtw89_8922ae", "mt7921e",
                "mt7925e", "mt7915e", "mt76x2e", "brcmfmac", "brcmsmac", "b43", "wl", "rtl8192ce", "rtl8188ee",
                "rtl8723be", "rtl8821ae"}


def _no_driver(d):
    mods = [m for m in d.get("modules") or () if m not in ("bcma", "ssb")]
    if mods:
        return ngettext("Its driver {modules} is there, but it did not start.",
                        "Its drivers {modules} are there, but none started.", len(mods)).format(modules=join(mods))
    return _("No driver is using it.")


def _usb_drivers(u):
    return sorted({i["driver"] for i in u.get("ifaces", ()) if i.get("driver") and i["driver"] not in ("usbhid",
                                                                                                      "hub")})


def _graphics_card(d, hw):
    name = device_name(d)
    title = _("Graphics: {name}").format(name=name)
    fw = firmware_of([d["alias"]], hw)
    if d["vid"] == NVIDIA:
        return _nvidia_card(d, hw, title)
    if not d["driver"]:
        return _card("graphics", "video-display", title, _no_driver(d), "nodriver", devices=[d],
                     aliases=[d["alias"]])
    detail = _("Free driver {driver}, part of the kernel.").format(driver=d["driver"]) + _firmware_sentence(fw)
    if d["vid"] in (INTEL, AMD) and d["driver"] in ("i915", "xe", "amdgpu", "radeon"):
        detail += " " + _("It is the best one for this card: nothing else is needed.")
    return _card("graphics", "video-display", title, detail, devices=[d], aliases=[d["alias"]])


def nvidia_plan(d, hw):
    """What Zypp Desk would set up for this NVIDIA card: dict(series, ok, why, packages, repo, key, compiled…)."""
    series = nvidia_series(d["did"])
    plan = dict(series=series, ok=False, why="")
    if not series:
        plan["why"] = _("NVIDIA no longer makes a driver for this card.")
        return plan
    info = SERIES[series]
    plan.update(version=info["version"], compiled=info["compiled"], legacy=info["legacy"])
    dist = NVIDIA_DISTS.get(hw.get("distro", ""))
    flavor = hw.get("flavor") or ""
    if not dist:
        plan["why"] = _("Zypp Desk can set it up on openSUSE Tumbleweed and Slowroll; on {distro} follow "
                        "openSUSE's guide (SDB:NVIDIA drivers).").format(distro=hw.get("distro_name") or "?")
        return plan
    if hw.get("arch") not in ("x86_64", "aarch64"):
        plan["why"] = _("NVIDIA's repository has no packages for this kind of processor ({arch}).").format(
            arch=hw.get("arch") or "?")
        return plan
    if flavor not in info["flavors"]:
        plan["why"] = _("There is no NVIDIA package for the kernel this computer runs ({kernel}).").format(
            kernel=hw.get("kernel") or "?")
        return plan
    url = NVIDIA_URL.format(dist=dist)
    repo = next((r for r in hw["repos"] if "download.nvidia.com/opensuse" in r["url"]), None)
    packages = []
    for pattern, from_nvidia in info["packages"]:
        name = pattern.format(flavor=flavor)
        if not from_nvidia and hw.get("available") and name not in hw["available"] and name not in hw["installed"]:
            plan["why"] = _("{package}, the kernel part, is not in your repositories: is openSUSE's main repository "
                            "(repo-oss) enabled?").format(package=name)
            return plan
        packages.append((name, from_nvidia))
    plan.update(ok=True, url=url, packages=packages, flavor=flavor)
    actions = []
    if repo is None:
        alias = NVIDIA_ALIAS
        used = {r["alias"] for r in hw["repos"]}
        n = 2
        while alias in used:
            alias, n = f"{NVIDIA_ALIAS}-{n}", n + 1
        plan["repo"] = dict(alias=alias, url=url, name=NVIDIA_ALIAS, state="new")
        plan["key"] = dict(url=url + "/repodata/repomd.xml.key", fpr=NVIDIA_KEY_FPR, name="NVIDIA Linux Driver Team")
        actions.append(("key-import", NVIDIA_KEY_FPR, dict(name="NVIDIA Linux Driver Team", for_repo=alias)))
        actions.append(("repo-add", alias, dict(url=url, name=NVIDIA_ALIAS, autorefresh=True, priority=99,
                                                gpg="default")))
    else:
        alias = repo["alias"]
        plan["repo"] = dict(alias=alias, url=repo["url"], name=repo["name"], state="ok" if repo["enabled"]
                            else "disabled")
        plan["key"] = None
        if not repo["enabled"]:
            actions.append(("repo-modify", alias, dict(enabled=True)))
    for name, from_nvidia in packages:
        target = f"{alias}:{name}" if from_nvidia and plan["repo"]["state"] == "new" else name
        actions.append(("install", target, dict(package=name)))
    plan["actions"] = actions
    return plan


def _nvidia_card(d, hw, title):
    drv = d["driver"]
    plan = nvidia_plan(d, hw)
    series = plan["series"]
    aliases = [d["alias"]]
    if drv == "nvidia":
        inst, open_kmp = nvidia_installed(hw)
        if open_kmp:
            detail = _("NVIDIA's driver (series {series}). Its kernel part is open source and signed by openSUSE; "
                       "the rest is proprietary, from NVIDIA.").format(series=inst)
        elif inst:
            detail = _("NVIDIA's driver (series {series}), proprietary, from NVIDIA.").format(series=inst)
        else:
            detail = _("NVIDIA's driver, installed without zypper (it is not in the rpm database).")
        return _card("graphics", "video-display", title, detail, devices=[d], aliases=aliases)
    now = (_("It uses nouveau now (free), which is slow with this card.") if drv == "nouveau"
           else _("No driver is using it now."))
    if not plan["ok"]:
        status = "ok" if drv else "nodriver"
        return _card("graphics", "video-display", title, (now if series else _("It uses nouveau (free).")) + " "
                     + plan["why"], status, devices=[d], aliases=aliases)
    sb = hw.get("secure_boot")
    if series == "G07":
        detail = now + " " + _("Proposed: NVIDIA's driver (series {series}, its kernel part is open source) from "
                               "NVIDIA's repository, signed to start with Secure Boot.").format(series=series)
        status = "better"
    elif not plan["legacy"]:
        detail = now + " " + _("Proposed: NVIDIA's driver for older cards (series {series}), from NVIDIA's "
                               "repository. It is built on this computer when it is installed.").format(series=series)
        if sb:
            detail += " " + _("Secure Boot is on: at the next start you will be asked to enroll a key.")
        status = "better"
    else:
        detail = (_("It uses nouveau (free).") if drv == "nouveau" else now) + " " + _(
            "NVIDIA's old driver for this card (series {series}) still exists, but NVIDIA no longer updates it and "
            "it only works in the X11 session.").format(series=series)
        status = "optional"
    proposal = dict(kind="nvidia", plan=plan, device=device_name(d), secure_boot=sb, actions=plan["actions"],
                    model=re.sub(r"^NVIDIA\s+", "", device_name(d)))
    return _card("graphics", "video-display", title, detail, status, proposal, devices=[d], aliases=aliases)


def _wl_conflicts(d, hw):
    """The other devices that use a driver broadcom-wl would switch off."""
    out = []
    for o in hw["pci"]:
        if o is d:
            continue
        if o["driver"] in WL_BLACKLIST:
            out.append((device_name(o), o["driver"]))
    return out


def _network_card(d, hw, wifi, twin):
    name = device_name(d)
    aliases = [d["alias"]]
    if twin is not None:
        aliases += [i["alias"] for i in twin["ifaces"] if i.get("alias")]
        title = _("Wi-Fi and Bluetooth: {name}").format(name=name)
        icon = "network-wireless"
    elif wifi:
        title = _("Wi-Fi: {name}").format(name=name)
        icon = "network-wireless"
    else:
        title = _("Network: {name}").format(name=name)
        icon = "network-wired"
    fw = firmware_of(aliases, hw)
    drv = d["driver"]
    if d["vid"] == BROADCOM and wifi:
        card = _broadcom_card(d, hw, title, icon, aliases, fw)
        if card is not None:
            return card
    if drv == "wl":
        return _card("network", icon, title, _("Broadcom's driver wl (proprietary), from openSUSE's non-free "
                                               "repository."), devices=[d], aliases=aliases)
    if not drv:
        return _card("network", icon, title, _no_driver(d), "nodriver", devices=[d], aliases=aliases)
    if twin is not None:
        bt_drv = _usb_drivers(twin)
        detail = _("Free drivers {drivers}, part of the kernel.").format(drivers=join([drv] + bt_drv))
        if fw:
            detail += " " + ngettext("With their firmware ({packages}).", "With their firmware ({packages}).",
                                     len(fw)).format(packages=join(fw, 3))
    else:
        detail = _("Free driver {driver}, part of the kernel.").format(driver=drv) + _firmware_sentence(fw)
    return _card("network", icon, title, detail, devices=[d], aliases=aliases)


def _broadcom_card(d, hw, title, icon, aliases, fw):
    """A Broadcom Wi-Fi card that needs Broadcom's wl driver, or None when a free driver handles it."""
    chip = WL_ONLY.get(d["did"]) or WL_TOO.get(d["did"])
    if not chip or d["driver"] == "wl":
        return None
    has_net = bool(d.get("net"))
    if d["did"] in WL_TOO and has_net:
        return None  # b43 made a network interface: it works
    fl = hw.get("flavor") or "default"
    kmp = f"broadcom-wl-kmp-{fl}"
    detail = _("No free driver works with this card ({chip}).").format(chip=chip)
    conflicts = _wl_conflicts(d, hw)
    if conflicts:
        detail += " " + _("Broadcom's driver wl would switch off {drivers}, which {devices} use: Zypp Desk does not "
                          "propose it.").format(drivers=join(sorted({c[1] for c in conflicts})),
                                                devices=join([c[0] for c in conflicts], 3))
        return _card("network", icon, title, detail, "nodriver", devices=[d], aliases=aliases)
    avail = hw.get("available") or {}
    actions = []
    repo = None
    if avail and kmp not in avail and kmp not in hw["installed"]:  # (no answer from zypper: no guess either)
        repo = next((r for r in hw["repos"] if re.search(r"(^|:)repo-non-oss$", r["alias"])
                     or "/repo/non-oss" in r["url"]), None)
        if repo is None or repo["enabled"]:
            detail += " " + _("Broadcom's driver wl is in openSUSE's non-free repository (repo-non-oss), which "
                              "this computer does not have.")
            return _card("network", icon, title, detail, "nodriver", devices=[d], aliases=aliases)
        actions.append(("repo-modify", repo["alias"], dict(enabled=True)))
    detail += " " + _("Proposed: Broadcom's driver wl (proprietary), from openSUSE's non-free repository, signed "
                      "by openSUSE.")
    for name in ("broadcom-wl", kmp):
        actions.append(("install", name, dict(package=name)))
    proposal = dict(kind="broadcom", chip=chip, device=device_name(d), packages=["broadcom-wl", kmp],
                    repo=repo, secure_boot=hw.get("secure_boot"), actions=actions)
    return _card("network", icon, title, detail, "nodriver", proposal, devices=[d], aliases=aliases)


def printer_vendor(p):
    text = " ".join((p.get("model") or "", p.get("info") or "", p.get("name") or "")).lower()
    for v in PRINTER_VENDORS:
        if re.search(r"\b" + v + r"\b", text) or (v == "brother" and re.search(r"\b(dcp|mfc|hl)-", text)):
            return "hp" if v == "hewlett" else "konica" if v == "minolta" else v
    return ""


def driverless(p):
    m = (p.get("model") or "").lower()
    return "ipp everywhere" in m or "driverless" in m or "airprint" in m or \
        (p.get("uri") or "").startswith(("ipp://", "ipps://", "implicitclass://")) and not m


def _printer_card(p, hw):
    name = p.get("info") or p.get("name")
    name = re.sub(r"\s*\(driverless\)\s*$", "", name or "", flags=re.I) or p.get("name", "")
    title = _("Printer: {name}").format(name=name)
    vendor = printer_vendor(p)
    if driverless(p):
        detail = _("Prints without a driver (IPP Everywhere).")
        if vendor == "hp":
            detail += " " + _("HP also has HPLIP, for its scanners and ink levels, but printing does not need it.")
        elif vendor in ("brother", "epson", "canon", "samsung", "kyocera", "ricoh", "lexmark", "xerox"):
            detail += " " + _("{vendor} also has a driver of its own, but it is not needed.").format(
                vendor=vendor.capitalize() if vendor != "hp" else "HP")
    elif p.get("model"):
        detail = _("It uses the driver “{model}”.").format(model=p["model"])
    else:
        detail = _("Zypp Desk could not read which driver it uses.")
    return _card("printer", "printer", title, detail, "ok")


def _log_cards(hw, out):
    """Firmware the kernel asked for and did not find this boot (when this user can read the kernel's messages)."""
    lines = hw.get("journal") or []
    files = []
    for ln in lines:
        for m in re.finditer(r"(?:Direct firmware load for|firmware file|load firmware|loading firmware|firmware:?)"
                             r"\s+\"?([\w.+-]+(?:/[\w.+-]+)*\.\w+)\"?", ln, re.I):
            f = m.group(1)
            if f not in files:
                files.append(f)
    if not files:
        return []
    avail = hw.get("available") or {}
    have = {}
    for name, p in avail.items():
        for cap in p["provides"]:
            m = re.match(r"^firmware\((.+?)\)", cap)
            if m:
                have.setdefault(m.group(1), name)
    proposed = {n for c in out if c.get("proposal") for n in c["proposal"].get("packages", ())}
    found, unknown = [], []
    for f in files[:12]:
        pkg = have.get(f)
        if pkg and not avail[pkg]["installed"] and pkg not in hw["installed"]:
            if pkg not in proposed and pkg not in found:
                found.append(pkg)
        elif not pkg and not _firmware_exists(f):
            unknown.append(f)
    cards_ = []
    if found:
        cards_.append(dict(kind="other", icon="dialog-warning", title=_("Firmware the kernel asked for"),
                           detail=ngettext("At start, a driver could not load its firmware. {names} has it.",
                                           "At start, drivers could not load their firmware. {names} have it.",
                                           len(found)).format(names=join(found)),
                           status="firmware", devices=[], aliases=[],
                           proposal=dict(kind="install", packages=found, firmware=True, device="",
                                         actions=[("install", n, dict(package=n)) for n in found])))
    if unknown:
        detail = _("At start, a driver asked for {files}, and no package of your repositories has it. Some drivers "
                   "ask for a newer file first and then use an older one: if everything works, it does not "
                   "matter.").format(files=join(unknown, 3))
        if any(f.startswith(("b43/", "b43legacy/")) for f in unknown):
            detail += " " + _("The b43 driver needs Broadcom's firmware, which openSUSE may not ship: install "
                              "b43-fwcutter and run install_bcm43xx_firmware as root, which downloads it.")
        cards_.append(dict(kind="other", icon="dialog-information", title=_("Firmware no package has"),
                           detail=detail, status="info", proposal=None, devices=[], aliases=[]))
    return cards_


def _firmware_exists(f):
    for d in FIRMWARE_DIRS:
        for ext in ("", ".xz", ".zst"):
            if os.path.exists(os.path.join(d, f + ext)):
                return True
    return False


# ============================================================================================== not needed

def _base(name):
    return name[:-6] if name.endswith("-32bit") else name


def classify(name, vendor=""):
    """The group of a package that is only useful with some hardware, or None."""
    b = _base(name)
    if b == "sof-firmware":
        return "firmware"
    if b.startswith("ucode-"):
        return "microcode"
    if b in WIRELESS_PACKAGES or b.startswith("broadcom-wl"):
        return "wireless"
    if b in VM_PACKAGES:
        return "vm"
    if b in GRAPHICS_FOR or NVIDIA_USERSPACE.match(b) or NVIDIA_KMP.match(b):
        return "graphics"
    if any(rx.match(b) for rx, _v, _k in PRINTER_PACKAGES):
        return "printers"
    v = (vendor or "").lower()
    if any(k in v for k, _vv in VENDOR_OF_PACKAGER) and re.search(r"lpr|cups|scan|print|wrapper", b):
        return "printers"
    return None


def candidate_names(hw):
    return [n for n, p in hw["installed"].items() if classify(n, p.get("vendor")) and n not in (FIRMWARE_ALL,)]


def _display_vendors(hw):
    return {d["vid"] for d in hw["pci"] if d["cc"].startswith("03")}


def _vendor_names(vids):
    names = {"1af4": _("virtual (virtio)"), "1b36": _("virtual (QXL)"), "15ad": _("VMware's virtual"),
             "80ee": _("VirtualBox's virtual"), "1234": _("QEMU's virtual")}
    return join(sorted({names.get(v) or PCI_VENDORS.get(v, v) for v in vids})) or _("no")


def _upper_first(text):
    return text[:1].upper() + text[1:]


def _rule(name, p, hw):
    """(reason, risk) when this installed package is for hardware this computer does not have, else None.
    risk: 'safe' (ticked), 'unsure' (unticked: a device that is often plugged in later), 'soft' (printers)."""
    b = _base(name)
    group = classify(name, p.get("vendor"))
    pats = hw_patterns(p.get("supplements", ()), hw["provided"])
    if pats and group != "printers":
        if matching(pats, hw["aliases"]):
            return None
        if group == "vm" and hw.get("virt") != "none":
            return None
    if group == "microcode":
        if not hw.get("cpu_vendor"):
            return None
        want = "amd" if b == "ucode-amd" else "intel" if b == "ucode-intel" else ""
        if not want or want == hw["cpu_vendor"]:
            return None
        return (_("For AMD processors; this one is Intel.") if want == "amd"
                else _("For Intel processors; this one is AMD.")), "safe"
    if group == "graphics":
        present = _display_vendors(hw)
        if NVIDIA_USERSPACE.match(b) or NVIDIA_KMP.match(b):
            if NVIDIA in present or "nvidia" in hw["modules"]:
                return None
            return _("For NVIDIA cards; this computer has {vendors} graphics.").format(
                vendors=_vendor_names(present)), "safe"
        need = GRAPHICS_FOR.get(b)
        if need is None:
            return None  # a generic one (vesa, fbdev, lavapipe…) or one Zypp Desk does not know
        if need & present:
            return None
        what = _vendor_names(need)
        return _("For {need} graphics; this computer has {vendors}.").format(
            need=what, vendors=_vendor_names(present)), "safe"
    if group == "vm":
        if hw.get("virt") != "none":
            return None
        return _("{what}. This computer is not a virtual machine.").format(
            what=_upper_first(VM_PACKAGES.get(b, b))), "safe"
    if group == "wireless":
        if b.startswith("broadcom-wl"):
            if any(d["vid"] == BROADCOM for d in hw["pci"]) or "wl" in hw["modules"]:
                return None
            return _("Broadcom's own Wi-Fi driver; this computer has no Broadcom card."), "safe"
        if not pats:
            return None
        what = WIRELESS_PACKAGES.get(b, b)
        only_usb = all(x.startswith("usb:") for x in pats)
        return _("For {what}; this computer has none.").format(what=what), "unsure" if only_usb else "safe"
    if group == "firmware":  # sof-firmware (kernel-firmware-* come from health.firmware_plan)
        if not pats:
            return None
        return _("For the sound processor of some Intel and AMD laptops; this computer has none."), "safe"
    if group == "printers":
        return _printer_rule(name, p, hw)
    return None


def _printer_rule(name, p, hw):
    b = _base(name)
    vendors, words = (), ()
    for rx, vv, kk in PRINTER_PACKAGES:
        if rx.match(b):
            vendors, words = vv, kk
            break
    else:
        pv = (p.get("vendor") or "").lower()
        vendors = tuple(v for k, v in VENDOR_OF_PACKAGER if k in pv)
    printers_ = hw["printers"]
    if any(not (q.get("model") or q.get("info")) for q in printers_):
        return None  # a printer whose driver could not be read might use it
    for q in printers_:
        text = (q.get("model") or "").lower()
        if any(w in text for w in words):
            return None
        if vendors and printer_vendor(q) in vendors:
            return None
    for u in hw["usb"]:
        v = USB_PRINTER_VENDORS.get(u["vid"])
        if v and v in vendors and (any(i["cls"] == "07" for i in u.get("ifaces", ()))
                                   or PRINTERISH.search(usb_name(u))):
            return None
    if not printers_:
        return _("No printer is set up on this computer."), "soft"
    if vendors:
        return _("For {vendors} printers; yours is not one of them.").format(
            vendors=join(sorted({v.upper() if v == "hp" else v.capitalize() for v in vendors}), 3)), "soft"
    return _("Your printer does not use it."), "soft"


def _requirement_names(req):
    if req.startswith("("):
        return [t for t in re.findall(r"[^\s()]+", req) if t not in _OPS and not re.match(r"^[<>=!]+$", t)
                and not re.match(r"^[\d.:~^+-]+[\w.+~^-]*$", t)]
    return [req.split(" ", 1)[0]]


def _capabilities(cands, hw):
    """{capability: {candidates}}: what these packages give (their provides, their names, their files)."""
    inst = hw["installed"]
    files = hw.get("candidate_files") or {}
    prov = {}
    for c in cands:
        for cap in inst[c]["provides"]:
            prov.setdefault(cap.split(" ", 1)[0], set()).add(c)
        prov.setdefault(c, set()).add(c)
        for f in files.get(c, ()):
            prov.setdefault(f, set()).add(c)
    return prov


def _dependencies(caps, hw):
    """(requirers, providers) for these capabilities: {package: requirements that name one of them} and {package:
    the ones of them it provides}, for the packages of hw["installed"]. From hw["installed"] itself when it has every
    requirement (tests), else from rpm: --whatrequires, read_rpm's requirements rpm cannot find that way, and
    providers_of() the capabilities that are required."""
    inst = hw["installed"]
    extra = hw.get("rpm_extra")
    if extra is None:
        requirers = {n: p["requires"] for n, p in inst.items() if p["requires"]}
        providers = {n: [c.split(" ", 1)[0] for c in p["provides"]] for n, p in inst.items() if p["provides"]}
        return requirers, providers
    requirers = {}

    def add(name, reqs):
        if name not in inst:
            return
        for req in reqs:
            if any(cap in caps for cap in _requirement_names(req)):
                requirers.setdefault(name, set()).add(req)

    for name, reqs in _who("--whatrequires", caps, "REQUIRENAME").items():
        add(name, reqs)
    for req, names in (extra.get("unindexed") or {}).items():
        if any(cap in caps for cap in _requirement_names(req)):
            for name in names:
                add(name, (req,))
    required = {cap for reqs in requirers.values() for req in reqs for cap in _requirement_names(req) if cap in caps}
    providers = {n: c for n, c in providers_of(required, hw).items() if n in inst}
    return requirers, providers


def needed_by_others(cands, hw):
    """{candidate: [packages that need it]} for the candidates something installed and staying still requires
    (also through a file it owns). Repeated until nothing changes: a kept one keeps what it needs."""
    cands = set(cands)
    # each round only looks at fewer candidates: who requires and who provides their capabilities is asked once
    requirers, providers = _dependencies(_capabilities(cands, hw), hw)
    kept = {}
    while True:
        prov = _capabilities(cands, hw)
        other_prov = set()
        for name, caps in providers.items():
            if name in cands:
                continue
            for cap in caps:
                if cap in prov:
                    other_prov.add(cap)
        new = {}
        for name, reqs in requirers.items():
            if name in cands:
                continue
            for req in reqs:
                for cap in _requirement_names(req):
                    if cap in prov and cap not in other_prov:
                        for c in prov[cap]:
                            new.setdefault(c, set()).add(name)
        if not new:
            return {c: sorted(v) for c, v in kept.items()}
        for c, by in new.items():
            kept.setdefault(c, set()).update(by)
            cands.discard(c)


def _locked(name, hw):
    return any(fnmatch.fnmatchcase(name, g) for g in hw.get("locks") or ())


def _loaded_module_owner(name, hw):
    """The loaded kernel module this package ships, or ''."""
    for f in (hw.get("candidate_files") or {}).get(name, ()):
        m = re.search(r"/lib/modules/[^/]+/.*/([\w-]+)\.ko(\.\w+)?$", f)
        if m and m.group(1).replace("-", "_") in {x.replace("-", "_") for x in hw["modules"]}:
            return m.group(1)
    return ""


def firmware_plan(hw):
    """health.firmware_plan() (another part of Zypp Desk), or None when it is not there."""
    if "fw_plan" in hw:
        return hw["fw_plan"]
    try:
        from . import health
        fn = health.firmware_plan
    except Exception:  # not written yet, or broken: no firmware group
        hw["fw_plan"] = None
        return None
    try:
        hw["fw_plan"] = fn()
    except Exception:
        hw["fw_plan"] = None
    return hw["fw_plan"]


def _pairs(items):
    out = []
    for it in items or ():
        if isinstance(it, str):
            out.append((it, ""))
        elif isinstance(it, dict):
            out.append((it.get("package") or it.get("name") or "", it.get("reason") or it.get("why") or ""))
        else:
            seq = list(it)
            out.append((seq[0], seq[1] if len(seq) > 1 else ""))
    return [(n, r) for n, r in out if n]


def _firmware_firewall(name, hw):
    """Why a firmware package health would remove must stay after all, or '': a device here matches it, or a loaded
    driver lists one of its files."""
    p = hw["installed"].get(name)
    if p is None:
        return ""
    if matching(hw_patterns(p["supplements"], hw["provided"]), hw["aliases"]):
        return _("A device of this computer can use it.")
    mf = hw.get("module_firmware") or {}
    for cap in p["provides"]:
        m = re.match(r"^firmware\((.+?)\)", cap)
        if m and m.group(1) in mf:
            return _("The driver {module}, loaded now, can use it.").format(module=mf[m.group(1)])
    return ""


def unneeded(hw):
    """What is installed for hardware this computer does not have: [dict(package, reason, size, group, risk)].
    Conservative: nothing a present device, a loaded module, the running kernel or a staying package needs; nothing
    locked or protected. risk: 'safe' (ticked), 'unsure' or 'soft' (printers): unticked."""
    inst = hw["installed"]
    items = {}
    for name, p in inst.items():
        if not classify(name, p.get("vendor")) or name in hw.get("protected", ()) or _locked(name, hw):
            continue
        if NVIDIA_KMP.match(name) or name.startswith("broadcom-wl-kmp"):
            if _loaded_module_owner(name, hw):
                continue
        r = _rule(name, p, hw)
        if r:
            items[name] = dict(package=name, reason=r[0], size=p["size"], group=classify(name, p.get("vendor")),
                               risk=r[1])
    plan = firmware_plan(hw)
    if plan:
        sizes = plan.get("sizes") or {}
        for name, why in _pairs(plan.get("remove")):
            if name not in inst or name in hw.get("protected", ()) or (_locked(name, hw) and name != FIRMWARE_ALL):
                continue
            if name != FIRMWARE_ALL and _firmware_firewall(name, hw):
                continue
            items[name] = dict(package=name, reason=why, size=sizes.get(name, inst[name]["size"]),
                               group="firmware", risk="safe")
    needed = needed_by_others(set(items), hw)
    hw["kept_needed"] = needed
    for name in needed:
        items.pop(name, None)
    if FIRMWARE_ALL in items and not any(i["group"] == "firmware" and n != FIRMWARE_ALL for n, i in items.items()):
        items.pop(FIRMWARE_ALL)  # nothing else to remove: the metapackage can stay
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    return sorted(items.values(), key=lambda i: (order.get(i["group"], 99), i["package"] != FIRMWARE_ALL,
                                                 -i["size"], i["package"]))


def group_title(group, items):
    names = [i["package"] for i in items]
    if group == "firmware":
        return _("Firmware for hardware you don't have")
    if group == "microcode":
        if names == ["ucode-amd"]:
            return _("Microcode for AMD processors")
        if names == ["ucode-intel"]:
            return _("Microcode for Intel processors")
        return _("Microcode for other processors")
    if group == "graphics":
        return _("Graphics drivers for cards you don't have")
    if group == "wireless":
        return _("Wi-Fi drivers for cards you don't have")
    if group == "vm":
        return _("Parts for virtual machines")
    if group == "printers":
        return _("Printer drivers")
    if group == "later":
        return _("For devices that are often plugged in later")
    return group


def group_detail(group, items, hw):
    names = [i["package"] for i in items]
    n = len(names)
    if group == "firmware":
        short = [x[len("kernel-firmware-"):] if x.startswith("kernel-firmware-") else x for x in names
                 if x != FIRMWARE_ALL]
        return ngettext("{n} package: {names}.", "{n} packages: {names}.", n).format(n=n, names=join(short, 6))
    if group == "microcode":
        cpu = {"intel": "Intel", "amd": "AMD"}.get(hw.get("cpu_vendor"), "?")
        own = {"intel": "ucode-intel", "amd": "ucode-amd"}.get(hw.get("cpu_vendor"), "")
        return _("{names}. This processor is {vendor} ({own} is the one it needs).").format(
            names=join(names), vendor=cpu, own=own)
    if group == "graphics":
        return _("{names}. This computer has {vendors} graphics.").format(
            names=join(names, 5), vendors=_vendor_names(_display_vendors(hw)))
    if group == "vm":
        if n == 1:
            return _("{name}, {what}. This computer is not a virtual machine.").format(
                name=names[0], what=VM_PACKAGES.get(_base(names[0]), ""))
        return _("{names}. This computer is not a virtual machine.").format(names=join(names, 5))
    if group == "wireless":
        return _("{names}. This computer has no such card.").format(names=join(names, 5))
    if group == "later":
        return _("{names}. None is plugged in now, but they are for USB devices. Unticked: keep them if you might "
                 "use one.").format(names=join(names, 5))
    if group == "printers":
        if not hw["printers"]:
            return _("{names}. No printer is set up here. Unticked: if one day you connect a printer, it may need "
                     "one of them.").format(names=join(names, 5))
        return _("{names}. Your printer does not use any of them. Unticked: if one day you connect another "
                 "printer, it may need one.").format(names=join(names, 5))
    return join(names)


def groups(items, hw):
    """The items by group, in order: [dict(group, title, detail, items, size, ticked)]. The unsure ones (for USB
    devices, which may just not be plugged in now) are a group of their own, unticked."""
    out = []
    for g in GROUP_ORDER:
        if g == "later":
            its = [i for i in items if i["risk"] == "unsure"]
        else:
            its = [i for i in items if i["group"] == g and i["risk"] != "unsure"]
        if not its:
            continue
        out.append(dict(group=g, title=group_title(g, its), detail=group_detail(g, its, hw), items=its,
                        size=sum(i["size"] for i in its), ticked=all(i["risk"] == "safe" for i in its)))
    return out


def stays(hw):
    """What stays and why, in sentences (the soft "Stays" card)."""
    out = []
    plan = hw.get("fw_plan")
    if plan:
        keep = [n for n, _w in _pairs(plan.get("keep"))]
        unsure = [n for n, _w in _pairs(plan.get("unsure"))]
        short = lambda names: join([n[len("kernel-firmware-"):] if n.startswith("kernel-firmware-") else n
                                    for n in names], 8)
        if keep:
            out.append(_("The firmware your devices use ({names}).").format(names=short(keep)))
        if unsure:
            out.append(_("And, just in case, firmware for devices that are often plugged in later, like USB network "
                         "adapters, cameras and docks ({names}).").format(names=short(unsure)))
    needed = hw.get("kept_needed") or {}
    if needed:
        out.append(ngettext("Other packages need {names}, so it stays.", "Other packages need {names}, so they stay.",
                            len(needed)).format(names=join(sorted(needed), 5)))
    locked = [n for n in hw["installed"] if classify(n, hw["installed"][n].get("vendor")) and _locked(n, hw)]
    if locked:
        out.append(ngettext("Locked, so it stays: {names}.", "Locked, so they stay: {names}.", len(locked)).format(
            names=join(sorted(locked), 5)))
    prot = [n for n in hw.get("protected", ()) if n in hw["installed"] and classify(n)]
    if prot:
        out.append(_("Protected in Applications: {names}.").format(names=join(sorted(prot), 5)))
    return out


def removal_changes(names, hw):
    """The queue changes for removing these packages: [(kind, target, opts)]: each removal, and the lock of
    kernel-firmware-all when it goes (health.firmware_changes knows how: limited to the repository that has it)."""
    names = list(dict.fromkeys(names))
    plan = hw.get("fw_plan")
    fw_changes = []
    if plan and any(n in names for n, _w in _pairs(plan.get("remove"))):
        try:
            from . import health
            fw_changes = [(k, t, dict(o)) for k, t, o in health.firmware_changes(plan)]
        except Exception:
            fw_changes = [("remove", n, {}) for n, _w in _pairs(plan.get("remove"))] + [
                ("lock", n, {"comment": _("Zypp Desk: only the firmware this computer uses")})
                for n in plan.get("lock") or ()]
    # health's changes for the firmware that is ticked here (not what this page kept), the lock of
    # kernel-firmware-all only when it goes; the queue runs that lock after the removal
    fw_removes = {t: o for k, t, o in fw_changes if k == "remove" and t in names}
    out = []
    for n in names:
        opts = dict(fw_removes.get(n) or {})
        opts.setdefault("package", n)
        out.append(("remove", n, opts))
    if FIRMWARE_ALL in names:
        out += [c for c in fw_changes if c[0] == "lock"]
    return out
