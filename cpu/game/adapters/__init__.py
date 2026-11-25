# SPDX-License-Identifier: MPL-2.0
"""The game profile's adapters: the front GameProfile (profile), the real adapters of its ports, one module per
boundary (systemd, files, kernel, power, output), and the reads it shares with cst cpu: settings, accounts, files
(host)."""
