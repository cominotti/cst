default_shell: "/bin/bash"
workdir: ".."

depends_on: {
	tools: [
		{alternatives: ["python3"]},
		{alternatives: ["bash"]},
	]
}

cmds: [
	{
		name:        "test unit"
		category:    "test"
		description: "Run all unit tests cleanly without generating __pycache__"
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "python3 -I -B -m unittest discover -s . -v"}
		}]
	},
	{
		name:        "test lint"
		category:    "test"
		description: "Run bash -n and shellcheck across cst shell entrypoints"
		depends_on: {
			tools: [{alternatives: ["shellcheck", "toolbox"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				shopt -s nullglob
				scripts=(
					cst
					cst-link.bash
					btrfs-setup.sh
					cpu-setup.sh
					swap-setup.sh
					bluetooth-setup.sh
					toolchains-setup.sh
					fix-anaconda-webui-btrfs-compress.sh
					cpu/game-profile
					tests/e2e/scripts/*.sh
				)
				bash -n "${scripts[@]}"
				if command -v shellcheck >/dev/null 2>&1; then
					shellcheck -S warning -x "${scripts[@]}"
				else
					toolbox run -c devbox shellcheck -S warning -x "${scripts[@]}"
				fi
				"""}
		}]
	},
	{
		name:        "test e2e tier1"
		category:    "test"
		description: "Run Docker Layer 1 E2E suite (systemd PID 1, toolchains, bluetooth/wp, restic+SFTP, alerts, doctor)"
		depends_on: {
			tools: [{alternatives: ["docker"]}]
			capabilities: [{alternatives: ["containers"]}]
		}
		flags: [
			{name: "keep_logs", type: "bool", default_value: "false", description: "Preserve collected journald/systemd artifacts even on success"},
			{name: "rebuild", type: "bool", default_value: "false", description: "Force rebuild of E2E Docker images"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			timeout: "20m"
			script: {content: """
				set -euo pipefail
				args=(--tier 1)
				[[ "${INVOWK_FLAG_KEEP_LOGS:-false}" == "true" ]] && args+=(--keep-logs)
				[[ "${INVOWK_FLAG_REBUILD:-false}" == "true" ]] && args+=(--rebuild)
				python3 -I -B tests/e2e/run.py "${args[@]}"
				"""}
		}]
	},
	{
		name:        "test e2e tier2"
		category:    "test"
		description: "Run Docker Layer 2 E2E suite (--privileged loopback btrfs RAID1, DinD, cgroups v2, Polkit; needs --allow_privileged or CST_E2E_ALLOW_PRIVILEGED=1)"
		depends_on: {
			tools: [{alternatives: ["docker"]}]
			capabilities: [{alternatives: ["containers"]}]
			filepaths: [{alternatives: ["/dev/loop-control"]}]
		}
		flags: [
			{name: "keep_logs", type: "bool", default_value: "false", description: "Preserve collected journald/systemd artifacts even on success"},
			{name: "rebuild", type: "bool", default_value: "false", description: "Force rebuild of E2E Docker images"},
			{name: "allow_privileged", type: "bool", default_value: "false", description: "Opt in to tier 2's --privileged container, which sees this machine's block devices (same as CST_E2E_ALLOW_PRIVILEGED=1; meant for disposable CI runners and VMs)"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			timeout: "30m"
			script: {content: """
				set -euo pipefail
				args=(--tier 2)
				[[ "${INVOWK_FLAG_KEEP_LOGS:-false}" == "true" ]] && args+=(--keep-logs)
				[[ "${INVOWK_FLAG_REBUILD:-false}" == "true" ]] && args+=(--rebuild)
				[[ "${INVOWK_FLAG_ALLOW_PRIVILEGED:-false}" == "true" ]] && args+=(--allow-privileged)
				python3 -I -B tests/e2e/run.py "${args[@]}"
				"""}
		}]
	},
	{
		name:        "test all"
		category:    "test"
		description: "Run unit tests, shell lint, Docker Layer 1 and Docker Layer 2 E2E suites (Layer 2 needs --allow_privileged or CST_E2E_ALLOW_PRIVILEGED=1)"
		depends_on: {
			tools: [
				{alternatives: ["docker"]},
				{alternatives: ["shellcheck", "toolbox"]},
			]
			capabilities: [{alternatives: ["containers"]}]
			filepaths: [{alternatives: ["/dev/loop-control"]}]
		}
		flags: [
			{name: "keep_logs", type: "bool", default_value: "false", description: "Preserve collected journald/systemd artifacts even on success"},
			{name: "rebuild", type: "bool", default_value: "false", description: "Force rebuild of E2E Docker images"},
			{name: "allow_privileged", type: "bool", default_value: "false", description: "Opt in to tier 2's --privileged container, which sees this machine's block devices (same as CST_E2E_ALLOW_PRIVILEGED=1; meant for disposable CI runners and VMs)"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			timeout: "50m"
			script: {content: """
				set -euo pipefail
				shopt -s nullglob
				e2e_args=(--tier 1 --tier 2)
				[[ "${INVOWK_FLAG_ALLOW_PRIVILEGED:-false}" == "true" ]] && e2e_args+=(--allow-privileged)
				# Stop before the long unit and lint runs if tier 2 is not allowed here.
				python3 -I -B tests/e2e/run.py "${e2e_args[@]}" --check-only
				python3 -I -B -m unittest discover -s . -v
				scripts=(
					cst
					cst-link.bash
					btrfs-setup.sh
					cpu-setup.sh
					swap-setup.sh
					bluetooth-setup.sh
					toolchains-setup.sh
					fix-anaconda-webui-btrfs-compress.sh
					cpu/game-profile
					tests/e2e/scripts/*.sh
				)
				bash -n "${scripts[@]}"
				if command -v shellcheck >/dev/null 2>&1; then
					shellcheck -S warning -x "${scripts[@]}"
				else
					toolbox run -c devbox shellcheck -S warning -x "${scripts[@]}"
				fi
				[[ "${INVOWK_FLAG_KEEP_LOGS:-false}" == "true" ]] && e2e_args+=(--keep-logs)
				[[ "${INVOWK_FLAG_REBUILD:-false}" == "true" ]] && e2e_args+=(--rebuild)
				python3 -I -B tests/e2e/run.py "${e2e_args[@]}"
				"""}
		}]
	},
	{
		name:        "cst doctor"
		category:    "cst"
		description: "Run cst read-only workstation doctor"
		flags: [
			{name: "brief", type: "bool", default_value: "false", description: "Compact summary output"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				if [[ "${INVOWK_FLAG_BRIEF:-false}" == "true" ]]; then
					exec ./cst doctor --brief
				fi
				exec ./cst doctor
				"""}
		}]
	},
	{
		name:        "cst status"
		category:    "cst"
		description: "Show consolidated read-only status across cst modules"
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "exec ./cst status"}
		}]
	},
]
