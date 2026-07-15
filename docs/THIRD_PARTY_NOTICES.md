# Third-party notices

## Minibook

`minibook/` is vendored third-party software. Its own license is preserved at `minibook/LICENSE` and is AGPL-3.0. Captain Cook treats Minibook as a separate integration boundary; the offline demo does not import or run it.

## Hermes Agent

`hermes-agent/` is a Git submodule from `NousResearch/hermes-agent`. It remains external third-party code; its upstream license is MIT. Initialize it after cloning with:

```powershell
git submodule update --init --recursive
```

## Scope of these notices

These notices identify included third-party components. They do not select a license for the original Captain Cook code at the repository root.
