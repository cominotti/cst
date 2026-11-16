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
- restore() puts every replaced name back. Creating a view restores the
  views created before it, so a test's setUp starts from the real code even
  when the previous test registered no cleanup.

This is a test helper only: production code imports the module that
defines a name.
"""

from types import ModuleType
from typing import ClassVar, Self

_MISSING = object()


class PackageView:
    """Read from the first module with a name; write to every module with it."""

    _active: ClassVar[list[PackageView]] = []

    def __init__(self, *modules: ModuleType) -> None:
        for view in list(PackageView._active):
            view.restore()
        object.__setattr__(self, "_modules", modules)
        object.__setattr__(self, "_saved", {})
        PackageView._active.append(self)

    def __getattr__(self, name: str) -> object:
        for mod in object.__getattribute__(self, "_modules"):
            value = getattr(mod, name, _MISSING)
            if value is not _MISSING:
                return value
        raise AttributeError(f"no module of this view defines {name}")

    def __setattr__(self, name: str, value: object) -> None:
        saved: dict[tuple[int, str], tuple[ModuleType, object]] = object.__getattribute__(self, "_saved")
        found = False
        for mod in object.__getattribute__(self, "_modules"):
            old = getattr(mod, name, _MISSING)
            if old is _MISSING:
                continue
            found = True
            saved.setdefault((id(mod), name), (mod, old))
            setattr(mod, name, value)
        if not found:
            raise AttributeError(f"no module of this view defines {name}: nothing would see the replacement")

    def restore(self) -> Self:
        """Put back every name this view replaced (safe to call twice)."""
        saved: dict[tuple[int, str], tuple[ModuleType, object]] = object.__getattribute__(self, "_saved")
        for (_, name), (mod, old) in saved.items():
            setattr(mod, name, old)
        saved.clear()
        if self in PackageView._active:
            PackageView._active.remove(self)
        return self
