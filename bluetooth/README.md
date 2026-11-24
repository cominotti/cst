<!-- SPDX-License-Identifier: MPL-2.0 -->
# `cst bluetooth`: LE Audio and adaptive LDAC for the headset

This folder holds the parts of `bluetooth-setup.sh`, the `bluetooth` module of
CST (Cominotti Setup Tools). The script lives one level up, at the top
of the CST folder. You run it as `cst bluetooth <command>`
(how to set up `cst`: [../README.md](../README.md#the-cst-command));
`bash bluetooth-setup.sh <command>` from the repository folder does the same.
It is written for one machine, the workstation **sophia**, and one headset,
the **JBL Live Beam 4**, which can play over classic Bluetooth (A2DP, here
with the LDAC codec) and over LE Audio (LC3). CST names no headset: you set
yours as `DEVICE` (see [section 4](#4-commands-options-and-settings),
"The headset").
LE Audio is kept **off in PipeWire by default**: see
[LE Audio: off by default](#le-audio-off-by-default).

It replaces `apply-le-audio.sh`, a standalone script that lived outside this
repository. What that script wrote is recognised (see
[section 5](#5-coming-from-apply-le-audiosh)).

The entry point of the whole repository is [../README.md](../README.md).

> **In short: what this does to your system**
>
> - **BlueZ** (the Bluetooth service): two lines in `/etc/bluetooth/main.conf`,
>   section `[General]`: `Experimental = true` and
>   `KernelExperimental = 6fbaf188-05e0-496a-9885-d6ddfdb4e03e` (the ISO
>   socket). A backup is taken before each change.
> - **WirePlumber** (your audio session): one rule file,
>   `~/.config/wireplumber/wireplumber.conf.d/50-bluez-<ID>.conf` (`<ID>`: the
>   headset's address with `_` for `:`, in capitals), that
>   sets the headset's LDAC quality to `auto` (adaptive).
> - **LE Audio off in PipeWire** (the default, `LE_AUDIO=off`): a second
>   WirePlumber file, `90-cst-le-audio-off.conf`, so that PipeWire offers
>   only classic Bluetooth audio (A2DP and HFP), for all devices.
>   `cst bluetooth le-audio on` turns LE Audio back on, `le-audio off` turns
>   it off again (see [LE Audio: off by default](#le-audio-off-by-default)).
> - **Optional, off by default**: with `LDAC_RATE=48000` in your settings, a
>   small user service, `cst-bt-ldac48.service`, that puts LDAC back to
>   48 kHz when the headset itself set it up at 96 kHz
>   (see [section 7](#7-ldac-sample-rate-the-optional-watcher)).
> - **Restarts**: only when something really changed.
>   `bluetooth.service` is restarted only after you say yes: Bluetooth
>   keyboards, mice and headsets drop for a few seconds.
> - **Undo**: `cst bluetooth revert` (see [section 8](#8-how-to-undo)).

---

## LE Audio: off by default

**What it means.** BlueZ stays ready for LE Audio (the two `main.conf`
lines below are still set), but PipeWire does not offer it. `apply` writes
`~/.config/wireplumber/wireplumber.conf.d/90-cst-le-audio-off.conf`, which
tells WirePlumber to register only the classic Bluetooth roles with BlueZ:

```
override.monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hfp_ag ]
}
```

That is PipeWire's default role list (`man pipewire-props`, `bluez5.roles`,
PipeWire 1.6.11) without the four LE Audio roles (`bap_sink`, `bap_source`,
`bap_bcast_sink`, `bap_bcast_source`). The `90-` name makes it one of the
last files WirePlumber reads.

**Why `override.` sits on the section name.** WirePlumber 0.5 joins the
settings of all its files: lists are added together, so a plain
`bluez5.roles` could end up as `[ bap_sink, a2dp_sink, ... ]` if an earlier
file also lists roles. The `override.` prefix stops that, but only where it
is written matters. Checked with WirePlumber 0.5.19's own library on this
host:

- `override.monitor.bluez.properties = { bluez5.roles = [...] }` (what CST
  writes) always gives WirePlumber a plain `bluez5.roles` with exactly the
  four classic roles.
- `monitor.bluez.properties = { override.bluez5.roles = [...] }` (what an
  earlier version of CST wrote) only works when another, earlier file also
  sets `monitor.bluez.properties`. On this host no file does, so the key kept
  its literal name `override.bluez5.roles`, PipeWire ignored it and **LE
  Audio stayed on**. `apply` replaces such a file (`plan` shows it as
  `update`, with the diff).

The price of `override.` on the section: any other
`monitor.bluez.properties` setting in an earlier WirePlumber file is dropped
while LE Audio is off. `plan` and `status` name such files with a warning,
and also files read *after* the CST file, which could bring the LE Audio
roles back. `status` then checks the result the way WirePlumber itself
sees it (the `WirePlumber bluez5.roles` line, see
[section 6](#6-reading-the-status)).

**This is global: it affects every Bluetooth device**, not only the
headset.

- The Beam 4 and other **dual-mode** headsets (classic and LE) play over
  A2DP, here with LDAC, and use HFP for calls. Nothing else changes for them.
- An **LE-only** device (one with no classic Bluetooth audio) gets no audio
  profile while LE Audio is off.
- `status` checks that it took effect: the role list WirePlumber really
  uses must have no LE Audio role, the PipeWire BAP endpoint must not be
  registered, and the headset must not be on a `bap-*` profile.

**Why.** On 2026-11-11 (Fedora 45, BlueZ 5.87, PipeWire 1.6.11, WirePlumber
0.5.19, kernel 7.2.15, Intel BE200) the Beam 4 connected over classic
Bluetooth and LE at the same time. PipeWire picked the LE profile
`bap-duplex`, because it ranks LE profiles higher (priority 513 against 134
for `a2dp-sink`). But:

- only the right earbud had an LE bond, and
- LE stereo to true-wireless earbuds does not work on this controller anyway:
  the BE200 refuses more than two isochronous streams (CIS) per group, and
  the two earbuds need more. Upstream BlueZ is working on this.

The result was `Start: Connection timed out` from AVDTP every 15 seconds or
so, and a disconnect loop. Forcing `a2dp-sink` brought LDAC back. With LE
Audio off, PipeWire cannot pick LE in the first place.

**Turn it on again** (for example once the BlueZ fix has landed):

```
cst bluetooth le-audio on
```

This writes `LE_AUDIO=on` into `~/.config/cst/bluetooth.conf` (it creates
the folder and the file if needed, keeps your other lines and replaces an
older `LE_AUDIO` line), then runs `cst bluetooth apply`. `apply` removes the
file and restarts WirePlumber (audio stops for a moment). `--yes` and
`--no-restart` are passed on to `apply`.

**Turn it off again:**

```
cst bluetooth le-audio off
```

`cst bluetooth le-audio` without `on` or `off` only shows the current
setting and where it comes from. You can also edit `LE_AUDIO` in
`~/.config/cst/bluetooth.conf` by hand and run `cst bluetooth apply`. To
try it once without changing the settings: `cst bluetooth apply --le-audio
on` (the next plain `apply` turns it off again). `cst bluetooth revert`
removes the file too.

**Preferred bearer.** With LE Audio off, a headset whose `PreferredBearer`
is `le` connects over LE when the PC starts the connection, and then has no
audio profile. `status` warns about that; `cst bluetooth apply --bearer
bredr` (or `BEARER=bredr` in the settings) fixes it. `apply` does not change
the bearer by itself: only when `BEARER` or `--bearer` is set.

## 1. Basic ideas you need

- **BlueZ**: Linux's Bluetooth service (`bluetoothd`, unit
  `bluetooth.service`). It reads `/etc/bluetooth/main.conf` when it starts.
- **PipeWire** and **WirePlumber**: the audio system of the desktop.
  PipeWire moves the sound; WirePlumber, its session manager, decides which
  device uses which profile and codec. Both run in your user session, not as
  root. WirePlumber reads extra rules from
  `~/.config/wireplumber/wireplumber.conf.d/*.conf` when it starts.
- **A2DP**: the classic ("BR/EDR") Bluetooth profile for music. Its best
  codec on this headset is **LDAC**, at 990, 660 or 330 kbps.
- **HFP**: the classic profile for calls (microphone, low quality, mono).
- **LE Audio**: the newer Bluetooth audio over Bluetooth Low Energy. Its
  profile for music and calls is **BAP**, its codec **LC3**. The sound
  travels in **isochronous** channels (CIS), which the kernel offers through
  an **ISO socket**.
- **Bearer**: which of the two transports (LE or BR/EDR) the PC uses when
  *it* starts the connection.
- The `bap-*` profiles (LE Audio) and `a2dp-sink` (LDAC) never show up
  together: you see the ones of the transport the headset is connected over
  right now. With LE Audio off (the default), there are no `bap-*`
  profiles at all.

## 2. Why it is set up like this

- **`Experimental = true`** switches on BlueZ's experimental plugins and
  D-Bus interfaces. LE Audio needs several of them (BAP, BASS, VCP, MICP,
  CSIP), and the per-device `PreferredBearer` setting is one of them too.
- **`KernelExperimental` = only the ISO socket UUID**, not `true`. `true`
  would also switch on every other experimental kernel feature, among them
  codec offload and the Bluetooth Quality Report, which are not needed. An
  old `KernelExperimental = true` is replaced by the UUID. A list of UUIDs
  that already contains the ISO one is left as it is.
- **LDAC `auto`**: the adaptive bitrate (ABR) drops from 990 to 660 or 330
  kbps when the radio loses packets. A fixed `hq` (990 kbps) made the
  Beam 4, which relays the sound between its two earbuds, stall and play the
  audio in bursts.
- **Only the lines that matter are edited.** `main.conf` keeps its
  comments and every other setting; the vendor's commented examples
  (`#Experimental = false`) are replaced in place, so the file still reads
  like the original. bluetoothd uses the last line of a key in a section,
  and a `[General]` header that shows up again continues the same section;
  the module reads the file the same way. Earlier active lines of the same
  key are commented out.
- **Root only where needed.** `apply` runs as your user. It calls `sudo` by
  itself for `main.conf` and for restarting `bluetooth.service`. The
  WirePlumber rule and the WirePlumber restart belong to your session, so
  they are never done as root (run through `sudo`, the script starts again
  as the user who called `sudo`). No file in your home becomes owned by root.

## 3. Quick start

```
cst bluetooth plan       # what would change; changes nothing
cst bluetooth apply      # asks before restarting Bluetooth, if that is needed
cst bluetooth status     # read-only checks
```

`plan` and `status` also work inside a toolbox (they read the host's
`/etc` at `/run/host/etc` and ask the host's tools through `flatpak-spawn`).
`apply` and `revert` run on the host only (`revert --check`, which only
reads, also works in a toolbox).

## 4. Commands, options and settings

| Command | What it does |
|---|---|
| `plan [OPTIONS]` | Dry run. Exit status 0 = nothing to do, 10 = `apply` would write or restart something. |
| `apply [OPTIONS]` | Writes what is missing, restarts only what changed, then runs the checks of `status`. |
| `status [--device MAC] [--ldac-quality Q] [--le-audio on\|off]` | Read-only report: the files, then the live checks of [section 6](#6-reading-the-status). Exit status 1 when a check failed. |
| `le-audio [on\|off] [--yes] [--no-restart]` | Sets `LE_AUDIO` in `~/.config/cst/bluetooth.conf`, then runs `apply` (see [LE Audio: off by default](#le-audio-off-by-default)). Without `on`/`off`: shows the setting, read-only. Runs on the host only, like `apply`. |
| `revert [--check] [--yes] [--no-restart] [--device MAC]` | Undo, see [section 8](#8-how-to-undo). |

| Option | For | Meaning |
|---|---|---|
| `--device MAC` | plan, apply, status, revert | The headset's address (`bluetoothctl devices` lists them), for this run only; without it, `DEVICE` in the settings. |
| `--ldac-quality Q` | plan, apply, status | `auto` (adaptive, the default), `hq` (990 kbps), `sq` (660), `mq` (330). |
| `--bearer B` | plan, apply | Preferred transport: `le`, `bredr` or `last-used`. Only for connections the PC starts (`bluetoothctl connect`, GNOME); a headset that reconnects by itself chooses on its own. |
| `--le-audio on\|off` | plan, apply, status | LE Audio in PipeWire for this run only (see [LE Audio: off by default](#le-audio-off-by-default)). To keep it, use `cst bluetooth le-audio on\|off`. |
| `--force` | apply | Restart bluetoothd and WirePlumber even when nothing changed. |
| `--yes` | apply, revert, le-audio | Do not ask before restarting Bluetooth (needed without a terminal). |
| `--no-restart` | apply, revert, le-audio | Write the files, restart nothing; prints the commands to run later. The restarts stay owed (see below). |
| `--check` | revert | Only show what `revert` would do. |

**Restarts still owed.** `apply`, `revert` and `le-audio` record the
restart a change needs before they make that change (`main.conf`:
bluetoothd and WirePlumber; the WirePlumber rule, its old name and the LE
Audio drop-in: WirePlumber; a renewal of comments: nothing), and again right
before their restarts. A restart that a run did not make (`--no-restart`,
the question answered no, a restart that failed, a run stopped after a
change) is so remembered in
`~/.local/state/cst/bluetooth-restart-pending.json`, with
the invocation ID the service had then (`systemctl show -p ActiveState -p
InvocationID`, in the C locale and given up after 20 s: one call per
service, read again only where it may have changed: before each time the
record is written and after CST restarts the service; the wait for the BAP endpoint
after a restart reads bluetoothd's at each look; the `services` rows of
the checks use the same reading).
While the service keeps running with that ID, `plan` lists the restart
("still owed", exit 10), `status` says so, `revert --check` lists it, and
the next `apply` or `revert` makes it (asking first for Bluetooth). Any restart of the service (by `apply`, by
hand with the printed commands, or a reboot) or a stop ends it. The file
goes with the next restart that `apply`, `revert` or `le-audio` makes;
until then a record whose restarts were made is only ignored. A file that
cannot be read counts as no record, and the next one written replaces it.
A service that does not run owes nothing: it reads the new files when it
starts (running is what `systemctl is-active` counts: active, reloading or
refreshing).

**The headset.** CST ships no headset address: `DEVICE` is empty in
[`bluetooth.conf`](bluetooth.conf). Put yours into
`~/.config/cst/bluetooth.conf` (`DEVICE=AA:BB:CC:DD:EE:FF`;
`bluetoothctl devices` lists the paired ones), or pass `--device`. Without
one, `plan`, `apply`, `status` and `revert` still handle what needs no
headset (`main.conf`, the LE Audio drop-in, the restarts) and skip the
headset's parts, each with a `skip` line that says how to set one: its
WirePlumber LDAC rule, the preferred bearer (`BEARER` is left alone), the
LDAC rate watcher (it needs a headset, so `LDAC_RATE=48000` alone keeps it
off) and the headset's live checks. A `DEVICE` that is not a MAC address
stops every command (exit 2).

An older CST set one headset by default in `bluetooth.conf`. A machine set up
with it has CST's LDAC rule for that headset
(`wireplumber.conf.d/50-bluez-<ID>.conf`) and no `DEVICE` line in its own
file. So when your file has no `DEVICE` line at all, the headset of CST's one
LDAC rule there is used (`plan` shows "from CST's LDAC rule" and a `set` line
for it, exit 10), and `apply` saves it as `DEVICE` in your file, the other
lines kept. A rule CST did not write (no CST marker), a `.bak` copy, a name
with the address in lower case (CST always writes capitals), and the case of
several rules (then none is taken; the `skip` line lists them) name no
headset. An empty `DEVICE=` in your file means "no headset" and is kept.

**Settings.** The defaults are in [`bluetooth.conf`](bluetooth.conf) in this
folder (`DEVICE`, `LDAC_QUALITY`, `BEARER`, `LE_AUDIO`, `LDAC_RATE`). To change them for your user,
write the lines you want to change into `~/.config/cst/bluetooth.conf`, in
the same `KEY=value` format (a line that is not one, or a value with `$`, a
backtick or a backslash outside single quotes, is ignored with a warning
naming the line); that file wins over the defaults, and the
command line options win over both. `BEARER` is empty by default: the
headset's own setting is left alone. `LE_AUDIO` is `off` by default (`on`
or `off`; see [LE Audio: off by default](#le-audio-off-by-default)).
`LDAC_RATE` is empty by default (the LDAC rate watcher is off); `48000`
turns it on (section 7). It has no command line option. `plan` and `status`
say when a setting comes from your file rather than the defaults.

**Files that are links.** The files `apply` writes in your home (the
WirePlumber rule, the LE Audio drop-in, the watcher's program and its unit)
may be symbolic links, for example from a dotfiles manager. A link is judged
by the file it points to, as the CST installer does: when that file already
holds what `apply` would write, the link stays as it is; when it holds an
older text of CST, `apply` replaces the link with a plain file and never
writes into the file it pointed to; when it holds something CST did not
write, or the link points to `/dev/null` or to a folder, it is a conflict, as
a plain file would be. `revert` removes a link to a CST file, never the file it points to.
`/etc/bluetooth/main.conf` is different: CST edits it in place, and refuses
to edit it when it is a link.

**What happens at a restart.** After WirePlumber restarts, the headset's A2DP
channel closes and often only HFP comes back by itself (the headset then sits
in `headset-head-unit`). `apply` reconnects only the A2DP Audio Sink (no
unpairing) and switches back to `a2dp-sink`, unless the headset's microphone
is in use (a call is going on): then it only warns.

If a restart itself fails (for example a wrong sudo password),
`apply` and `revert` stop with an error and the command to run by hand.
WirePlumber is not restarted after a failed Bluetooth restart, and `apply`
does not go on to the bearer and the checks. If you only answered "no" at
the question, they end normally and print the commands for later.

A command that stops before its end (Ctrl-C, `SIGTERM`, a closed terminal)
ends killed by that signal; its scratch copy of `main.conf` (in `$TMPDIR`) is
removed first. When the reader of its output goes away (`cst bluetooth plan |
head`), a command that only reads (`plan`, `status`, `revert --check`,
`le-audio` without on/off) ends quietly the same way (`help`: exit status 141). `apply`,
`revert` and `le-audio on|off` stop instead, with `stopped: its output was
closed` on stderr (for `apply` and `revert` also the backup of `main.conf`
and the undo command) and exit status 1. After any stop, a change already
made leaves its restart owed (above): `plan` shows it and the next `apply`
makes it. A stop between recording a restart and making its change leaves
that restart owed too, and the next run makes it once. Only when the record
cannot be written (the run then warns: `could not record the restarts still
owed`) can a stop after a change lose its restart: restart the service by
hand (`sudo systemctl restart bluetooth`, `systemctl --user restart
wireplumber`).

## 5. Coming from `apply-le-audio.sh`

- `main.conf`: the result of the old script is exactly what this module
  wants, so `plan` shows `ok` for both keys. No marker is written into
  `main.conf`: the two keys themselves are the state.
- The WirePlumber rule: the old script's file has the same settings with
  Portuguese comments. `plan` shows it as `adopt`; `apply` only rewrites the
  comments (with the marker `# managed-by: bluetooth-setup.sh (CST)`) and
  restarts nothing, because WirePlumber does not read comments.
- The old script's very first rule name, `50-jbl-live-beam-4.conf`, is
  removed when found and a headset is set, whose rule replaces it (that does
  restart WirePlumber, as the old script did). With no headset it is left
  alone: nothing would take its place.
- Backups: the old script's `main.conf.bak-YYYYmmdd-HHMMSS` files use the
  same names as this module's, and `revert` reads them (see below). When a
  backup of that second is there already (an `apply` and a `revert` in the
  same second), the new one gets `-1`, `-2`, ... after the name: a backup
  is never replaced. It is made with `cp --update=none-fail`, which creates
  only a name that is not there (GNU coreutils 9.5 or later; Fedora 45 has
  9.11), so a name another run takes at the same moment is skipped too.
- The old `--check` is now `plan`, `--revert` is `revert`; the other options
  kept their names.

On sophia, on 2026-10-20, the first `cst bluetooth plan` showed both
`main.conf` keys `ok`, the rule as `adopt`, and no restart (exit status 10
only because of the comment renewal).

## 6. Reading the status

`status` first shows the same file lines as `plan`, then:

| Check | Meaning when it fails |
|---|---|
| `bluetooth.service`, `wireplumber` | The service is not running. |
| `LE Audio` | Off (the default) or on, and where that comes from, with the command that switches it. A warning when it is off in the settings but `apply` has not written the file yet. |
| `WirePlumber bluez5.roles` | The role list WirePlumber will offer BlueZ, read the way WirePlumber reads all its files (through its own library, `libwireplumber-0.5`, read-only: nothing is started). With LE Audio off it must have no `bap_*` role. A failure means another WirePlumber file adds LE Audio roles back, or WirePlumber does not use the CST file ("not set"). With LE Audio on, a warning when the list has no LE Audio role. "could not be read" = the library was not found (a warning only); an answer of `bt-helper wp-roles` that names no roles state is a warning too, with the answer. |
| `adapter ISO feature` | bluetoothd did not switch the ISO socket on for the adapter: `KernelExperimental` not in effect (restart bluetooth). |
| `kernel ISO socket` | The kernel does not offer the ISO socket. The kernel flag lasts until reboot, which is why the adapter check above matters too. |
| `BAP in BlueZ` | BlueZ does not publish the LE Audio PACs (Sink 0x2bc9, Source 0x2bcb): `Experimental` not in effect. |
| `PipeWire BAP LC3 endpoint` (a warning only) | Your WirePlumber's registration of the LC3 sink with bluetoothd, as the journal shows it. Only the events of *your* WirePlumber count: test sessions with their own WirePlumber (for example GUI tests under `dbus-run-session`) register and drop the same endpoint every few seconds. "not confirmed: the journal no longer has ..." means the journal entry was rotated away, not that something is wrong. With LE Audio off it is the other way round: "not registered" is what we want, and "still registered" means WirePlumber has not been restarted since the file was written. |
| `preferred bearer` (a warning only) | With LE Audio off: the headset prefers LE, so a connection the PC starts would carry no audio. Fix: `cst bluetooth apply --bearer bredr`. |
| `headset` | A warning when it is on a `bap-*` profile although LE Audio is off (restart WirePlumber). Profile, the LDAC rule as WirePlumber loaded it, the codec, quality and sample rate of the output node. "not in PipeWire now" = the headset is not connected. A rule or quality that differs means: reconnect the headset. |
| `LDAC sample rate` (a warning only) | LDAC runs above 48 kHz (96000 Hz): the headset set the stream up by itself. See [section 7](#7-ldac-sample-rate-the-optional-watcher). With the watcher active, it should be gone within seconds; if it stays, the watcher gave up (its log says why). |
| `cst-bt-ldac48.service` | Only with `LDAC_RATE=48000`: the watcher should be active. A warning when it is not (run `apply`; its log: `journalctl --user -u cst-bt-ldac48.service`), or when it still runs although `LDAC_RATE` is empty or no headset is set. |
| `LDAC rate watcher` (a warning only) | Its last attempt, as its log shows it, was `failed: ...`: the time, the reason and the line before it, and how to turn the watcher off. `plan` shows the same line. |

## 7. LDAC sample rate: the optional watcher

**The problem.** An A2DP stream is set up ("configured", in AVDTP, the
Bluetooth protocol under A2DP) by whichever side starts it, and that side
picks the codec settings, the sample rate among them:

- When the **PC** sets it up (you connect from GNOME or `bluetoothctl`, or
  you switch the profile), PipeWire prefers **48000 Hz**, the rate the rest
  of the desktop's audio runs at.
- When the **headset** reconnects by itself (you take it out of the case,
  it comes back in range), the JBL Live Beam 4 often picks **96000 Hz**.

LDAC's bitrate does not grow with the sample rate: at 96 kHz the same 990,
660 or 330 kbps carry twice as many samples, so each sample gets about half
the bits. With the adaptive quality (`auto`), the lower steps (660 and
330 kbps) then sound clearly worse than at 48 kHz, and the desktop's
48 kHz audio is resampled to 96 kHz on the way. 48 kHz is the better choice
for this headset.

**The fix.** Switch the card to its SBC profile and straight back to LDAC.
The headset closes its stream and the PC sets it up again, at 48000 Hz. The
Bluetooth link stays up: this is a new stream on the same connection, not a
reconnection. You hear about one second of silence. Tested on sophia on
2026-10-20: `pactl set-card-profile bluez_card.<ID> a2dp-sink-sbc` (`<ID>`: the
headset's address with `_` for `:`),
about 3 seconds, then `... a2dp-sink`, turned "LDAC 96000 Hz" into "LDAC
48000 Hz".

**The watcher** does that for you, only when it is needed. It is a small
Python program, `bt-ldac-watch` in this folder, run by a user service:

- It listens to PipeWire's events (`pactl subscribe`), so it does not poll;
  it also takes one look every two minutes, should an event be missed.
  When PipeWire restarts, it connects again (after 1, 2, 4 ... 60 seconds).
- It runs `pactl` and `pw-dump` with `LC_ALL=C`, whatever language your
  session uses: it reads pactl's English event lines and profile names.
  Before, a session that set `LC_ALL` to another language kept it, and the
  watcher never saw an event.
- It acts only when the headset's output node uses LDAC at a rate other
  than 48000 Hz, and the card is on its LDAC profile (`a2dp-sink`). It first
  waits 5 seconds and looks again (just after a connection, the headset and
  WirePlumber are often still busy).
- It does nothing while the headset's **microphone is in use** (a call), and
  nothing when the card is on another profile (HFP, LE Audio).
- **One attempt per connection, two at most.** An output node that went
  away by itself (for example HFP for a call, then back) allows one more,
  but never more than two per connection; a new connection starts afresh.
  Between two attempts there are always at least 10 minutes. It gives up
  until the headset connects again, and logs why, after a failed attempt
  (the headset came back at 96 kHz at once, or a profile switch failed),
  and also when a fix did not hold: the headset went back to 96 kHz by
  itself within 5 minutes. So a headset that insists on 96 kHz costs at
  most two short dropouts per connection, never a loop.
- **Your choice wins during the switch.** Just before it switches back to
  LDAC it looks again: if the card went to another profile in the
  meantime (HFP, because an app opened the microphone; or you picked one),
  or the microphone is in use, it leaves the card as it is, logs that with
  the command to go back to LDAC by hand, and counts the attempt as failed.
- The profiles are found by their codec in `pactl list cards` (`codec LDAC`,
  `codec SBC`), with PipeWire's usual names (`a2dp-sink`, `a2dp-sink-sbc`)
  as a fallback. Without an SBC profile it switches through another A2DP
  codec (SBC-XQ, AAC); without any, it logs that and does nothing.
- A card in `pactl list cards` or an object in `pw-dump` of an unexpected
  shape (a list where an object belongs, a profile that is not an object) is
  skipped, as if it were not there: every switch needs a card and a node that
  match, so a skipped one never leads to a switch. Before, it stopped the
  watcher, and systemd restarted it on the same output.

**Known problem on PipeWire 1.6.11 (2026-11-11): keep it off for now.**
After the update to PipeWire 1.6.11 the watcher's profile switch failed:
`a2dp-sink-sbc did not become active within 6 s`, then
`failed: LDAC came back at 96000 Hz again`. It was stopped by hand
(`systemctl --user stop cst-bt-ldac48.service`). CST's default stays off
(`LDAC_RATE` empty) until the switch is checked again on this PipeWire.
On sophia, `~/.config/cst/bluetooth.conf` still sets `LDAC_RATE=48000`, so
the watcher is still on there: `plan` and `status` say so (`note` lines),
`status` shows the failure from the log, and `apply` would start the
service again. To turn it off: remove the `LDAC_RATE` line from that file,
then `cst bluetooth apply`.

**Turn it on.** Put this line into `~/.config/cst/bluetooth.conf`, then:

```
LDAC_RATE=48000
```

```
cst bluetooth plan      # shows the unit, the program and "enable"
cst bluetooth apply     # writes them, enables and starts the service
cst bluetooth status    # cst-bt-ldac48.service active; the headset's rate
```

`apply` installs, as your user (no root):

| Path | What |
|---|---|
| `~/.local/lib/cst/bt-ldac-watch` | A **copy** of `bluetooth/bt-ldac-watch`, built as a zipapp with the packages it imports. |
| `~/.config/systemd/user/cst-bt-ldac48.service` | The user unit, enabled for `default.target` (it starts with your session). |

Both go through CST's installer (atomic writes, as your user) and are listed
in your install record `~/.local/state/cst/installed.json` (component
`bluetooth:ldac`), which `cst doctor` checks; removing the watcher takes them
out of it. A copy already current but missing from the record (installed
before the record existed, or listed there otherwise) is added to it by the
next `apply`, which prints `unchanged:` for it and neither rewrites nor
restarts it; `plan` and `status` show it as `adopt` ("apply adds it to the
install record"), from the installer's own `plan` (its `would record:` line). What the installer
writes (`written:`) decides the restart, also for a file that changed after
`plan` looked at it.

Why a copy, and not the file in this folder: the service then does not
depend on this working copy. Moving or renaming the folder, editing the
script, or restoring a snapshot of `~/Workspace` cannot break or silently
change a running service, and it does not matter whether you call CST as
`./cst` from the CST folder or through the `~/.local/bin/cst` link. The other
modules do the same (their programs are copied to `/usr/local`). `plan`
compares the copy with the one in CST; after a change here, `apply` copies
it again and restarts the service.

**See what it does.**

```
journalctl --user -u cst-bt-ldac48.service        # its log
journalctl --user -u cst-bt-ldac48.service -f     # live
systemctl --user status cst-bt-ldac48.service
```

It writes one line when something changes, for example
`act: LDAC at 96000 Hz (want 48000): switching a2dp-sink -> a2dp-sink-sbc -> a2dp-sink (about a second of silence)`,
then `fixed: LDAC now at 48000 Hz`, or `failed: ...` with the reason
(`journalctl --user -u cst-bt-ldac48.service | grep -E 'act:|fixed:|failed:|done:'`
shows only those).
To see what it would do without switching anything, run it by hand with
`--dry-run`:

```
python3 -I ~/.local/lib/cst/bt-ldac-watch --device AA:BB:CC:DD:EE:FF --dry-run   # your headset's address
```

**Turn it off.** Make `LDAC_RATE` empty again (or remove the line from
`~/.config/cst/bluetooth.conf`), then `cst bluetooth apply`: it stops and
disables the service and removes both files. `cst bluetooth revert` removes
them too. For a moment only: `systemctl --user stop cst-bt-ldac48.service`
(it starts again at the next login). A stop while it switches profiles
(also the restart by `apply` after a change, or the end of the session)
first finishes the switch back to LDAC, up to about 20 seconds, so the
headset is never left on SBC.

If you answer **no** to the Bluetooth restart (or `apply` runs without a
terminal and without `--yes`), `apply` still installs or removes the
watcher, then says what is left to do: the restarts, the preferred bearer
(if one is set) and the checks. Run `cst bluetooth apply` again after
restarting.

## 8. How to undo

```
cst bluetooth revert --check   # what it would do
cst bluetooth revert
```

- `main.conf`: the two keys go back to how the **oldest** backup
  (`main.conf.bak-*`, by time, then by name and `-N` number; the old
  script's count) has them. Only those
  two lines change: anything else you changed since stays. Without a backup,
  `main.conf` is not touched. (The old script restored its newest backup as
  a whole file instead.) A new backup is taken first.
- The WirePlumber rule is removed, if CST or the old script wrote it (with
  no headset set, there is none to remove: `revert --device MAC` removes
  that headset's rule). When that rule is the only place that names the
  headset (an older CST's default, before any `apply` saved it), `revert`
  first saves it as `DEVICE` in your file, as `apply` would.
- The LE Audio file `90-cst-le-audio-off.conf` is removed, if CST wrote it:
  after `revert`, PipeWire offers LE Audio again (a later `apply` with
  `LE_AUDIO=off` writes it again).
- The LDAC rate watcher is stopped, disabled and removed, if it is installed
  (a later `apply` with `LDAC_RATE=48000` installs it again).
- Restarts as for `apply`: Bluetooth only after you say yes. `revert` also
  makes the restarts still owed (section 4), from an earlier `apply` or
  `revert` that did not make them, also when nothing is left to revert; each
  shows as an `owed:` line before them (`revert --check`: exit 10).
- Kept: the backups (also the rule's `.bak`), `~/.config/cst/bluetooth.conf`
  (also a `DEVICE` line `apply` saved there from an older CST's rule),
  and the headset's preferred bearer. A restart `revert` did not make
  (`--no-restart`, the question answered no) stays owed (section 4), until
  the service restarts or the next `apply` or `revert` makes it; delete
  `~/.local/state/cst/bluetooth-restart-pending.json` to forget it.

## 9. Not managed here

`~/Documents/Agentic/dota2-bluetooth-audio-fix.md` describes four more
"layers" against Bluetooth audio drops in Dota 2 (written for Fedora 43 and
the JBL Live Beam 3). This module does **not** set them. Their state, as
found read-only on sophia on 2026-10-20:

| Layer | What | State found |
|---|---|---|
| 1 | WirePlumber `bluetooth.autoswitch-to-headset-profile = false` (`70-disable-bt-autoswitch.conf`) | **Absent**: no such file; `wpctl settings` shows `true`. |
| 2 | PipeWire rate locked to 48 kHz (`~/.config/pipewire/pipewire.conf.d/10-gaming.conf`) | **Absent as a file** (there is no `~/.config/pipewire`). The engine runs at 48000 Hz with allowed rates `[ 48000 ]` anyway (PipeWire's default); quantum 1024 (min 32, max 2048), not the 256 of the note. |
| 3 | No suspend for Bluetooth nodes (`52-bluez-no-suspend.conf`) | **Absent**: no such file; the headset's output node has no `session.suspend-timeout-seconds` (its `node.pause-on-idle` is false by default). |
| 4 | Dota 2 launch option `PULSE_LATENCY_MSEC=60 %command%` | **Absent**: the launch option is `gamemoderun %command%` (from the [cpu module](../cpu/README.md#4-quick-start)). |

The note's "pre-existing" files (`10-bluez-lc3.conf`, `50-bluetooth.conf`,
`51-usb-audio-no-suspend.conf`) are not on this host either. The folder
`~/.config/wireplumber/wireplumber.conf.d/` holds this module's rule and
`50-bluez-<ID>.conf.bak`, the old script's backup of its earlier
rule with `ldac.quality = "hq"`. WirePlumber reads only `*.conf` files, so
the `.bak` has no effect; a later `apply` that changes the rule's settings
replaces it with a backup of the current rule.

## 10. Files

| File | What it is |
|---|---|
| `../bluetooth-setup.sh` | The module's entry: a shim that runs `bt-setup` on the system Python (`/usr/bin/python3 -I -B`). Its header comment is the help (`cst bluetooth help`). |
| `bt-setup` | The Python entry point of `cst bluetooth` (run from the checkout only, through `bluetooth-setup.sh`): the bootstrap and a `main()` that runs `setup/cli.py`. |
| `setup/` | The program of `cst bluetooth`, in the full profile ([docs/architecture.md](../docs/architecture.md#package-profile)): `domain.py` (the command line and its errors, the settings, the paths, the words of the tables), `application/` (`survey.py`: what plan and status read, the plan; `checks.py`: the live checks; `change.py`: apply, revert, le-audio and the restarts, whose skipped ones stay owed for the next run), `ports.py` (the terminal and the user's files; commands go through cst_core's `CommandRunnerPort`, waits through `ClockPort`), `adapters.py`, `wiring.py` (the environment, the step down from root to `SUDO_USER` through `runuser`, the run again for `le-audio`'s apply) and `cli.py`. It runs the same commands as the shell script did, with the same arguments, and prints the same lines, except for the headset: with no `DEVICE` it skips the headset's parts, and it saves a headset it took from an older CST's rule ([section 4](#4-commands-options-and-settings)). |
| `domain/__init__.py` | Pure domain model: `main.conf` [General] rules, WirePlumber/systemd rendering and state classification, settings parsing/merging, journal failure parsing, and live state JSON parsing, with that JSON as `TypedDict`s (`PwObject`, `PactlCard`, ...), which `domain/watch.py` reads too. No I/O or subprocesses. |
| `domain/watch.py` | The watcher's rules, pure as well: what it sees in `pw-dump` (`snapshot`, `decide`), the `pactl` events and profiles that matter, and the one-attempt-per-connection rule (`Tracker`). |
| `ports.py` | Ports (`Protocol` interfaces): `BluetoothHostPort` for files, WirePlumber resolution, stdin, and the record of restarts still owed; `LiveAudioPort` for live PipeWire/PulseAudio state and profile switching. |
| `adapters.py` | Local host adapter reading config files, writing the record of restarts still owed (atomically, `write_atomic`), scanning WirePlumber `.conf` directories, and resolving `bluez5.roles` through `libwireplumber`; `Live`, the watcher's `LiveAudioPort` over `pw-dump` and `pactl`; and the watcher's loop (`Watcher`, `run_watcher`): it listens to `pactl subscribe`, looks at the headset through `LiveAudioPort`, bounces the profile (LDAC, SBC, LDAC) when `Tracker` says so, and defers a stop signal until a bounce is done. |
| `cli.py` | The two command lines: `main` for `bt-helper` (argv to the `domain` rules through `BluetoothHostPort`, answers printed in the formats `bluetooth-setup.sh` parsed; `setup/` calls the rules directly, and runs `bt-helper wp-roles` on the host, where `libwireplumber` is) and `ldac_watch` for `bt-ldac-watch` (its options; `--snapshot` prints what the watcher sees, otherwise it runs the loop in `adapters.py`). |
| `bt-helper` | The Python entry point: only the bootstrap and a `main()` that runs `cli.py` with the local adapter. The one file it writes is the record of restarts still owed (`restart-pending save`). |
| `bt-ldac-watch` | Python, no root: the LDAC rate watcher (section 7), only the bootstrap and a `main()` that runs `cli.ldac_watch`; its docstring is the help. `apply` installs it as a zipapp (the program plus every CST package it imports, see `cst_core/installer.py`) at `~/.local/lib/cst/bt-ldac-watch` for the user service `cst-bt-ldac48.service`. |
| `bluetooth.conf` | The default settings. |
| `tests/test_bluetooth.py` | Unit tests of `bt-helper`'s rules (`python3 -I -B tests/run_unit.py`, from the repository root, runs them with all others; see [AGENTS.md, "Commands"](../AGENTS.md#commands)): `main.conf`, the WirePlumber rule, the LE Audio file's text, the settings, the parsing of `busctl`, `pactl` and `pw-dump`, the record of restarts still owed, and reading the watcher's last failure from its log, and `bt-helper` on a file it cannot read or decode (one line on stderr, exit 1, no traceback). Three tests ask `libwireplumber` how it merges the CST file; inside a toolbox they are skipped (the library is on the host). |
| `tests/e2e` (tier 1) | `test_05`: `plan`, `apply --no-restart`, the WirePlumber roles read by the real `libwireplumber`, the LDAC unit and `revert`. `test_80`: after `apply --no-restart`, `plan` still owes WirePlumber's restart and the next `apply` makes it; a second `apply` changes nothing (files, backups, no restart), with the watcher missing from the install record `installer.py check` exits 1, `plan` shows it as `adopt`, and an `apply` adds it back (no write, no restart), after which `check` exits 0, and `le-audio on`, `on` again and `off` with real WirePlumber restarts; the watcher's `pactl subscribe` runs with `LC_ALL=C` whatever the user manager's `LC_ALL`. `test_95`: this program and the bash one it replaced (`tests/fixtures/legacy/bluetooth-setup.sh@d4d8c91`) share the files and the restarts still owed, both ways; as root with `SUDO_USER` it runs again as that user through `runuser`. `test_96`: run as root with `SUDO_USER`, the test overrides `CST_BT_ROOT`, `CST_BT_CONTAINER` and `CST_BT_LIBWP` do not reach the run as that user. `test_101`: the headset of an older CST's LDAC rule is used and saved as `DEVICE`, and with no headset at all `plan`, `apply` and `revert` skip the headset's parts. `test_178`: an `apply` stopped by `SIGTERM` after its writes and before its restarts leaves WirePlumber's restart owed (the record, under systemd's InvocationID), `plan` shows it, the next `apply` makes it once. `test_179`: a `revert` stopped the same way leaves the restart owed, `revert --check` lists it and the next `revert` makes it once. `test_180`: with a backup of each second of the next two minutes already there, `apply` and `revert` each back `main.conf` up under a name of their own (`-1`, `-2`) with the real `cp --update=none-fail`, and replace none. See [tests/e2e/README.md](../tests/e2e/README.md). |
| `tests/test_setup.py`, `tests/setup_fakes.py` | Behaviour tests of `bluetooth-setup.sh` as a whole: it runs as a user runs it, on a fake machine (stub commands on `PATH` that keep their state in files and log every call, a scratch home and a scratch host root). Two settings exist for these tests only: `CST_BT_CONTAINER` (1: behave as inside a toolbox, 0: as on the host) and `CST_BT_ROOT` (where the host's `/` is, for `/etc` and `/usr/share`), as `CST_DOCTOR_CONTAINER` and `CST_DOCTOR_ROOT` are for `cst doctor`. Never set them on a real machine: `CST_BT_CONTAINER=0` inside a toolbox lifts the refusal of `apply`, `revert` and `le-audio on\|off` there, and `CST_BT_ROOT` names the `main.conf` that `apply` and `revert` write through `sudo`. A third one, `CST_BT_LIBWP`, names the `libwireplumber` that `bt-helper wp-roles` loads (`tests/test_bluetooth.py` points it at a missing library). Run as root (through `sudo`, which by default does not pass them on either), the program drops all three before it runs again as `SUDO_USER` (`StepDownTest`, under `unshare -r`, and tier 1 `test_96`). They cover the options and their errors, the help, `plan`, `status`, `apply` (the order of its steps, `main.conf` through `sudo` with and without SELinux, the restarts and what each failure stops, the question, the restarts still owed (recorded before each write that needs them: a run stopped after `main.conf` or WirePlumber's files leaves them owed), the A2DP restore, the bearer under `timeout` with `/dev/null` as stdin), `revert` (the oldest backup, `--check`, the restarts still owed that it makes, also with nothing left to revert), the backups' names (`-1`, `-2` in one second, a name another run takes meanwhile, no free name left, the oldest by time, name and number), `le-audio on\|off` (a fresh home, a settings file without a final newline, a symlinked one), the LE Audio drop-in (off/on/off, adopted, changed, not ours), the rule, the drop-in, the watcher's program and unit as links (same text, older text, not ours, `/dev/null`, a folder), the role check, other WirePlumber files, the watcher's install and removal and its last failure, a file it cannot read (the settings, the rule: the run stops), the scratch file (every command needs it; removed on a closed stdout and on `SIGTERM`), a closed stdout (`apply`, `revert` and `le-audio on\|off` stop with a message), the headset (`HeadsetTest`: none set, the one of an older CST's rule saved as `DEVICE`, an empty `DEVICE=`, `--device` over the file), and that the commands get the caller's environment (no added variable, `LC_CTYPE` as it was). |
| `tests/test_setup_units.py` | Unit tests of the program in `setup/` for what the whole-program tests cannot reach on a host: `apply`'s last lines when every check passes (`ApplyDoneTest`), the waits of the restarts with a fake clock (`WaitTest`), and the step down from root to `SUDO_USER` through `runuser` (`StepDownTest`). |
| `tests/test_ldac_watch.py` | Unit tests of the watcher: parsing, the act/skip decision, one attempt per connection (two at most, none after a fix that did not hold), the cooldown, a stop signal during a profile switch, a SIGTERM to the real program outside one (exit status 0, `stopped` last), the C locale of the real program's `pactl subscribe` and `pw-dump` whatever the session's locale, `--snapshot` on input that is not JSON (exit 3), a profile change by WirePlumber during it, the profile switch against a fake system, and its user unit. |

## 11. Glossary

| Term | Meaning |
|---|---|
| A2DP | Classic Bluetooth profile for music (stereo, good codecs such as LDAC). |
| AVDTP | The protocol under A2DP that sets up ("configures") the audio stream, codec settings and sample rate included. |
| BAP | Basic Audio Profile: the LE Audio profile for music and calls. |
| BR/EDR | Classic Bluetooth, as opposed to Bluetooth Low Energy (LE). |
| CIS | Connected Isochronous Stream: one LE Audio stream. True-wireless earbuds need one or more per earbud, grouped in a CIG. |
| Dual-mode | A device that speaks both classic Bluetooth and LE, like the Beam 4. |
| True-wireless (TWS) | Two separate earbuds with no cable between them. |
| HFP | Hands-Free Profile: classic Bluetooth for calls, with the microphone. |
| ISO socket | The kernel interface for LE Audio's isochronous channels. |
| LC3 | The codec of LE Audio. |
| LDAC | Sony's high-bitrate codec for A2DP. Its bitrate (990/660/330 kbps) is the same at 48 and 96 kHz. |
| Sample rate | Samples per second of the audio (48000 Hz = 48 kHz). |
| PAC / PACS | Published Audio Capabilities: what LE Audio formats a device offers. |
| WirePlumber | PipeWire's session manager; it applies the rule file of this module. |
