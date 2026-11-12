# Snapshots of /home (and an optional backup on the NAS)

This document explains everything that `cst btrfs snapshots ...` does.
The other storage tools of `cst btrfs` (health check, stall monitor,
degraded boot, Docker cleanup) are in [README.md](README.md). The entry point
of the whole repository is [../README.md](../README.md).
It is written for a reader who is not a Linux or btrfs expert. Technical words
are explained the first time they appear, and there is a glossary at the end
([section 18](#18-glossary)).

The document has two parts. **Part 1** is what you need to use it: setup,
what is kept, how to get a file back, alerts, problems, undo. **Part 2**
explains how it works inside. You do not need Part 2 for daily use.

Want a file back? Go to [section 6](#6-how-to-restore-a-file-or-a-folder).

Contents:

Part 1: using it

1. [What a snapshot is, and what it is not](#1-what-a-snapshot-is-and-what-it-is-not)
2. [Quick start](#2-quick-start)
3. [Which folders are left out of /home snapshots, and why](#3-which-folders-are-left-out-of-home-snapshots-and-why)
4. [Changes to your cargo and uv settings](#4-changes-to-your-cargo-and-uv-settings)
5. [When snapshots are taken and how long they are kept](#5-when-snapshots-are-taken-and-how-long-they-are-kept)
6. [How to restore a file or a folder](#6-how-to-restore-a-file-or-a-folder)
7. [Optional backup on the NAS (restic)](#7-optional-backup-on-the-nas-restic)
8. [All commands](#8-all-commands)
9. [Alerts](#9-alerts)
10. [What to do if...](#10-what-to-do-if)
11. [What changes on my system](#11-what-changes-on-my-system)
12. [How to undo](#12-how-to-undo)
13. [Known limitations](#13-known-limitations)

Part 2: how it works inside

14. [How a folder becomes a subvolume](#14-how-a-folder-becomes-a-subvolume)
15. [Safety rules of btrfs-snapshot-core](#15-safety-rules-of-btrfs-snapshot-core)
16. [The space guard](#16-the-space-guard)
17. [Why btrbk is no longer used](#17-why-btrbk-is-no-longer-used)
18. [Glossary](#18-glossary)

---

# Part 1: using it

## 1. What a snapshot is, and what it is not

**btrfs** is the filesystem of this computer. A filesystem is the part of
the operating system that stores files on the disk.

A **subvolume** is a part of a btrfs filesystem that looks like a normal
folder, but btrfs can manage it on its own. On this machine, `/var/home` (the
folder with the home folders of all users) is one subvolume.

**`/home` and `/var/home` are the same folder.** On Fedora Silverblue, `/home`
is a link to `/var/home`. In this document, "/home snapshots" and the source
name `home` both mean snapshots of `/var/home`.

A **snapshot** is a frozen picture of a subvolume at one moment. Think of a
photo of a bookshelf: later you can look at the photo and see which books
were there. Snapshots here are **read-only**: nobody can change them, not even
by mistake.

A snapshot is fast and small at first. btrfs uses **copy-on-write**: when you
change a file, btrfs writes the new data to a new place and keeps the old data
for the snapshot. So a snapshot only uses disk space for the data that changed
or was deleted after it was taken.

### What snapshots protect you against

- You deleted a file or a folder by mistake.
- You overwrote a file, or a program damaged it.
- You want to see how a file looked yesterday or last week.

### What snapshots do NOT protect you against

Snapshots are stored **on the same disks** as your data. If the disks are lost
(fire, theft, both disks broken), the snapshots are lost too. The disks are
also encrypted with **dm-crypt** (the Linux disk encryption). If the
encryption password is lost, the data and the snapshots cannot be read.

This machine uses **RAID1**: every piece of data is stored on both disks (two
**NVMe** disks, a fast type of SSD). RAID1 protects against **one** broken
disk. It does not protect against deleting a file: the deletion happens on
both disks at once.

A real **backup** is a copy on another device. That is the job of the optional
NAS backup ([section 7](#7-optional-backup-on-the-nas-restic)).

| Problem                         | RAID1 | Snapshots | NAS backup |
|---------------------------------|-------|-----------|------------|
| One disk breaks                 | yes   | no        | yes        |
| File deleted or overwritten     | no    | yes       | yes        |
| Both disks lost, machine stolen | no    | no        | yes        |

---

## 2. Quick start

### Where and how to run it

- Run the commands **on the host**. The host is the normal Fedora system,
  outside any container. A **toolbox** is a container (a separate, small
  Linux system inside your system) for development tools. The script refuses
  to run inside a toolbox.
- Run them through the `cst` command of this repository (CST, Cominotti
  Setup Tools): `cst btrfs ...` runs the script `btrfs-setup.sh`. How to make
  `cst` available (a symlink in `~/.local/bin` or an alias) is in
  [../README.md](../README.md#the-cst-command). Without that, type the full
  path, `~/Workspace/cst/cst btrfs ...`, or run `bash btrfs-setup.sh ...` from
  `~/Workspace/cst`: all three are the same script.
- Run them **as your normal user**, without `sudo`. The script asks for your
  password (through `sudo`) by itself when it needs root (administrator
  rights). Do not type `sudo cst ...`: `sudo` does not find `cst`.
- Do **not** run it from a root shell (for example after `sudo -i` or
  `su -`). Then the script cannot tell whose home to use and stops with
  `snapshots: run through sudo from your user account (needs SUDO_USER)`.
- Messages and alerts of the script write `cst btrfs ...`. Alerts and
  messages of an older version may still write `btrfs-setup.sh ...` or
  `sudo btrfs-setup.sh ...`: it is the same script, run it as shown above.

In this document, commands that start with `sudo` (for example `sudo ls`)
really need the `sudo`.

### The two commands

```
cd ~/Workspace/cst
cst btrfs snapshots setup        # once; shows a plan and asks once
cst btrfs snapshots status       # at any time
```

`setup` does everything in one run and is safe to run again:

1. It shows the plan: which folders become separate subvolumes, what moves
   into `~/Bulk` (with sizes), which old Rust `target/` folders it deletes
   (with their size), and the settings it writes.
2. It asks once: `Apply all of the above? [y/N]`. With no terminal (for
   example from a script), use `snapshots setup --yes`.
3. It installs the programs and the systemd units (systemd is the part of
   Linux that starts services and timers), writes your cargo and uv settings
   (through `cst toolchains`, which also starts the hourly pruning of cargo's
   build folders) and Chrome's cache flags ([section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)),
   fills `~/Bulk` ([section 3](#bulk-one-folder-for-everything-you-can-download-again)),
   deletes the old `target/` folders, converts the folders it can convert now,
   and starts the hourly snapshots.
4. At the end it prints the same text as `snapshots status`. If some
   `~/Bulk` items are not in place yet, it prints a `WARNING` first and says
   what to do.

> **Warning: answering `y` deletes data that cannot be restored.**
> It deletes `~/.cache/uv` and the old `target/` folders listed in the plan.
> This cannot be undone. They are only caches and build files: uv downloads
> again what it needs, and cargo builds again. But the first installs and
> builds after setup are slower.

`setup` can take a while. After each converted folder it deletes the old copy
of that folder right away (see [section 14](#14-how-a-folder-becomes-a-subvolume)).

### Do I need to reboot?

You do not need to reboot now. But if some folders were busy (used by your
desktop session, an IDE, Steam...), they are converted (or moved into
`~/Bulk`) at your **next normal reboot**, before the login screen. **Until then, no snapshot is taken**
(the reason is in [section 14](#why-snapshots-wait)). The two kinds differ
in how you can avoid the reboot:

- A busy **folder to convert**: close the program that uses it, and the
  hourly run may convert it by itself; then snapshots start without a reboot.
- A busy **`~/Bulk` item**: the hourly run never moves it. Close the program
  that uses it and run `cst btrfs bulk apply` again (or reboot).

### How do I know it worked?

Run `cst btrfs snapshots status`. Example (shortened, the numbers are
made up), a few hours after setup, with one folder still busy:

```
hourly snapshots: enabled, keep /home 72h 7d 4w 3m, ~/Workspace 48h 7d (btrfs-snapshot-core)
  last successful run: 2026-10-13 14:03:12
  conversion of busy directories pending: at the next boot, before login (no snapshot until then)
nested subvolumes:
  ok                      ~/.cache
  ok                      ~/.local/share/flatpak
  pending (busy)          ~/Workspace
  ok                      ~/Bulk
Chrome (Flatpak): HTTP cache in the sandbox /tmp (RAM, emptied at reboot), at most 512 MiB
  Chrome Flatpak:        installed (system)
  ~/.var/app/com.google.Chrome/config/chrome-flags.conf:
                         flags present (managed by CST)
  in RAM now:            87 MiB (/run/user/1000/.flatpak/com.google.Chrome/tmp/chrome-cache)
  old on-disk HTTP cache: 2.5 GiB in 2 profile(s), in /home snapshots. Quit Chrome
                         (all windows), start it again, then delete it (never done automatically):
                         rm -rf ~/.var/app/com.google.Chrome/cache/google-chrome/*/Cache
NAS backup: off
```

The state of `~/Bulk` is shown by `cst btrfs bulk status`
(and in `cst btrfs status`). Example (shortened):

```
~/Bulk: everything in it can be downloaded again: no snapshots, no NAS backup.
  ~/Bulk                                                   ok (subvolume)
  ~/Bulk/CACHEDIR.TAG                                      ok
  ~/Programs -> ~/Bulk/apps                                ok
  ~/.android/avd -> ~/Bulk/emulators/android/avd           ok
                                                           +C (NOCOW): yes
  ~/Bulk/steam                                             ok
    flatpak override: com.valvesoftware.Steam may use it   ok
  ~/.m2/repository -> ~/Bulk/toolchains/jvm/m2/repository  move 8.0 GiB into ~/Bulk (reflink clone, no data written), then the link; waits for the next boot (busy)
  ~/.gradle/caches -> ~/Bulk/toolchains/jvm/gradle/caches  ok  (no link: ~/.gradle absent; linked once it exists)
  ~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps
                                                           still 72.7 GiB of games here, in /home snapshots: move them to
                                                           the ~/Bulk/steam library (Steam > Settings > Storage; btrfs/SNAPSHOTS.md, section 3)
```

`ok` means in place. `waits for the next boot (busy)` means it is moved at the
next boot. `NEEDS A LOOK: ...` means it was left alone: see
[section 3](#order-with-snapshots-setup).

What to look at:

- `hourly snapshots: enabled`: the hourly timer is on.
- `conversion of busy directories pending` or `old trees still being deleted`:
  no snapshot is taken yet. This is expected right after setup.
- `pending (busy)`: this folder is still waiting to be converted.
- `items to move into ~/Bulk pending`: busy `~/Bulk` items wait for the next
  boot; no snapshot until then.
- `absent`: the folder does not exist (for example, the app is not installed).
  Nothing to do.
- `Chrome (Flatpak): ...`: the state of Chrome's cache flags, and how much
  old cache is still on the disk
  ([section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)).
- Once snapshots run, lines like these appear:
  `home: 41 snapshot(s); oldest 20261001T0900; newest 20261013T1400`.

The program messages say "directory". A directory is the same as a folder.

The NAS backup is a separate, optional step: [section 7](#7-optional-backup-on-the-nas-restic).

---

## 3. Which folders are left out of /home snapshots, and why

**Why:** a snapshot keeps every old version of the data that changed after it.
If a folder is **big**, is **rewritten often**, and can be **downloaded or
built again**, then snapshots of it would fill the disk with old copies that
have no value. Caches, IDE programs, SDKs and game files are the typical
example.

**How:** btrfs never puts a subvolume inside the snapshot of its parent. So
the script makes such folders **nested subvolumes** (a subvolume inside
another subvolume). In a `/home` snapshot, a nested subvolume shows up as an
**empty folder**. Programs see no difference: it is still a normal folder at
the same place.

There are two kinds:

1. **`~/Bulk`**: one visible folder for everything you can download again
   ([below](#bulk-one-folder-for-everything-you-can-download-again)). This is
   where IDE programs, SDKs, emulator disks, AI models, ISOs, a Steam library
   and toolchain caches live.
2. **A few folders that stay where they are**, because they cannot be moved
   to `~/Bulk` (the table below). The list comes from `SNAP_EXCLUDE` in
   `btrfs/setup-snapshots.bash`:

| Folder (in your home)                | Why it leaves /home snapshots |
|--------------------------------------|-------------------------------|
| `.cache/uv`                          | Pure cache of the Python tool uv. It can be very large (about 157 GiB on this PC when the script was written). It is **deleted**, not kept: uv downloads again what it needs. |
| `.cache`                             | Caches of many programs. Also holds cargo's build files (see [section 4](#4-changes-to-your-cargo-and-uv-settings)). |
| `.local/share/flatpak`               | Flatpak apps installed for your user (Flatpak is a way to install desktop apps in their own sandbox). The whole folder, because Flatpak uses hardlinks between its parts. A **hardlink** is a second name for the same file, and hardlinks cannot cross from one subvolume to another. |
| `Workspace`                          | Your projects. Not lost: it gets **its own snapshots**, with a shorter retention ([section 5](#5-when-snapshots-are-taken-and-how-long-they-are-kept)). |
| `Bulk`                               | Everything you can download again (next part). Normally created by the Bulk step just before; if you already had a plain `~/Bulk` folder, it is converted like the others. |

> **Important:** files in these folders (except `Workspace`) are **not in any
> snapshot and not in the NAS backup**. If you delete them, they are gone.

**What stays in /home snapshots** (examples): your documents, `Downloads`
(except `Downloads/iso`, which is a link into `~/Bulk`), `Videos`,
`~/.claude`, `~/.codex`, game saves, LM Studio conversations, toolbox
containers, the **settings and credentials** of your toolchains (for example
`~/.m2/settings.xml`, `~/.cargo/config.toml`, `~/.cargo/credentials.toml`,
`~/.cargo/bin`, `~/go/bin`, `~/.rustup/settings.toml`,
`~/.gradle/gradle.properties`, `~/.npmrc`, npm's logs in `~/.npm/_logs`), any
`~/Bulk` item you keep in /home on purpose
([section 12](#undo-one-bulk-link)), and everything else not listed here.

If a folder in the table does not exist yet, but its parent folder exists,
`setup` creates it as an empty subvolume, so the program writes straight into
it later. If the parent does not exist either (for example, an app that is not
installed), nothing happens now. The hourly run looks again every hour, and
creates the subvolume when the parent appears.

How a folder is converted, step by step: [section 14](#14-how-a-folder-becomes-a-subvolume).

One more cache is kept out without a subvolume: Chrome's (Flatpak) web cache
now lives in RAM ([below](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)).
Earlier versions made `~/.var/app/com.google.Chrome/cache` a subvolume.

### Bulk: one folder for everything you can download again

**The rule is simple: everything in `~/Bulk` can be downloaded (or built)
again. So it gets no snapshots and no NAS backup.** If you lose something in
`~/Bulk`, you download it again. Put nothing there that you cannot get back.

`~/Bulk` is one nested subvolume, owned by you, in your home. It is normal
btrfs: compressed and with checksums (only the `emulators` folder inside is
different, see [below](#why-copy-on-write-stays-on-except-for-the-emulator-disks)).

The programs keep using their usual paths. Each usual path becomes a
**symlink** (a shortcut that points to another path) into `~/Bulk`:

| Usual path (now a symlink)                          | Real place in `~/Bulk`          | What it is | Id |
|-----------------------------------------------------|---------------------------------|------------|----|
| `~/Programs`                                        | `apps/`                         | Programs you installed by hand (AppImages, tarballs). Your `.desktop` launchers keep working, because the old path still leads there. | `apps` |
| `~/.local/share/JetBrains/Toolbox/apps`             | `apps/jetbrains/`               | The IDE programs that JetBrains Toolbox installs. Toolbox's accounts and settings stay in /home. | `jetbrains` |
| `~/.var/app/ai.lmstudio.lm-studio/.lmstudio/models` | `models/`                       | LM Studio (Flatpak) models. Your LM Studio conversations stay in /home. | `models` |
| `~/.android/avd`                                    | `emulators/android/avd/`        | Android emulator disks. **The only NOCOW (`+C`) data** (see below). | `avd` |
| `~/Downloads/iso`                                   | `iso/`                          | ISOs and disk images. The rest of `Downloads` keeps its snapshot history. | `iso` |
| (no link)                                           | `steam/`                        | A Steam library; `bulk steam apply` moves your installed games into it ([below](#steam-move-your-games-into-the-bulk-library)). | `steam` |
| `~/Android/Sdk`                                     | `toolchains/android/sdk/`       | The Android SDK (the emulator disks are in `emulators/`, see the notes). | `android-sdk` |
| `~/.m2/repository`                                  | `toolchains/jvm/m2/repository/` | Maven's downloaded artifacts. `~/.m2/settings.xml` stays. | `m2` |
| `~/.gradle/caches`, `~/.gradle/wrapper`             | `toolchains/jvm/gradle/caches/`, `toolchains/jvm/gradle/wrapper/` | Gradle's downloads. `gradle.properties` stays. | `gradle-caches`, `gradle-wrapper` |
| `~/go/pkg/mod`                                      | `toolchains/go/pkg/mod/`        | Go's module cache. `~/go/bin` stays. `go clean -modcache` removes the link (see the notes). | `go-mod` |
| `~/.rustup/toolchains`                              | `toolchains/rust/rustup/toolchains/` | Installed Rust toolchains. `~/.rustup/settings.toml` stays. | `rustup` |
| `~/.rustup/tmp`, `~/.rustup/downloads`              | `toolchains/rust/rustup/tmp/`, `toolchains/rust/rustup/downloads/` | rustup's scratch folders. rustup unpacks into `tmp` and then **renames** the files into the toolchain; a rename cannot cross from one subvolume to another (the error is `Invalid cross-device link`), so they must sit next to the toolchains. | `rustup-tmp`, `rustup-downloads` |
| `~/.cargo/registry`, `~/.cargo/git`                 | `toolchains/rust/cargo/registry/`, `toolchains/rust/cargo/git/` | Downloaded crates. `config.toml`, `credentials.toml` and `bin/` stay. | `cargo-registry`, `cargo-git` |
| `~/.npm/_cacache`                                   | `toolchains/node/npm/_cacache/` | npm's download cache. The rest of `~/.npm` (logs, `_npx`) and `~/.npmrc` stay. `npm cache clean --force` removes the link (see the notes). | `npm-cache` |

Notes:

- The folders are grouped by **how the data is treated**, not by product.
  `toolchains/` holds SDKs and download caches: all copy-on-write, like the
  rest of `~/Bulk`. `emulators/` holds virtual disks you can throw away: all
  `+C` (NOCOW). So the Android SDK is in `toolchains/android/sdk`, and its
  emulator disks are in `emulators/android/avd`. There is no `android/`
  folder at the top of `~/Bulk`.
- The toolchain caches are grouped as `toolchains/<language>/<tool>/`, and
  each folder keeps its own name. So the real place reads like the path it
  replaces: `~/.m2/repository` lives in `toolchains/jvm/m2/repository`,
  `~/go/pkg/mod` in `toolchains/go/pkg/mod`. You can guess where a cache is
  without looking it up. The language is `jvm`, not `java`, because Maven and
  Gradle also serve Kotlin, Scala and Groovy. The folders in between (for
  example `toolchains/jvm/m2`) are made by the script, as you.
- All the folders in `~/Bulk` are always created, also for tools you do not
  have. The **link** is only made when the tool's own folder exists (for
  example `~/.gradle` for Gradle, `~/.npm` for npm). Install the tool later and run
  `cst btrfs bulk apply` again: then its link is made, and what it
  downloaded meanwhile is moved in.
- Only the part that can be downloaded again moves. Settings and credentials
  stay in /home, so they keep their snapshot history (the script never reads
  `settings.xml` or any credentials file).
- You can also put your own downloadable things into `~/Bulk` directly (for
  example a `~/Bulk/vm-images` folder). The same rule applies to them.
- **You want one item to stay in /home** (with its snapshot history)? Tell
  the script, with the id from the last column:
  `cst btrfs bulk keep android-sdk`. It then leaves that item alone
  and shows `kept in /home (by choice)`; nothing is reported as a problem.
  `bulk unkeep android-sdk` undoes it. If the item is already in `~/Bulk`,
  move its data back as in [section 12](#undo-one-bulk-link).
- A program that **renames** files from a folder that stays in /home into a
  folder in `~/Bulk` (or back) fails with `Invalid cross-device link`, because
  a rename cannot cross subvolumes. Most tools copy in that case, and rustup
  is handled (its scratch folders moved too, see the table). *(Not verified:
  JetBrains Toolbox downloads into `~/.local/share/JetBrains/Toolbox/download`,
  which stays in /home. If an IDE install or update fails with that error,
  undo that link as in [section 12](#undo-one-bulk-link) and tell the
  maintainer.)*
- `npm cache clean --force` and `go clean -modcache` delete **the link
  itself**, not what it points to. The next `npm` or `go` command then makes
  a new real folder in /home, and the old copy stays in `~/Bulk`. Nothing
  breaks: the next `bulk apply` (or `snapshots setup`) sees this for these
  two caches, deletes the stale copy in `~/Bulk` and moves the new folder in.
  Until then the new cache is in /home (and in snapshots taken meanwhile).
- **Containers** (Docker, Podman) that mount only the tool's folder, for
  example `docker run -v ~/.m2:/root/.m2`, see the link inside, but not
  `~/Bulk`: the link is broken in there, and the build fails or downloads
  everything into the container. Mount the `~/Bulk` place as well, or mount
  the real place directly: `-v ~/Bulk/toolchains/jvm/m2/repository:/root/.m2/repository`.
  (Toolboxes are not affected: they see your whole home.) While such a
  container runs, `bulk apply` treats the item as busy.

#### Why symlinks, and not a setting in each tool

One symlink works for everybody at once: the host, every toolbox (they see
your home at the same path), IDEs, Flatpak apps (with the permission below)
and scripts. A setting would have to be made in each tool, sometimes in
several places, and forgotten ones would quietly write into /home again. A
symlink also changes nothing in the tool's own files: undoing it is easy
([section 12](#undo-one-bulk-link)).

#### Why copy-on-write stays on (except for the emulator disks)

**Copy-on-write** is how btrfs normally writes; it is what gives each block a
**checksum**. With RAID1 (two copies of every block, one per disk), the
checksum is what lets btrfs **repair itself**: when one copy is damaged, btrfs
sees that its checksum is wrong and uses the other copy. Compression also
needs copy-on-write.

The **NOCOW** mark (`+C`) turns copy-on-write off, and with it checksums and
compression. Then btrfs cannot tell a damaged copy from a good one, and RAID1
cannot repair it. That is a bad trade for files that are written once and
read many times (IDE programs, SDKs, models, ISOs, toolchains): they gain
nothing from NOCOW.

Only **`~/Bulk/emulators`** and the folders in it have `+C`; today that is
`emulators/android/avd`. Emulator disk images are big files
that the emulator rewrites in place all the time; with copy-on-write they
become very fragmented and slow. They are also disposable: if one gets
damaged, you wipe the emulator and start it again. The Android SDK, next to
them in spirit, is written once and read many times, so it stays
copy-on-write in `toolchains/android/sdk`.

The script also puts `+C` on `~/Bulk/emulators` and `~/Bulk/emulators/android`
themselves. A folder's `+C` is passed on to what is created in it later, so a
folder you add there for another emulator (for example
`~/Bulk/emulators/my-vm`) gets `+C` by itself. `+C` on a folder only affects
files created in it **afterwards**: files that were already there keep
copy-on-write. `bulk status` checks the AVD folder, everything in it, and
`emulators`/`emulators/android`; a missing mark there is set again by the next
`bulk apply` (in place, nothing is copied).

`+C` only works on **empty** files. So the existing emulator data is
**really copied** (not moved, not reflinked) into the `+C` folder: every file
is created there empty, gets the mark from its folder, and only then receives
its data. For a while this needs the free space of that data twice (the move
is skipped if less than its size plus 20 GiB is free).

#### CACHEDIR.TAG

The file `~/Bulk/CACHEDIR.TAG` is a standard marker for "this folder is a
cache" ([the standard](https://bford.info/cachedir/)). Backup programs such as
restic and borg skip folders that have it (restic with `--exclude-caches`,
which the NAS backup uses). It is a second safety net: even a copy of your
home made by another backup tool leaves `~/Bulk` out. Do not delete it.

#### Steam: move your games into the Bulk library

Steam (Flatpak) keeps its main library in
`~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps`. `bulk
apply` does **not** move or link it: Steam must know about a library itself.
Instead it creates `~/Bulk/steam` and gives the Steam Flatpak permission to
use it (`flatpak override --user --filesystem=<your home>/Bulk/steam com.valvesoftware.Steam`;
only if Steam is installed). The games then move with their own command, once:

```
# Quit Steam first (Steam > Exit; closing the window is not enough).
cst btrfs bulk steam plan     # dry run: what would move, and why not
cst btrfs bulk steam apply    # move them
```

**What moves.** Every installed game of the main library: its folder in
`steamapps/common`, its Workshop content and Workshop file, and its manifest
(`appmanifest_<id>.acf`, the file that tells Steam the game is installed).
First, before any game is copied, `~/Bulk/steam` becomes a library: it gets
its marker file (`~/Bulk/steam/libraryfolder.vdf`) and an entry in both
copies of Steam's library list (`config/libraryfolders.vdf` and
`steamapps/libraryfolders.vdf`, which current Steam versions keep identical).
Each of those two files is backed up first, next to it
(`libraryfolders.vdf.cst-bak-<date>`, the five newest are kept). If
`~/Bulk/steam` is already in the list (for example you added it in Steam,
even under another spelling of the same folder such as `/home/...` instead of
`/var/home/...`), that entry is used and no second one is added. Then each
game moves by a **reflink copy** (the data blocks are shared, nothing is
written, so it takes seconds even for 70 GB), is checked, and is switched into
`~/Bulk/steam/steamapps`; the manifest goes last, so Steam never sees a game
without its files. Last, the game is listed under the new library in both
files. So if a move stops (a game is busy, a check fails), `~/Bulk/steam` may
already be listed as an empty library; that is harmless, and the next `apply`
goes on from there. Nothing is downloaded again, and your saves and settings
are not touched.

**Games with NOCOW data: a real copy, only with `--copy`.** If the game's
files have the NOCOW mark (`+C`; for example the whole Steam folder was made
NOCOW at some point, which `lsattr -d <folder>` shows as a `C`), they cannot
be reflinked: btrfs refuses to share blocks between a NOCOW file and a
copy-on-write one (`cp` says `failed to clone ... Invalid argument`), and
`~/Bulk/steam` stays copy-on-write on purpose ([why](#why-copy-on-write-stays-on-except-for-the-emulator-disks)).
The mark cannot be taken off a file that already has data, so the data has to
be written again. `plan` checks every file and says, per game, either
`move N GiB (reflink, no data written)` or `copy N GiB byte by byte: K files
in it have +C (NOCOW), for example <path>`; a game can have both (for example
a NOCOW game folder and a copy-on-write manifest). `N` is the size of every
part (game folder, Workshop content, Workshop file, manifest) that holds at
least one `+C` file, even an empty one, so look at `K` and the example: if
only a few small files have `+C`, deleting them (or writing them again into
a folder without `+C`) lets the game move by reflink instead. Without `--copy`, `apply` refuses the
games that need a copy (`NEEDS A LOOK`, exit status 1) and still moves the
others. With `cst btrfs bulk steam apply --copy`:

- The copy (`cp -a --reflink=never`) goes into a copy-on-write folder, so the
  new files **gain checksums** (RAID1 scrub can repair them) **and zstd
  compression**. The script checks that no folder on the way has `+C` (it
  refuses otherwise) and that the copy has no `+C` entry.
- **Space**: the copy shares nothing with the original, so the game's full
  size plus **170 GiB** must be free in `~/Bulk`, checked before each game.
  The old copy may stay in older `/home` snapshots for a while, so its space
  comes back only when those snapshots are deleted. That is why 170 GiB must
  stay free after the copy: the snapshot emergency limit (150 GiB, see
  [section 16](#16-the-space-guard)) plus 20 GiB. Without it, a large copy
  could push free space down to the emergency purge, which deletes most of
  the snapshot history. The script reads the limit the way `btrfs-snapshot`
  does (its default, then `/etc/btrfs-setup/snapshots.conf`), so a changed
  limit counts here too. If free space after all copies would be 250 GiB or less, `plan`
  adds a `NOTE`: hourly `/home` snapshots then pause until free space is above
  270 GiB again (once old snapshots expire). Moving fewer games per run
  (`--app ID`) may avoid that.
- **Time**: every byte is written once and then read twice (the original
  and the copy, compared byte for byte; the copy is read back from the disk,
  so btrfs also checks its new checksums). It runs at the lowest CPU priority
  and the idle IO class; the idle class only has an effect with the BFQ IO
  scheduler (NVMe disks often use `none`, then only the CPU priority is
  lower). Expect minutes per 10 GiB, more while the PC is busy. `apply` prints
  a line when each game starts and ends (with the time it took), and a
  progress line every 30 seconds or 5 GiB, also in the middle of a large
  file.
- The copy is written to the disk before it is read back. If that write
  reports an error (`sync` or `fsync` fails, for example an IO error on a
  disk), the copy is deleted and the original stays.
- If anything differs (a name, a type, a size, a mode, a link, or the content
  of one file), the copy is deleted, the original stays, and the game is
  reported (`NEEDS A LOOK`).

**What stays** in the main library:

- **Tools**: the Steam Linux Runtime, Proton, the Steamworks Common
  Redistributables. Games need them, and they are small. `plan` shows them as
  `skipped: a tool`; to move one anyway, name it: `bulk steam apply --app 1628350`.
- **Shader caches** (`steamapps/shadercache/<id>`): Steam builds a new one in
  `~/Bulk/steam` the next times you play. `plan` shows the size of the old
  one; delete it by hand once Steam has built the new one (with Steam closed).
- An **empty** Proton folder (`steamapps/compatdata/<id>`): Steam makes it
  again where needed.

**When it refuses** (`NEEDS A LOOK`, nothing touched for that game; [section 10](#10-what-to-do-if)
says what to do for each):

- Steam is running (exit status 5, nothing done at all).
- A game is in the middle of an update or download.
- The game has a Proton prefix with data (`compatdata/<id>`, where Windows
  games keep saves that may not be in the cloud): use Steam's own **Move
  install folder** for that game.
- Something already sits at the new place (it is never merged), the game
  folder is a symlink, holds files that are not yours, or another subvolume.
- One of the `libraryfolders.vdf` files is not in the exact form Steam writes,
  or has a key the script does not know, or the two copies differ. The script
  only writes a file it can write back byte for byte.
- `~/Bulk` is not on the same filesystem (no reflink: `reflink of a
  copy-on-write file into ~/Bulk/steam failed`), or has less than 5 GiB free.
- The game has NOCOW data and you did not add `--copy` (see above), or there
  is not enough free space for its real copy.
- A folder in `~/Bulk/steam` on the way to the game has `+C` (what is created
  in it would be NOCOW too).
- You keep `steam` out of `~/Bulk` (`bulk keep steam`).

**One step stays manual: the default library.** New games still go to the
main library until you change that, once: **Steam > Settings > Storage**, pick
`~/Bulk/steam` in the drive list, then the `...` menu > **Make default**. The
script does not set it, because Steam keeps that setting in
`config/config.vdf`, the same file that holds your login data; the script never
reads or writes that file.

**One library per drive.** Steam's own settings refuse a second library on the
same drive, and `~/Bulk` is on the same disks as your home. That is one more
reason why the script writes the library entry itself: Steam accepts libraries
that are already in its list. *(Not verified on every Steam version: after the
first `apply`, start Steam and check that **Settings > Storage** lists both
libraries and that the moved games start.)*

**Or by hand, in Steam** (also the way for a game the script refuses):

1. Quit Steam completely and start it again (it reads the new permission at
   start).
2. **Steam > Settings > Storage**. Open the drop-down list of drives and choose
   **Add Drive** (on some versions: the `+` button). Choose the folder
   `~/Bulk/steam` (your home, then `Bulk`, then `steam`). If Steam refuses
   because it is the same drive, use `bulk steam apply` for one game first:
   it registers the library.
3. Right-click the game in the library > **Properties > Installed Files >
   Move install folder**. Steam moves the files and keeps your saves and settings.
4. Optional: make `~/Bulk/steam` the default library, as above.

*(Not verified: the exact menu names differ between Steam versions.)*

Until they move, the games stay in the old library, **inside `/home`
snapshots**. `cst btrfs bulk status` (and `snapshots setup`) shows
how much game data is still there. On a machine where an earlier version of
this script already made `steamapps` a subvolume, it stays one, still left
out of snapshots; moving the games is then optional.

#### LM Studio

LM Studio is a Flatpak. A Flatpak app only sees the folders it was given
permission for, so a symlink to a folder outside its own data would be broken
inside the app. If LM Studio is installed, the script therefore also runs, as
you, `flatpak override --user --filesystem=<your home>/Bulk/models
ai.lmstudio.lm-studio`. Restart LM Studio after the first `apply`. In LM
Studio's settings the models folder stays the usual one.

#### Order with `snapshots setup`

`snapshots setup` fills `~/Bulk` **first**, before it converts the folders of
the table and before the first snapshot. So the data that moves to `~/Bulk` is
never cloned for nothing, and never captured by a snapshot. You can also fill
`~/Bulk` on its own, before (or without) snapshots:

```
cst btrfs bulk plan      # dry run: what would happen, item by item
cst btrfs bulk apply     # do it (safe to run again)
cst btrfs bulk status    # the state of each item
```

`apply` is **idempotent**: you can run it any time; what is already right is
left alone. For each item it does one of these:

| What it finds | What it does |
|---|---|
| nothing at the usual path (the tool's folder exists) | creates the folder in `~/Bulk` and the link |
| the link, already right | nothing |
| a real folder with data, and an empty place in `~/Bulk` | moves the data into `~/Bulk` (a reflink clone: no data is written, so even many GiB take seconds; for the emulator disks a real copy), checks it, then puts the link in place of the folder |
| an empty folder (even if `~/Bulk` already has data for it, for example after a tool replaced the link by a new folder) | replaces it by the link |
| for `go-mod` and `npm-cache` only: a real folder with data **and** data in `~/Bulk` (their clean command removed the link, [notes above](#bulk-one-folder-for-everything-you-can-download-again)) | deletes the stale copy in `~/Bulk` (only a cache), then moves the folder in as above |
| files in the folder that are **not yours** (for example made by a container that ran as root) | touches nothing and shows `NEEDS A LOOK` with the `chown` command that fixes it (your account could copy them, but could not delete the old folder afterwards) |
| a subvolume made by an earlier version of this script | renames it into `~/Bulk` (instant; it stays a nested subvolume there); the emulator disks are copied instead, to get `+C` |
| something unexpected: a file where a folder belongs, a symlink that points somewhere else, data **both** at the usual path and in `~/Bulk` (except the two caches above), a symlink on the way (for example `~/.cargo` itself a symlink) | **touches nothing** and shows `NEEDS A LOOK` with the reason |
| a folder that is **in use** (an IDE, the emulator, LM Studio, a terminal open in it, a program started from it) | leaves it for the **next boot** (below) |
| an item you keep in /home on purpose (`cst btrfs bulk keep ID`, listed in `/etc/btrfs-setup/bulk-keep`) | **touches nothing** and shows `kept in /home (by choice)`; not a problem, nothing waits for it |

**Busy items** are moved at the next normal boot, **before the login screen**
(`btrfs-bulk-boot.service`), when nothing of your session runs. Until then
**no snapshot is taken** (it would keep their data for months). To do it
without a reboot, close the programs and run `cst btrfs bulk apply`
again. After two boots with items still busy, it gives up, removes the flag
and sends an alert (shown at your next login). The items stay where they are,
and snapshots start.

**`NEEDS A LOOK` items are not moved automatically.** `snapshots setup`
warns about them **before** it starts the snapshots, and then asks whether to
start them anyway (with `--yes` it does not start them). Fix each one by hand
(for example move the data yourself, or remove the odd symlink), then run
`bulk apply` again, and `snapshots setup` again if it did not start the
snapshots. If you want that item to stay where it is, say so with
`cst btrfs bulk keep ID` (the id is in the table above): then it is
no longer reported, and `snapshots setup` no longer asks. (Keeping `apps`
keeps `~/Programs` in /home, and `jetbrains` then cannot move into
`~/Bulk/apps`: it waits, or, if JetBrains Toolbox has IDEs installed,
it needs a look. Keep `jetbrains` as well, or unkeep `apps`.) Once snapshots run,
the hourly run does not wait for these items: their data would stay in /home
snapshots for months. (One exception that never makes `snapshots setup` ask:
a problem with the emulator folder **inside** `~/Bulk`, from the `+C` repair of
[section 14](#how-an-item-moves-into-bulk): interrupted, written on both sides,
or failed, for example for lack of free space. Its data is not in /home.)

How a move works inside, and why an interruption (power loss) loses nothing:
[section 14](#how-an-item-moves-into-bulk).

### Chrome (Flatpak): its cache in RAM, not a subvolume

Chrome installed as a Flatpak keeps its cache in
`~/.var/app/com.google.Chrome/cache`, not in `~/.cache`. Most of it is the
**HTTP cache**: copies of web pages, images and scripts, about 1.3 GiB per
Chrome profile on this PC. Chrome rewrites it all the time, so every hourly
snapshot would hold another old copy of it.

**Why not just leave that folder out?** btrfs cannot leave a plain folder out
of a snapshot: a snapshot always takes the whole subvolume. The only way is
to make the folder a nested subvolume, as for `~/.cache`. Earlier versions of
this script did that for Chrome's cache folder. It works, but it is one more
subvolume for one app, for a cache that does not need to be on the disk at
all.

**What `setup` does instead.** Inside the Flatpak sandbox, `/tmp` is a
**tmpfs** (a filesystem that lives in RAM, not on the disk). On the host it is
the folder `/run/user/<your user id>/.flatpak/com.google.Chrome/tmp`. `setup`
tells Chrome to put its HTTP cache there. Chrome's Flatpak reads extra
command-line options ("flags") from
`~/.var/app/com.google.Chrome/config/chrome-flags.conf`: one flag per line,
and lines starting with `#` are comments. `setup` adds three lines at the end
of that file, as your user. Your other lines are not changed or moved.

```
# btrfs-setup.sh (CST) chrome-cache: HTTP cache in the sandbox /tmp (RAM, emptied at reboot), at most 512 MiB. Undo: ...
--disk-cache-dir=/tmp/chrome-cache
--disk-cache-size=536870912
```

A file written by an older version (called f44-tools) has `(f44-tools)` in
that comment instead of `(CST)`. It counts as the same comment: `setup` does
not rewrite it, and `off` works on it.

**The 512 MiB limit is not optional.** `--disk-cache-size=536870912` is
512 MiB. The RAM behind that `/tmp` is shared: it is your user runtime folder
(`/run/user/<id>`), limited to about 10% of the RAM (about 9.4 GiB here), and
it also holds the sockets of your desktop session, the portals and podman's
state. Without a limit, Chrome's cache could fill it, and then other programs
of your session fail in strange ways. That is why `setup` always writes both
lines together, and puts the limit line back if it goes missing under the
comment.

What to expect:

- The cache is **emptied at every reboot** (it is in RAM). The first page
  loads after that are a bit slower, until the cache fills again. A logout
  does **not** empty it on this PC: your user "lingers" (it is set so for
  podman: `loginctl show-user $USER -p Linger` says `Linger=yes`), so systemd
  keeps `/run/user/<id>`, and the cache in it, while you are logged out. On a
  PC without lingering, a logout empties it too. Flatpak may also delete the
  app's folder in `/run/user/<id>/.flatpak/` some time after Chrome has quit
  completely (it tracks running apps with a `.ref` file there); that is **not
  verified** here. Either way only the cache is lost, never your data.
- The rest of `~/.var/app/com.google.Chrome/cache` stays on the disk and in
  the snapshots: it is small. The **shader caches** (`mesa_shader_cache`,
  `radv_builtin_shaders`) stay on the disk on purpose: rebuilding them after
  every login would make Chrome stutter. `fontconfig` and `tmp` stay too.
- `setup` changes the file only if Chrome's Flatpak is installed (for your
  user, or for the whole system). If you already set `--disk-cache-dir` or
  `--disk-cache-size` yourself, it changes nothing and `status` says
  `set by you`. If the file, or a folder on the way to it, is a symlink, it
  changes nothing and says `NEEDS A LOOK`.
- It takes effect **when Chrome starts again**. Quit Chrome completely (all
  windows; `flatpak ps` must not list `com.google.Chrome`), then start it.

**Then delete the old cache on the disk, once.** It is never deleted
automatically, because Chrome may still use it until it restarts.
`snapshots status` shows how much is left, and the command:

```
rm -rf ~/.var/app/com.google.Chrome/cache/google-chrome/*/Cache
```

The cache that was on the disk before stays in the older snapshots until they
expire (about 3 months), like any deleted file.

**Two points not checked yet on this PC** (please look once, after Chrome
ran with the new flags for a while):

1. **Several profiles.** `--disk-cache-dir` is one folder for every Chrome
   profile. How Chrome arranges the profiles inside it (one subfolder each, or
   something else), and whether the 512 MiB limit is per profile (two open
   profiles could then use up to about 1 GiB, still well inside the tmpfs) is
   not verified. Look with either command:

   ```
   ls -la /run/user/$UID/.flatpak/com.google.Chrome/tmp/chrome-cache
   flatpak run --command=ls com.google.Chrome -la /tmp/chrome-cache
   ```

2. **The Code Cache.** Chrome also keeps compiled JavaScript in
   `google-chrome/<profile>/Code Cache` (about 0.75 GiB per profile here).
   Whether `--disk-cache-dir` moves it too is not verified. `snapshots status`
   shows its size on the disk as `Code Cache on disk`. If that number keeps
   growing after the restart, the Code Cache stayed on the disk, and it is in
   the /home snapshots.

**If `~/.var/app/com.google.Chrome/cache` is already a subvolume** (from an
earlier version of this script): it is left as it is. It is harmless (it is
still left out of the snapshots), only no longer needed, and `snapshots
status` says so. First run `cst btrfs snapshots setup` again: it
rewrites `/etc/btrfs-setup/convert-plan`, which still lists that folder
(`snapshots status` says when the plan is from an older version; until it is
rewritten, the hourly run keeps that folder a subvolume). Then, only if you
want a plain folder again, with Chrome closed:

```
cd ~/.var/app/com.google.Chrome
mv cache cache.subvol
mkdir cache
cp -a --reflink=always cache.subvol/. cache/        # instant: shares the data
rm -rf cache/google-chrome/*/Cache                  # the old HTTP cache: not needed
sudo btrfs subvolume delete cache.subvol
```

**How to undo:**

```
cst btrfs snapshots chrome-cache off
```

This removes the two flag lines (only those) and turns the comment into an
"off" line: `setup` never adds the flags again while that line is there.
Only lines that CST wrote are removed: if the cache flags in the file
are your own (no CST comment above them, or a line you edited), `off`
adds the "off" line, leaves your flags as they are, and says so; delete
them by hand to put the cache back on the disk. By
hand, the same: delete the two flag lines and **keep the comment** above them
(it tells `setup` not to add them again; deleting all three lines would let
the next `setup` add them back). To turn it on again, delete that comment or
"off" line, then run `cst btrfs snapshots chrome-cache apply` (or
`snapshots setup`). Restart Chrome after either change. `snapshots disable`
does not change this file.

---

## 4. Changes to your cargo and uv settings

Hardlinks cannot point from one subvolume to another. **Reflink** copies can
(a reflink copy is a copy that shares the data on the disk with the original,
so it is instant and uses no extra space). Two tools are affected. `setup`
writes these settings **as your user**, through the `toolchains` module
(`cst toolchains config apply`, which you can also run by itself; `config
plan` shows what it would add), and never changes a setting you already have:

| File                        | What is added                                   | Why |
|-----------------------------|-------------------------------------------------|-----|
| `~/.cargo/config.toml`      | `[build]` `build-dir = "<home>/.cache/cargo-build/{workspace-path-hash}"` | Rust intermediate build files go to `~/.cache` (left out of snapshots). The final programs stay in each project's `target/`. |
| `~/.config/uv/uv.toml`      | `link-mode = "clone"`                           | uv installs Python packages from its cache in `~/.cache/uv`. It now uses reflink clones instead of hardlinks, which work across subvolumes. |

If the file already has `build-dir` (or `link-mode`), it prints `kept: ...`.
If `config.toml` already has a `[build]` section, or `uv.toml` already has
tables, it prints a `NOTE:` with the exact line to add by hand (adding it
automatically could put it in the wrong section).

`setup` also writes, the same way (as your user, never over your own
settings), the cache lines of Chrome's `chrome-flags.conf`:
[section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume).

### Pruning of cargo's build folders in `~/.cache/cargo-build`

With `build-dir`, cargo makes one folder per project path in
`~/.cache/cargo-build` (`XX/YYYYYYYYYYYYYY`, a hash of the path). Each git
worktree is a new path, so each one gets its own folder, and cargo never
deletes any of them. `setup` therefore also runs `cst toolchains auto on
--unless-off`, as your user: the hourly user timer `cst-toolchains-prune.timer`
deletes folders not built in for **48 hours** or whose git worktree is gone,
only the incremental part of folders not built in for **6 hours**, then the
least recently built while all together are over **150 GiB**, but never one
built in during the last **2 hours** or in use (also by a running
`cargo test`). The same timer runs `uv cache prune` once a day.

`setup` never writes the opt-in lean debug info for Rust dev builds
(`RUST_LEAN_DEBUGINFO`, see the toolchains README): it passes
`--no-lean-debuginfo`, because turning it on makes every workspace rebuild
once.

If `build-dir` is not exactly the line above (you set your own), `setup`
prints why. The timer is then installed only for `uv cache prune` (if uv is
on the PATH of your user manager, which the timer uses), and each of its
runs reports `rust:build` as refused; with nothing left to run, no timer is
installed. If you turned it off with
`cst toolchains auto off`, `setup` leaves it off and says so
(`cst toolchains auto on` turns it on again). The settings, the safety rules,
the logs and how to stop it:
[../toolchains/README.md](../toolchains/README.md).
`cst toolchains prune plan` shows what it would delete now; `snapshots status`
points to `cst toolchains status`, which shows the timer and the last run.

### Cleanup of old cargo `target/` folders

Old `target/` folders in `~/Workspace` still contain intermediate files
(they have `debug/.fingerprint` or `release/.fingerprint` inside). `setup`
lists them with their size and deletes **the whole `target/` folder**, as your
user. The next `cargo build` recreates it: the final programs in `target/`,
the intermediate files in `~/.cache/cargo-build`. This happens **before** the
conversion, so no time is wasted cloning them.

It does **not** delete them if a Rust build, rust-analyzer (the Rust helper
that IDEs use) or RustRover is running. Then it prints
`Later: cst btrfs snapshots clean-targets --apply`.

```
cst btrfs snapshots clean-targets           # list only (dry run: shows, deletes nothing)
cst btrfs snapshots clean-targets --apply   # delete
```

---

## 5. When snapshots are taken and how long they are kept

### When

- `btrfs-snapshot.timer` starts `btrfs-snapshot.service` **every hour**
  (with a random delay up to 5 minutes). If the computer was off or asleep,
  the missed run happens soon after it is on again.
- It runs at the lowest CPU and disk priority.
- A snapshot is taken **only if something changed** since the newest snapshot.
  btrfs counts changes with a number called the *generation*; the program
  compares the subvolume's generation with the one recorded in the newest
  snapshot.
- Exception: if nothing changed for 24 hours, one snapshot is taken anyway,
  so there is always a recent one.

### Two sources

A **source** is a subvolume that gets snapshots. There are two:

| Name        | What                                   | Retention (how long snapshots are kept) |
|-------------|----------------------------------------|---------------------------|
| `home`      | the whole `/var/home` subvolume (minus nested subvolumes) | `72h 7d 4w 3m` |
| `Workspace` | `~/Workspace` (once it is a subvolume)  | `48h 7d`                  |

Until `~/Workspace` is a subvolume, it is simply part of the `home` snapshots.
Its retention is shorter because builds change a lot of files there, and git
already keeps the long history of your code.

### Reading the retention

`72h 7d 4w 3m` means:

- `72h`: for the last 72 hours, keep the first snapshot of every hour;
- `7d`: for the last 7 days, keep the first snapshot of every day (a day
  starts at 00:00);
- `4w`: for the last 4 weeks, keep the first snapshot of every week (a week
  starts on Sunday 00:00);
- `3m`: for the last 3 months, keep the first weekly snapshot of every month.

**Example.** Today is Tuesday, 14:00. For your home, you can go back to:

- any hour since Saturday 14:00;
- any day of the last week;
- the start of each of the last 4 weeks (around each Sunday);
- roughly the first week of each of the last 3 months.

For `~/Workspace` (`48h 7d`): any hour since Sunday 14:00, and any day of the
last week.

The newest snapshot is always kept. More rules that keep snapshots are in
[section 15](#15-safety-rules-of-btrfs-snapshot-core).

In practice, `/home` has at most about 90 snapshots, `~/Workspace` at most
about 58. They share most of their data, so they use much less space than the
number suggests.

### Changing the retention

You can change the retention by adding `HOME_PRESERVE='...'`,
`WS_PRESERVE='...'` or `EMERGENCY_PRESERVE='...'` (default `24h 3d`, used by
the emergency purge of [section 16](#16-the-space-guard)) to
`/etc/btrfs-setup/snapshots.conf`. The hourly run reads this file.

- `snapshots status` always shows the default text (`keep /home 72h 7d 4w 3m,
  ~/Workspace 48h 7d`), even after you change it. To see the retention that
  is really used, run `cst btrfs snapshots list`: it prints
  `policy '...'` for each source.
- If the new retention is shorter, the circuit breaker
  ([section 15](#15-safety-rules-of-btrfs-snapshot-core)) holds the extra
  deletions back until you confirm them with
  `cst btrfs snapshots prune --confirm`.

### Where snapshots are stored, and their names

- Folder: `/var/mnt/btrfs-top/snapshots/` (mode 0700: only root can open it).
- `/var/mnt/btrfs-top` is the **top level** of the btrfs filesystem (the
  level above all subvolumes, `subvolid=5`). It is not visible in your normal
  folder tree. systemd mounts it **on demand** when something opens the path,
  and unmounts it after 10 minutes without use (an *automount*; to *mount*
  means to attach a filesystem to a folder, the *mount point*).
- Names: `home.YYYYMMDDTHHMM` and `Workspace.YYYYMMDDTHHMM`, in local time.
  Example: `home.20261013T1400` is the snapshot of 13 October 2026, 14:00. If
  that name is taken, a suffix is added: `home.20261013T1400_1`.

---

## 6. How to restore a file or a folder

### From a local snapshot

Snapshots are read-only, so you cannot damage them while looking. Run these
commands on the host, as your normal user. They need `sudo`, because only root
can open the snapshot folder. `$USER` is your user name, and `~` is your home
folder: your own shell fills them in before `sudo` runs.

1. Find the snapshots:

   ```
   sudo ls /var/mnt/btrfs-top/snapshots/
   ```

2. Look inside one. A `home` snapshot contains the home folders of all users,
   so your files are under `$USER/`. A `Workspace` snapshot contains the
   content of `~/Workspace` directly.

   ```
   sudo ls -l /var/mnt/btrfs-top/snapshots/home.20261013T1400/$USER/Documents/
   sudo ls -l /var/mnt/btrfs-top/snapshots/Workspace.20261013T1400/myproject/
   ```

   To see one file in every snapshot (useful to find the right version):

   ```
   sudo sh -c "ls -l /var/mnt/btrfs-top/snapshots/home.*/$USER/Documents/report.odt"
   ```

   The `sh -c "..."` is needed here. Your own user cannot open the snapshot
   folder, so your shell cannot expand `home.*` into the list of names. The
   shell started by `sudo` runs as root and can.

3. Copy it back **under a new name**, then compare and replace it yourself.
   `cp -a` keeps the owner and permissions. `--reflink=auto` makes the copy
   instant and free when possible (it shares the data with the snapshot).

   ```
   sudo cp -a --reflink=auto /var/mnt/btrfs-top/snapshots/home.20261013T1400/$USER/Documents/report.odt ~/Documents/report.restored.odt
   ```

   A whole folder works the same way:

   ```
   sudo cp -a --reflink=auto /var/mnt/btrfs-top/snapshots/home.20261013T1400/$USER/Documents ~/Documents.restored
   ```

Remember: the folders of [section 3](#3-which-folders-are-left-out-of-home-snapshots-and-why)
are **empty** in `home` snapshots. Files in `~/Workspace` are in the
`Workspace.*` snapshots (only for the last 48 hours of hourly snapshots and 7
daily ones).

### From the NAS backup (commands checked against restic 0.19)

Use this when the local snapshots do not have what you need (older than 3
months, or the disks are lost). It needs the NAS backup of
[section 7](#7-optional-backup-on-the-nas-restic). restic is the backup
program; inside the NAS backup, each saved state is called a *restic
snapshot*. The commands below were checked for restic 0.19 (the version this
code was written for); other versions may differ.

Run as root on the host. restic uses root's SSH settings and the password
file. First open a root shell and set the variables:

```
sudo -i
ME=your-user-name   # replace with YOUR user name (in this root shell, $USER is root)
export RESTIC_REPOSITORY="sftp:btrfs-setup-nas:$(. /etc/btrfs-setup/snapshots.conf; echo "$NAS_PATH")"
export RESTIC_PASSWORD_FILE=/etc/btrfs-setup/restic-password
export RESTIC_CACHE_DIR=/var/cache/btrfs-snapshot-restic
HOSTNAME_IN_BACKUP="$(. /etc/btrfs-setup/snapshots.conf; echo "$NAS_RESTIC_HOST")"
restic snapshots --tag btrfs-setup
```

`HOSTNAME_IN_BACKUP` is the host name the backups were saved under
(`NAS_RESTIC_HOST` in `snapshots.conf`, normally the short host name of this
computer, for example `sophia`).

Inside restic, the files are stored under the paths where they were read:
`/run/btrfs-snapshot/nas/home/<user>/...` and
`/run/btrfs-snapshot/nas/Workspace/...`. Look first:

```
restic ls latest --host "$HOSTNAME_IN_BACKUP" --tag btrfs-setup,home "/run/btrfs-snapshot/nas/home/$ME/Documents"
```

(`--tag btrfs-setup,home` means "has both tags". Use `btrfs-setup,Workspace`
for the projects. Instead of `latest`, you can give a snapshot ID from
`restic snapshots`; then `--host` and `--tag` are not needed.)

Restore one folder into an empty place:

```
restic restore "latest:/run/btrfs-snapshot/nas/home/$ME/Documents" \
    --host "$HOSTNAME_IN_BACKUP" --tag btrfs-setup,home --target /var/tmp/restic-restore
```

This puts the content of `Documents` into `/var/tmp/restic-restore/`.

Restore one file:

```
restic restore "latest:/run/btrfs-snapshot/nas/home/$ME" \
    --host "$HOSTNAME_IN_BACKUP" --tag btrfs-setup,home \
    --target /var/tmp/restic-restore --include /Documents/report.odt
```

With the `latest:<folder>` form, `--include` is relative to that folder.

Or browse all restic snapshots like normal folders. This uses a **FUSE
mount** (FUSE lets a program show something, here the backup, as folders).
Stop it with Ctrl-C:

```
mkdir -p /var/tmp/restic-mnt
restic mount /var/tmp/restic-mnt
# in another terminal, as root: ls /var/tmp/restic-mnt/snapshots/
```

Then copy the restored files to your home and fix the owner if needed
(`chown -R <user>: <path>`).

**If this computer is lost:** you need the NAS address, port, user, path and
**the saved password**. On a new machine:

- add a new SSH key to the NAS user's `authorized_keys`;
- put the password in a file;
- use the URL form for a non-standard port, for example
  `RESTIC_REPOSITORY=sftp://<user>@<nas>:2204//<share>/<folder>`;
- the new machine usually has another host name. Give the **old** host name
  (for example `--host sophia`), or leave out `--host` and choose a snapshot
  ID from `restic snapshots`. With `--host "$(hostname -s)"` on the new
  machine, `latest` usually finds nothing.

---

## 7. Optional backup on the NAS (restic)

**Why:** local snapshots are lost when the disks are lost. A backup on the
Synology NAS survives that.

**restic** is a backup program. It stores files in a **repository** (a folder
on the NAS with restic's own format). The data is **encrypted** with a
password and **deduplicated** (a piece of data that is already there is not
stored again). You cannot read the files on the NAS without restic and the
password. restic talks to the NAS over **SFTP** (file transfer over SSH, an
encrypted connection).

### Setup on the NAS (Synology DSM)

DSM is the Synology web interface. `snapshots setup` must have run before.

**Step 1. Run the setup command:**

```
cst btrfs snapshots nas setup --path /<share>/<folder> [--host H] [--port P] [--user U]
```

- Defaults: `--host diskstation.local`, `--port 2204`, `--user` = your user
  name. `--port` must be a number.
- The path cannot contain spaces. Allowed: letters, digits and `. _ / @ + -`.
- Give the path **as SFTP shows it**: `/<share>/<folder>`, not
  `/volume1/<share>/<folder>`. If you give the `/volumeN/...` form and only
  the short form works, the script switches to it and prints a note.

The first time, it:

- creates an SSH key `/etc/btrfs-setup/ssh/id_ed25519` (for root, no passphrase);
- writes a managed block `Host btrfs-setup-nas` in `/root/.ssh/config`;
- records the NAS **host key** in `/root/.ssh/known_hosts`. A host key is the
  NAS's ID card for SSH. The script trusts the key it sees the first time
  ("trust on first use") and prints its fingerprint. Later, SSH refuses to
  connect if the key changes;
- installs restic with `rpm-ostree` (it may become active only after a reboot);
- tests SFTP.

If the SFTP test **passes**, setup continues in the same run: go to step 4
(the password). If it **fails**, the script prints what to do on the NAS and
the public key. Continue with step 2.

**Step 2. On the NAS (DSM):**

1. Enable SFTP: Control Panel > File Services > FTP > SFTP, on the chosen port.
2. Enable "User Home": Control Panel > User & Group > Advanced (so `~/.ssh`
   exists).
3. The home folder and `~/.ssh` must not be writable by others
   (`chmod 755 ~ ; chmod 700 ~/.ssh`).
4. The user needs write access to the path (or its parent: the repository
   folder is created).
5. Append the printed key to `~<user>/.ssh/authorized_keys` (`chmod 600`).

**Step 3. Run the same command of step 1 again.** It tests again, creates the
password and the repository, and enables the daily timer.

If restic is not active yet (the script says so), the repository is created by
the first backup after the next reboot (see the password rules below). You do
not need to run setup again for that.

**Step 4. Save the password** (next subsection).

**Step 5. Start the first backup now and watch it:**

```
cst btrfs snapshots nas run
journalctl -fu btrfs-snapshot-nas
```

(`journalctl` shows the **journal**, the system log. `-f` follows new lines;
stop with Ctrl-C.)

### The password: save it now

The repository password is created at random in
`/etc/btrfs-setup/restic-password` (root only, mode 0600).

**Without this password, nobody can read the backups.** If the disks of this
computer are lost, this file is lost too, and the NAS backup is useless. So:

```
sudo cat /etc/btrfs-setup/restic-password
```

and store it in your password manager as a new entry. The script reminds you
when the repository is created (and with a desktop notification
`NAS backup: repository created` if it is created later, after a reboot).

Rules the script follows about the password:

- If restic is not active yet (it needs a reboot), the password is
  *provisional*: the first run after the reboot creates the repository with it.
- If a repository already exists at the path and there is no password file,
  setup stops. It never makes a new password that cannot open it. Put the
  saved password into the file (owner root, mode 0600) and run setup again.
- A missing repository is created only when setup asked for it, never on its
  own: a moved or deleted repository must be noticed, not silently replaced.

### What it does every day

`btrfs-snapshot-nas.timer` starts `btrfs-snapshot-nas.service` once a day
(random delay up to 1 hour; a missed day runs after the next start). One run:

1. Checks the NAS is reachable. If not, it skips the run. You get an alert
   only when the last good backup is `NAS_STALE_DAYS` (7) days old or more.
2. Opens the repository with the password.
3. Takes the **newest local snapshot** of `home` and of `Workspace`, **pins**
   them (a pin is a mark that stops the hourly run from deleting them during
   the upload), and mounts them read-only at fixed paths:
   `/run/btrfs-snapshot/nas/home` and `/run/btrfs-snapshot/nas/Workspace`.
   (A *bind mount* shows a folder at a second place. The fixed path lets
   restic recognize unchanged files and skip them.)
4. `restic backup` of each, with the time of the local snapshot, the host name
   (`NAS_RESTIC_HOST`) and the tags `btrfs-setup` and `home` (or `Workspace`).
   It skips folders marked as caches (a `CACHEDIR.TAG` file) and does not
   enter other filesystems or nested subvolumes. A snapshot already uploaded
   is not sent again.
5. Unmounts and unpins, also if the run is stopped. After a hard kill, the
   next run cleans up first (and does not start if it cannot); a reboot also
   clears the pins.
6. `restic forget` keeps **14 daily, 8 weekly and 12 monthly** restic
   snapshots per source. Every 7 days it also runs `--prune` (which really
   frees space on the NAS). Every 30 days, `restic check
   --read-data-subset=5%` reads a random 5 % of the data to find damage.

Step 6 runs **only if all backups of this run succeeded**. Steps 4 to 6 share
one time budget of 6 hours (`NAS_TIMEOUT=6h`): forget, prune and check use
the time that is left. The service also has a hard limit of 7 hours. Data
already uploaded is reused next time. The NAS backup never creates or deletes
local snapshots, and a NAS problem never blocks the hourly snapshots.

### Other NAS commands

```
cst btrfs snapshots nas status    # connection test and the latest restic snapshot
cst btrfs snapshots nas run       # start a backup now
cst btrfs snapshots nas disable   # stop the daily backup (repository and password kept)
cst btrfs snapshots nas enable    # turn it on again (not shown in the script's help)
```

Settings in `/etc/btrfs-setup/snapshots.conf`: `NAS_HOST`, `NAS_PORT`,
`NAS_USER`, `NAS_PATH`, `NAS_RESTIC_HOST`, `NAS_STALE_DAYS=7`,
`NAS_TIMEOUT=6h`, `NAS_KEEP_DAILY=14`, `NAS_KEEP_WEEKLY=8`,
`NAS_KEEP_MONTHLY=12`.

---

## 8. All commands

Run them as described in [section 2](#where-and-how-to-run-it):
`cst btrfs <command>`, as your normal user. Every command needs root; the script asks for `sudo` by itself.

| Command | What it does |
|---------|--------------|
| `snapshots setup [--yes]` | Plan, one confirmation, then everything ([section 2](#2-quick-start)). Safe to re-run. Also turns the hourly snapshots on again after `disable`. |
| `snapshots` or `snapshots status` | Timer state, number and dates of snapshots, pause/emergency, last successful run, waiting conversions, state of each nested subvolume, NAS state. |
| `snapshots list [--json]` | Every snapshot with its decision (`KEEP` / `DELETE`) and the reason, plus `IGNORED` and `UNKNOWN` entries ([section 15](#15-safety-rules-of-btrfs-snapshot-core)). Read-only. |
| `snapshots prune [--confirm]` | The normal retention by hand, including what the circuit breaker held back. Without `--confirm` it is a dry run. |
| `snapshots clean-targets [--apply]` | Old cargo `target/` folders: list, or delete with `--apply`. |
| `snapshots disable` | Stops the hourly snapshots and the NAS timer, and cancels a pending conversion at boot. Keeps the snapshots. |
| `snapshots disable --purge` | The same, then deletes **all snapshots owned by btrfs-snapshot-core** and removes the top-level mount units. |
| `snapshots nas setup ...` | See [section 7](#7-optional-backup-on-the-nas-restic). |
| `snapshots nas status / run / disable / enable` | See [section 7](#other-nas-commands). |
| `snapshots chrome-cache [status\|apply\|off]` | Only Chrome's cache flags ([section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)): show their state and the old cache left on the disk, add them (also done by `setup`), or remove them for good (`off`). Runs as your user. |
| `bulk plan` | Dry run of the `~/Bulk` step: each item and what would happen, with sizes ([section 3](#order-with-snapshots-setup)). Changes nothing. |
| `bulk apply` | Fills `~/Bulk` (also done by `snapshots setup`). Safe to re-run; busy items wait for the next boot. |
| `bulk status` | The state of each `~/Bulk` item, the `+C` state of the emulator folders (`~/Bulk/emulators` and the AVD folder), items waiting for the boot, old game data still in /home. |
| `bulk keep ID...` | Keeps these items in /home on purpose: `bulk apply` leaves them alone, and they are not reported as problems ([section 3](#bulk-one-folder-for-everything-you-can-download-again) has the ids). Wrong ids are refused, with the list of valid ones. |
| `bulk unkeep ID...` | Undoes `bulk keep`: the next `bulk apply` moves the item into `~/Bulk`. |
| `bulk steam plan [--app ID]... [--copy]` | Dry run: which installed Steam games would move into `~/Bulk/steam` (by reflink, or by a real copy for NOCOW data), which stay (tools) and which need a look ([section 3](#steam-move-your-games-into-the-bulk-library)). Changes nothing. |
| `bulk steam apply [--app ID]... [--copy]` | Moves them, with Steam closed. Without `--app`: every installed game, not the tools. `--copy`: games with NOCOW data may be copied byte by byte (without it they are refused, the others still move); a copy needs the game's size plus the snapshot emergency limit plus 20 GiB free ([section 16](#16-the-space-guard)). Exit status 0 done, 1 needs a look, 5 Steam is running (nothing done). First installs the current `btrfs-bulk` and enables the boot step, as `bulk apply` does, so that a move interrupted by a power loss is repaired at the next boot. |

Logs:

```
journalctl -u btrfs-snapshot            # hourly runs
journalctl -u btrfs-snapshot-nas        # NAS backup
journalctl -u btrfs-subvol-convert      # conversion at boot
journalctl -u btrfs-bulk-boot           # moves into ~/Bulk at boot
```

`cst btrfs status` also includes the snapshot status in its overview.

---

## 9. Alerts

### How alerts reach you

The snapshot programs write every alert to the **journal**. They also send it
as a **desktop notification** (through `btrfs-notify`, to every logged-in
desktop user), with these rules:

- Each kind is sent **at most once a day**.
- If nobody is logged in to the desktop when it happens (for example at boot,
  or at night), the alert is **kept and shown at your next login**, with the
  time it happened. Details: [README.md, section 4.9](README.md#49-desktop-alerts-btrfs-notify).
- This also holds for `Subvolume conversion at boot gave up`: it happens at
  boot, before any login, and you see it when you log in. It is also in the
  journal (`journalctl -u btrfs-subvol-convert`) and on the boot screen. Check
  `snapshots status` after a reboot that converted folders.

### From the snapshot programs

| Title | Meaning | What to do |
|-------|---------|------------|
| `Snapshots paused: low space` | Free space at or below 250 GiB ([section 16](#16-the-space-guard)). | Free space. Snapshots resume above 270 GiB. |
| `Snapshots: emergency purge` | Free space at or below 150 GiB; many snapshots were deleted. | Free space now. Snapshots stay paused until free space is above 270 GiB. |
| `Snapshots: safety check blocked deletions` / `... blocked a new snapshot` | A rule of [section 15](#15-safety-rules-of-btrfs-snapshot-core) stopped the run. The text says which one. | [Section 10](#10-what-to-do-if): "circuit breaker" or "the clock". |
| `Snapshots: purge failed` / `Snapshots: emergency purge failed` | btrfs-snapshot-core failed while deleting. | Read `journalctl -u btrfs-snapshot`. The next hourly run tries again. |
| `Snapshots: waiting for old trees to be deleted` | For more than a day, no snapshot because old trees are still being deleted. | [Section 10](#10-what-to-do-if): "old trees still being deleted". |
| `Snapshots wait for a reboot` | For more than a day, busy folders (or busy `~/Bulk` items) wait for the next boot. | Reboot normally. |
| `Snapshots wait for ~/Bulk` | For more than a day, a move into `~/Bulk` was interrupted, or stopped because it needs a look (a journal in `/var/lib/btrfs-bulk`). | Run `cst btrfs bulk status` and follow [section 10](#10-what-to-do-if). |
| `~/Bulk: moving at boot gave up` | Two boots did not finish moving the busy items into `~/Bulk`. Seen at your next login. | [Section 10](#10-what-to-do-if): "moving into ~/Bulk at boot gave up". |
| `Subvolume conversion at boot gave up` | Two boots did not finish the conversion. It happens before the login, so you see it when you log in (see above). | [Section 10](#10-what-to-do-if): "the conversion at boot gave up". |
| `Subvolume conversion: manual check needed` | A rename failed in the middle. The message names where the original tree and the clone are. Nothing is deleted. | [Section 10](#10-what-to-do-if): "manual check needed". |
| `NAS backup is N days old` | No successful NAS backup for 7 days or more (`NAS_STALE_DAYS`). | [Section 10](#10-what-to-do-if): "the NAS backup fails". |
| `NAS backup: wrong repository password` | The password file does not open the repository. | Put the saved password into the file ([section 10](#10-what-to-do-if)). |
| `NAS backup: repository created` | A new repository was made. | Save the password now ([section 7](#the-password-save-it-now)). |
| `NAS backup: repository damaged` / `... check keeps failing` / `... not checked for N days` | Results of `restic check`. | [Section 10](#10-what-to-do-if): "the NAS repository is damaged". |

### From the hourly health check (`btrfs-health-check`)

The health check is part of the **monitoring** setup (`cst btrfs
install`), not of `snapshots setup`. Without it, these messages do not exist.
Details: [README.md, section 4.2](README.md#42-health-check-every-hour).

It runs every hour. When it finds problems, it sends a critical notification
with the title `Disk health: problems detected`. Its text starts with the
number of problems, then lists them one per line. This notification has **no daily limit**: it comes every hour while
the problem lasts. About snapshots:

- `snapshots: no successful hourly run for over 3 hours - check 'journalctl -u btrfs-snapshot'`.
  Not raised while paused, while old trees are being deleted, while a boot
  conversion is pending, or in the first 20 minutes after boot or resume.
  What to do: read `journalctl -u btrfs-snapshot` and check
  `systemctl status btrfs-snapshot.timer`.
- A journal line only (no notification): `no successful NAS backup (restic) for over 8 days`.

---

## 10. What to do if...

**`status` says `conversion of busy directories pending`.**
Reboot normally. The conversion runs before the login screen. Snapshots start
after that. If you close the programs that use those folders, an hourly run
may convert them earlier.

**The conversion at boot gave up** (journal message `Subvolume conversion at
boot gave up`, or folders still `pending (busy)` in `status` after two reboots).
- What happens now: the boot flag is removed and snapshots **start anyway**.
  The remaining folders are retried every hour, whenever they are not in use.
  Until they are converted, their data is in the `/home` snapshots (and stays
  there until those snapshots expire, up to about 3 months).
- What you can do: close the program that uses the folder (Steam, the IDE...)
  and wait for the next hourly run. Or run `cst btrfs snapshots setup`
  again: if folders are still busy, it plans a new conversion at the next
  boot. Check with `cst btrfs snapshots status`.

**`bulk status` shows `NEEDS A LOOK: ...` for an item.**
The script found something it does not change by itself, and left it
exactly as it was. The reason says what:
- `symlink to X (expected ~/Bulk/...)`: you (or a tool) already moved that
  folder somewhere else. To keep it that way, run
  `cst btrfs bulk keep ID` (no more warnings). Or move its content
  into the `~/Bulk` folder, delete the old link (`rm ~/<path>`, without a
  slash at the end: this removes only the link) and run `bulk apply`.
- `a file, not a folder`: move the file away, then `bulk apply`.
- `data in both ~/X and ~/Bulk/Y`: look at both. Move what you want to keep
  into `~/Bulk/Y`, delete the old folder, then `bulk apply` (it makes the link).
  Often a tool's clean command deleted the link and then made a new folder.
  For npm's and Go's caches, `bulk apply` repairs that by itself
  ([section 3](#bulk-one-folder-for-everything-you-can-download-again)). For
  other caches, if you are sure it is only a cache, empty `~/Bulk/Y`
  (`rm -rf ~/Bulk/Y/*`) and run `bulk apply`: it moves the new folder in.
- `~/.cargo is a symlink, not a folder` (or another parent folder): the
  script does not follow symlinks on the way. To leave it as it is, run
  `cst btrfs bulk keep cargo-registry cargo-git` (the ids of the
  items below it, from the table in [section 3](#bulk-one-folder-for-everything-you-can-download-again)).
  Or replace that symlink by a real folder, then `bulk apply`.
- `~/Programs is kept in /home (by choice), so this cannot move into ~/Bulk`:
  you keep `apps`, and JetBrains Toolbox has IDEs installed. Run
  `cst btrfs bulk keep jetbrains` to keep them in /home too, or
  `bulk unkeep apps`.
- `is or contains a mount point`: unmount it first.
- `contains another subvolume or a mount`: a subvolume inside the folder.
  Move it out by hand first.

- `N entries there are not yours (made by a container or sudo?)`: files or
  folders owned by another user (often root, from a container that mounted
  the folder). Run the `sudo chown -R ...` command shown, then `bulk apply`.

Until it is fixed, that item's data stays in /home. Once snapshots run, **they
do not wait for it**. If you want it in /home for good, run
`cst btrfs bulk keep ID` ([section 12](#undo-one-bulk-link)).

**`bulk apply` says `NEEDS A LOOK: /etc/btrfs-setup/bulk-keep cannot be read`.**
That file (the items you keep in /home) is a folder, or cannot be opened.
Since the script cannot know what you want to keep, it moves **no** item
until it is fixed. Delete it (`sudo rm -r /etc/btrfs-setup/bulk-keep`) and
run `bulk keep ID` again for each item you keep. (Odd characters in the file
are fine: such a line shows up as an `unknown item` and is ignored.)

**`bulk status` says `waits for the next boot (busy)`.**
Reboot normally: the item is moved before the login screen. Or close the
program that uses it (IDE, Android emulator, LM Studio, a terminal whose
current folder is inside it) and run `cst btrfs bulk apply`.

**Moving into ~/Bulk at boot gave up** (alert `~/Bulk: moving at boot gave
up`). Two boots in a row found the items still in use (a program that starts
before the login, for example). The flag is removed and snapshots start; the
items stay at their old place (their data is in the `/home` snapshots).
Find what uses them (`sudo fuser -vm ~/<path>` or `lsof +D ~/<path>`), stop it,
and run `cst btrfs bulk apply`. Log: `journalctl -u btrfs-bulk-boot`.

**`bulk status` says `an interrupted move is recorded`.**
A move was interrupted (power loss, kill). Nothing is lost. Normally the next
boot already handled it, before the login screen (`btrfs-bulk-boot.service`
also runs when such a record exists). Otherwise run
`cst btrfs bulk apply`. It first finishes the move (if the switch
was already complete) or undoes it (the data goes back to the old path), then
tries again. Snapshots wait until the record is gone (except for the `+C`
repair inside `~/Bulk`, whose data is not in /home: there `status` says
`snapshots do not wait`).

**`bulk status` says `NEEDS A LOOK: ... was created again while the data waited in ...`.**
A move was interrupted halfway, and before it was undone a program (or you)
created the usual folder again, empty or with a few new files. Your data is
safe in the `<path>.bulk-old-<date>` folder named in the message; the script
deletes nothing. Move anything you need from the new folder into the
`.bulk-old-` one, delete the new folder, then run `cst btrfs bulk
apply`: it puts the data back at the usual path and then moves it into
`~/Bulk` properly.

**`bulk status` says `NEEDS A LOOK: the data is back at ..., but the copy ... was written to as well`.**
A move was being undone (or an interrupted one was found without its link),
and you had kept working through the link meanwhile, so the copy in `~/Bulk`
(now named `<path>.bulk-new`) holds new files too. The script put the old data
back at the usual path and deletes neither side. Copy what you need from the
`.bulk-new` folder into the usual path, delete the `.bulk-new` folder, then run
`cst btrfs bulk apply`.

**`bulk status` says `NEEDS A LOOK: ... was created again after its data was moved to ...`.**
An old subvolume was being moved into `~/Bulk` (the rename is instant) and the
run was interrupted before the link was made; then a program created the usual
folder again. Your full data is in `~/Bulk`, at the path named in the message;
the script deletes nothing. Move what you need from the usual folder into that
path, delete the usual folder, then run `cst btrfs bulk apply`: it
makes the link.

**`bulk status` says `NEEDS A LOOK: written to on both sides during the move`.**
A program wrote into the old folder and, through the new link, into the copy
in `~/Bulk`, in the same instant of the switch. The script cannot know which
side to keep, so it keeps both. Compare the `<path>.bulk-old-<date>` folder
with the `~/Bulk` one (the one in use now), copy what you need into the
`~/Bulk` one, delete the `.bulk-old-` folder, then run `cst btrfs
bulk apply`. This can also be found after a power loss: the repair at boot
makes the same check. When both folders are inside `~/Bulk` (the `+C` repair
of the emulator folder: `~/Bulk/emulators/android/avd.bulk-old-<date>`), snapshots do
not wait for it, because none of that data is in /home.

**`bulk steam` says `Steam is running`** (exit status 5). Nothing was
changed. Quit Steam with **Steam > Exit** (closing the window only hides it),
wait a few seconds, and run the command again. The script looks for Steam's
processes (also inside the Flatpak) and Steam's own pid files. If it still
says so, the line names the process: check it with `ps -p <pid> -o pid,comm,args`.
If Steam started while a game was being moved, that move was undone first.

**`bulk steam` says `mid-update`.** Steam is in the middle of an update or a
download of that game (its manifest says so, or `steamapps/downloading/<id>`
is not empty). Start Steam, let the download finish (**Downloads** at the
bottom of the library), quit Steam, and run the command again.

**`bulk steam` says `steamapps/compatdata/<id> has data`.** That game has a
Proton prefix: the Windows folder where many Windows games keep their saves
and settings, sometimes not in the cloud. The script does not move it; use
Steam's own **Properties > Installed Files > Move install folder** for that
game ([section 3](#steam-move-your-games-into-the-bulk-library)).

**`bulk steam` says `format not as expected`, `unknown key ...` or another
problem with `libraryfolders.vdf`.** The file is not in the exact form the
script knows, so it does not write it (Steam's file format has changed, or the
file was edited by hand). Nothing was changed. Starting Steam once and quitting
it rewrites the file in Steam's own form; then try again. If it still refuses,
move the games by hand in Steam ([section 3](#steam-move-your-games-into-the-bulk-library))
and tell the maintainer the message.

**`bulk steam` says `the two copies of libraryfolders.vdf ... differ`.**
Steam keeps the same list in `Steam/config/libraryfolders.vdf` and
`Steam/steamapps/libraryfolders.vdf`, and the two are different now. With
Steam closed, copy the right one over the other (normally the `config/` one,
which current Steam versions read), or restore the backup the message names.
If the only difference is the `~/Bulk/steam` entry from an interrupted run of
the script, it repairs that by itself and says so in `plan`.

**`bulk steam` says `copy N GiB byte by byte: K files in it have +C` or
`run again with --copy`.** Some of the game's files have the NOCOW mark
(`+C`), so they cannot be reflinked into `~/Bulk/steam`
([section 3](#steam-move-your-games-into-the-bulk-library)). If `K` is small
and the example file is small or empty, delete those files (Steam's
"Verify integrity of game files" downloads missing game files again) or write
them again in a folder without `+C`, then run `plan` again. Otherwise, if
you have the space (the size `plan` shows, plus 170 GiB) and the time, run
`cst btrfs bulk steam apply --copy`, or leave that game where it is for now
(it stays in `/home` snapshots).

**`bulk steam` says `not enough free space in ~/Bulk for a real copy`.**
A real copy needs the game's size plus 170 GiB free (so the snapshot
emergency purge cannot start). Free space in `~/Bulk` (or delete old
snapshots that still hold data, see [section 16](#16-the-space-guard)), or
move that game later. Nothing was changed for it.

**`bulk steam` says `could not be written to the disk` or `could not be
compared with its copy`.** Writing the copy to the disk, or reading it back,
reported an error. The copy was deleted and the game stayed where it was.
Check the disks (`cst btrfs status`, `journalctl -k`) before you try again.

**`bulk steam` says `... has +C (NOCOW) ... must stay copy-on-write`.** A
folder in `~/Bulk/steam` has the NOCOW mark, so the moved files would lose
their checksums and compression. Run the `chattr -C` the message shows (on a
folder it changes only what is created there afterwards), then run the
command again. Files that are already in that folder keep their mark.

**`bulk steam` says `copy check failed`.** After a real copy, a file in the
copy differs from the original. The copy was deleted and the original kept.
Run it again; if it fails again on the same file, check the disks (`cst
btrfs status`, a scrub): a read error or a failing disk is the likely cause.

**`bulk steam` says `exists already: remove it or move it back`.** Something
already sits where the game would go (an earlier copy, or a `.bulk-new`
leftover). The script never merges two copies. Look at it: if it is a stale
copy, delete it; if it holds the game you want, move it back with Steam.

**A tool does not find its files after the move.**
Check the link: `ls -l ~/<path>` must show `-> /var/home/<you>/Bulk/...`. Some
tools write the real path (`.../Bulk/...`) into their own settings when they
see a symlink; that also keeps working. A Flatpak app needs the permission of
[section 3](#lm-studio). If a tool really cannot live with a symlink, undo the
link ([section 12](#undo-one-bulk-link)).

**A folder shows `skipped: ...` in `status`.**
Read the reason (symlink, mount point, nested subvolume inside). Fix the cause
if you want it converted; every run checks again.

**`old trees still being deleted` for a long time.**
Each run deletes for at most 15 minutes per tree. Look at
`journalctl -u btrfs-snapshot`. The list is in
`/var/lib/btrfs-snapshot/drop-pending`. A tree with something mounted inside
it is not deleted: unmount that first.

**Alert `Subvolume conversion: manual check needed`.**
Do not delete anything. The message names the folder that holds your latest
data (`the original tree is ...` or `the tree with the latest data is ...`).
The script has no repair command, and it never deletes that tree by itself.
Make sure the folder with your data is back at its normal name (for example
with `ls -la` in the parent folder, then `mv` by hand). *(Not verified: the
script gives no step-by-step recipe for this case.)*

**Alert `Snapshots: safety check blocked deletions` with `circuit breaker`.**
Run `cst btrfs snapshots list` and read the `DELETE` lines. If
they are right (for example, you shortened the retention), run
`cst btrfs snapshots prune --confirm`.

**The block is about the clock.**
Check `timedatectl`. Wait until `System clock synchronized: yes`. If a message
says the clock is behind the last successful run and you are sure the clock is
right, delete `/var/lib/btrfs-snapshot/core-last-run`. If a snapshot has a
future date, check `snapshots list`, then delete it by hand:
`sudo btrfs subvolume delete /var/mnt/btrfs-top/snapshots/<name>`.
Details: [section 15](#clock-guards).

**Snapshots are paused for low space.**
Free space (big files, VM images, Docker images, caches). Snapshots resume
above 270 GiB free. See [section 16](#16-the-space-guard) for how to check.

**The NAS backup fails.**
`cst btrfs snapshots nas status` tests the connection. Check that
the NAS is on, SFTP is enabled on the right port, and the key is still in
`authorized_keys`. Then `cst btrfs snapshots nas run` and
`journalctl -fu btrfs-snapshot-nas`.

**`wrong repository password`.**
Put the saved password into `/etc/btrfs-setup/restic-password` (owner root,
mode 0600).

**`No repository at ... (moved or deleted?)`.**
The script does not create a new one on its own. If you moved it, run
`snapshots nas setup --path <new path>`. If it is really gone, run
`snapshots nas setup` again to create a new one (and save the password).

**The NAS repository is damaged, or the check keeps failing.**
Read `journalctl -u btrfs-snapshot-nas`. Open a root shell with the variables
of [section 6](#from-the-nas-backup-commands-checked-against-restic-019) and
run `restic check` to see the details. For a real repair, follow restic's own
troubleshooting guide (`restic repair`); the script has no repair command.
*(The repair steps are restic's, not verified here.)* `not checked for N days`
usually means the NAS went away during the checks; it is retried every run.

**`status` says `convert-plan is from an older version (it still plans: ...)`.**
An older version of this script planned a folder that is no longer converted
(for example Chrome's cache folder). Run `cst btrfs snapshots setup`
again: it writes the current list. Until then, the hourly run keeps
converting that folder.

**`status` says `NEEDS A LOOK` under `Chrome (Flatpak)`.**
`chrome-flags.conf`, or a folder on the way to it, is a symlink or not a
normal file. Nothing was changed. Replace the symlink by a real file (or a
real folder) and run `cst btrfs snapshots chrome-cache apply`, or
add the two lines of [section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)
to the real file yourself.

**Chrome's flags show `set by you` with a `WARNING`.**
Your own `chrome-flags.conf` puts the cache in `/tmp` (RAM) without a
limit, or with a limit above 512 MiB. Add (or lower)
`--disk-cache-size=536870912`; the reason is in
[section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume).

**A file you need is in an excluded folder.**
It is not in any snapshot or in the NAS backup. This is intended; see
[section 3](#3-which-folders-are-left-out-of-home-snapshots-and-why).

---

## 11. What changes on my system

After `snapshots setup`:

| Path | What |
|------|------|
| `/usr/local/sbin/btrfs-snapshot`, `btrfs-snapshot-core`, `btrfs-notify`, `btrfs-bulk` | the programs |
| `/usr/local/bin/cst-pending-alerts`, `/etc/systemd/user/cst-pending-alerts.service`, `/var/lib/cst-alerts/` | alerts raised while nobody was logged in, shown at the next login ([README.md, section 4.9](README.md#49-desktop-alerts-btrfs-notify)); shared with the monitoring setup |
| `/etc/systemd/system/btrfs-snapshot.{service,timer}` | hourly snapshots |
| `/etc/systemd/system/btrfs-snapshot-nas.{service,timer}` | NAS backup (enabled only by `nas setup`) |
| `/etc/systemd/system/btrfs-subvol-convert.service` | conversion at boot (enabled only while needed) |
| `/etc/systemd/system/btrfs-bulk-boot.service` | moves busy items into `~/Bulk` at boot (enabled; does nothing unless the flag below exists) |
| `/etc/systemd/system/var-mnt-btrfs\x2dtop.{mount,automount}` | on-demand mount of the top level at `/var/mnt/btrfs-top` |
| `/var/mnt/btrfs-top/snapshots/` | the snapshots |
| `/etc/btrfs-setup/snapshots.conf` | settings (your manual edits are kept) |
| `/etc/btrfs-setup/convert-plan` | the folder list for your home |
| `/etc/btrfs-setup/convert-at-boot` | flag, only while a boot conversion is pending |
| `/etc/btrfs-setup/bulk.conf` | whose `~/Bulk` the boot step handles (`BULK_USER=`) |
| `/etc/btrfs-setup/bulk-at-boot` | flag, only while busy `~/Bulk` items wait for the boot (one item name per line) |
| `/etc/btrfs-setup/bulk-keep` | items you keep in /home on purpose (`bulk keep ID`; one id per line, `#` starts a comment); only if you used it |
| `/var/lib/btrfs-bulk/` | journal of a move in progress (`journal-<item>.json`, or `journal-steam-<appid>.json` for a Steam game; normally absent), `boot-attempts` |
| `/var/lib/btrfs-snapshot/` | state: `last-ok`, `core-last-run`, `drop-pending`, `skipped`, `paused`, `emergency`, `alerted-*`, `nas-*` |
| your home | the nested subvolumes of [section 3](#3-which-folders-are-left-out-of-home-snapshots-and-why); `~/Bulk` with `CACHEDIR.TAG` and its folders; the usual paths of section 3 now symlinks into it; `~/.cache/uv` deleted; old cargo `target/` folders deleted |
| `~/.local/share/flatpak/overrides/ai.lmstudio.lm-studio`, `.../com.valvesoftware.Steam` | the `--filesystem` permission for `~/Bulk/models` and `~/Bulk/steam` (only if the app is installed) |
| `~/.var/app/com.valvesoftware.Steam/.local/share/Steam/config/libraryfolders.vdf`, `.../steamapps/libraryfolders.vdf` | after `bulk steam apply`: `~/Bulk/steam` added as a library, the moved games listed under it; the old versions next to them as `libraryfolders.vdf.cst-bak-<date>` (five kept) |
| `~/Bulk/steam/libraryfolder.vdf`, `~/Bulk/steam/steamapps/` | after `bulk steam apply`: the new library's marker (a new random `contentid`) and the moved games (reflinked, or, with `--copy`, NOCOW games written again as copy-on-write files) |
| `~/.cargo/config.toml`, `~/.config/uv/uv.toml` | the lines of [section 4](#4-changes-to-your-cargo-and-uv-settings) |
| `~/.local/lib/cst/cst-toolchains`, `~/.local/lib/cst/toolchains.conf`, `~/.config/systemd/user/cst-toolchains-prune.{service,timer}` | the hourly pruning of `~/.cache/cargo-build` ([section 4](#pruning-of-cargos-build-folders-in-cachecargo-build)), written by `cst toolchains auto on` for your user (not if you ran `cst toolchains auto off` before: that leaves `~/.config/cst/toolchains-auto.off`); its last run in `~/.local/state/cst/toolchains-rust-build.json` |
| `~/.var/app/com.google.Chrome/config/chrome-flags.conf` | the comment and the two cache lines of [section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume) (only if Chrome's Flatpak is installed) |

After `snapshots nas setup`, in addition:

| Path | What |
|------|------|
| `/etc/btrfs-setup/ssh/id_ed25519` (+ `.pub`) | SSH key for the NAS |
| `/root/.ssh/config` | block between `# BEGIN btrfs-setup.sh NAS` and `# END btrfs-setup.sh NAS` (the rest of the file is kept) |
| `/root/.ssh/known_hosts` | the NAS host key |
| `/etc/btrfs-setup/restic-password` | the repository password |
| layered package `restic` | installed with `rpm-ostree` |
| `/var/cache/btrfs-snapshot-restic` | restic's local cache |
| on the NAS | the repository folder; the public key in the user's `authorized_keys` |

---

## 12. How to undo

| Goal | Command |
|------|---------|
| Stop new snapshots and the NAS backup, keep existing snapshots | `cst btrfs snapshots disable` |
| Stop and delete all local snapshots | `cst btrfs snapshots disable --purge` |
| Stop only the NAS backup | `cst btrfs snapshots nas disable` |
| Chrome's cache back on the disk | `cst btrfs snapshots chrome-cache off`, then restart Chrome ([section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)) |

`disable` also cancels a pending conversion at boot: it removes
`/etc/btrfs-setup/convert-at-boot` and disables `btrfs-subvol-convert.service`.

`disable` does **not** cancel pending moves into `~/Bulk`: `~/Bulk` is not
part of the snapshots, and `btrfs-bulk-boot.service` stays enabled (it also
finishes interrupted moves). Busy items still move at the next boot. To cancel
that, remove the flag: `sudo rm /etc/btrfs-setup/bulk-at-boot`.

`disable --purge` deletes only the snapshots btrfs-snapshot-core owns
([section 15](#15-safety-rules-of-btrfs-snapshot-core)), in groups of 10, and
skips pinned ones. Anything else in the folder stays, and the script says how
many entries were left.

### Undo one Bulk link

The links into `~/Bulk` are plain symlinks and folders, and nothing else
depends on them. Putting one item back in /home takes two steps: tell the
script to leave it alone (otherwise the next `bulk apply` or `snapshots
setup` moves it back into `~/Bulk`), then move the data back. Example, the
Android SDK (its id is `android-sdk`; the ids are in the table of
[section 3](#bulk-one-folder-for-everything-you-can-download-again)). Close
the programs that use it, then:

```
cst btrfs bulk keep android-sdk    # leave this item alone from now on
rm ~/Android/Sdk                            # removes only the link (no slash at the end!)
mv ~/Bulk/toolchains/android/sdk ~/Android/Sdk   # moves the data back (a copy: it crosses a subvolume)
```

Its data is then in the `/home` snapshots again. `bulk status` shows
`kept in /home (by choice)` for it; that is not a problem, and `snapshots
setup` does not warn about it. Changed your mind? `cst btrfs bulk
unkeep android-sdk`, then `bulk apply` moves it into `~/Bulk` again.

For `~/Programs` (id `apps`): the JetBrains IDEs are inside `apps/jetbrains`,
so keep both (`bulk keep apps jetbrains`), undo the JetBrains link first
(`rm ~/.local/share/JetBrains/Toolbox/apps; mv ~/Bulk/apps/jetbrains
~/.local/share/JetBrains/Toolbox/apps`), then `rm ~/Programs; mv ~/Bulk/apps
~/Programs`. (Keeping only `apps` makes the JetBrains item wait, or need a
look if its IDEs are in /home; `bulk status` says why.) For the Flatpak permissions:
`flatpak override --user --nofilesystem=<path> <app>`.

To remove `~/Bulk` completely (everything in it is gone afterwards):

1. Undo every link as above (`bulk keep` with every id).
2. Stop it from coming back. With snapshots set up, the hourly run creates
   every path of `/etc/btrfs-setup/convert-plan` that is missing, `~/Bulk`
   too. Remove the line `copy /var/home/<you>/Bulk` from that file (or run
   `cst btrfs snapshots disable` first). `snapshots setup` writes
   that line back, and `bulk apply` and `snapshots setup` create `~/Bulk`
   and its links again: do not run them afterwards.
3. Delete the subvolumes **inside** `~/Bulk` first (an earlier version may
   have left some there, for example `~/Bulk/iso`): btrfs refuses to delete a
   subvolume that still contains others (`Directory not empty`). List them
   with `sudo btrfs subvolume list -o ~/Bulk` and delete each one with
   `sudo btrfs subvolume delete <path>`.
4. Then `sudo btrfs subvolume delete ~/Bulk`.

### Move a Steam game back

Games that `bulk steam apply` moved are normal Steam games in a normal Steam
library. To move one back to the main library, use Steam itself: right-click
the game > **Properties > Installed Files > Move install folder**, and choose
the main library. Then keep the script from moving it again: `cst btrfs bulk
keep steam` (this stops `bulk steam` for every game; the folder `~/Bulk/steam`
stays).

A game that `--copy` moved is copy-on-write now. Moved back into a main
library whose folders have `+C`, Steam's new files there get `+C` again.

To undo everything at once instead, with **Steam closed**: copy the oldest
backup of this round back over both files (`config/libraryfolders.vdf.cst-bak-<date>`
over `config/libraryfolders.vdf`, the same in `steamapps/`), then move the
game's folders and its `appmanifest_<id>.acf` from `~/Bulk/steam/steamapps`
back to the same places in the main library (`mv` makes a real copy here:
it crosses a subvolume). Do not restore a backup without moving the files
back: Steam would then believe the games are missing.

### Turn it back on

| Goal | Command |
|------|---------|
| Hourly snapshots again | `cst btrfs snapshots setup` (re-enables only the hourly timer) |
| NAS backup again | `cst btrfs snapshots nas enable`, or `snapshots nas setup --path ...` again |

### Cannot be undone

- The deletion of `~/.cache/uv` and of the old `target/` folders during
  `setup`, and of the cargo build folders the hourly pruning removes. They
  are caches and build files: uv and cargo recreate them.

### What stays after `disable --purge`

No command removes these:

- `~/Bulk`, its links and the Flatpak permissions (undo by hand:
  [above](#undo-one-bulk-link));
- the nested subvolumes in your home. They behave as normal folders, so you
  can leave them. Turning them back into plain folders is **not supported**
  by the script;
- the cargo and uv settings (remove the lines marked
  `# Added by btrfs-setup.sh (CST)`, or `(f44-tools)` from an older version,
  by hand if you want);
- the hourly pruning of `~/.cache/cargo-build`: it goes with the cargo
  setting above. Remove it with `cst toolchains auto off` (no `sudo`;
  a later `setup` then leaves it off);
- `/etc/btrfs-setup/snapshots.conf` (`disable` does not set `NAS_ENABLED=0`;
  only `nas disable` does), `convert-plan`, the programs in `/usr/local/sbin`
  and the unit files of the snapshot services (disabled, except
  `btrfs-bulk-boot.service`, which only runs when a `~/Bulk` move waits);
- the login-time alert part (`cst-pending-alerts`, its user unit and
  `/var/lib/cst-alerts/`). `cst btrfs uninstall` keeps it too, as
  long as the snapshot programs are installed;
- the state folder `/var/lib/btrfs-snapshot/` (including `core-last-run`) and
  the empty mount point folder `/var/mnt/btrfs-top`;
- for the NAS backup: the restic repository on the NAS,
  `/etc/btrfs-setup/restic-password`, the SSH key
  `/etc/btrfs-setup/ssh/id_ed25519` (+ `.pub`), the `# BEGIN btrfs-setup.sh
  NAS` block in `/root/.ssh/config`, the NAS host key in
  `/root/.ssh/known_hosts`, restic's cache `/var/cache/btrfs-snapshot-restic`,
  and the public key in the NAS user's `authorized_keys`. If you stop using
  the NAS backup, remove that key from `authorized_keys` on the NAS;
- the layered `restic` package (remove with `rpm-ostree uninstall restic`
  if you want).

`disable` does not change Chrome's `chrome-flags.conf` either: to put
Chrome's cache back on the disk, run `cst btrfs snapshots
chrome-cache off` ([section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)).

`cst btrfs uninstall` (the monitoring part, see
[README.md, section 6](README.md#6-uninstall-how-to-undo)) does not touch
snapshots.

---

## 13. Known limitations

- **About 30 seconds of delay.** btrfs writes changes to disk in batches,
  about every 30 seconds (a *commit*). A file saved a few seconds before a
  run may not count as a change yet; the next run takes it.
- **Up to one hour of loss.** Snapshots are hourly. Work done since the last
  snapshot is not in any snapshot.
- **Nested subvolumes are empty in snapshots.** Their data cannot be restored
  from snapshots or the NAS backup. This includes everything in `~/Bulk`.
- **The emulator disks in `~/Bulk/emulators` have no checksums** (NOCOW):
  scrub cannot check or repair them. After a degraded RAID1 episode (one disk
  missing for a while), wipe the emulators' data instead of trusting it.
- **Tools see symlinks.** A tool that resolves paths may show
  `/var/home/<you>/Bulk/...` instead of the usual path. A tool that refuses
  symlinks (rare) needs its link undone ([section 12](#undo-one-bulk-link)).
- **Chrome's web cache is lost at every reboot** (it lives in RAM; not at
  logout on this PC, because your user lingers;
  [section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)): the first
  page loads after that are slower.
- **NOCOW Steam games are copied, not reflinked** (`bulk steam apply
  --copy`): it needs the game's full size plus 170 GiB free and takes time,
  and the old copy's space comes back only when the `/home` snapshots that
  hold it are deleted. A game folder with only some NOCOW files (even one
  empty file) is copied as a whole.
- **Steam games move only when you move them**, with `bulk steam apply` or
  in Steam ([section 3](#steam-move-your-games-into-the-bulk-library)), and new
  games go to `~/Bulk/steam` only after you make it the default library by
  hand: the script never touches `config/config.vdf`, which holds that
  setting together with your login.
- **The list of Steam tools can go stale.** Tools (runtimes, Proton,
  redistributables) are recognised by a short list of app ids and by their
  names. A new tool with another name would be moved like a game (harmless:
  Steam finds it in the other library, or downloads it again).
- **Same disks.** Local snapshots are not a backup ([section 1](#1-what-a-snapshot-is-and-what-it-is-not)).
- **The NAS backup has only the newest snapshot of each day**, not every
  hourly one.
- **Do not replace the whole /home with a snapshot.** This is for experts
  only; to get data back, copy files as shown in [section 6](#6-how-to-restore-a-file-or-a-folder).
  The script has no rollback command. If you do it by hand anyway, the new
  `home` subvolume has a new UUID (ID number). The old snapshots then no
  longer have a matching Parent UUID: btrfs-snapshot-core shows them as
  `IGNORED`, never deletes them, and `disable --purge` does not delete them
  either. You must delete them by hand with `btrfs subvolume delete`.

---

# Part 2: how it works inside

## 14. How a folder becomes a subvolume

You cannot "switch" an existing folder into a subvolume. The script builds a
new subvolume next to it and swaps the names. It is done by
`/usr/local/sbin/btrfs-snapshot` (`convert` during `setup`, `convert-boot` at
boot, and at the start of every hourly run).

The **old tree** is the original folder after the swap, renamed to
`FOLDER.pre-subvol-<date>`. It is deleted later.

### The steps for one folder (strategy `copy`)

1. **Checks.** The folder is skipped (and the reason is shown by `status` as
   `skipped: ...`) if it is a symlink (a shortcut that points to another path)
   or a file, if it is or contains a mount point, or if it already contains a
   nested subvolume. Skipped folders are checked again on every run, because
   the cause may go away.
2. **In use?** The script looks at every process: its open files, its working
   folder, and the bind mounts of containers (for example `docker run -v
   ~/Workspace/x:/src`). If anything uses the folder, it is left for later.
3. **New subvolume** `FOLDER.subvol-new`, with the same owner, permissions and
   SELinux label as the folder (an SELinux label is a security tag that Fedora
   puts on every file).
4. **Reflink clone.** All files are copied with `cp -a --reflink=always`. A
   reflink copy writes no file data: the new file points to the same data
   blocks on the disk. So even 100 GiB is copied in seconds or minutes, and no
   extra space is used. Files marked NOCOW (no copy-on-write) get the same mark
   first, because a reflink between NOCOW and normal files is not possible.
   If anything fails, or `cp` prints any warning, the new subvolume is deleted
   and the folder stays as it was.
5. **Count check.** Old and new must have the same number of entries.
6. **Race guard.** Right before the swap, the script checks again: is a
   process using the folder now? Did anything write into it during the clone
   (it compares the *ctime*, a time stamp that programs cannot set back)? If
   yes, the clone is deleted and the folder is tried again later.
7. **Rename twice.** `FOLDER` becomes `FOLDER.pre-subvol-<date>`, then
   `FOLDER.subvol-new` becomes `FOLDER`. The old name is written to
   `/var/lib/btrfs-snapshot/drop-pending` first, so it is never forgotten.
8. **Last check after the swap.** If something wrote into the old tree in that
   short moment, everything is renamed back, and tried again later.
9. **Old tree deleted.** The old tree is deleted with `rm` at the lowest CPU
   and disk priority, at most 15 minutes per tree each time; what is left is
   retried on the next hourly run. Because of the reflinks, this frees no
   space and costs no space: the data is still used by the new subvolume.
   - During `setup` and in the hourly run, this happens **right away**, after
     each converted folder. That is why `setup` can take a while.
   - At boot, nothing is deleted: the next hourly run does it.

If a run is interrupted between the two renames, the next run finishes the
work (log message: `finished an interrupted conversion`).

### Strategy `aside` (only `~/.cache/uv`)

Before `~/.cache` is cloned, `~/.cache/uv` is moved out of the way as
`~/.uv-aside.pre-subvol-<date>`. This is done only while neither `~/.cache/uv`
nor `~/.cache` is a subvolume yet. Cloning a pure cache of that size would be
wasted work. The moved folder is then deleted like an old tree (step 9):
during `setup` right away (at most 15 minutes each time), the rest by the
hourly runs.

### Folders in use: converted at the next boot

During `setup`, folders that your session keeps open (IDE, Steam, browser...)
cannot be swapped safely. For them:

- `setup` writes the flag file `/etc/btrfs-setup/convert-at-boot` and enables
  `btrfs-subvol-convert.service`.
- Before the reboot, every hourly run already tries the busy folders again.
  If all of them are done (for example after you close Steam), the flag is
  removed and snapshots start without a reboot.
- At the next normal boot, this service runs **before the login screen**
  (`Before=systemd-user-sessions.service display-manager.service`), when
  nothing of your session is running. It may take some minutes (time limit
  45 minutes). Its messages are also shown on the boot console.
- After two boots with folders still left, it gives up and removes the flag.
  The message `Subvolume conversion at boot gave up` is written to the
  journal (`journalctl -u btrfs-subvol-convert`) and the boot console.
  Nobody is logged in yet at that moment, so the desktop alert is kept and
  shown at your next login ([README.md, section 4.9](README.md#49-desktop-alerts-btrfs-notify)).
  It never blocks your login more than twice.
- After the give-up, snapshots start anyway. The remaining folders are retried
  by every hourly run whenever they are not in use. Until then, their data is
  in the `/home` snapshots. `snapshots status` shows them as `pending (busy)`.

### How an item moves into ~/Bulk

This is done by `/usr/local/sbin/btrfs-bulk` (Python), from `bulk apply`,
from `snapshots setup` and at boot (`btrfs-bulk-boot.service`). It runs as
root, but **every change in your home is made by a child process that runs
as you** (your user ID, your groups). So a symlink in your home can never
make root write somewhere else, and everything it creates belongs to you.
It takes the same lock as the snapshot program (`/run/btrfs-snapshot.lock`),
so they never work at the same time.

1. **`~/Bulk` itself.** Absent: created as a subvolume, as you (btrfs lets a
   user create a subvolume wherever they may create a folder), given its
   SELinux label with `restorecon` (a new subvolume has none), then
   `CACHEDIR.TAG`. A plain folder: converted by `btrfs-snapshot convert-one`
   (the steps of this section). A file or symlink: nothing is done at all.
2. **Each item is checked** (read-only): absent, the right link, a real
   folder, a subvolume, or something unexpected ([section 3](#order-with-snapshots-setup)).
   Nothing in the path is followed through a symlink.
3. **In use?** The same check as for the conversion (open files, working
   folders, container bind mounts), plus programs **started from** the folder,
   files mapped into memory (for example a model LM Studio reads), and
   containers that mounted a folder **above** it inside your home (for
   example `-v ~/.m2:/root/.m2` for `~/.m2/repository`).
   In use: left for the boot.
4. **Copy** into `~/Bulk/<target>.bulk-new`: `cp -a --reflink=auto` (the data
   blocks are shared, nothing is written), or for the emulator disks
   `cp -a --reflink=never` into a folder that has `+C`. Before the copy the
   script notes the time (the *ctime* of the new folder). If the folder
   contains another subvolume or a mount, or entries that are not yours
   (your account could not delete the old folder afterwards), it stops
   (`NEEDS A LOOK`).
5. **Check**: the copy must have the same number of entries and bytes; for
   the emulator disks, every file must have `+C`. Otherwise the copy is
   deleted and the item reported.
6. **Race guard** right before the switch: in use now? Changed since the
   copy started (any *ctime* newer than the noted time)? Then the copy is
   deleted and the item waits for the boot.
7. **Journal**: the item is added to the boot flag
   (`/etc/btrfs-setup/bulk-at-boot`), the script notes the time again, then
   `/var/lib/btrfs-bulk/journal-<item>.json` (owned by root) records the
   paths and both times before anything is renamed. So after a power loss
   the boot step runs and repairs it before you log in (with the same checks
   as step 9), and no snapshot is taken meanwhile.
8. **Switch**: the old folder becomes `<path>.bulk-old-<date>`, the copy
   becomes `~/Bulk/<target>`, and the symlink is made at the old path.
9. **Last check**: if something wrote into the old folder in that instant,
   everything is put back, and the item waits for the boot. If something
   **also** wrote into the copy (through the new link), both are kept and the
   item is reported (`written to on both sides`, [section 10](#10-what-to-do-if)).
10. **Old folder deleted** (as you; read-only folders such as Go's module
    cache are made writable first). Just before, the journal is marked
    `"finishing"`. Then the journal is removed.

**Interrupted?** The boot step (or the next run) reads the journal first. If
the link is in place, the switch was complete: it makes the last check of
step 9 (with the two times from the journal), then deletes the old folder.
If the journal is already marked `"finishing"`, the last check had passed
and the old folder was being deleted: it just goes on deleting (each deleted
file changes the folder it was in, which would otherwise look like a late
write). If
something wrote into the old folder after the copy started, it puts the old
folder back instead (or, if the copy was written to as well, keeps both and
reports `written to on both sides`). If the switch was not complete, it puts
the old folder back at its usual path and deletes the copy, unless the copy
was written to through the link after the switch: then it keeps both and
reports it ([section 10](#10-what-to-do-if)). Either way your
data is never only in a half-finished place, and a late write is never
deleted. Then it tries again. One exception: if the usual path was **created
again** meanwhile (an app started before the repair), it deletes nothing,
keeps the journal and reports it ([section 10](#10-what-to-do-if)). A repair
that fails (for example a file it cannot delete) is reported the same way,
and snapshots wait while such a journal exists.

**Old subvolumes** (an earlier version of this script made
`~/.local/share/JetBrains/Toolbox/apps`, `~/Downloads/iso`, `~/.android/avd`
and the LM Studio models nested subvolumes): such a subvolume is simply
**renamed** into `~/Bulk` (btrfs allows renaming a subvolume into another
one; nothing is copied) and stays a nested subvolume there. That is fine:
`~/Bulk` is left out of snapshots anyway. Only the emulator disks are copied
(steps 4-10), because they need `+C`. An old `steamapps` subvolume stays
where it is.

**In-place `+C` repair**: if `~/Bulk/emulators/android/avd` holds files without `+C`
(for example after such a rename), the same copy-and-switch is done inside
`~/Bulk` (no link involved). The last check works the same: a late write into
the old folder puts the old folder back in place (the copy is deleted). All
of its paths are inside `~/Bulk`, so its journal is marked `"bulk_only":
true`: snapshots do not wait for it, and if it needs a look, `bulk apply`
exits with 4 instead of 1, and `snapshots setup` does not ask. If only a
folder lacks `+C` (the AVD folder, one inside it, `~/Bulk/emulators` or
`~/Bulk/emulators/android`) and no file does, the mark is simply set on the
folders: nothing is copied.

### How a Steam game moves into ~/Bulk/steam

`bulk steam apply` (the same program, as root, every change in your home made
as you, under the same lock) works like the move of an item above, with
Steam's own files added:

1. **Before anything**: `cst btrfs bulk steam apply` installs the current
   program as `/usr/local/sbin/btrfs-bulk`, writes
   `/etc/btrfs-setup/bulk.conf` and enables `btrfs-bulk-boot.service`, as
   `bulk apply` does. The boot step runs that installed copy, so it must be
   as new as the journal it may have to repair. (`bulk steam plan` installs
   nothing.) Then an interrupted earlier move is finished or undone
   (see below). Then it stops if Steam runs (exit status 5), if `steam` is
   kept out of `~/Bulk`, if `~/Bulk/steam` or the Flatpak permission is
   missing, or if `libraryfolders.vdf` is not exactly as expected. The two
   copies are read and parsed by a strict reader: only quoted text and braces,
   no comments or includes, no duplicate keys, every library entry with only
   known keys. Each file must also come out byte for byte the same when the
   script writes it back.
2. **Each game is checked** ([section 3](#steam-move-your-games-into-the-bulk-library)
   lists the refusals). A game that fails is reported; the others go on.
3. **Reflink or copy, per part**: every file of each part (game folder,
   Workshop content, Workshop file, manifest) is checked for the NOCOW mark
   (`+C`). A part with a NOCOW file is copied byte by byte (btrfs cannot
   clone a NOCOW file into copy-on-write `~/Bulk/steam`), the others are
   reflinked. Games that need a copy are refused without `--copy`. Every
   folder from `~/Bulk/steam` to the new place must be copy-on-write.
   **Reflink test**: one copy-on-write file of a part that is reflinked is
   cloned into `~/Bulk/steam` and deleted again. If that fails, nothing is
   done (`reflink ... failed (is ~/Bulk on another filesystem?)`). With
   only real copies to do, there is no test.
4. **The library**: `~/Bulk/steam/steamapps/common` is made, the marker
   `~/Bulk/steam/libraryfolder.vdf` gets a new random `contentid`, and the
   entry is added to both `libraryfolders.vdf` (after a backup of each).
   An entry counts as `~/Bulk/steam` when its path leads to the same folder
   after symlinks are resolved (on Silverblue `/home` is a link to
   `/var/home`), so a library you added by hand is reused, never added twice.
   Every list is checked again before it is written (the same checks as
   when it is read, including "no two entries for the same folder"); a list
   that fails them is never written (`NEEDS A LOOK`, nothing changed).
   Each file is written as a new file next to it, synced to the disk, then
   renamed over the old one; `config/` first, then `steamapps/`.
5. **Per game**, in order of app id: Steam running or the files in use? Then
   stop. A game with a real copy needs its size plus 170 GiB free (the
   snapshot emergency limit, 150 GiB, or the value in `snapshots.conf`,
   plus 20 GiB), or it is skipped (`NEEDS A LOOK`). A journal `/var/lib/btrfs-bulk/journal-steam-<appid>.json` (owned by
   root) is written, phase `prepared`.
6. **Copy**: each part (game folder, Workshop content, Workshop file,
   manifest) is cloned with `cp -a --reflink=always` to `<name>.bulk-new` in
   `~/Bulk/steam/steamapps` (a NOCOW part: `cp -a --reflink=never`, at idle
   priority, with progress lines). **Check**: name, type, size, time stamp,
   permissions and link target of every entry must match the original. For a
   real copy also: no entry with `+C`, and every file has the same bytes
   (the copy is synced, each file fsynced, then read back from the disk; a
   sync or fsync error counts as a failed check). A failed check deletes
   the copies; the game stays where it was.
7. **Race guard**: Steam running, the files in use, or anything changed since
   the copy started? Then the copies are deleted and the game is not moved.
8. **Switch** (phase `switching`): per part, the original becomes
   `<name>.bulk-old-<date>` and the copy takes the final name; the manifest is
   the last part.
9. **Last check**: if something wrote into an old copy in that instant, the
   move is undone; if something also wrote into the new copy, both are kept
   and reported (`written to on both sides`).
10. **Library list** (phase `switched`): both `libraryfolders.vdf` list the
    game under `~/Bulk/steam` only, with the same size value as before.
11. **Old copies deleted** (phase `finishing`), then the journal is removed.

**Interrupted?** The next `bulk steam apply`, `bulk apply`, or the boot step
(`btrfs-bulk-boot.service` also runs when only a journal exists) reads the
journal first:

| Phase found | What it does |
|---|---|
| `prepared` | deletes the `.bulk-new` copies (also a half-done real copy); the game stays in the main library |
| `switching`, not every part switched | puts every part back (the manifest leaves `~/Bulk/steam` first), deletes the copies |
| `switching`, every part switched | the last check of step 9, then steps 10 and 11 |
| `switched` | writes both `libraryfolders.vdf` as intended (also when only one was written), then step 11 |
| `finishing` | makes sure both lists are right, deletes what is left of the old copies |
| written on both sides | keeps everything and reports it, until you resolve it |

Writing the list is safe to repeat: listing a game under one library and
removing it from the others gives the same result however often it is done.
If a path in the main library was created again while the data waited in its
`.bulk-old-` copy, nothing is deleted and it is reported, as for the other
items. Snapshots wait while such a journal exists (the game's data is still
in /home).

### Why snapshots wait

No new snapshot is taken while:

- old trees are still being deleted (`drop-pending` is not empty), or
- the boot flag is set and planned folders are still waiting, or
- busy `~/Bulk` items wait for the boot (`/etc/btrfs-setup/bulk-at-boot`), or
- a move into `~/Bulk` is unfinished (a journal in `/var/lib/btrfs-bulk`,
  also of a Steam game),
  except the `+C` repair inside `~/Bulk` (none of its data is in /home).

Reason: a snapshot taken now would still contain the data that is about to
leave /home. Because the first snapshot of a month is kept for 3 months, that
data would stay on the disk for months. Old snapshots still expire normally
while new ones wait. After one day of waiting you get an alert.

---

## 15. Safety rules of btrfs-snapshot-core

`/usr/local/sbin/btrfs-snapshot-core` is a small Python program in this
repository (`btrfs/btrfs-snapshot-core`). It creates and deletes the
snapshots. It uses only Python's standard library, so nothing has to be
installed. Its main idea: **a snapshot is deleted only if every rule agrees.**

### What it considers "its own" (ownership)

It looks only at entries in the snapshot folder that:

- have a name of the form `NAME.YYYYMMDDTHHMM` or `NAME.YYYYMMDDTHHMM_N`
  (`NAME` is `home` or `Workspace`);
- are a real subvolume (not a symlink, not a normal folder);
- are read-only;
- have a *Parent UUID* equal to the UUID of the source. (A UUID is a unique ID
  number; the Parent UUID of a snapshot is the ID of the subvolume it was
  taken from.)

Only these are ever deleted.

- An entry with a matching name that fails another check is shown as
  `IGNORED` by `snapshots list`.
- Entries with other names are not shown at all.
- An entry that could not be checked is shown as `UNKNOWN`, and while one
  exists, no deletions happen for that source.

Neither `IGNORED` entries nor entries with other names are ever deleted.

Example of `snapshots list` (shortened, the numbers are made up):

```
home: /var/mnt/btrfs-top/home (generation 812345, changed since the newest snapshot), policy '72h 7d 4w 3m'
  KEEP    home.20261013T1400               preserve min: latest
  KEEP    home.20261013T1300               preserve hourly: first of hour, 1 hours ago
  KEEP    home.20261006T0900               preserve daily: first of day, 7 days ago, 9h after 00:00
  DELETE  home.20260601T0900               not kept by policy '72h 7d 4w 3m'
  IGNORED home.20261012T0800               not read-only
  41 snapshot(s), 1 due for deletion
```

### What it always keeps

- the **3 newest** snapshots of each source;
- every snapshot **younger than 24 hours**;
- every **pinned** snapshot: the NAS backup writes the snapshots it is reading
  into `/run/btrfs-snapshot/pinned`, and these are never deleted while listed.

In `snapshots list` these show as `KEEP` with `safeguard: ...` as the reason.

### Clock guards

A snapshot's name is its time. A wrong clock would give a wrong name, and the
retention would then keep or delete the wrong snapshots. NTP is the network
service that keeps the clock correct; "synchronized" means NTP has set it
(check with `timedatectl`).

| Situation | What the program does | What you do |
|-----------|-----------------------|-------------|
| Clock not synchronized yet (no network, just after boot), and it looks plausible (1) | No deletions. A new snapshot is taken, with a notice in the journal. | Usually nothing. Wait for the network. Check `timedatectl`. |
| Clock not synchronized, and it does not look plausible (1) | No deletions and no new snapshot. | Wait for `System clock synchronized: yes`. |
| Clock synchronized, but the newest snapshot has a date in the future (it was named while the clock was ahead) | No deletions until that date passes. A new, correctly named snapshot is taken, and a warning names the wrong one. | Check `snapshots list`. Delete the wrong one by hand if you want: `sudo btrfs subvolume delete /var/mnt/btrfs-top/snapshots/<name>`. |
| Clock is behind the last successful run (more than 5 minutes) | No deletions and no new snapshot. | Check `timedatectl`. If you are sure the clock is right, delete `/var/lib/btrfs-snapshot/core-last-run`. |

(1) "Plausible" means all of these: the clock is not behind the newest
snapshot; since the last successful run in the same boot, the clock did not
move forward more than the real time that passed; and in the first 30
minutes after boot, the clock is not more than 24 hours ahead of the newest
snapshot and the last successful run.

The last successful run is recorded in `/var/lib/btrfs-snapshot/core-last-run`,
only when the clock is synchronized. It never moves backwards.

### Circuit breaker

A circuit breaker is a switch that stops everything when something looks
wrong. Here: if a normal run would delete **more than 5 snapshots and more
than 10 %** of a source's snapshots, it deletes **none** of them and reports
`circuit breaker: ...`.

Deletions that are explained by time passing do not count. For example, after
a week with the computer off, many hourly snapshots expire at once, and that
is expected. What counts is the rest: a shorter retention, old-named
snapshots that appeared, or a clock that jumped forward.

To review and then confirm:

```
cst btrfs snapshots list
cst btrfs snapshots prune             # dry run: shows what would go
cst btrfs snapshots prune --confirm   # deletes it
```

Even then, the 3 newest, the last 24 h and pinned snapshots are kept, and the
clock guards still apply.

### For scripts and debugging

- Right before deleting, the folder is listed again and every candidate is
  checked again (same UUID, still owned, still due, not pinned meanwhile).
  Deletions go one by one.
- Only one run at a time: the lock file `/run/btrfs-snapshot.lock` is shared
  by the hourly run, the conversions, `prune` and `purge`.
- Exit codes: 0 ok, 1 error, 2 wrong usage, 3 another run holds the lock,
  4 a safety rule blocked a creation or deletion.

---

## 16. The space guard

Snapshots keep old data, so they use space. Before each hourly snapshot, the
script reads btrfs' **"Free (estimated)"** value. This value already takes
RAID1 into account. You can read it yourself:

```
sudo btrfs filesystem usage /var
```

Look at the line `Free (estimated):`.

How it works, as a sequence:

1. Free space falls to **250 GiB or less**: snapshots **pause**. No new
   snapshots; old snapshots still expire normally. Alert
   `Snapshots paused: low space`.
2. They stay paused until free space is **above 270 GiB**. The gap between
   250 and 270 stops them from switching on and off every hour.
3. Free space falls to **150 GiB or less**: **emergency purge**. The retention
   becomes `24h 3d` (`EMERGENCY_PRESERVE`) for both sources, the circuit
   breaker is off, and the extra snapshots are deleted. Alert
   `Snapshots: emergency purge`.
   - The 3 newest, the last 24 h and pinned snapshots are still kept.
   - The clock guards of [section 15](#clock-guards) still apply: if the clock
     is not synchronized, the emergency purge deletes nothing (alert
     `Snapshots: safety check blocked deletions`).
   - After an emergency purge, snapshots stay paused until free space is
     above 270 GiB.

The limits are in `/etc/btrfs-setup/snapshots.conf`: `SKIP_AT_OR_BELOW_GIB=250`,
`RESUME_ABOVE_GIB=270`, `EMERGENCY_AT_OR_BELOW_GIB=150`.

`cst btrfs bulk steam apply --copy` respects them: a real copy of a game
needs its size plus the emergency limit plus 20 GiB free, and `plan` warns
when the copies would pause the snapshots
([section 3](#steam-move-your-games-into-the-bulk-library)).

Deleting snapshots frees space only for data that no other snapshot and no
current file still uses, and btrfs frees it in the background. The real fix
for low space is usually to delete big files (caches, images, VMs).

---

## 17. Why btrbk is no longer used

Earlier versions used **btrbk**, a common snapshot tool, for the snapshots and
a raw copy to the NAS. It was replaced by `btrfs-snapshot-core` (local
snapshots) and restic (NAS backup). The core keeps btrbk's names and
retention rules (those of btrbk version 0.32.7: each count includes the
current hour, day, week or month, so `72h` covers hours 0 to 72). What the
code shows about the reasons:

- nothing to install: btrbk had to be added with `rpm-ostree`, which needs a
  reboot; the core is a script in this repository;
- the change detection follows a newer kernel behavior (the code notes btrbk
  issue #656: since kernel 7.1, an idle subvolume's generation can be lower
  than its snapshot's);
- extra safety rules (ownership, pins, clock guards, circuit breaker);
- restic can restore a single file or folder directly (`restic restore`,
  `restic mount`). The old raw btrfs streams had to be replayed as a whole
  chain first.

`snapshots disable` removes btrbk config files left by earlier versions (only
the ones marked `Managed by btrfs-snapshot`).

---

## 18. Glossary

- **automount**: systemd mounts a filesystem the first time something opens
  its path, and unmounts it after some idle time.
- **bind mount**: showing a folder at a second place in the folder tree.
- **btrfs**: the filesystem of this computer. It supports subvolumes,
  snapshots, checksums and RAID1.
- **Bulk (`~/Bulk`)**: the folder for everything that can be downloaded
  again; a nested subvolume with no snapshots and no NAS backup
  ([section 3](#bulk-one-folder-for-everything-you-can-download-again)).
- **CACHEDIR.TAG**: a small file that marks a folder as a cache. restic skips
  such folders. `~/Bulk` has one.
- **circuit breaker**: a rule that stops all deletions when too many would
  happen at once.
- **commit**: the moment btrfs writes a batch of changes to disk (about every
  30 s).
- **copy-on-write (CoW)**: changed data is written to a new place; the old
  data stays for snapshots.
- **ctime**: a file's "change time", set by the system on every change;
  programs cannot set it back.
- **deduplication**: storing a piece of data only once even if it appears many
  times.
- **directory**: the same as a folder. The program messages use this word.
- **dm-crypt**: the Linux disk encryption under btrfs on this machine.
- **dry run**: a test run that only shows what would happen and changes
  nothing.
- **Flatpak**: a way to install desktop apps, each in its own sandbox.
- **FUSE**: a way for a program to show something (here, the restic
  repository) as normal folders.
- **generation**: a counter btrfs increases with every commit; used to see if
  a subvolume changed.
- **GiB**: gibibyte, 1024 × 1024 × 1024 bytes (a little more than a gigabyte).
- **hardlink**: a second name for the same file. Cannot cross subvolumes.
- **host**: the normal Fedora system, outside any container.
- **host key / trust on first use**: the NAS's SSH ID. The script accepts the
  key it sees the first time and refuses later if it changes.
- **IDE**: a program for writing code (for example RustRover).
- **journal / journalctl**: the system log, and the command that shows it.
- **layered package**: an extra package installed on Fedora Silverblue with
  `rpm-ostree`.
- **mount / mount point**: attaching a filesystem to a folder; that folder is
  the mount point.
- **NAS backup**: the copy of your snapshots on the Synology NAS, made with
  restic.
- **nested subvolume**: a subvolume inside another one. Left out of the
  parent's snapshots.
- **NOCOW**: a file mark (`+C`) that turns off copy-on-write for that file
  (and with it checksums and compression).
  `cst btrfs install` uses it for Docker volumes
  ([README.md, section 4.10](README.md#410-docker-volumes-without-copy-on-write-nocow)),
  and `~/Bulk/emulators` (with everything in it) has it.
- **NTP**: the network service that keeps the clock correct.
- **NVMe**: a fast type of SSD disk.
- **old tree**: the original folder after the swap of [section 14](#14-how-a-folder-becomes-a-subvolume),
  named `FOLDER.pre-subvol-<date>`; deleted later.
- **pin**: a mark that protects a snapshot from deletion while the NAS backup
  reads it.
- **RAID1**: every piece of data is stored on two disks.
- **reflink**: a copy that shares the data blocks with the original; no data
  is written until one of them changes.
- **repository (restic)**: the folder on the NAS where restic stores the
  encrypted backups.
- **restic**: the backup program used for the NAS backup.
- **restic snapshot**: one saved state inside the restic repository (not a
  btrfs snapshot).
- **retention**: the rules for how long snapshots are kept.
- **rpm-ostree**: how Fedora Silverblue installs extra packages ("layering");
  often active only after a reboot.
- **SELinux label**: a security tag that Fedora puts on every file.
- **SFTP / SSH**: encrypted connection and file transfer to the NAS.
- **snapshot**: a frozen, read-only picture of a subvolume.
- **source**: a subvolume that gets snapshots: `home` or `Workspace`.
- **subvolume**: a part of a btrfs filesystem that looks like a folder.
- **symlink**: a shortcut that points to another path.
- **systemd unit / timer / service**: systemd is the part of Linux that starts
  programs. A unit is one of its configuration files; a timer starts a service
  (a program) on a schedule.
- **tmpfs**: a filesystem that lives in RAM; its content is gone when it is
  removed (at reboot; your user runtime folder also at logout, unless your
  user lingers, as on this PC). Chrome's
  Flatpak sees one as `/tmp`
  ([section 3](#chrome-flatpak-its-cache-in-ram-not-a-subvolume)).
- **toolbox**: a container (a small separate Linux system) for development
  tools. These commands do not run inside it.
- **top level**: the root of the btrfs filesystem (`subvolid=5`), above all
  subvolumes; mounted at `/var/mnt/btrfs-top`.
- **UUID / Parent UUID**: unique ID of a subvolume / of the subvolume a
  snapshot was taken from.
- **/var/home**: the real place of `/home` on Fedora Silverblue.
