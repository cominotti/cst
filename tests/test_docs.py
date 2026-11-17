"""Static gate: the Markdown documents and the agent files hold together.

- Every relative link in a Markdown file of the repository, [text](path),
  [text](path#anchor) or [text](#anchor), and every reference definition,
  [label]: path, names a repository file or an existing folder, and every
  anchor names a heading of the target file. A target with a space or a
  parenthesis in it is not read.
  Anchors follow GitHub's rule for heading ids (see slug()). Links with a
  scheme (https:, mailto:, ...) are not checked; nor is text inside fenced
  code blocks or `code spans`.
- Every Markdown document is linked from the index table of AGENTS.md
  ("Index: where each part is documented"), so that a new module README or a
  new page in docs/ cannot be forgotten there. The exceptions are AGENTS.md
  itself, the agent rules and skills in .agents/ (checked below) and the
  files in INDEX_EXCEPTIONS, each with its reason.
- Every folder in .agents/skills has a SKILL.md whose frontmatter has a
  name (the folder's name) and a description (what the skill is for and when
  to use it). Every rule in .agents/rules has a description.
- .claude/rules and .claude/skills are relative symbolic links to
  .agents/rules and .agents/skills, so Claude Code reads the same files as
  the other agents.

Standard library only: the unit tests run on /usr/bin/python3 without uv.
"""

from collections.abc import Iterable, Sequence
import os
import re
import tempfile
from typing import Final
import unittest
from urllib.parse import unquote

from tests.repo_files import REPO, repo_files

# Markdown files that need no row in the AGENTS.md index, with the reason.
INDEX_EXCEPTIONS: Final[dict[str, str]] = {
    "AGENTS.md": "the index itself",
}
# Folders whose Markdown files are not documents for the index: the agent
# rules and skills, which test_agent_files checks instead.
NOT_INDEXED_PREFIXES: Final = (".agents/",)
# Local files of an editor, untracked and not ignored in some checkouts.
SKIPPED_PREFIXES: Final = (".idea/",)
INDEX_HEADING: Final = "## Index"
AGENT_DIRS: Final = ("rules", "skills")

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
# A code span: a run of backticks, the shortest text up to the same run, and
# never across a blank line (a stray backtick does not swallow the file).
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
# [text](target) or ![alt](target), with an optional "title"; the text may
# hold one level of brackets and span lines.
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^\s()<>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
# A reference definition on a line of its own, [label]: target (a footnote,
# [^1]: text, is not a link).
REF_DEF = re.compile(r"^ {0,3}\[(?!\^)[^\]\n]+\]:[ \t]*<?([^\s<>]+)>?", re.M)
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
INLINE_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
HTML_TAG = re.compile(r"<[^>]+>")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
NOT_IN_SLUG = re.compile(r"[^\w\- ]")


