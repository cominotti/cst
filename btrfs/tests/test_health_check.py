"""Unit tests for the SMART part of btrfs/btrfs-health-check (no root needed).

The health check reads the NVMe health log (SMART) of the btrfs member disks
and of the disks under every swap device in /proc/swaps. These tests source
the script, which then defines its functions and stops before any check
runs, and call the functions with a fake /proc/swaps
and fake `lsblk` and `nvme` commands from a temporary folder. Nothing reads
the real disks or writes outside the temporary folder.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_health_check -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "btrfs-health-check")

# `lsblk -lnso NAME,TYPE DEV`: the device and everything below it.
LSBLK = {
    "dm-2": "dm-2 crypt\nnvme1n1p2 part\nnvme1n1 disk\n",
    "dm-3": "dm-3 crypt\nnvme1n1p3 part\nnvme1n1 disk\n",       # a second swap on the same disk
    "zram0": "zram0 disk\n",
    "dm-0": "dm-0 crypt\nnvme0n1p3 part\nnvme0n1 disk\n",       # swap on a btrfs member disk
}
HEALTHY = """Smart Log for NVME device:nvme1 namespace-id:ffffffff
critical_warning                        : 0
temperature                             : 38 C
available_spare                         : 100%
available_spare_threshold               : 10%
percentage_used                         : 3%
media_errors                            : 0
"""
FAILING = HEALTHY.replace("critical_warning                        : 0",
                          "critical_warning                        : 0x1").replace(
    "media_errors                            : 0", "media_errors                            : 12").replace(
    "available_spare                         : 100%", "available_spare                         : 5%")


class HealthCheckSmartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="health-check-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(os.path.join(self.tmp, "lsblk"))
        os.makedirs(os.path.join(self.tmp, "smart"))
        os.makedirs(self.bin)
        for name, out in LSBLK.items():
            self.write(f"lsblk/{name}", out)
        self.stub("lsblk", 'cat "${FAKE}/lsblk/$(basename "${@: -1}")" 2>/dev/null || exit 32')
        self.stub("nvme", '[[ "$1" == smart-log ]] || exit 2\ncat "${FAKE}/smart/$(basename "$2")" 2>/dev/null || exit 1')

    def write(self, rel, text):
        with open(os.path.join(self.tmp, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/bash\n{body}\n")
        os.chmod(path, 0o755)

    def swaps(self, *lines):
        self.write("swaps", "Filename\tType\tSize\tUsed\tPriority\n" + "".join(l + "\n" for l in lines))
        return os.path.join(self.tmp, "swaps")

    def run_lib(self, code):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE": self.tmp}
        r = subprocess.run(["bash", "-c", f'. "$1"; {code}', "health-check-test", SCRIPT],
                           env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def test_disk_under_encrypted_swap(self):
        swaps = self.swaps("/dev/dm-2                               partition\t134217724\t0\t-2")
        self.assertEqual(self.run_lib(f'swap_disks "{swaps}" nvme0n1 nvme2n1'), "nvme1n1\n")

    def test_files_zram_and_repeats_are_skipped(self):
        swaps = self.swaps("/dev/dm-2 partition 1 0 -2", "/dev/dm-3 partition 1 0 -3",
                           "/var/swap/swapfile file 1 0 -4", "/dev/zram0 partition 1 0 100")
        self.assertEqual(self.run_lib(f'swap_disks "{swaps}"'), "nvme1n1\n")

    def test_swap_on_a_member_disk_is_not_checked_twice(self):
        swaps = self.swaps("/dev/dm-0 partition 1 0 -2")
        self.assertEqual(self.run_lib(f'swap_disks "{swaps}" nvme0n1 nvme2n1'), "")

    def test_no_swaps_file(self):
        self.assertEqual(self.run_lib(f'swap_disks "{self.tmp}/missing"'), "")

    def test_failing_swap_disk_is_a_problem(self):
        self.write("smart/nvme1", FAILING)
        out = self.run_lib('smart_check nvme1n1 swap; printf "%s\\n" "${problems[@]}"')
        self.assertEqual(out.splitlines(), [
            "NVMe nvme1n1 (swap): critical_warning=1",
            "NVMe nvme1n1 (swap): media_errors=12",
            "NVMe nvme1n1 (swap): available_spare 5% below threshold 10%",
        ])

    def test_healthy_disk_and_member_label(self):
        self.write("smart/nvme1", HEALTHY)
        self.write("smart/nvme0", FAILING.replace("12", "1"))
        out = self.run_lib('smart_check nvme1n1 swap; smart_check nvme0n1; echo "n=${#problems[@]}";'
                           ' printf "%s\\n" "${problems[@]}"')
        lines = out.splitlines()
        self.assertEqual(lines[0], "n=3")
        self.assertIn("NVMe nvme0n1: media_errors=1", lines)     # member disks keep the old text

    def test_hex_critical_warning(self):
        # nvme-cli prints critical_warning in hex: 0x8 (read-only media) and
        # 0xa must count, not become "08" (a bash error) or "0".
        for raw, value in (("0x8", 8), ("0xa", 10), ("0", None), ("4", 4)):
            with self.subTest(raw):
                self.write("smart/nvme1", HEALTHY.replace(
                    "critical_warning                        : 0", f"critical_warning                        : {raw}"))
                out = self.run_lib('smart_check nvme1n1 swap; printf "%s\n" "${problems[@]}"')
                want = [f"NVMe nvme1n1 (swap): critical_warning={value}"] if value is not None else [""]
                self.assertEqual(out.splitlines(), want)

    def test_unreadable_smart_log_is_skipped(self):
        out = self.run_lib('smart_check nvme9n1 swap; echo "n=${#problems[@]}"')
        self.assertEqual(out, "n=0\n")

    def test_main_checks_swap_disks_and_names_them_in_the_ok_line(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        main = text.split('if [[ "${BASH_SOURCE[0]}" != "$0" ]]', 1)[1]
        self.assertIn('swap_disks /proc/swaps "${disks[@]}"', main)
        self.assertIn('smart_check "${disk}" swap', main)
        self.assertIn("swap disks: ${swap_only[*]}", main)


if __name__ == "__main__":
    unittest.main()
