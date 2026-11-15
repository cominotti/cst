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
		description: "Run all unit tests (with a socket as stdin, see tests/run_unit.py) without generating __pycache__"
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "python3 -I -B tests/run_unit.py -v"}
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
					vm-setup.sh
					toolchains/rustc-wrapper
					fix-anaconda-webui-btrfs-compress.sh
					cpu/game-profile
					tests/uv-dev
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
		name:        "test pylint"
		category:    "test"
		description: "Run ruff's linter (the rules in pyproject.toml), then its format check, on every Python file, through uv (tests/uv-dev)"
		depends_on: {
			tools: [{alternatives: ["uv"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				tests/uv-dev ruff check
				tests/uv-dev ruff format --check
				"""}
		}]
	},
	{
		name:        "test types"
		category:    "test"
		description: "Run ty on the ty gate list and ruff's annotation rules (ANN) on the typed list, through uv (tests/uv-dev)"
		depends_on: {
			tools: [{alternatives: ["uv"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				tests/uv-dev ty check
				tests/uv-dev ruff check --select ANN
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
		description: "Run the fast gates (test unit, lint, pylint, types), then the Docker Layer 1 and Docker Layer 2 E2E suites (Layer 2 needs --allow_privileged or CST_E2E_ALLOW_PRIVILEGED=1)"
		depends_on: {
			tools: [
				{alternatives: ["invowk"]},
				{alternatives: ["docker"]},
				{alternatives: ["shellcheck", "toolbox"]},
				{alternatives: ["uv"]},
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
				e2e_args=(--tier 1 --tier 2)
				[[ "${INVOWK_FLAG_ALLOW_PRIVILEGED:-false}" == "true" ]] && e2e_args+=(--allow-privileged)
				# Stop before the long unit and lint runs if tier 2 is not allowed here.
				python3 -I -B tests/e2e/run.py "${e2e_args[@]}" --check-only
				# The fast gates, by running their own tasks (one copy of each
				# script list and gate step, the one CI runs).
				invowk cmd test unit
				invowk cmd test lint
				invowk cmd test pylint
				invowk cmd test types
				[[ "${INVOWK_FLAG_KEEP_LOGS:-false}" == "true" ]] && e2e_args+=(--keep-logs)
				[[ "${INVOWK_FLAG_REBUILD:-false}" == "true" ]] && e2e_args+=(--rebuild)
				python3 -I -B tests/e2e/run.py "${e2e_args[@]}"
				"""}
		}]
	},
	{
		name:        "ci pin-invowk"
		category:    "ci"
		description: "Print the archive and binary SHA256 pins for .github/actions/setup-invowk (downloads to a temp dir; verifies cosign)"
		args: [
			{name: "version", description: "Exact invowk release tag, e.g. v0.16.0", required: true},
		]
		flags: [
			{name: "skip_cosign", type: "bool", default_value: "false", description: "Skip the cosign check of checksums.txt when cosign is not installed (prints a warning; do not commit pins made this way unchecked)"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				args=("${INVOWK_ARG_VERSION}")
				[[ "${INVOWK_FLAG_SKIP_COSIGN:-false}" == "true" ]] && args+=(--skip-cosign)
				exec python3 -I -B .github/scripts/pin_invowk.py "${args[@]}"
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
