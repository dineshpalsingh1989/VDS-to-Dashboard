"""Where the AI advisor keeps its data: eMMC by default, the NVMe SSD when one is mounted.

The IRIV Edge AI has an M.2 Key-M slot. An SSD there is faster and has much more
write endurance than the built-in eMMC, so the advisor moves its learning file
and report folders onto it. Formatting a blank SSD is never automatic; see
setup-ssd.sh.
"""

import json
import os
import tempfile
from pathlib import Path

SSD_FILESYSTEMS = {"ext4", "xfs", "btrfs", "f2fs", "exfat", "vfat"}
SYSTEM_MOUNTS = {"/", "/boot", "/boot/firmware"}


def read_mounts(path="/proc/mounts"):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    mounts = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            # /proc/mounts escapes spaces in paths as \040.
            mounts.append((parts[0], parts[1].replace("\\040", " "), parts[2]))
    return mounts


def mount_of(path, mounts):
    path = os.path.realpath(path)
    best = None
    for device, mount, fstype in mounts:
        prefix = mount.rstrip("/") + "/"
        if (path == mount or path.startswith(prefix) or mount == "/") and (best is None or len(mount) > len(best[1])):
            best = (device, mount, fstype)
    return best


def detect(data_dir, mounts_file="/proc/mounts", sys_block="/sys/block"):
    """Describe the SSD situation: none, ssd, ssd-unmounted or local-ssd."""
    mounts = read_mounts(mounts_file)
    own = mount_of(data_dir, mounts)
    if own and own[0].startswith("/dev/nvme"):
        return {"kind": "local-ssd", "mount": own[1]}
    for device, mount, fstype in mounts:
        if (device.startswith("/dev/nvme") and mount not in SYSTEM_MOUNTS and fstype in SSD_FILESYSTEMS
                and os.path.isdir(mount) and os.access(mount, os.W_OK)):
            return {"kind": "ssd", "mount": mount, "device": device}
    block = Path(sys_block)
    if block.is_dir() and any(entry.name.startswith("nvme") for entry in block.iterdir()):
        return {"kind": "ssd-unmounted"}
    return {"kind": "none"}


class Storage:
    """Remembers the chosen location in <data>/ai/storage.json so it survives restarts."""

    def __init__(self, data_dir):
        self.local = Path(data_dir) / "ai"
        self.pointer = self.local / "storage.json"

    def root(self):
        try:
            return Path(json.loads(self.pointer.read_text(encoding="utf-8"))["root"])
        except (OSError, ValueError, KeyError, TypeError):
            return self.local

    def on_ssd(self):
        return self.root() != self.local

    def set_root(self, root):
        handle, temporary = tempfile.mkstemp(dir=self.local, prefix=".storage-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"root": str(root)}, stream)
        os.replace(temporary, self.pointer)
