"""Inspect and edit Jupyter notebooks without ad-hoc scripts (standard library only).

The agents use these subcommands instead of writing `python -c` one-liners or heredocs:

  python3 nb_tool.py info    <nb.ipynb>                       JSON: kernel name and number of cells
  python3 nb_tool.py cells   <nb.ipynb>                       list cells: index, type, first line
  python3 nb_tool.py show    <nb.ipynb> <index>               print one cell's full source
  python3 nb_tool.py errors  <executed.ipynb>                 JSON: every cell with an error output
                                                              (index, first line, ename, evalue, traceback tail)
  python3 nb_tool.py set-source  <nb.ipynb> <index> <file>    replace a cell's source with the file's text
  python3 nb_tool.py insert-cell <nb.ipynb> <index> <file>    insert a new code cell (source from file) at index
  python3 nb_tool.py insert-pip  <nb.ipynb> <index> <pkg>...  insert a `%pip install <pkg> ...` code cell at index

Cell indexes are 0-based. Edits rewrite the notebook in place and clear that cell's outputs.
"""
import json
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, nb):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)
        f.write("\n")


def source(cell):
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else src


def first_line(cell):
    lines = source(cell).strip().splitlines()
    return lines[0] if lines else ""


def code_cell(text):
    return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": text}


def cell_index(nb, raw, allow_end=False):
    i = int(raw)
    limit = len(nb["cells"]) + (1 if allow_end else 0)
    if not 0 <= i < limit:
        sys.exit(f"index {i} out of range (notebook has {len(nb['cells'])} cells)")
    return i


def main(argv):
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, path, rest = argv[0], argv[1], argv[2:]
    nb = load(path)
    cells = nb.get("cells", [])

    if cmd == "info":
        kernel = nb.get("metadata", {}).get("kernelspec", {}).get("name")
        print(json.dumps({"kernel": kernel, "cells": len(cells)}))
    elif cmd == "cells":
        for i, c in enumerate(cells):
            print(f"{i:3d}  {c.get('cell_type', '?'):8s} {first_line(c)[:100]}")
    elif cmd == "show":
        print(source(cells[cell_index(nb, rest[0])]))
    elif cmd == "errors":
        found = []
        for i, c in enumerate(cells):
            for o in c.get("outputs", []):
                if o.get("output_type") == "error":
                    tb = [ANSI.sub("", line) for line in o.get("traceback", [])]
                    tail = "\n".join("\n".join(tb).splitlines()[-15:])
                    found.append({"cell": i, "first_line": first_line(c), "ename": o.get("ename"),
                                  "evalue": o.get("evalue"), "traceback_tail": tail})
        print(json.dumps({"cells_total": len(cells), "error_cells": found}, indent=1))
    elif cmd == "set-source":
        i = cell_index(nb, rest[0])
        with open(rest[1], encoding="utf-8") as f:
            cells[i]["source"] = f.read()
        if cells[i].get("cell_type") == "code":
            cells[i]["outputs"], cells[i]["execution_count"] = [], None
        save(path, nb)
        print(f"cell {i} updated")
    elif cmd == "insert-cell":
        i = cell_index(nb, rest[0], allow_end=True)
        with open(rest[1], encoding="utf-8") as f:
            cells.insert(i, code_cell(f.read()))
        save(path, nb)
        print(f"inserted code cell at {i}")
    elif cmd == "insert-pip":
        i = cell_index(nb, rest[0], allow_end=True)
        pkgs = [p for p in rest[1:] if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~]*", p)]
        if not pkgs or len(pkgs) != len(rest[1:]):
            sys.exit("give one or more valid package names, e.g. insert-pip nb.ipynb 0 pandas scikit-learn")
        cells.insert(i, code_cell(f"%pip install {' '.join(pkgs)}\n"))
        save(path, nb)
        print(f"inserted '%pip install {' '.join(pkgs)}' at {i}")
    else:
        sys.exit(f"unknown command {cmd!r}\n{__doc__}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
