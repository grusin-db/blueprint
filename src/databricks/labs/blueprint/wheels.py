import datetime
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.mixins.compute import SemVer
from databricks.sdk.service.workspace import ImportFormat

from databricks.labs.blueprint.entrypoint import find_project_root
from databricks.labs.blueprint.installer import InstallState

logger = logging.getLogger(__name__)


class Wheels(AbstractContextManager):
    """Wheel builder"""

    def __init__(
        self,
        ws: WorkspaceClient,
        install_state: InstallState,
        released_version: str,
        *,
        github_org: str = "databrickslabs",
    ):
        self._ws = ws
        self._install_state = install_state
        self._this_file = Path(__file__)
        self._github_org = github_org
        self._released_version = released_version

    def version(self):
        """Returns current version of the project"""
        if hasattr(self, "__version"):
            return self.__version
        project_root = find_project_root()
        if not (project_root / ".git/config").exists():
            # normal install, downloaded releases won't have the .git folder
            return self._released_version
        try:
            out = subprocess.run(["git", "describe", "--tags"], stdout=subprocess.PIPE, check=True)  # noqa S607
            git_detached_version = out.stdout.decode("utf8")
            dv = SemVer.parse(git_detached_version)
            datestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            # new commits on main branch since the last tag
            new_commits = dv.pre_release.split("-")[0] if dv.pre_release else None
            # show that it's a version different from the released one in stats
            bump_patch = dv.patch + 1
            # create something that is both https://semver.org and https://peps.python.org/pep-0440/
            semver_and_pep0440 = f"{dv.major}.{dv.minor}.{bump_patch}+{new_commits}{datestamp}"
            # validate the semver
            SemVer.parse(semver_and_pep0440)
            self.__version = semver_and_pep0440
            return semver_and_pep0440
        except Exception as err:
            product = self._install_state.product()
            msg = (
                f"Cannot determine unreleased version. Please report this error "
                f"message that you see on https://github.com/{self._github_org}/{product}/issues/new. "
                f"Meanwhile, download, unpack, and install the latest released version from "
                f"https://github.com/{self._github_org}/{product}/releases. Original error is: {err!s}"
            )
            raise OSError(msg) from None

    def __enter__(self) -> "Wheels":
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._local_wheel = self._build_wheel(self._tmp_dir.name)
        self._remote_wheel = f"{self._install_state.install_folder()}/wheels/{self._local_wheel.name}"
        self._remote_dir_name = os.path.dirname(self._remote_wheel)
        return self

    def __exit__(self, __exc_type, __exc_value, __traceback):
        self._tmp_dir.cleanup()

    def upload_to_dbfs(self) -> str:
        with self._local_wheel.open("rb") as f:
            self._ws.dbfs.mkdirs(self._remote_dir_name)
            logger.info(f"Uploading wheel to dbfs:{self._remote_wheel}")
            self._ws.dbfs.upload(self._remote_wheel, f, overwrite=True)
        return self._remote_wheel

    def upload_to_wsfs(self) -> str:
        with self._local_wheel.open("rb") as f:
            self._ws.workspace.mkdirs(self._remote_dir_name)
            logger.info(f"Uploading wheel to /Workspace{self._remote_wheel}")
            self._ws.workspace.upload(self._remote_wheel, f, overwrite=True, format=ImportFormat.AUTO)
        return self._remote_wheel

    def _build_wheel(self, tmp_dir: str, *, verbose: bool = False):
        """Helper to build the wheel package

        :param tmp_dir: str:
        :param *:
        :param verbose: bool:  (Default value = False)

        """
        stdout = subprocess.STDOUT
        stderr = subprocess.STDOUT
        if not verbose:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        project_root = find_project_root()
        is_non_released_version = "+" in self.version()
        if (project_root / ".git" / "config").exists() and is_non_released_version:
            tmp_dir_path = Path(tmp_dir) / "working-copy"
            # copy everything to a temporary directory
            shutil.copytree(project_root, tmp_dir_path)
            # and override the version file
            version_file = tmp_dir_path / f"src/databricks/labs/{self._install_state.product()}/__about__.py"
            with version_file.open("w") as f:
                f.write(f'__version__ = "{self.version()}"')
            # working copy becomes project root for building a wheel
            project_root = tmp_dir_path
        logger.debug(f"Building wheel for {project_root} in {tmp_dir}")
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", tmp_dir, project_root.as_posix()],
            check=True,
            stdout=stdout,
            stderr=stderr,
        )
        # get wheel name as first file in the temp directory
        return next(Path(tmp_dir).glob("*.whl"))
