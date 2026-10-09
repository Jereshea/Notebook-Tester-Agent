"""Execute a notebook in a fresh, isolated venv and ALWAYS save the executed copy.

Usage (start it with the system python3; it builds the venv itself):
  python3 run_notebook.py --venv <venv_dir> <input.ipynb> <output.ipynb> <working_dir> [cell_timeout_seconds]

What it does:
  1. Creates <venv_dir> as a brand-new venv (no system site-packages) and installs ONLY the
     pinned kernel tools below. The notebook's own packages are never installed here: the
     notebook must install them itself (e.g. a `%pip install ...` cell).
  2. Re-runs itself inside that venv and executes the notebook with nbclient from <working_dir>.
     The venv's bin/ is put first on PATH, so `!pip install` in a cell installs into the venv.
  3. Saves the executed copy to <output.ipynb> even when a cell fails (outputs up to the error).

Exit code 0 = every cell ran without error; 1 = a cell raised (traceback printed);
2 = the notebook could not be started; 3 = the venv could not be created.
"""
import os
import shutil
import subprocess
import sys
import time
import traceback

# Kernel tools every notebook needs in order to run at all. Pinned, chosen by this project.
KERNEL_TOOLS = ["ipykernel==7.4.0", "nbclient==0.11.0", "nbformat==5.11.1"]


def build_venv(venv: str) -> None:
    if os.path.exists(venv):
        shutil.rmtree(venv)
    subprocess.run([sys.executable, "-m", "venv", venv], check=True)
    py = os.path.join(venv, "bin", "python")
    subprocess.run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", *KERNEL_TOOLS], check=True)
    print(f"SETUP venv={venv} kernel_tools={' '.join(KERNEL_TOOLS)}", flush=True)


def execute(src: str, dst: str, cwd: str, timeout: int) -> int:
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError

    venv_bin = os.path.dirname(os.path.abspath(sys.executable))
    os.environ["VIRTUAL_ENV"] = os.path.dirname(venv_bin)
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")
    os.environ.pop("PYTHONHOME", None)

    nb = nbformat.read(src, as_version=4)
    client = NotebookClient(nb, timeout=timeout, kernel_name="python3",
                            resources={"metadata": {"path": cwd}})
    start = time.time()
    code = 0
    try:
        client.execute()
    except CellExecutionError as exc:
        print(str(exc), file=sys.stderr)
        code = 1
    except Exception:
        traceback.print_exc()
        code = 2
    finally:
        nbformat.write(nb, dst)
    errors = [i for i, c in enumerate(nb.cells)
              if any(o.get("output_type") == "error" for o in c.get("outputs", []))]
    print(f"RESULT exit={code} cells={len(nb.cells)} error_cells={errors} "
          f"seconds={round(time.time() - start)} saved={dst}", flush=True)
    return code if code else (1 if errors else 0)


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 5 or args[0] != "--venv":
        print(__doc__, file=sys.stderr)
        return 2
    venv, src, dst, cwd = os.path.abspath(args[1]), args[2], args[3], args[4]
    timeout = int(args[5]) if len(args) > 5 else 1800
    venv_python = os.path.join(venv, "bin", "python")

    if os.path.abspath(sys.prefix) != venv:
        try:
            build_venv(venv)
        except subprocess.CalledProcessError as exc:
            print(f"RESULT exit=3 setup_failed: {exc}", flush=True)
            return 3
        os.execv(venv_python, [venv_python, os.path.abspath(__file__), *args])

    return execute(src, dst, cwd, timeout)


if __name__ == "__main__":
    sys.exit(main())