def blank_fences(text: str) -> str:
    """The text with every line of a fenced code block emptied (line numbers kept)."""
    out: list[str] = []
    fence = ""
    for line in text.split("\n"):
        m = FENCE.match(line)
        if fence:
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= len(fence)
                and not line.strip()[len(m.group(1)) :]
            ):
                fence = ""
            out.append("")
        elif m:
            fence = m.group(1)
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def blank_code_spans(text: str) -> str:
    """The text with each code span replaced by spaces (newlines kept)."""
    return CODE_SPAN.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def slug(heading: str) -> str:
    """GitHub's id for a heading: the text as rendered (link text, no code
    marks or HTML tags), lowercased, without punctuation other than - and _,
    each space turned into -."""
    text = HTML_TAG.sub("", INLINE_LINK.sub(r"\1", heading)).strip().lower()
    return NOT_IN_SLUG.sub("", text).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """The ids of the headings of a Markdown text; a repeated one gets -1, -2, ..."""
    seen: dict[str, int] = {}
    found: set[str] = set()
    for line in blank_fences(text).split("\n"):
        m = HEADING.match(line)
        if not m:
            continue
        base = slug(m.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def link_targets(text: str) -> list[tuple[int, str]]:
    """(line number, target) of every link outside code, in order."""
    plain = blank_code_spans(blank_fences(text))
    found = sorted((m.start(), m.group(1)) for pattern in (LINK, REF_DEF) for m in pattern.finditer(plain))
    return [(plain.count("\n", 0, start) + 1, target) for start, target in found]


def read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


def markdown_files(files: Iterable[str]) -> list[str]:
    return [rel for rel in files if rel.endswith(".md") and not rel.startswith(SKIPPED_PREFIXES)]


def link_problems(repo: str, files: Sequence[str]) -> list[str]:
    """One line per relative link or anchor that does not resolve."""
    known = set(files)
    headings: dict[str, set[str]] = {}

    def heading_ids(dest: str) -> set[str]:
        if dest not in headings:
            headings[dest] = anchors(read(repo, dest))
        return headings[dest]

    problems: list[str] = []
    for rel in markdown_files(files):
        for no, target in link_targets(read(repo, rel)):
            if SCHEME.match(target):
                continue
            path, _, anchor = target.partition("#")
            dest = os.path.normpath(os.path.join(os.path.dirname(rel), unquote(path))) if path else rel
            if dest == ".." or dest.startswith("../") or os.path.isabs(dest):
                problems.append(f"{rel}:{no}: {target}: outside the repository")
            elif dest not in known and not os.path.isdir(os.path.join(repo, dest)):
                problems.append(f"{rel}:{no}: {target}: no such file or folder in the repository")
            elif anchor and dest.endswith(".md") and anchor not in heading_ids(dest):
                problems.append(f"{rel}:{no}: {target}: no heading with the id #{anchor} in {dest}")
    return problems


def index_links(agents_text: str) -> set[str]:
    """The files the rows of the AGENTS.md index table link to, without anchors."""
    section: list[str] = []
    inside = False
    for line in blank_fences(agents_text).split("\n"):
        if line.startswith("## "):
            inside = line.startswith(INDEX_HEADING)
        elif inside and line.lstrip().startswith("|"):
            section.append(line)
    found: set[str] = set()
    for _, target in link_targets("\n".join(section)):
        path = target.partition("#")[0]
        if path and not SCHEME.match(target):
            found.add(os.path.normpath(unquote(path)))
    return found


def unindexed(files: Iterable[str], agents_text: str) -> list[str]:
    """Markdown documents that the AGENTS.md index does not link to."""
    linked = index_links(agents_text)
    return [
        rel
        for rel in markdown_files(files)
        if rel not in linked and rel not in INDEX_EXCEPTIONS and not rel.startswith(NOT_INDEXED_PREFIXES)
    ]


def frontmatter(text: str) -> dict[str, str]:
    """The top-level "key: value" lines of a YAML frontmatter block (quotes removed); {} without one."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep and key and not key[0].isspace():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            fields[key.strip()] = value
    return {}


def agent_file_problems(repo: str) -> list[str]:
    """Skills without a valid SKILL.md, rules without a description."""
    problems: list[str] = []
    skills = os.path.join(repo, ".agents", "skills")
    rules = os.path.join(repo, ".agents", "rules")
    for base in (skills, rules):
        if not os.path.isdir(base) or not os.listdir(base):
            problems.append(f"{os.path.relpath(base, repo)}: missing or empty")
    for name in sorted(os.listdir(skills)) if os.path.isdir(skills) else []:
        where = f".agents/skills/{name}"
        if not os.path.isdir(os.path.join(skills, name)):
            problems.append(f"{where}: not a folder (a skill is a folder with a SKILL.md)")
            continue
        path = os.path.join(skills, name, "SKILL.md")
        if not os.path.isfile(path):
            problems.append(f"{where}: no SKILL.md")
            continue
        fields = frontmatter(read(repo, f"{where}/SKILL.md"))
        if fields.get("name") != name:
            problems.append(f"{where}/SKILL.md: frontmatter name is {fields.get('name')!r}, not {name!r}")
        if not fields.get("description"):
            problems.append(f"{where}/SKILL.md: no description in the frontmatter")
    for name in sorted(os.listdir(rules)) if os.path.isdir(rules) else []:
        where = f".agents/rules/{name}"
        if not name.endswith(".md"):
            problems.append(f"{where}: a rule is a .md file")
        elif not frontmatter(read(repo, where)).get("description"):
            problems.append(f"{where}: no description in the frontmatter")
    return problems


def claude_link_problems(repo: str) -> list[str]:
    """.claude/rules and .claude/skills must be relative links into .agents."""
    problems: list[str] = []
    for name in AGENT_DIRS:
        link = os.path.join(repo, ".claude", name)
        where = f".claude/{name}"
        if not os.path.islink(link):
            problems.append(f"{where}: not a symbolic link (ln -s ../.agents/{name} {where})")
        elif os.path.isabs(os.readlink(link)):
            problems.append(f"{where}: absolute link {os.readlink(link)!r}; it breaks in another clone")
        elif os.path.realpath(link) != os.path.realpath(os.path.join(repo, ".agents", name)):
            problems.append(f"{where}: points to {os.readlink(link)!r}, not ../.agents/{name}")
    return problems


def write_tree(root: str, tree: dict[str, str]) -> list[str]:
    """Write {relative path: text} under root; return the paths, sorted."""
    for rel, text in tree.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return sorted(tree)


class DetectorTest(unittest.TestCase):
    """The checks on small made-up trees: each one fails when its rule is broken."""

    def test_slug_follows_github(self) -> None:
        self.assertEqual(slug("6. How to restore a file or a folder"), "6-how-to-restore-a-file-or-a-folder")
        self.assertEqual(slug("The `cst` command"), "the-cst-command")
        self.assertEqual(slug("sccache (opt-in)"), "sccache-opt-in")
        self.assertEqual(slug("Local crates (`RUST_SCCACHE_WORKSPACE`)"), "local-crates-rust_sccache_workspace")
        self.assertEqual(slug("See [the docs](x.md) now"), "see-the-docs-now")
        self.assertEqual(slug("A  -- B"), "a-----b")
        self.assertEqual(anchors("# A\n## A\n### A\n"), {"a", "a-1", "a-2"})

    def test_code_is_not_read(self) -> None:
        text = "```\n[x](gone.md)\n# Not a heading\n```\nuse `[y](gone.md)` here\n[z](\n"
        self.assertEqual(link_targets(text), [])
        self.assertEqual(anchors(text), set())
        self.assertEqual(link_targets('one\n[see `a.md`\nhere](b.md#c "t")'), [(2, "b.md#c")])

    def test_broken_links_are_found(self) -> None:
        tree = {
            "README.md": "# Top\n## Part two\n[ok](docs/a.md#a-heading) [ok](#part-two) [dir](docs/)\n"
            "[web](https://example.org/x.md) [mail](mailto:a@b)\n"
            "[gone](docs/b.md)\n[bad anchor](#nope)\n[out](../x.md)\n"
            "[ok]: docs/a.md#a-heading\n  [ref]: <docs/c.md>\n[^1]: a footnote, not a link\n",
            "docs/a.md": "# A heading\n[up](../README.md#top) [bad](../README.md#part-three)\n",
        }
        with tempfile.TemporaryDirectory() as root:
            files = write_tree(root, tree)
            self.assertEqual(
                link_problems(root, files),
                [
                    "README.md:5: docs/b.md: no such file or folder in the repository",
                    "README.md:6: #nope: no heading with the id #nope in README.md",
                    "README.md:7: ../x.md: outside the repository",
                    "README.md:9: docs/c.md: no such file or folder in the repository",
                    "docs/a.md:2: ../README.md#part-three: no heading with the id #part-three in README.md",
                ],
            )

    def test_index_must_list_every_document(self) -> None:
        agents = (
            "# Rules\n[not the index](docs/b.md)\n## Index: where\n| a | b |\n|---|---|\n"
            "| x | [README.md](README.md) |\n| y | [docs/a.md](docs/a.md#part) |\n## Commands\n"
        )
        files = ["AGENTS.md", "README.md", "docs/a.md", "docs/b.md", ".agents/rules/r.md", ".idea/n.md"]
        self.assertEqual(index_links(agents), {"README.md", "docs/a.md"})
        self.assertEqual(unindexed(files, agents), ["docs/b.md"])

    def test_frontmatter(self) -> None:
        self.assertEqual(
            frontmatter('---\nname: x\ndescription: "Do y. Use when z."\nmeta:\n  k: v\n---\n# X\n'),
            {"name": "x", "description": "Do y. Use when z.", "meta": ""},
        )
        self.assertEqual(frontmatter("# no frontmatter\nname: x\n"), {})
        self.assertEqual(frontmatter("---\nname: x\n"), {})

    def test_agent_files_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_tree(
                root,
                {
                    ".agents/skills/good/SKILL.md": "---\nname: good\ndescription: Use when.\n---\n",
                    ".agents/skills/renamed/SKILL.md": "---\nname: other\ndescription: Use when.\n---\n",
                    ".agents/skills/bare/SKILL.md": "---\nname: bare\n---\n",
                    ".agents/skills/empty/notes.txt": "",
                    ".agents/rules/good.md": "---\ndescription: A rule\n---\n",
                    ".agents/rules/plain.md": "# no frontmatter\n",
                },
            )
            self.assertEqual(
                agent_file_problems(root),
                [
                    ".agents/skills/bare/SKILL.md: no description in the frontmatter",
                    ".agents/skills/empty: no SKILL.md",
                    ".agents/skills/renamed/SKILL.md: frontmatter name is 'other', not 'renamed'",
                    ".agents/rules/plain.md: no description in the frontmatter",
                ],
            )
            self.assertEqual(claude_link_problems(root)[0].split(":")[0], ".claude/rules")
            os.makedirs(os.path.join(root, ".claude"))
            os.symlink("../.agents/rules", os.path.join(root, ".claude", "rules"))
            os.symlink(os.path.join(root, ".agents", "skills"), os.path.join(root, ".claude", "skills"))
            self.assertEqual(len(claude_link_problems(root)), 1)
            self.assertIn("absolute link", claude_link_problems(root)[0])
            os.remove(os.path.join(root, ".claude", "skills"))
            os.symlink("../.agents/rules", os.path.join(root, ".claude", "skills"))
            self.assertIn("not ../.agents/skills", claude_link_problems(root)[0])


class RepositoryDocsTest(unittest.TestCase):
    """The checks on this repository."""

    def test_relative_links_and_anchors_resolve(self) -> None:
        problems = link_problems(REPO, repo_files())
        self.assertEqual(problems, [], "broken links in the Markdown files:\n" + "\n".join(problems))

    def test_every_document_is_in_the_agents_index(self) -> None:
        missing = unindexed(repo_files(), read(REPO, "AGENTS.md"))
        self.assertEqual(missing, [], "add a row for each to the index table of AGENTS.md: " + ", ".join(missing))

    def test_index_exceptions_exist(self) -> None:
        stale = sorted(set(INDEX_EXCEPTIONS) - set(repo_files()))
        self.assertEqual(stale, [], "no longer in the repository: remove from INDEX_EXCEPTIONS")

    def test_agent_skills_and_rules(self) -> None:
        problems = agent_file_problems(REPO)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_claude_reads_the_agent_folders(self) -> None:
        problems = claude_link_problems(REPO)
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
