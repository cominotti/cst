#!/usr/bin/env bash
set -euo pipefail

# Sets up a real loopback btrfs RAID1 layout matching Silverblue (/sysroot, /var, /var/home)
# and bind-mounts private writable shadow files over shared host-kernel sysfs/procfs knobs
# so Layer 2 tests cannot mutate the workstation kernel.

mkdir -p /e2e-disks /mnt/e2e-btrfs-init /run/e2e-shadow

for i in $(seq 0 63); do
    [[ -b "/dev/loop${i}" ]] || mknod -m 0660 "/dev/loop${i}" b 7 "${i}"
done

dev0=""
dev1=""
cleanup_on_err() {
    umount -R /var/home /sysroot /var /mnt/e2e-btrfs-init 2>/dev/null || true
    [[ -n "${dev0}" ]] && losetup -d "${dev0}" 2>/dev/null || true
    [[ -n "${dev1}" ]] && losetup -d "${dev1}" 2>/dev/null || true
}
trap cleanup_on_err ERR

truncate -s 25G /e2e-disks/disk0.img
truncate -s 25G /e2e-disks/disk1.img

dev0="$(losetup --find --show /e2e-disks/disk0.img)"
dev1="$(losetup --find --show /e2e-disks/disk1.img)"
printf '%s\n%s\n' "${dev0}" "${dev1}" >/run/e2e-loop-devs

mkfs.btrfs -f -d raid1 -m raid1 -L fedora-e2e "${dev0}" "${dev1}" >/dev/null

mount "${dev0}" /mnt/e2e-btrfs-init
btrfs subvolume create /mnt/e2e-btrfs-init/root >/dev/null
btrfs subvolume create /mnt/e2e-btrfs-init/var >/dev/null
btrfs subvolume create /mnt/e2e-btrfs-init/home >/dev/null

# Seed /var and /home before mounting the fresh subvolumes over them.
cp -a /var/. /mnt/e2e-btrfs-init/var/
if [[ -d /home/tester ]]; then
    cp -a /home/. /mnt/e2e-btrfs-init/home/
fi
umount /mnt/e2e-btrfs-init

opts="compress=zstd:1,flushoncommit,noatime,space_cache=v2,ssd,degraded"
mkdir -p /sysroot
mount -o "${opts},subvol=root" "${dev0}" /sysroot
mount -o "${opts},subvol=var" "${dev0}" /var
mkdir -p /var/home
mount -o "${opts},subvol=home" "${dev0}" /var/home
find /home -mindepth 1 -delete 2>/dev/null || true
rmdir /home
ln -s /var/home /home

# Record loopback filesystem UUID and expose /dev/disk/by-uuid symlink (udevd is masked)
uuid="$(findmnt -rn -o UUID --target /sysroot)"
printf '%s\n' "${uuid}" >/run/e2e-loop-uuid
mkdir -p /dev/disk/by-uuid
ln -sfn "${dev0}" "/dev/disk/by-uuid/${uuid}"

# Isolate shared kernel sysfs/sysctl files inside the container mount namespace.
shadow_file() {
    local target="$1" name="$2" default_val="$3"
    [[ -e "${target}" ]] || return 0
    cat "${target}" >"/run/e2e-shadow/${name}" 2>/dev/null || printf '%s\n' "${default_val}" >"/run/e2e-shadow/${name}"
    mount --bind "/run/e2e-shadow/${name}" "${target}"
}

shadow_file /sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost hwp_dynamic_boost 0
shadow_file /proc/sys/vm/dirty_bytes dirty_bytes 0
shadow_file /proc/sys/vm/dirty_background_bytes dirty_background_bytes 0
shadow_file /sys/kernel/mm/transparent_hugepage/defrag thp_defrag madvise
shadow_file /proc/sys/vm/watermark_scale_factor watermark_scale_factor 10
shadow_file /proc/sys/vm/min_free_kbytes min_free_kbytes 67584
for epp in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do
    [[ -e "${epp}" ]] || continue
    cpu_tag="$(basename "$(dirname "$(dirname "${epp}")")")"
    shadow_file "${epp}" "epp_${cpu_tag}" "balance_performance"
done
