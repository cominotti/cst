# SPDX-License-Identifier: MPL-2.0
"""One object over the modules of a package, for tests written against a monolith.

A helper used to be one file that each test loaded afresh, so a test could
replace any of its names (`m.systemctl = fake`) and the next test started
clean. Its code now lives in a package (domain, adapters, cli) imported
once. A PackageView keeps the tests' view of one namespace:

- reading a name gives it from the first module that has it;
- setting a name sets it in EVERY module that has it (the one that defines
  it and every module that imported it by name), so a fake reaches every
  caller; a name no module has is an error, so a misspelt fake cannot pass
  silently;
- deleting a name (what `mock.patch.object` does when its block ends)
  undoes the latest set of that name, so a patch nests and restores like
  it does on a module;
- restore() puts every replaced name back. Creating a view restores the
  views created before it, so a test's setUp starts from the real code even
  when the previous test registered no cleanup.

This is a test helper only: production code imports the module that
defines a name.
"""

from types import ModuleType
from typing import ClassVar, Self, override

_MISSING = object()


class PackageView:
    """Read from the first module with a name; write to every module with it."""

    _active: ClassVar[list[PackageView]] = []

    def __init__(self, *modules: ModuleType) -> None:
        for view in list(PackageView._active):
            view.restore()
        object.__setattr__(self, "_modules", modules)
        object.__setattr__(self, "_saved", {})
        object.__setattr__(self, "_sets", {})
        PackageView._active.append(self)

    def __getattr__(self, name: str) -> object:
        for mod in object.__getattribute__(self, "_modules"):
            value = getattr(mod, name, _MISSING)
            if value is not _MISSING:
                return value
        raise AttributeError(f"no module of this view defines {name}")

    @override
    def __setattr__(self, name: str, value: object) -> None:
        saved: dict[tuple[int, str], tuple[ModuleType, object]] = object.__getattribute__(self, "_saved")
        previous: list[tuple[ModuleType, object]] = []
        for mod in object.__getattribute__(self, "_modules"):
            old = getattr(mod, name, _MISSING)
            if old is _MISSING:
                continue
            previous.append((mod, old))
            saved.setdefault((id(mod), name), (mod, old))
            setattr(mod, name, value)
        if not previous:
            raise AttributeError(f"no module of this view defines {name}: nothing would see the replacement")
        sets: dict[str, list[list[tuple[ModuleType, object]]]] = object.__getattribute__(self, "_sets")
        sets.setdefault(name, []).append(previous)

    @override
    def __delattr__(self, name: str) -> None:
        """Undo the latest set of `name` (the end of a `mock.patch.object` block)."""
        sets: dict[str, list[list[tuple[ModuleType, object]]]] = object.__getattribute__(self, "_sets")
        stack = sets.get(name)
        if not stack:
            raise AttributeError(f"{name} was not replaced through this view")
        for mod, old in stack.pop():
            setattr(mod, name, old)
        if not stack:
            del sets[name]
            saved: dict[tuple[int, str], tuple[ModuleType, object]] = object.__getattribute__(self, "_saved")
            for key in [k for k in saved if k[1] == name]:
                del saved[key]

    def restore(self) -> Self:
        """Put back every name this view replaced (safe to call twice)."""
        saved: dict[tuple[int, str], tuple[ModuleType, object]] = object.__getattribute__(self, "_saved")
        for (_, name), (mod, old) in saved.items():
            setattr(mod, name, old)
        saved.clear()
        object.__getattribute__(self, "_sets").clear()
        if self in PackageView._active:
            PackageView._active.remove(self)
        return self
