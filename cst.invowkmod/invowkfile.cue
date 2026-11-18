// SPDX-License-Identifier: MPL-2.0
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
		// The one entry point for people, agents and CI (fast-gates and nightly run it).
		name:        "check"
		category:    "check"
		description: "Run every fast gate: invowk validate and audit, test lint, test pylint, test types, test workflows, test unit (the docs gate is in the unit suite)"
		depends_on: {
			tools: [
				{alternatives: ["invowk"]},
				{alternatives: ["shellcheck", "toolbox"]},
				{alternatives: ["uv"]},
			]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			timeout: "20m"
			script: {content: """
				set -euo pipefail
				# Each gate by its own task (one copy of each script list and step).
				invowk validate cst.invowkmod
				invowk audit --severity low cst.invowkmod
				invowk cmd test lint
				invowk cmd test pylint
				invowk cmd test types
				invowk cmd test workflows
				invowk cmd test unit
				"""}
		}]
	},
	{
		name:        "fmt"
		category:    "check"
		description: "Apply ruff's safe fixes (ruff check --fix), then format every Python file (ruff format), through uv (tests/uv-dev)"
		depends_on: {
			tools: [{alternatives: ["uv"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				# Fixes first: a fix can change a line the formatter would then rewrite.
				tests/uv-dev ruff check --fix
				tests/uv-dev ruff format
				"""}
		}]
	},
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
		name:        "test arch"
		category:    "test"
		description: "Run the static gate modules only (tests/run_unit.py --gates: architecture, budgets, entry points, manifest, Python and shell lists, docs)"
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "python3 -I -B tests/run_unit.py -v --gates"}
		}]
	},
	{
		name:        "test module"
		category:    "test"
		description: "Run one unit test module, class or method through tests/run_unit.py (so the socket-stdin gate still applies)"
		args: [
			{name: "name", description: "A unittest name, e.g. vm.tests.test_vm or vm.tests.test_vm.PartsTest.test_disks_nvram_tpm", required: true},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				exec python3 -I -B tests/run_unit.py -v "${INVOWK_ARG_NAME}"
				"""}
		}]
	},
	{
		name:        "test docs"
		category:    "test"
		description: "Run the docs gate alone (tests/test_docs.py: links and anchors, the AGENTS.md index, skills and rules)"
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "python3 -I -B tests/run_unit.py -v tests.test_docs"}
		}]
	},
	{
		name:        "test license"
		category:    "test"
		description: "Run the license header gate alone (tests/test_license_headers.py: the SPDX line in every file, the exemptions, LICENSE's notice)"
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "python3 -I -B tests/run_unit.py -v tests.test_license_headers"}
		}]
	},
	{
		name:        "fix license"
		category:    "check"
		description: "List the files that lack the SPDX license header (dry run); with --apply, insert it (tests/license_headers.py; a second run changes nothing)"
		flags: [
			{name: "apply", type: "bool", default_value: "false", description: "Insert the header instead of only listing the files"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				if [[ "${INVOWK_FLAG_APPLY:-false}" == "true" ]]; then
					exec python3 -I -B tests/license_headers.py --apply
				fi
				exec python3 -I -B tests/license_headers.py
				"""}
		}]
	},
	{
		name:        "fix budgets"
		category:    "check"
		description: "List the budget, ceiling and allow-list entries the code is below (dry run); with --apply, lower them to the measured values (tests/fix_budgets.py; never raises or adds an entry; RUFF_BUDGETS is measured through uv)"
		depends_on: {
			tools: [{alternatives: ["uv"]}]
		}
		flags: [
			{name: "apply", type: "bool", default_value: "false", description: "Rewrite the tables instead of only listing the entries"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				if [[ "${INVOWK_FLAG_APPLY:-false}" == "true" ]]; then
					exec python3 -I -B tests/fix_budgets.py --apply
				fi
				exec python3 -I -B tests/fix_budgets.py
				"""}
		}]
	},
	{
		name:        "test lint"
		category:    "test"
		description: "Run bash -n and shellcheck on every shell file of the checkout (the list tests/shell_files.py prints)"
		depends_on: {
			tools: [{alternatives: ["shellcheck", "toolbox"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				# Derived from the repository (tests/test_distribution.py checks each
				# one is a known kind), so a new script is linted without a list edit.
				mapfile -t scripts < <(python3 -I -B tests/shell_files.py)
				if [[ "${#scripts[@]}" -eq 0 ]]; then
					echo "test lint: tests/shell_files.py found no shell file" >&2
					exit 1
				fi
				# One file per bash -n: with several, bash parses the first and takes the rest as its arguments.
				for script in "${scripts[@]}"; do
					bash -n "${script}"
				done
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
		description: "Run ruff's linter (the rules in pyproject.toml), its format check and the ruff budgets (tests/ruff_budget.py) on every Python file, through uv (tests/uv-dev)"
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
				python3 -I -B tests/ruff_budget.py
				"""}
		}]
	},
	{
		name:        "test types"
		category:    "test"
		description: "Run ty on the ty gate list and ruff's annotation rules (ANN) on every Python file, through uv (tests/uv-dev)"
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
		name:        "test workflows"
		category:    "test"
		description: "Run zizmor offline on the workflows, the actions and dependabot.yml of .github, with the exceptions in .github/zizmor.yml (a file it cannot read fails), through uv (tests/uv-dev)"
		depends_on: {
			tools: [{alternatives: ["uv"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "tests/uv-dev zizmor --offline --strict-collection --config .github/zizmor.yml .github"}
		}]
	},
	{
		// Slow (a second unit run): the nightly runs it, `check` does not. See docs/dev-tools.md, "Coverage".
		name:        "test coverage"
		category:    "test"
		description: "Run the unit suite under coverage.py (statements and branches), write the reports and check COVERAGE_FLOORS of tests/coverage_floors.py (domain and application layers), through uv (tests/uv-dev)"
		flags: [
			{name: "out", description: "A folder outside the checkout for the data and the reports (default: a new folder under ~/.cache/cst-coverage/)"},
		]
		depends_on: {
			tools: [{alternatives: ["uv"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			timeout: "20m"
			script: {content: """
				set -euo pipefail
				out="${INVOWK_FLAG_OUT:-${XDG_CACHE_HOME:-${HOME}/.cache}/cst-coverage/$(date +%Y%m%d-%H%M%S)}"
				if [[ "${out}" != /* ]]; then
					echo "test coverage: --out must be an absolute path (outside the checkout)" >&2
					exit 2
				fi
				repo="$(pwd -P)"
				# Judged before anything is created: a refused folder is never made.
				out="$(realpath -m -- "${out}")"
				if [[ "${out}/" == "${repo}/"* ]]; then
					echo "test coverage: ${out} is inside the checkout; give a folder outside it" >&2
					exit 2
				fi
				mkdir -p "${out}"
				# coverage runs the suite on uv's Python without -B: keep __pycache__ out of the checkout.
				export PYTHONDONTWRITEBYTECODE=1
				cov=(tests/uv-dev --group coverage coverage)
				data="--data-file=${out}/coverage.data"
				"${cov[@]}" run "${data}" tests/run_unit.py
				"${cov[@]}" report "${data}" > "${out}/report.txt"
				"${cov[@]}" json "${data}" -q -o "${out}/coverage.json"
				"${cov[@]}" html "${data}" -q -d "${out}/html"
				echo "test coverage: reports in ${out} (report.txt, coverage.json, html/index.html)"
				python3 -I -B tests/coverage_floors.py "${out}/coverage.json" | tee "${out}/floors.txt"
				"""}
		}]
	},
	{
		// Optional and slow: `test all` and CI do not run it. See docs/dev-tools.md, "Mutation testing".
		name:        "test mutation"
		category:    "test"
		description: "Mutation-test one Python helper with cosmic-ray, on a throwaway copy, inside a bubblewrap sandbox (tests/mutation/cst-mutation, through uv)"
		args: [
			{name: "helper", description: "The Python file to mutate, relative to the top of the checkout, e.g. swap/domain.py", required: true},
			{name: "test_module", description: "The unittest module that tests it, e.g. swap.tests.test_swap", required: true},
		]
		flags: [
			{name: "base", description: "Mutate the lines changed since this git ref (what happens without --all or --functions, with main)"},
			{name: "all", type: "bool", default_value: "false", description: "Mutate the whole file"},
			{name: "functions", description: "Mutate only these functions: comma-separated def names (methods and nested defs count)"},
			{name: "out", description: "A new or empty folder for the session and the reports (default: a new folder under ~/.cache/cst-mutation/)"},
			{name: "timeout", type: "int", default_value: "60", description: "Seconds per mutant before it counts as killed"},
		]
		depends_on: {
			tools: [
				{alternatives: ["uv"]},
				{alternatives: ["bwrap"]},
				{alternatives: ["git"]},
			]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				args=("${INVOWK_ARG_HELPER}" "${INVOWK_ARG_TEST_MODULE}" --timeout "${INVOWK_FLAG_TIMEOUT:-60}")
				[[ -n "${INVOWK_FLAG_BASE:-}" ]] && args+=(--base "${INVOWK_FLAG_BASE}")
				[[ "${INVOWK_FLAG_ALL:-false}" == "true" ]] && args+=(--all)
				[[ -n "${INVOWK_FLAG_FUNCTIONS:-}" ]] && args+=(--functions "${INVOWK_FLAG_FUNCTIONS}")
				[[ -n "${INVOWK_FLAG_OUT:-}" ]] && args+=(--out "${INVOWK_FLAG_OUT}")
				exec tests/uv-dev --group mutation python3 -I -B tests/mutation/cst-mutation "${args[@]}"
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
		description: "Run the fast gates (check), then the Docker Layer 1 and Docker Layer 2 E2E suites (Layer 2 needs --allow_privileged or CST_E2E_ALLOW_PRIVILEGED=1)"
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
				# The fast gates: the same task CI runs.
				invowk cmd check
				[[ "${INVOWK_FLAG_KEEP_LOGS:-false}" == "true" ]] && e2e_args+=(--keep-logs)
				[[ "${INVOWK_FLAG_REBUILD:-false}" == "true" ]] && e2e_args+=(--rebuild)
				python3 -I -B tests/e2e/run.py "${e2e_args[@]}"
				"""}
		}]
	},
	{
		name:        "ci audit"
		category:    "ci"
		description: "Run invowk audit --severity low as JSON into a report file, and add its summary to the GitHub job summary when there is one (.github/scripts/audit_summary.py)"
		flags: [
			{name: "report", default_value: "audit-report.json", description: "Where to write the JSON report (relative to the top of the checkout, or absolute)"},
		]
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: """
				set -euo pipefail
				report="${INVOWK_FLAG_REPORT:-audit-report.json}"
				rc=0
				invowk audit --format json --severity low cst.invowkmod > "${report}" || rc=$?
				python3 -I -B .github/scripts/audit_summary.py "${report}" "${rc}"
				exit "${rc}"
				"""}
		}]
	},
	{
		// Needs the network (the vulnerability database): the nightly runs it, `check` does not.
		name:        "ci uv-audit"
		category:    "ci"
		description: "Check every package of uv.lock (all dependency groups) against the known vulnerabilities, with uv audit --locked (needs the network; the nightly runs it)"
		depends_on: {
			tools: [{alternatives: ["uv"]}]
		}
		implementations: [{
			platforms: [{name: "linux"}]
			runtimes: [{name: "native"}]
			script: {content: "uv audit --locked --preview-features audit-command"}
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
