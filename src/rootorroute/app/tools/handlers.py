from rootorroute.app.tools.browser import ScreenshotTool
from rootorroute.app.tools.executor import Handler
from rootorroute.app.tools.files import FileTools
from rootorroute.app.tools.shell import ShellTool
from rootorroute.domain.sandbox import Sandbox


def workspace_handlers(sandbox: Sandbox) -> dict[str, Handler]:
    files, shell, browser = FileTools(sandbox), ShellTool(sandbox), ScreenshotTool(sandbox)
    return {
        "read_file": files.read_file,
        "list_files": files.list_files,
        "search_code": files.search_code,
        "write_file": files.write_file,
        "edit_file": files.edit_file,
        "shell": shell.shell,
        "screenshot": browser.screenshot,
    }
