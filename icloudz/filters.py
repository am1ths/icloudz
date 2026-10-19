import fnmatch
from pathlib import PurePosixPath


def should_exclude(rel: str, excludes: list[str]) -> bool:
    name = PurePosixPath(rel).name
    return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern)
               for pattern in excludes)


def should_include(rel: str, selective: list[str], remote_root: str = "/") -> bool:
    if not selective:
        return True

    root = str(PurePosixPath(remote_root)).strip("/")
    for path in selective:
        prefix = str(PurePosixPath(path)).strip("/")
        if root and (prefix == root or prefix.startswith(root + "/")):
            prefix = prefix[len(root):].lstrip("/")
        if not prefix or rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False
