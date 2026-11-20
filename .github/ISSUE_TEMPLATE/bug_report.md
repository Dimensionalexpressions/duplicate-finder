---
name: Bug report
about: Something Duplicate Finder did not do what you expected
title: "[BUG] "
labels: bug
---

<!-- Short is fine. Rough is fine. Just enough that we can re-test it on our own machines. -->

**1. Did any file disappear or change?**
<!-- If Duplicate Finder deleted or trashed something you did not choose, say that FIRST - it is the highest-priority kind of bug for us.
Is it still in your system Recycle Bin / Trash? (Please do not empty the Trash before telling us.) -->

**2. What were you doing?**
<!-- Which folder you scanned, roughly how many files and what kind (photos, videos, documents),
and the steps 1-2-3. Example: "Dragged my 20 GB Pictures folder onto the window, clicked Scan, then clicked Keep Best." -->

**3. What did you expect, and what happened instead?**
<!-- Copy the exact error text or add a screenshot. If the window froze, crashed, or showed wrong groups, say which.
Please include everything it printed or showed, not just the last line - the earlier lines often say why. -->

**4. Does it happen every time?**
<!-- Every time / only with this folder / only sometimes. If another folder works fine, tell us - it narrows it down fast. -->

**5. Did it work before?**
<!-- First time you used it / it worked in an earlier version (which one?) / it started after an update.
If you still have the older download, naming both files (like 0.9 and 1.0) tells us where to look. -->

**6. Can we re-test it ourselves?**
<!-- What you can share decides how fast this gets fixed:
- a zip of a few files that behaved wrong (duplicates of each other is fine), or
- a list of the affected file names - names only, we do not need the contents, or
- "the files are personal" - say that, and we will ask for them privately if we need them. -->

**7. Your machine**
- Operating system: <!-- e.g. Windows 11, Windows 10, Ubuntu 24.04, Fedora 42 -->
- Version: <!-- shown in the window title bar, or the exact file name you downloaded (like dupe-finder-1.0.0-win64.zip) -->
- Where the scanned folder lives: <!-- your disk / external USB drive / network share / cloud folder (OneDrive, Dropbox...) -->

If you can run a command line, this one command fills the rest in - versions, the filesystem behind
that folder, and whether it treats `Notes.txt` and `notes.txt` as the same file. It masks your user
and machine names, so it is safe to paste, and it does not read your files:

```
./scripts/bugreport.sh --command "what you did" --path "the folder you scanned"
```

On Windows run it from Git Bash. No bash? The three lines above are enough on their own.

**8. Anything else that might matter**
<!-- Unusual file names (accents, emoji, very long paths), huge videos, files that other programs had open, recently moved anything... write it. -->
