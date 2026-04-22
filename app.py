import runpy
from pathlib import Path


APP_PATH = Path(__file__).with_name("app (3).py")
runpy.run_path(str(APP_PATH), run_name="__main__")
